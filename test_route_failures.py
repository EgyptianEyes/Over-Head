import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from monitor import (
    FlightRouteStore, FlightTrackingState, FrameState, VisitorSessionStore,
    schedule_route_retry, update_state,
)
from overhead import Settings
import route_failures
from route_failures import RouteFailureLog, report


class RouteFailureLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "route-failures.sqlite3"
        self.log = RouteFailureLog(self.path, retention_days=30, max_records=20, history_limit=5)
        self.plane = {"flight": "AMX008", "r": "XA-AMQ", "hex": "0d0001"}

    @staticmethod
    def debug(lookup_id="lookup-1", attempts=None, alternative="AMX8"):
        return {
            "lookup_id": lookup_id,
            "original_callsign": "AMX008",
            "alternative_callsign": alternative,
            "decision": "No trustworthy route",
            "elapsed_seconds": 0.842,
            "attempts": attempts or [
                {"source": "ADSB.lol route", "callsign": "AMX008", "requested": True,
                 "request_count": 1, "http_status": 200, "accepted": False,
                 "airport_information_returned": True,
                 "geographical_validation_passed": False,
                 "raw": {"airport_codes": "unknown", "airports": []},
                 "reason": "outside route corridor"},
                {"source": "ADSB.lol route", "callsign": "AMX8", "requested": True,
                 "request_count": 1, "http_status": 200, "accepted": False,
                 "retry_delay_seconds": 0.25,
                 "airport_information_returned": True,
                 "geographical_validation_passed": False,
                 "raw": {"airport_codes": "unknown", "airports": [
                     {"iata": "LHR", "icao": "EGLL"}, {"iata": "MEX", "icao": "MMMX"}
                 ]},
                 "reason": "outside route corridor",
                 "validation": {"reason": "outside route corridor"}},
            ],
        }

    def test_records_failure_attempt_once_with_utc_classification_and_provider_details(self):
        now = datetime(2026, 11, 10, 23, 39, tzinfo=timezone.utc).timestamp()
        debug = self.debug()
        self.log.observe(self.plane, {}, debug, now=now)
        self.log.observe(self.plane, {}, debug, now=now + 5)
        records = self.log.records(now=now + 5)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["operating_date"], "2026-11-10")
        self.assertEqual(record["first_failure_utc"], "2026-11-10T23:39:00+00:00")
        self.assertEqual(record["classification"], "missing_both")
        self.assertEqual(record["status"], "unresolved")
        self.assertEqual(record["failed_attempts"], 1)
        self.assertEqual(record["provider_requests"], 2)
        self.assertEqual(record["fallback_attempts"], 1)
        self.assertEqual(record["fallback_successes"], 0)
        self.assertEqual(record["history"][0]["attempts"][1]["http_status"], 200)
        self.assertEqual(record["history"][0]["attempts"][1]["retry_delay_seconds"], 0.25)
        self.assertEqual(record["history"][0]["resolution_elapsed_seconds"], 0.842)
        self.assertEqual(record["history"][0]["attempts"][1]["airports"]["returned"][0],
                         {"iata": "LHR", "icao": "EGLL"})
        serialized = json.dumps(record)
        self.assertNotIn("latitude", serialized)
        self.assertNotIn("session", serialized)

    def test_missing_origin_and_destination_are_classified_independently(self):
        for route, expected in (
            ({"destination": "MEX"}, "missing_origin"),
            ({"origin": "LHR"}, "missing_destination"),
            ({}, "missing_both"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                logger = RouteFailureLog(Path(temporary) / "failure.sqlite3")
                logger.observe(self.plane, route, self.debug())
                self.assertEqual(logger.records()[0]["classification"], expected)

    def test_successful_recovery_keeps_failure_history_and_counts_fallback_success(self):
        first = self.debug()
        self.log.observe(self.plane, {}, first, now=1_800_000_000)
        recovered = self.debug("lookup-2", [
            {"source": "ADSB.lol route", "callsign": "AMX008", "requested": True,
             "request_count": 1, "http_status": 200, "accepted": False,
             "reason": "provider returned no route data"},
            {"source": "ADSB.lol route", "callsign": "AMX8", "requested": True,
             "request_count": 1, "http_status": 200, "accepted": True,
             "airport_information_returned": True, "geographical_validation_passed": True,
             "route": "LHR to MEX", "raw": {"airport_codes": "EGLL-MMMX"}},
        ])
        self.log.observe(self.plane, {
            "origin": "LHR", "destination": "MEX", "route": "LHR to MEX",
            "route_source": "ADSB.lol route + live validation",
        }, recovered, now=1_800_000_030)
        record = self.log.records()[0]
        self.assertEqual(record["status"], "resolved")
        self.assertEqual(record["origin"], "LHR")
        self.assertEqual(record["destination"], "MEX")
        self.assertEqual(record["provider"], "ADSB.lol route + live validation")
        self.assertEqual(record["failed_attempts"], 1)
        self.assertEqual(record["provider_requests"], 4)
        self.assertEqual(record["fallback_attempts"], 2)
        self.assertEqual(record["fallback_successes"], 1)
        self.assertEqual(len(record["history"]), 2)

    def test_persistence_retention_and_record_bound(self):
        for index in range(3):
            plane = dict(self.plane, flight=f"AMX{index}", hex=f"0d000{index}")
            self.log.observe(plane, {}, self.debug(f"lookup-{index}"), now=1000 + index)
        reloaded = RouteFailureLog(self.path, retention_days=1, max_records=2)
        records = reloaded.records(now=1002)
        self.assertEqual(len(records), 2)
        self.assertEqual({row["callsign"] for row in records}, {"AMX1", "AMX2"})
        self.assertEqual(len(RouteFailureLog(self.path, retention_days=1).records(now=1000 + 2 * 86400)), 0)

    def test_same_flight_on_different_operating_dates_has_distinct_records(self):
        day_one = datetime(2026, 11, 10, 23, 59, tzinfo=timezone.utc).timestamp()
        self.log.observe(self.plane, {}, self.debug("lookup-1"), now=day_one)
        self.log.observe(self.plane, {}, self.debug("lookup-2"), now=day_one + 120)
        records = self.log.records(now=day_one + 120)
        self.assertEqual(len(records), 2)
        self.assertEqual({row["operating_date"] for row in records}, {"2026-11-10", "2026-11-11"})

    def test_observation_without_actual_provider_request_does_not_increment_attempts(self):
        debug = self.debug("lookup-1", [{
            "source": "cache", "callsign": "AMX008", "requested": False,
            "accepted": False, "reason": "cached route is stale",
        }])
        self.log.observe(self.plane, {}, debug, now=1000)
        record = self.log.records(now=1000)[0]
        self.assertEqual(record["failed_attempts"], 0)
        self.assertEqual(record["provider_requests"], 0)
        self.assertEqual(record["fallback_attempts"], 0)

    def test_lookup_failure_is_logged_even_if_identity_enrichment_later_fails(self):
        class EmptyStore:
            def get(self, *_args):
                return None

        class BrokenIdentities:
            def get(self, _plane):
                raise OSError("offline identity service")

        plane = {
            "hex": "0d0001", "flight": "EAG21C", "r": "G-CMJL",
            "lat": 53.2, "lon": -4.4, "seen_pos": 0.1, "alt_baro": 18000,
        }
        with tempfile.TemporaryDirectory() as temporary:
            sessions = VisitorSessionStore(
                Settings(latitude=53.2, longitude=-4.4, radius_nm=30),
                Path(temporary) / "snapshots", global_min_interval_seconds=0,
                route_failure_log=self.log,
            )
            session, _ = sessions.get_or_create()
            routes = FlightRouteStore(Path(temporary) / "routes.json")
            debug = self.debug("lookup-observed", [
                {"source": "ADSB.lol routeset", "callsign": "EAG21C", "requested": True,
                 "request_count": 1, "http_status": 200, "accepted": False,
                 "airport_information_returned": False,
                 "reason": "provider returned no route data"},
            ], alternative="")

            with patch("monitor.fetch_aircraft", return_value=[plane]), \
                 patch.object(routes, "get", return_value={}), \
                 patch.object(routes, "_current_lookup_debug", return_value=debug):
                update_state(
                    session.state, session.settings, False, EmptyStore(), BrokenIdentities(),
                    routes=routes, persist_frame=False,
                )
                session.state.last_enrichment_future.result(timeout=5)

            records = self.log.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["callsign"], "EAG21C")
            self.assertEqual(records[0]["failed_attempts"], 1)
            self.assertEqual(records[0]["history"][0]["attempts"][0]["http_status"], 200)

    def test_storage_errors_do_not_escape_observation(self):
        with self.assertLogs("route_failures", level="ERROR"):
            with patch.object(self.log, "_connect", side_effect=OSError("disk unavailable")):
                self.log.observe(self.plane, {}, self.debug())
        self.assertEqual(self.log.records(), [])

    def test_diagnostic_events_are_bounded_private_and_do_not_count_requests(self):
        self.log.record_event(self.plane, "retry_deferred_cooldown", {
            "retry_in_seconds": 15, "provider_cooldown": True,
            "aircraft_latitude": 51.5, "aircraft_longitude": -0.1,
            "visitor_session_id": "private", "reason": "standing provider cooling down",
        }, now=1_800_000_000)
        record = self.log.records(now=1_800_000_000)[0]
        self.assertEqual(record["provider_requests"], 0)
        self.assertEqual(record["failed_attempts"], 0)
        self.assertEqual(record["diagnostics"][0]["event"], "retry_deferred_cooldown")
        serialized = json.dumps(record)
        self.assertNotIn("51.5", serialized)
        self.assertNotIn("private", serialized)

    def test_resolved_event_without_existing_failure_does_not_create_unresolved_record(self):
        self.log.record_event(self.plane, "background_lookup_completed", {"resolved": True})
        self.assertEqual(self.log.records(), [])

    def test_retry_status_respects_backoff_and_first_available_provider(self):
        routes = FlightRouteStore(self.path.with_name("routes.json"))
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        routes._failures[routes._cache_key(plane)] = 1000
        routes._failure_counts[routes._cache_key(plane)] = 2
        routes._adsblol_disabled_until.update({"standing": 1400, "api": 1100})
        routes._flightaware_disabled_until = 2000
        routes.api_key = "configured"
        status = routes.retry_status(plane, now=1000)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["retry_at_epoch"], 1120)
        self.assertEqual(status["retry_in_seconds"], 120)
        self.assertTrue(routes.retry_status(plane, now=1120)["eligible"])

    def test_retry_waits_for_all_usable_provider_cooldowns(self):
        routes = FlightRouteStore(self.path.with_name("cooldown-routes.json"))
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        key = routes._cache_key(plane)
        routes._failures[key] = 1000
        routes._failure_counts[key] = 1
        routes._adsblol_disabled_until.update({"standing": 1100, "api": 1080})
        status = routes.retry_status(plane, now=1050)
        self.assertFalse(status["eligible"])
        self.assertEqual(status["retry_at_epoch"], 1080)
        self.assertEqual(status["retry_in_seconds"], 30)

    def test_retry_status_reports_unusable_aircraft_provider_set(self):
        routes = FlightRouteStore(self.path.with_name("unsupported-routes.json"))
        plane = {"hex": "0d0001", "lat": 51.5, "lon": -0.1}
        key = routes._cache_key(plane)
        routes._failures[key] = 1000
        routes._failure_counts[key] = 1
        status = routes.retry_status(plane, now=1000)
        self.assertFalse(status["provider_available"])

    def test_provider_cooldown_and_geographical_rejection_are_recorded_as_diagnostics(self):
        class Response(BytesIO):
            status = 200

            def __init__(self, value):
                super().__init__(json.dumps(value).encode())

        routes = FlightRouteStore(self.path.with_name("provider-events.json"))
        routes._adsblol_disabled_until["standing"] = time.time() + 60
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with patch.object(routes, "_adsblol_routeset", return_value={}), \
             patch.object(routes, "_adsblol", return_value={}), \
             patch("monitor.urllib.request.urlopen", return_value=Response({
                 "airport_codes": "JFK-SYD", "_airports": [],
             })):
            routes.get(plane)
        debug = routes._current_lookup_debug()
        diagnostic_names = [item.get("source") for item in debug.get("diagnostic_attempts", [])]
        self.assertIn("ADSB.lol standing route", diagnostic_names)
        routes._lookup_local.diagnostics = []
        key = routes._cache_key(plane)
        routes._failures.pop(key, None)
        routes._failure_counts.pop(key, None)
        with patch.object(routes, "_adsblol_standing", return_value={}), \
             patch.object(routes, "_adsblol_routeset", return_value={
                 "airport_codes": "JFK-SYD", "_airports": [],
             }), \
             patch.object(routes, "_adsblol", return_value={}), \
             patch.object(routes, "_normalise_routeset", return_value={
                 "route": "JFK to SYD", "origin": "JFK", "destination": "SYD",
             }), \
             patch.object(routes, "_plausibility", return_value=(False, {"reason": "outside route corridor"})):
            routes.get(plane)
        debug = routes._current_lookup_debug()
        self.assertTrue(any(
            item.get("geographical_validation_passed") is False
            for item in debug.get("diagnostic_attempts", [])
        ))

    def test_background_retry_runs_route_only_and_publishes_validated_recovery(self):
        class FakeRoutes:
            def __init__(self):
                self.calls = 0

            def retry_status(self, _plane):
                return {"eligible": True, "retry_in_seconds": 0, "retry_at_epoch": None}

            def can_lookup(self, _plane):
                return True

            def get(self, _plane):
                self.calls += 1
                return {"route": "LHR to MEX", "origin": "LHR", "destination": "MEX",
                        "route_source": "validated provider", "origin_country": "GB",
                        "destination_country": "MX"}

            def _current_lookup_debug(self):
                return {"attempts": [{"requested": True, "request_count": 1}], "decision": "validated"}

        state = FrameState()
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with state.lock:
            state.payload["aircraft_id"] = "0D0001"
            state.location_revision = 3
            state.position_observed_at_monotonic = time.monotonic()
        routes = FakeRoutes()
        schedule_route_retry(state, routes, plane, 0.01, 3, 0, None, None, None)
        future = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with state.lock:
                future = state.route_retry_future
                published = state.payload.get("route")
            if published:
                break
            time.sleep(0.01)
        if future is not None:
            future.result(timeout=2)
        self.assertEqual(routes.calls, 1)
        self.assertEqual(state.payload["route"], "LHR to MEX")
        self.assertEqual(state.payload["route_resolution_status"], "resolved")

    def test_retry_is_cancelled_after_selected_identity_changes(self):
        class FakeRoutes:
            def retry_status(self, _plane):
                return {"eligible": True, "retry_in_seconds": 0, "retry_at_epoch": None}

            def get(self, _plane):
                raise AssertionError("obsolete selection must not trigger a lookup")

        state = FrameState()
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with state.lock:
            state.payload["aircraft_id"] = "0D0001"
            state.position_observed_at_monotonic = time.monotonic()
        schedule_route_retry(state, FakeRoutes(), plane, 0.05, 0, 0, None, None, None)
        with state.lock:
            state.payload["aircraft_id"] = "0D0002"
        timer = state.route_retry_timer
        if timer is not None:
            timer.join(timeout=1)
        self.assertEqual(state.payload["aircraft_id"], "0D0002")

    def test_background_retry_is_cancelled_when_viewer_lease_expires(self):
        class FakeRoutes:
            def retry_status(self, _plane):
                return {
                    "eligible": self.calls == 0,
                    "retry_in_seconds": 0 if self.calls == 0 else 60,
                    "retry_at_epoch": None,
                    "provider_available": True,
                }

            def can_lookup(self, _plane):
                return True

            def get(self, _plane):
                raise AssertionError("inactive viewer must not trigger a lookup")

        class InactiveLease:
            def active(self):
                return False

        state = FrameState()
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with state.lock:
            state.payload["aircraft_id"] = "0D0001"
            state.position_observed_at_monotonic = time.monotonic()
        schedule_route_retry(state, FakeRoutes(), plane, 0.01, 0, 0, None, InactiveLease(), None)
        timer = state.route_retry_timer
        if timer is not None:
            timer.join(timeout=1)
        self.assertEqual(state.route_retry_identity, "")

    def test_background_retry_retries_after_worker_saturation(self):
        class FakeRoutes:
            def __init__(self):
                self.calls = 0

            def retry_status(self, _plane):
                return {"eligible": True, "retry_in_seconds": 0}

            def can_lookup(self, _plane):
                return True

            def get(self, _plane):
                self.calls += 1
                return {"route": "LHR to MEX", "origin": "LHR", "destination": "MEX"}

            def _current_lookup_debug(self):
                return {"attempts": []}

        state = FrameState()
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with state.lock:
            state.payload["aircraft_id"] = "0D0001"
            state.position_observed_at_monotonic = time.monotonic()
        routes = FakeRoutes()
        with patch("monitor._enrichment_slots.acquire", side_effect=[False, True]):
            schedule_route_retry(state, routes, plane, 0.01, 0, 0, None, None, None)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with state.lock:
                    future = state.route_retry_future
                    resolved = state.payload.get("route_resolution_status") == "resolved"
                if resolved:
                    break
                time.sleep(0.01)
        self.assertEqual(routes.calls, 1)
        self.assertEqual(state.payload["route_resolution_status"], "resolved")

    def test_two_visitors_share_one_route_lookup_and_each_publish_recovery(self):
        routes = FlightRouteStore(self.path.with_name("shared-visitor-routes.json"))
        provider_calls = []
        provider_lock = threading.Lock()

        def mocked_routeset(_callsign, _plane):
            with provider_lock:
                provider_calls.append("routeset")
            routes._lookup_local.request_count += 1
            routes._lookup_local.provider_requests += 1
            time.sleep(0.05)
            return {"airport_codes": "LHR-MEX", "_airports": []}

        states = [FrameState(), FrameState()]
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with patch.object(routes, "_adsblol_standing", return_value={}), \
             patch.object(routes, "_adsblol_routeset", side_effect=mocked_routeset), \
             patch.object(routes, "_normalise_routeset", return_value={
                 "route": "LHR to MEX", "origin": "LHR", "destination": "MEX",
                 "route_source": "ADSB.lol routeset", "cached_at": time.time(),
             }), \
             patch.object(routes, "_plausibility", return_value=(True, {"reason": "valid"})):
            for state in states:
                with state.lock:
                    state.payload["aircraft_id"] = "0D0001"
                    state.position_observed_at_monotonic = time.monotonic()
                schedule_route_retry(state, routes, plane, 0.01, 0, 0, None, None, None)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if all(state.payload.get("route_resolution_status") == "resolved" for state in states):
                    break
                time.sleep(0.01)
        self.assertEqual(len(provider_calls), 1)
        self.assertTrue(all(state.payload.get("route") == "LHR to MEX" for state in states))

    def test_background_provider_failure_reschedules_with_backoff(self):
        class FailingRoutes:
            def __init__(self):
                self.calls = 0

            def retry_status(self, _plane):
                return {
                    "eligible": self.calls == 0,
                    "retry_in_seconds": 0 if self.calls == 0 else 60,
                    "retry_at_epoch": None,
                }

            def can_lookup(self, _plane):
                return True

            def get(self, _plane):
                self.calls += 1
                return {}

            def _current_lookup_debug(self):
                return {"attempts": [{"requested": True, "request_count": 1}],
                        "decision": "provider returned no route"}

        state = FrameState()
        plane = dict(self.plane, lat=51.5, lon=-0.1)
        with state.lock:
            state.payload["aircraft_id"] = "0D0001"
            state.position_observed_at_monotonic = time.monotonic()
        routes = FailingRoutes()
        schedule_route_retry(state, routes, plane, 0.01, 0, 0, None, None, None)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with state.lock:
                timer = state.route_retry_timer
                status = state.payload.get("route_resolution_status")
            if routes.calls and timer is not None and status == "unresolved":
                break
            time.sleep(0.01)
        self.assertEqual(routes.calls, 1)
        self.assertIsNotNone(state.route_retry_timer)

    def test_report_and_csv_include_summary_and_attempt_history(self):
        now = time.time()
        self.log.observe(self.plane, {}, self.debug(), now=now)
        self.assertIn("Unresolved flights: 1", report(self.log.records(now=now)))
        self.assertIn("Missing origin: 1", report(self.log.records(now=now)))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "review.csv"
            with patch.object(route_failures, "configured_log", return_value=self.log), \
                 patch("sys.argv", ["route_failures.py", "--csv", str(path)]):
                self.assertEqual(route_failures.main(), 0)
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(route_failures.csv.DictReader(stream))
            self.assertEqual(rows[0]["callsign"], "AMX008")
            self.assertIn("airport_information_returned", rows[0]["history"])

    def test_only_visitor_displayed_aircraft_is_logged_not_radar_contacts(self):
        class EmptyStore:
            def get(self, *args):
                return None

        class Logos:
            def get(self, *args):
                return None

        class Response(BytesIO):
            status = 200

            def __init__(self, value):
                super().__init__(json.dumps(value).encode())

        selected = {
            "hex": "0d0001", "flight": "AMX008", "r": "XA-AMQ",
            "lat": 51.5, "lon": -0.1, "seen_pos": 0.1, "alt_baro": 30000,
        }
        radar_only = {
            "hex": "0d0002", "flight": "BAW123", "r": "G-ABCD",
            "lat": 51.6, "lon": -0.2, "seen_pos": 0.1, "alt_baro": 30000,
        }
        with tempfile.TemporaryDirectory() as temporary:
            log = RouteFailureLog(Path(temporary) / "failures.sqlite3")
            sessions = VisitorSessionStore(
                Settings(latitude=51.5, longitude=-0.1, radius_nm=30),
                Path(temporary) / "snapshots", global_min_interval_seconds=0,
                route_failure_log=log,
            )
            session, _ = sessions.get_or_create()
            self.assertIs(session.state.route_failure_log, log)
            store = FlightRouteStore(Path(temporary) / "routes.json")
            with patch.object(store, "_adsblol_standing", return_value={}), \
                 patch("monitor.fetch_aircraft", return_value=[selected, radar_only]), \
                 patch("monitor.urllib.request.urlopen", side_effect=[
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                 ]):
                update_state(
                    session.state, session.settings, False, Logos(), EmptyStore(),
                    routes=store, persist_frame=False,
                )
                session.state.last_enrichment_future.result(timeout=5)
            records = log.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["callsign"], "AMX008")
            self.assertNotIn("BAW123", {row["callsign"] for row in records})
            self.assertEqual(records[0]["failed_attempts"], 1)

    def test_nonvisitor_frame_state_has_no_failure_logger_by_default(self):
        self.assertIsNone(FrameState().route_failure_log)

    def test_explicitly_tracked_aircraft_is_logged_instead_of_nearest_radar_contact(self):
        class EmptyStore:
            def get(self, *args):
                return None

        class Logos:
            def get(self, *args):
                return None

        class Response(BytesIO):
            status = 200

            def __init__(self, value):
                super().__init__(json.dumps(value).encode())

        nearest = {
            "hex": "0d0002", "flight": "BAW123", "r": "G-ABCD",
            "lat": 51.5, "lon": -0.1, "seen_pos": 0.1, "alt_baro": 30000,
        }
        tracked = {
            "hex": "0d0001", "flight": "AMX008", "r": "XA-AMQ",
            "lat": 51.6, "lon": -0.2, "seen_pos": 0.1, "alt_baro": 30000,
        }
        with tempfile.TemporaryDirectory() as temporary:
            log = RouteFailureLog(Path(temporary) / "failures.sqlite3")
            sessions = VisitorSessionStore(
                Settings(latitude=51.5, longitude=-0.1, radius_nm=30),
                Path(temporary) / "snapshots", global_min_interval_seconds=0,
                route_failure_log=log,
            )
            session, _ = sessions.get_or_create()
            session.tracker.start("AMX008", ("AMX008",))
            store = FlightRouteStore(Path(temporary) / "routes.json")
            with patch.object(store, "_adsblol_standing", return_value={}), \
                 patch("monitor.fetch_aircraft", return_value=[nearest]), \
                 patch("monitor.fetch_tracked_aircraft", return_value=(tracked, "AMX008")), \
                 patch("monitor.urllib.request.urlopen", side_effect=[
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                     Response({"airport_codes": "unknown", "_airports": []}),
                 ]):
                update_state(
                    session.state, session.settings, False, Logos(), EmptyStore(),
                    routes=store, tracker=session.tracker, persist_frame=False,
                )
                session.state.last_enrichment_future.result(timeout=5)
            records = log.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["callsign"], "AMX008")
            self.assertNotIn("BAW123", {row["callsign"] for row in records})


if __name__ == "__main__":
    unittest.main()
