import copy
import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from monitor import ROUTE_FAILURE_RETRY_SECONDS, FlightRouteStore, FrameState, flight_details
from overhead import Settings


class EmptyAirports:
    def get(self, code):
        return {}


class FixtureAirports:
    def __init__(self, records):
        self.records = records

    def get(self, code):
        return dict(self.records.get(code, {}))


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
        standing = patch.object(FlightRouteStore, "_adsblol_standing", return_value={})
        standing.start()
        self._standing_patch = standing
        self.addCleanup(standing.stop)

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

    def test_unknown_route_negative_cache_suppresses_duplicate_requests_then_expires(self):
        aircraft = copy.deepcopy(self.plane)
        aircraft["flight"] = "OAT26"
        aircraft["hex"] = "408435"
        self.assertEqual(self.store.get(aircraft), {})
        self.assertEqual(len(self.requests), 2)

        self.requests.clear()
        self.assertEqual(self.store.get(aircraft), {})
        self.assertEqual(self.requests, [])
        self.assertIn("waiting before retry", self.store.debug_snapshot()["decision"])

        key = self.store._cache_key(aircraft)
        self.store._failures[key] -= ROUTE_FAILURE_RETRY_SECONDS
        self.assertEqual(self.store.get(aircraft), {})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(ROUTE_FAILURE_RETRY_SECONDS, 300)

    def test_success_is_persisted_under_original_identity_and_reused(self):
        route = self.store.get(self.plane)
        records = json.loads(self.cache_path.read_text())
        self.assertEqual(list(records), ["AMX008|XAAMQ"])
        self.assertEqual(records["AMX008|XAAMQ"], route)
        self.network.reset_mock()
        reloaded = FlightRouteStore(self.cache_path, airports=EmptyAirports())
        self.assertEqual(reloaded.get(self.plane)["route"], "LHR to MEX")
        self.network.assert_not_called()

    def test_expired_negative_cache_entries_are_pruned_during_lookup(self):
        expired = {f"OLD{index}|REG{index}": 1.0 for index in range(25)}
        self.store._failures.update(expired)
        aircraft = dict(self.plane, flight="OAT26", hex="408435")
        with patch("monitor.time.time", return_value=10_000):
            self.assertEqual(self.store.get(aircraft), {})
        self.assertFalse(set(expired) & self.store._failures.keys())
        self.assertIn(self.store._cache_key(aircraft), self.store._failures)

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

    def test_mixed_suffix_callsign_uses_exact_provider_identifier(self):
        plane = {
            "hex": "4CA123", "flight": "RYR99GN", "r": "EI-IJJ",
            "lat": 52.7, "lon": -1.0, "alt_baro": 28000,
        }
        response = {
            "callsign": "RYR99GN", "airline_code": "RYR", "number": "99GN",
            "_airports": [
                {"iata": "LHR", "icao": "EGLL", "lat": 51.4700, "lon": -0.4543},
                {"iata": "MAN", "icao": "EGCC", "lat": 53.3537, "lon": -2.2750},
            ],
        }
        self.network.side_effect = [Response(status=201, body=b""), Response(response)]

        route = self.store.get(plane)

        self.assertEqual(route["route"], "LHR to MAN")
        self.assertEqual(self.network.call_count, 2)
        requests = [call.args[0] for call in self.network.call_args_list]
        self.assertEqual(json.loads(requests[0].data)["planes"][0]["callsign"], "RYR99GN")
        self.assertIn("/RYR99GN/52.700000/-1.000000", requests[1].full_url)
        self.assertEqual(self.store.debug_snapshot()["alternative_callsign"], "")

    def test_eag21c_explicit_airport_codes_resolve_without_airport_objects(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        plane = fixture["aircraft"]
        airports = FixtureAirports(fixture["airports"])
        store = FlightRouteStore(self.cache_path, airports=airports)

        with patch.object(store, "_adsblol_routeset", return_value=fixture["provider_route"]) as routeset, \
               patch.object(store, "_adsblol_standing", return_value={}), \
             patch.object(store, "_adsblol", side_effect=AssertionError("validated routeset should stop fallback")):
            route = store.get(plane)

        self.assertEqual(route["route"], "CWL to BHD")
        self.assertEqual(route["origin_name"], "Cardiff International Airport")
        self.assertEqual(route["destination_name"], "George Best Belfast City Airport")
        self.assertEqual(route["origin_latitude"], fixture["airports"]["CWL"]["latitude"])
        self.assertEqual(route["destination_longitude"], fixture["airports"]["BHD"]["longitude"])
        self.assertTrue(store.debug_snapshot()["attempts"][-1]["accepted"])
        routeset.assert_called_once_with("EAG21C", plane)

    def test_adsblol_standing_route_is_tried_first_and_stops_provider_chain(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        plane = fixture["aircraft"]
        store = FlightRouteStore(self.cache_path, airports=FixtureAirports(fixture["airports"]))
        events = []

        def response(request, timeout):
            events.append((request.get_method(), request.full_url, timeout))
            if request.full_url == "https://vrs-standing-data.adsb.lol/routes/EA/EAG21C.json":
                return Response(fixture["provider_route"])
            raise AssertionError(f"unexpected provider request: {request.full_url}")

        with patch.object(
            FlightRouteStore, "_adsblol_standing", self._standing_patch.temp_original
        ), patch("monitor.urllib.request.urlopen", side_effect=response):
            route = store.get(plane)

        self.assertEqual(route["route"], "CWL to BHD")
        self.assertEqual(route["route_source"], "ADSB.lol standing route + live validation")
        self.assertEqual([event[1] for event in events], [
            "https://vrs-standing-data.adsb.lol/routes/EA/EAG21C.json",
        ])
        self.assertTrue(all(event[2] == 6 for event in events))
        standing_attempt = next(
            attempt for attempt in store.debug_snapshot()["attempts"]
            if attempt["source"] == "ADSB.lol standing route"
        )
        self.assertEqual(standing_attempt["source"], "ADSB.lol standing route")
        self.assertTrue(standing_attempt["requested"])
        self.assertTrue(standing_attempt["geographical_validation_passed"])
        self.assertTrue(standing_attempt["accepted"])

    def test_standing_route_supports_other_callsign_prefixes(self):
        store = FlightRouteStore(self.cache_path)
        requested = []

        def response(request, timeout):
            requested.append(request.full_url)
            return Response({"airport_codes": "CWL-BHD"})

        with patch.object(
            FlightRouteStore, "_adsblol_standing", self._standing_patch.temp_original
        ), patch("monitor.urllib.request.urlopen", side_effect=response):
            for callsign in ("EAG21C", "UAE123", "RYR99GN", "BAW1234", "A1B234", "ABCDEFGHIJKL"):
                self.assertEqual(store._adsblol_standing(callsign, self.plane), {"airport_codes": "CWL-BHD"})

        self.assertEqual(requested, [
            "https://vrs-standing-data.adsb.lol/routes/EA/EAG21C.json",
            "https://vrs-standing-data.adsb.lol/routes/UA/UAE123.json",
            "https://vrs-standing-data.adsb.lol/routes/RY/RYR99GN.json",
            "https://vrs-standing-data.adsb.lol/routes/BA/BAW1234.json",
            "https://vrs-standing-data.adsb.lol/routes/A1/A1B234.json",
            "https://vrs-standing-data.adsb.lol/routes/AB/ABCDEFGHIJKL.json",
        ])

    def test_standing_route_rejects_callsigns_outside_three_to_twelve_characters(self):
        store = FlightRouteStore(self.cache_path)
        with patch("monitor.urllib.request.urlopen") as urlopen:
            for callsign in ("AB", "ABCDEFGHIJKLM"):
                self.assertEqual(store._adsblol_standing(callsign, self.plane), {})
        urlopen.assert_not_called()

    def test_empty_or_implausible_standing_route_falls_through(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        store = FlightRouteStore(self.cache_path, airports=FixtureAirports(fixture["airports"]))
        for standing_result in ({}, {"airport_codes": "LHR-MEX"}):
            with self.subTest(standing_result=standing_result):
                store._records.clear()
                store._failures.clear()
                with patch.object(store, "_adsblol_standing", return_value=standing_result) as standing, \
                     patch.object(store, "_adsblol_routeset", return_value={}) as routeset, \
                     patch.object(store, "_adsblol", return_value=fixture["provider_route"]) as route_api:
                    route = store.get(fixture["aircraft"])

                self.assertEqual(route["route"], "CWL to BHD")
                self.assertEqual(route["route_source"], "ADSB.lol route + live validation")
                standing.assert_called_once_with("EAG21C", fixture["aircraft"])
                routeset.assert_called_once_with("EAG21C", fixture["aircraft"])
                route_api.assert_called_once_with("EAG21C", fixture["aircraft"])

    def test_standing_route_errors_fall_through_to_validated_api_route(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        failures = (
            urllib.error.HTTPError("fixture", 404, "missing", {}, None),
            urllib.error.HTTPError("fixture", 503, "unavailable", {}, None),
            TimeoutError("fixture timeout"),
            json.JSONDecodeError("bad fixture JSON", "{", 0),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, status=getattr(failure, "code", None)):
                store = FlightRouteStore(self.cache_path, airports=FixtureAirports(fixture["airports"]))
                store._records.clear()
                with patch.object(store, "_adsblol_routeset", return_value={}), \
                     patch.object(store, "_adsblol_standing", side_effect=failure), \
                     patch.object(store, "_adsblol", return_value=fixture["provider_route"]) as route_api:
                    route = store.get(fixture["aircraft"])

                self.assertEqual(route["route"], "CWL to BHD")
                self.assertNotEqual(route["route_source"], "ADSB.lol standing route + live validation")
                route_api.assert_called_once_with("EAG21C", fixture["aircraft"])
                attempts = store.debug_snapshot()["attempts"]
                standing_attempt = next(attempt for attempt in attempts if attempt["source"] == "ADSB.lol standing route")
                self.assertEqual(standing_attempt["error"], type(failure).__name__)
                if isinstance(failure, urllib.error.HTTPError):
                    self.assertEqual(standing_attempt["http_status"], failure.code)

    def test_standing_cooldown_does_not_suppress_api_fallback(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        store = FlightRouteStore(self.cache_path, airports=FixtureAirports(fixture["airports"]))
        restricted = urllib.error.HTTPError(
            "fixture", 429, "standing data limited", {"Retry-After": "120"}, None,
        )
        with patch.object(store, "_adsblol_standing", side_effect=restricted), \
             patch.object(store, "_adsblol_routeset", return_value={}), \
             patch.object(store, "_adsblol", return_value=fixture["provider_route"]) as route_api:
            route = store.get(fixture["aircraft"])

        self.assertEqual(route["route_source"], "ADSB.lol route + live validation")
        route_api.assert_called_once_with("EAG21C", fixture["aircraft"])
        self.assertGreater(store._adsblol_disabled_until["standing"], time.time())
        self.assertEqual(store._adsblol_disabled_until["api"], 0.0)

    def test_api_cooldown_does_not_suppress_standing_route_on_later_lookup(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        store = FlightRouteStore(self.cache_path, airports=FixtureAirports(fixture["airports"]))
        with patch.object(store, "_adsblol_standing", return_value={}), \
             patch.object(store, "_adsblol_routeset", side_effect=urllib.error.HTTPError(
                 "fixture", 429, "api limited", {"Retry-After": "120"}, None,
             )), \
             patch.object(store, "_adsblol", side_effect=AssertionError("API cooldown must skip route GET")):
            self.assertEqual(store.get(fixture["aircraft"]), {})

        store._records.clear()
        store._failures.clear()
        with patch.object(store, "_adsblol_standing", return_value=fixture["provider_route"]) as standing, \
             patch.object(store, "_adsblol_routeset", side_effect=AssertionError("API cooldown must skip routeset")), \
             patch.object(store, "_adsblol", side_effect=AssertionError("API cooldown must skip route GET")):
            route = store.get(fixture["aircraft"])

        self.assertEqual(route["route_source"], "ADSB.lol standing route + live validation")
        standing.assert_called_once_with("EAG21C", fixture["aircraft"])
        self.assertGreater(store._adsblol_disabled_until["api"], time.time())

    def test_invalid_callsigns_do_not_create_standing_route_requests(self):
        store = FlightRouteStore(self.cache_path)
        with patch("monitor.urllib.request.urlopen") as urlopen:
            self.assertEqual(store._adsblol_standing("", self.plane), {})
            self.assertEqual(store._adsblol_standing("AB-C", self.plane), {})
        urlopen.assert_not_called()

    def test_unrelated_aircraft_route_lookups_run_concurrently(self):
        entered = {"one": threading.Event(), "two": threading.Event()}
        release = threading.Event()

        def lookup(callsign, _plane):
            entered[callsign].set()
            if not release.wait(3):
                raise TimeoutError("second independent route lookup was serialized")
            return self.fixture["unknown"]

        planes = [
            dict(self.plane, flight="TST101", r="G-TEST1"),
            dict(self.plane, flight="TST202", r="G-TEST2"),
        ]
        callsigns = {"TST101": "one", "TST202": "two"}

        def controlled(callsign, plane):
            return lookup(callsigns[callsign], plane)

        with patch.object(self.store, "_adsblol_routeset", side_effect=controlled), \
             patch.object(self.store, "_adsblol", return_value={}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(self.store.get, plane) for plane in planes]
                try:
                    self.assertTrue(entered["one"].wait(1))
                    self.assertTrue(entered["two"].wait(1), "unrelated lookup waited behind a provider request")
                finally:
                    release.set()
                self.assertEqual([future.result(timeout=3) for future in futures], [{}, {}])

    def test_success_clears_failure_state_and_preserves_cached_route_after_save_error(self):
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        plane = fixture["aircraft"]
        key = self.store._cache_key(plane)
        self.store._failures[key] = 1000
        route_payload = {
            "_airport_codes_iata": "CWL-BHD",
            "plausible": True,
        }
        with patch("monitor.time.time", return_value=1000 + ROUTE_FAILURE_RETRY_SECONDS + 1), \
             patch.object(self.store, "_adsblol_routeset", return_value=route_payload), \
             patch.object(self.store, "_adsblol", side_effect=AssertionError("route should be resolved")), \
             patch.object(self.store, "_save", side_effect=OSError("read-only cache")):
            self.store.airports = FixtureAirports(fixture["airports"])
            route = self.store.get(plane)

        self.assertEqual(route["route"], "CWL to BHD")
        self.assertNotIn(key, self.store._failures)
        self.assertEqual(self.store._records[key]["route"], "CWL to BHD")

    def test_flightaware_registration_only_lookup_is_eligible_and_resolved(self):
        plane = {
            "hex": "407A12", "flight": "G-CMJL", "r": "G-CMJL",
            "lat": 53.2, "lon": -4.4, "alt_baro": 18000,
        }
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        store = FlightRouteStore(
            self.cache_path, api_key="fixture-key", airports=FixtureAirports(fixture["airports"]),
        )
        flight = {
            "registration": "G-CMJL", "ident": "EAG21C", "atc_ident": "EAG21C",
            "origin": {"code_iata": "CWL"}, "destination": {"code_iata": "BHD"},
            "actual_off": "2026-11-15T08:00:00Z",
            "last_position": {"latitude": 53.2, "longitude": -4.4, "timestamp": "2026-11-15T09:08:00Z"},
        }
        self.assertFalse(store.callsign(plane))
        self.assertTrue(store.can_lookup(plane))
        with patch.object(store, "_aeroapi", return_value=[flight]) as aeroapi, \
             patch.object(store, "_adsblol_routeset", side_effect=AssertionError("accepted FlightAware route should stop fallback")):
            route = store.get(plane)
        self.assertEqual(route["route"], "CWL to BHD")
        aeroapi.assert_called_once_with("GCMJL")

    def test_flightaware_registration_error_does_not_skip_callsign_fallback(self):
        plane = {
            "hex": "407A12", "flight": "EAG21C", "r": "G-CMJL",
            "lat": 53.2, "lon": -4.4, "alt_baro": 18000,
        }
        fixture = json.loads(
            Path(__file__).with_name("fixtures").joinpath("eag21c_cwl_bhd_route.json").read_text()
        )
        store = FlightRouteStore(
            self.cache_path, api_key="fixture-key", airports=FixtureAirports(fixture["airports"]),
        )
        flight = {
            "registration": "G-CMJL", "ident": "EAG21C", "atc_ident": "EAG21C",
            "origin": {"code_iata": "CWL"}, "destination": {"code_iata": "BHD"},
            "actual_off": "2026-11-15T08:00:00Z",
            "last_position": {"latitude": 53.2, "longitude": -4.4, "timestamp": "2026-11-15T09:08:00Z"},
        }

        def lookup(identifier):
            if identifier == "GCMJL":
                raise urllib.error.HTTPError("fixture", 404, "not found", {}, None)
            return [flight]

        with patch.object(store, "_aeroapi", side_effect=lookup) as aeroapi:
            route = store.get(plane)

        self.assertEqual(route["route"], "CWL to BHD")
        self.assertEqual([call.args[0] for call in aeroapi.call_args_list], ["GCMJL", "EAG21C"])

    def test_callsign_variants_for_one_registration_share_provider_lookup(self):
        first = dict(self.plane, flight="TST101", r="G-SHARED")
        second = dict(self.plane, flight="TST202", r="G-SHARED")
        entered = threading.Event()
        release = threading.Event()

        def slow_lookup(_callsign, _plane):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("shared registration route lock did not release")
            return self.fixture["unknown"]

        with patch.object(self.store, "_adsblol_routeset", side_effect=slow_lookup) as lookup, \
             patch.object(self.store, "_adsblol", return_value={}):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(self.store.get, first)
                self.assertTrue(entered.wait(1))
                second_future = executor.submit(self.store.get, second)
                release.set()
                self.assertEqual(first_future.result(timeout=3), {})
                self.assertEqual(second_future.result(timeout=3), {})
        self.assertEqual(lookup.call_count, 2)

    def test_cached_route_is_reused_when_live_callsign_is_missing(self):
        route = {
            "route": "CWL to BHD", "origin": "CWL", "destination": "BHD",
            "origin_latitude": 51.396702, "origin_longitude": -3.34333,
            "destination_latitude": 54.618099, "destination_longitude": -5.8725,
            "cached_at": time.time(), "route_source": "fixture",
        }
        self.store._records[self.store._cache_key(self.plane)] = route
        plane = dict(self.plane, flight="", lat=53.2, lon=-4.4, alt_baro=18000)
        with patch.object(self.store, "_adsblol_routeset", side_effect=AssertionError("cache should be reused")):
            self.assertEqual(self.store.cached_route(plane)["route"], "CWL to BHD")
            self.assertEqual(self.store.get(plane)["route"], "CWL to BHD")
        self.assertTrue(self.store.can_lookup(plane))
        self.assertEqual(self.store._records[self.store._cache_key(self.plane)], route)

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
                self.assertIn("error", next(
                    attempt for attempt in self.store.debug_snapshot()["attempts"]
                    if attempt["source"] == "ADSB.lol routeset"
                ))

    def test_empty_201_routeset_is_no_data_not_json_error(self):
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        attempts = self.store.debug_snapshot()["attempts"]
        empty_attempts = [
            attempt for attempt in attempts
            if attempt["source"] in {"ADSB.lol routeset", "ADSB.lol route"}
            and attempt["reason"] == "provider returned no route data"
        ]
        self.assertEqual(len(empty_attempts), 2)
        for attempt in empty_attempts:
            self.assertEqual(attempt["reason"], "provider returned no route data")
            self.assertNotIn("error", attempt)
        self.assertEqual(self.store.get(dict(self.plane, r="XA-TEST"))["route"], "LHR to MEX")

    def test_nonempty_malformed_routeset_is_reported_and_lookup_continues(self):
        self.network.side_effect = [
            Response(body=b"not json"), Response(self.fixture["unknown"]),
            Response(body=b""), Response(self.fixture["normalised"]),
        ]
        self.assertEqual(self.store.get(self.plane)["route"], "LHR to MEX")
        routeset_attempt = next(
            attempt for attempt in self.store.debug_snapshot()["attempts"]
            if attempt["source"] == "ADSB.lol routeset"
        )
        self.assertEqual(routeset_attempt["error"], "JSONDecodeError")

    def test_api_provider_rate_limit_pauses_both_api_endpoints_and_respects_retry_after(self):
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
        self.assertIn("cooldown", self.store.debug_snapshot()["decision"].lower())
        self.assertGreater(self.store.debug_snapshot()["retry_in_seconds"], 0)
        with patch("monitor.time.time", return_value=1301):
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
