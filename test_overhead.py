import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import AircraftIdentityStore, FlagStore, FlightRouteStore, FrameState, PAGE, RADAR_RANGES, aircraft_identity, aircraft_name, flight_details, operator_code, radar_contacts, safe_svg, update_state
from overhead import DEMO, HEIGHT, WIDTH, Settings, produce_frame, select_nearest


class OverHeadTests(unittest.TestCase):
    def test_selects_positioned_aircraft(self):
        plane, distance = select_nearest(DEMO["ac"], Settings())
        self.assertEqual(plane["flight"].strip(), "BAW283")
        self.assertGreaterEqual(distance, 0)

    def test_outputs_exact_rgb888_frame_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                output_rgb=str(Path(tmp) / "frame.rgb"),
                output_png=str(Path(tmp) / "frame.png"),
            )
            image, status = produce_frame(settings, demo=True)
            self.assertEqual(image.size, (WIDTH, HEIGHT))
            self.assertEqual(status, "demo")
            self.assertEqual((Path(tmp) / "frame.rgb").stat().st_size, WIDTH * HEIGHT * 3)
            self.assertTrue((Path(tmp) / "frame.png").is_file())

    def test_operator_code_ignores_registration_callsigns(self):
        self.assertEqual(operator_code({"flight": "RYR6432", "r": "SP-RZX"}), "RYR")
        self.assertEqual(operator_code({"flight": "GBNZB", "r": "G-BNZB"}), "")

    def test_flight_details_deduplicates_formatted_registration(self):
        self.assertEqual(flight_details({"flight": "GBTDV", "r": "G-BTDV"}), "GBTDV")
        self.assertEqual(flight_details({"flight": "BAW283", "r": "G-STBH"}), "BAW283  \u00b7  G-STBH")

    def test_aircraft_identity_prefers_hex_code(self):
        plane = {"hex": "4ca259", "r": "EI-DHD", "flight": "RYR701"}
        self.assertEqual(aircraft_identity(plane), "4CA259")
        self.assertEqual(aircraft_identity({"r": "G-BNZB"}), "GBNZB")

    def test_expands_common_aircraft_models(self):
        self.assertEqual(aircraft_name("B738"), "BOEING 737-800")
        self.assertEqual(aircraft_name("A20N"), "AIRBUS A320NEO")
        self.assertEqual(aircraft_name("P28A"), "PIPER PA-28 CHEROKEE")
        self.assertEqual(aircraft_name("B762"), "BOEING 767-200")
        self.assertEqual(aircraft_name("B764"), "BOEING 767-400ER")
        self.assertEqual(aircraft_name("B76F"), "BOEING 767 FREIGHTER")
        self.assertEqual(aircraft_name("CL60"), "BOMBARDIER CHALLENGER 600 SERIES")

    def test_svg_safety_filter(self):
        self.assertIsNotNone(safe_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'))
        self.assertIsNone(safe_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>'))

    def test_enrichment_is_persisted_and_reused(self):
        class StubStore(AircraftIdentityStore):
            calls = 0

            def _fetch(self, identifier):
                self.calls += 1
                return {
                    "type": "Challenger 650", "icao_type": "CL65", "manufacturer": "Bombardier",
                    "mode_s": "A1C25F", "registration": "N212QS",
                    "registered_owner_operator_flag_code": "EJA", "registered_owner": "NetJets",
                }

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "aircraft-identities.json"
            first = StubStore(cache)
            identity = first.get({"hex": "a1c25f", "r": "N212QS", "t": "CL60"})
            self.assertEqual(identity["aircraft_name"], "BOMBARDIER CL-600-2B16 CHALLENGER 650")
            self.assertEqual(identity["airline_name"], "NetJets")
            self.assertEqual(first.calls, 1)
            second = StubStore(cache)
            self.assertEqual(second.get({"hex": "a1c25f"}), identity)
            self.assertEqual(second.calls, 0)

    def test_monitor_retains_last_live_aircraft_during_outage(self):
        class EmptyStore:
            def get(self, value):
                return None if isinstance(value, str) else {}

        plane = dict(DEMO["ac"][0])
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                output_rgb=str(Path(tmp) / "frame.rgb"),
                output_png=str(Path(tmp) / "frame.png"),
            )
            state = FrameState()
            with patch("monitor.fetch_aircraft", side_effect=[[plane], OSError("offline")]):
                update_state(state, settings, False, EmptyStore(), EmptyStore())
                self.assertEqual(state.payload["mode"], "live")
                update_state(state, settings, False, EmptyStore(), EmptyStore())
            self.assertEqual(state.payload["mode"], "reconnecting")
            self.assertEqual(state.payload["aircraft_id"], aircraft_identity(plane))

    def test_route_is_cached_by_callsign(self):
        class StubRoutes(FlightRouteStore):
            calls = 0

            def _fetch(self, callsign):
                self.calls += 1
                return {
                    "origin": {"iata_code": "LHR", "icao_code": "EGLL", "country_iso_name": "GB"},
                    "destination": {"iata_code": "LAX", "icao_code": "KLAX", "country_iso_name": "US"},
                }

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "flight-routes.json"
            first = StubRoutes(cache)
            self.assertEqual(first.get({"flight": "BAW283", "r": "G-STBH"})["route"], "LHR to LAX")
            self.assertEqual(first.calls, 1)
            second = StubRoutes(cache)
            self.assertEqual(second.get({"flight": "BAW283"})["route"], "LHR to LAX")
            self.assertEqual(second.calls, 0)
            self.assertEqual(second.get({"flight": "N688CB", "r": "N688CB"}), {})

    def test_missing_logo_has_no_text_fallback(self):
        self.assertNotIn(b"operator-badge", PAGE)

    def test_unknown_route_has_muted_placeholder(self):
        self.assertIn(b"N/A to N/A", PAGE)
        self.assertIn(b"route-unknown", PAGE)

    def test_route_flags_fill_airport_letters(self):
        self.assertIn(b"viewBox','0 0 150 100", PAGE)
        self.assertIn(b"textLength','144", PAGE)
        self.assertIn(b"lengthAdjust','spacingAndGlyphs", PAGE)
        self.assertIn(b"route-mask-", PAGE)
        self.assertIn(b"preserveAspectRatio','none", PAGE)
        self.assertIn(b"airport('origin'", PAGE)
        self.assertIn(b"airport('destination'", PAGE)
        self.assertNotIn(b"route-letter", PAGE)
        self.assertEqual(FlagStore._codepoints("GB"), "1f1ec-1f1e7")

    def test_radar_centres_aircraft_at_home(self):
        settings = Settings(latitude=51.5, longitude=-3.3, radius_nm=25)
        plane = {"hex": "abc123", "flight": "TST123", "lat": 51.5, "lon": -3.3, "seen_pos": 0}
        contacts = radar_contacts([plane], settings, plane)
        self.assertEqual((contacts[0]["x"], contacts[0]["y"]), (50.0, 50.0))
        self.assertTrue(contacts[0]["selected"])

    def test_radar_ranges_are_ordered_and_include_default(self):
        self.assertEqual(tuple(sorted(RADAR_RANGES)), RADAR_RANGES)
        self.assertIn(25, RADAR_RANGES)

    def test_live_mode_requires_explicit_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.json"
            with self.assertRaises(FileNotFoundError):
                from overhead import load_settings
                load_settings(missing)

    def test_explicit_demo_may_run_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "config.json"
            from overhead import load_settings
            settings = load_settings(missing, allow_missing=True)
            self.assertIsInstance(settings, Settings)

    def test_live_fetch_never_falls_back_to_demo(self):
        settings = Settings(demo_on_failure=True)
        from overhead import fetch_traffic_snapshot
        with patch("overhead.fetch_aircraft", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                fetch_traffic_snapshot(settings, demo=False)

    def test_demo_data_requires_explicit_demo_flag(self):
        settings = Settings()
        from overhead import fetch_traffic_snapshot
        aircraft, status = fetch_traffic_snapshot(settings, demo=True)
        self.assertEqual(status, "demo")
        self.assertEqual(aircraft, DEMO["ac"])

    def test_monitor_has_clean_configuration_required_state(self):
        from monitor import CONFIG_REQUIRED_PAGE
        self.assertIn(b"LOCATION NOT CONFIGURED", CONFIG_REQUIRED_PAGE)
        self.assertIn(b"No default location", CONFIG_REQUIRED_PAGE)
        self.assertNotIn(b"51.5074", CONFIG_REQUIRED_PAGE)
        self.assertNotIn(b"BAW283", CONFIG_REQUIRED_PAGE)

    def test_route_airports_are_fixed_to_identical_flag_proportions(self):
        self.assertIn(b"min-width:1.32em", PAGE)
        self.assertIn(b"max-width:1.32em", PAGE)
        self.assertIn(b"min-height:.88em", PAGE)
        self.assertIn(b"max-height:.88em", PAGE)

    def test_route_airport_visible_heights_are_normalised(self):
        self.assertIn(b"route-airport-text", PAGE)
        self.assertIn(b"normaliseAirportHeights", PAGE)
        self.assertIn(b"Math.max(...boxes.map(box=>box.height))", PAGE)
        self.assertIn(b"scale(1 '+scaleY+')", PAGE)

    def test_only_square_national_flags_use_square_source_artwork(self):
        from monitor import SQUARE_FLAG_COUNTRIES
        self.assertEqual(SQUARE_FLAG_COUNTRIES, frozenset({"CH", "VA"}))
        self.assertIn(b"country==='CH'||country==='VA'", PAGE)
        self.assertIn(b"image.setAttribute('x','25')", PAGE)
        self.assertIn(b"image.setAttribute('width','100')", PAGE)

    def test_square_flags_extend_edge_colours_to_standard_route_width(self):
        self.assertIn(b"underlay.setAttribute('width','150')", PAGE)
        self.assertIn(b"underlay.setAttribute('preserveAspectRatio','none')", PAGE)
        self.assertIn(b"image.setAttribute('preserveAspectRatio','xMidYMid meet')", PAGE)

    def test_airport_letters_have_house_grey_outline_and_preserve_flag_black(self):
        self.assertIn(b"route-airport-outline", PAGE)
        self.assertIn(b"stroke:#6c9aac", PAGE)
        self.assertNotIn(b"recolour_flag_black", PAGE)


if __name__ == "__main__":
    unittest.main()
