import copy
import concurrent.futures
import json
import tempfile
import threading
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from monitor import FlightRouteStore, FrameState, flight_details
from overhead import Settings


class EmptyAirports:
    def get(self, code):
        return {}


class Response(BytesIO):
    def __init__(self, payload=None, status=200, body=None):
        super().__init__(body if body is not None else json.dumps(payload).encode())
        self.status = status


class FlightRouteRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("leading_zero_routes.json").read_text()
        )
        self.plane = copy.deepcopy(self.fixture["aircraft"])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache_path = Path(self.temporary.name) / "routes.json"
        self.store = FlightRouteStore(self.cache_path, airports=EmptyAirports())
        self.requests = []
        network = patch("monitor.urllib.request.urlopen", side_effect=self.respond)
        self.network = network.start()
        self.addCleanup(network.stop)

    def respond(self, request, timeout):
        self.requests.append(request)
        if request.get_method() == "POST":
            return Response(status=201, body=b"")
        callsign = request.full_url.split("/route/")[1].split("/")[0]
        payload = self.fixture["normalised"] if callsign == "AMX8" else self.fixture["unknown"]
        return Response(payload)

    def attempted_callsigns(self):
        return [
            json.loads(request.data)["planes"][0]["callsign"] if request.get_method() == "POST"
            else request.full_url.split("/route/")[1].split("/")[0]
            for request in self.requests
        ]

    def test_original_unknown_then_normalised_route_passes_live_validation(self):
        route = self.store.get(self.plane)
        self.assertEqual(route["route"], "LHR to MEX")
        self.assertEqual(route["route_source"], "ADSB.lol route + live validation")
        self.assertEqual(self.attempted_callsigns(), ["AMX008", "AMX008", "AMX8", "AMX8"])
        self.assertIn("/AMX8/51.517220/-7.161865", self.requests[-1].full_url)
        for request, callsign in zip(self.requests, self.attempted_callsigns()):
            self.assertTrue(request.full_url.startswith("https://api.adsb.lol/"))
            if request.get_method() == "POST":
                self.assertEqual(json.loads(request.data)["planes"][0], {
                    "callsign": callsign,
                    "lat": self.plane["lat"], "lng": self.plane["lon"],
                })
        accepted, detail = self.store._plausibility(route, self.plane)
        self.assertTrue(accepted, detail)
        debug = self.store.debug_snapshot()
        self.assertEqual(debug["original_callsign"], "AMX008")
        self.assertEqual(debug["alternative_callsign"], "AMX8")
        attempt = debug["attempts"][-1]
        self.assertTrue(attempt["airport_information_returned"])
        self.assertTrue(attempt["geographical_validation_passed"])
        self.assertTrue(attempt["accepted"])
        self.assertIn("Accepted ADSB.lol route", debug["decision"])

    def test_success_is_persisted_under_original_identity_and_reused(self):
        route = self.store.get(self.plane)
        records = json.loads(self.cache_path.read_text())
        self.assertEqual(list(records), ["AMX008|XAAMQ"])
        self.assertEqual(records["AMX008|XAAMQ"], route)
        self.network.reset_mock()
        reloaded = FlightRouteStore(self.cache_path, airports=EmptyAirports())
        self.assertEqual(reloaded.get(self.plane)["route"], "LHR to MEX")
        self.network.assert_not_called()

    def test_original_success_does_not_request_alternative(self):
        self.fixture["unknown"] = self.fixture["normalised"]
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        self.assertEqual(self.attempted_callsigns(), ["AMX008", "AMX008"])

    def test_original_routeset_success_stops_lookup_chain(self):
        self.network.side_effect = [Response([self.fixture["normalised"]])]
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        self.assertEqual(self.network.call_count, 1)

    def test_safe_normalisation_for_general_callsigns(self):
        for callsign, alternative in (
            ("AMX008", "AMX8"), ("BAW00012", "BAW12"), ("ZZ0010", "ZZ10"),
            ("ABC00", "ABC0"), ("AMX8", ""), ("ABC0", ""), ("RYR6TQ", ""),
            ("AMX008A", ""), ("ABC001AB", ""), ("A1B008", ""), ("008", ""),
        ):
            with self.subTest(callsign=callsign):
                self.assertEqual(self.store._alternative_callsign(callsign), alternative)

    def test_unchanged_callsigns_and_suffixes_do_not_generate_extra_requests(self):
        for callsign in ("AMX8", "BAW123", "AMX008A", "RYR6TQ"):
            with self.subTest(callsign=callsign):
                self.plane["flight"] = callsign
                self.fixture["normalised"] = self.fixture["unknown"]
                self.requests.clear()
                self.assertEqual(self.store.get(self.plane), {})
                self.assertEqual(self.attempted_callsigns(), [callsign, callsign])
                self.assertEqual(self.plane["flight"], callsign)

    def test_implausible_normalised_route_is_rejected_despite_airport_codes(self):
        self.fixture["normalised"] = self.fixture["implausible"]
        self.assertEqual(self.store.get(self.plane), {})
        self.assertFalse(self.cache_path.exists())
        debug = self.store.debug_snapshot()
        self.assertFalse(debug["attempts"][-1]["geographical_validation_passed"])
        self.assertEqual(debug["attempts"][-1]["reason"], "outside route corridor")
        self.assertIn("No trustworthy route", debug["decision"])

    def test_no_live_position_rejects_candidate(self):
        self.plane.pop("lat")
        with patch.object(self.store, "_adsblol", return_value=self.fixture["normalised"]):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.store.debug_snapshot()["attempts"][-1]["reason"],
                         "aircraft has no valid live position")

    def test_cached_alternative_is_revalidated_against_current_position(self):
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        self.plane.update(lat=34, lon=-118)
        self.assertEqual(self.store.get(self.plane), {})
        cache_attempt = self.store.debug_snapshot()["attempts"][0]
        self.assertFalse(cache_attempt["accepted"])
        self.assertFalse(cache_attempt["geographical_validation_passed"])

    def test_failures_do_not_block_subsequent_normalised_lookup(self):
        for failure in (TimeoutError(), urllib.error.HTTPError("fixture", 500, "failed", {}, None),
                        json.JSONDecodeError("fixture", "", 0)):
            with self.subTest(failure=type(failure).__name__):
                self.store._records.clear()
                self.requests.clear()
                self.network.side_effect = [
                    failure, Response(self.fixture["unknown"]),
                    Response(status=201, body=b""), Response(self.fixture["normalised"]),
                ]
                self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
                self.assertIn("error", self.store.debug_snapshot()["attempts"][1])

    def test_empty_201_routeset_is_no_data_not_json_error(self):
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        attempts = self.store.debug_snapshot()["attempts"]
        for attempt in (attempts[1], attempts[3]):
            self.assertEqual(attempt["reason"], "provider returned no route data")
            self.assertNotIn("error", attempt)
        self.assertEqual(self.store.get(dict(self.plane, r="XA-TEST"))["route"], "LHR to MEX")

    def test_nonempty_malformed_routeset_is_reported_and_lookup_continues(self):
        self.network.side_effect = [
            Response(body=b"not json"), Response(self.fixture["unknown"]),
            Response(body=b""), Response(self.fixture["normalised"]),
        ]
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        self.assertEqual(self.store.debug_snapshot()["attempts"][1]["error"], "JSONDecodeError")

    def test_provider_rate_limit_pauses_both_endpoints_and_respects_retry_after(self):
        self.network.side_effect = urllib.error.HTTPError(
            "fixture", 429, "limited", {"Retry-After": "120"}, None
        )
        with patch("monitor.time.time", return_value=1000):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.network.call_count, 1)
        self.network.side_effect = self.respond
        with patch("monitor.time.time", return_value=1031):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.network.call_count, 1)
        with patch("monitor.time.time", return_value=1121):
            self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")

    def test_failed_identity_cooldown_covers_original_and_alternative(self):
        self.fixture["normalised"] = self.fixture["unknown"]
        with patch("monitor.time.time", return_value=1000):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.network.call_count, 4)
        with patch("monitor.time.time", return_value=1029):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.network.call_count, 4)

    def test_provider_forbidden_response_is_not_retried_via_another_endpoint(self):
        self.network.side_effect = urllib.error.HTTPError(
            "fixture", 403, "approval required", {}, None
        )
        with patch("monitor.time.time", return_value=1000):
            self.assertEqual(self.store.get(self.plane), {})
        self.assertEqual(self.network.call_count, 1)
        with patch("monitor.time.time", return_value=1031):
            self.assertEqual(self.store.get(dict(self.plane, r="XA-OTHER")), {})
        self.assertEqual(self.network.call_count, 1)
        self.assertNotIn("adsb.im", str(self.store.debug_snapshot()))

    def test_concurrent_same_identity_lookups_share_successful_cache(self):
        barrier = threading.Barrier(2)

        def resolve():
            barrier.wait(timeout=5)
            return self.store.get(dict(self.plane))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(resolve) for _ in range(2)]
            self.assertEqual([future.result(timeout=5)["route"] for future in futures],
                             ["LHR to MEX", "LHR to MEX"])
        self.assertEqual(self.network.call_count, 4)

    def test_ui_and_aircraft_identity_keep_original_callsign(self):
        original = dict(self.plane)
        route = self.store.get(self.plane)
        state = FrameState()
        state.update(Image.new("RGB", (128, 64)), "live", self.plane, 1,
                     [self.plane], Settings(), None, route=route)
        self.assertEqual(self.plane, original)
        self.assertEqual(state.payload["callsign"], "AMX008")
        self.assertEqual(state.payload["route"], "LHR to MEX")
        self.assertIn("AMX008", flight_details(self.plane))


if __name__ == "__main__":
    unittest.main()
