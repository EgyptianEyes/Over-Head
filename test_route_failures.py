import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from monitor import FlightRouteStore, FlightTrackingState, FrameState, VisitorSessionStore, update_state
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
            "attempts": attempts or [
                {"source": "ADSB.lol route", "callsign": "AMX008", "requested": True,
                 "request_count": 1, "http_status": 200, "accepted": False,
                 "airport_information_returned": True,
                 "geographical_validation_passed": False,
                 "raw": {"airport_codes": "unknown", "airports": []},
                 "reason": "outside route corridor"},
                {"source": "ADSB.lol route", "callsign": "AMX8", "requested": True,
                 "request_count": 1, "http_status": 200, "accepted": False,
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

    def test_storage_errors_do_not_escape_observation(self):
        with self.assertLogs("route_failures", level="ERROR"):
            with patch.object(self.log, "_connect", side_effect=OSError("disk unavailable")):
                self.log.observe(self.plane, {}, self.debug())
        self.assertEqual(self.log.records(), [])

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
            with patch("monitor.fetch_aircraft", return_value=[selected, radar_only]), \
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
            with patch("monitor.fetch_aircraft", return_value=[nearest]), \
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
