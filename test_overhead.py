import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from monitor import APIHealthMonitor, AircraftIdentityStore, AirportStore, CONFIG_REQUIRED_PAGE, FlagStore, FlightRouteStore, FlightTrackingState, FrameState, LogoStore, PAGE, RADAR_RANGES, TRACK_RADAR_RANGES, aircraft_identity, aircraft_name, automatic_tracking_radius, flight_details, merge_tracked_aircraft, normalise_flight_query, operator_code, overhead_logo_asset, radar_contacts, safe_svg, save_location_config, tracking_distance_nm, update_live_location_config, update_state, flightaware_api_key
from overhead import DEMO, HEIGHT, WIDTH, Settings, load_settings, produce_frame, select_nearest


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
            def get(self, value, *args):
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

    def test_flightaware_maps_atc_callsign_to_current_commercial_flight(self):
        class StubAirports:
            records = {
                "BCN": {"country_iso_name": "ES", "latitude": 41.2971, "longitude": 2.0785},
                "LPL": {"country_iso_name": "GB", "latitude": 53.3336, "longitude": -2.8497},
            }
            def get(self, code):
                return dict(self.records.get(code, {}))

        flight = {
            "ident": "RYR6559",
            "ident_icao": "RYR6559",
            "ident_iata": "FR6559",
            "atc_ident": "RYR90RR",
            "registration": "EI-DYD",
            "cancelled": False,
            "actual_off": "2026-09-06T12:10:00Z",
            "actual_on": None,
            "actual_in": None,
            "origin": {"code": "LEBL", "code_icao": "LEBL", "code_iata": "BCN"},
            "destination": {"code": "EGGP", "code_icao": "EGGP", "code_iata": "LPL"},
            "last_position": {
                "latitude": 51.69,
                "longitude": -3.35,
                "timestamp": "2026-09-06T13:45:00Z",
            },
        }

        class StubRoutes(FlightRouteStore):
            def _aeroapi(self, ident):
                return [flight]
            def _fetch(self, callsign):
                raise AssertionError("ADSBDB fallback should not be used for a strong FlightAware match")

        with tempfile.TemporaryDirectory() as tmp, patch("monitor.time.time", return_value=1788702400):
            store = StubRoutes(Path(tmp) / "routes.json", api_key="test-key", airports=StubAirports())
            route = store.get({"flight": "RYR90RR", "r": "EI-DYD", "lat": 51.690057, "lon": -3.358753})
        self.assertEqual(route["route"], "BCN to LPL")
        self.assertEqual(route["commercial_flight"], "FR6559")
        self.assertEqual(route["atc_ident"], "RYR90RR")
        self.assertEqual(route["route_source"], "FlightAware AeroAPI")
        self.assertEqual(route["origin_country"], "ES")
        self.assertEqual(route["destination_country"], "GB")

    def test_flightaware_key_environment_overrides_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"latitude": 51.5, "longitude": -3.3, "flightaware_api_key": "config-key"}), encoding="utf-8")
            with patch.dict("os.environ", {"FLIGHTAWARE_AEROAPI_KEY": "environment-key"}):
                self.assertEqual(flightaware_api_key(config), "environment-key")
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(flightaware_api_key(config), "config-key")


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

    def test_monitor_has_first_run_location_selector(self):
        self.assertIn(b"LOCATION NOT CONFIGURED", CONFIG_REQUIRED_PAGE)
        self.assertIn(b"No default location", CONFIG_REQUIRED_PAGE)
        self.assertIn(b'id="setup-map"', CONFIG_REQUIRED_PAGE)
        self.assertIn(b'id="latitude"', CONFIG_REQUIRED_PAGE)
        self.assertIn(b'id="longitude"', CONFIG_REQUIRED_PAGE)
        self.assertIn(b"navigator.geolocation", CONFIG_REQUIRED_PAGE)
        self.assertIn(b"map.on('click'", CONFIG_REQUIRED_PAGE)
        self.assertIn(b"fetch('/setup'", CONFIG_REQUIRED_PAGE)
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

    def test_saved_location_config_round_trips_without_demo_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            latitude, longitude = save_location_config(config, 51.123456, -3.123456)
            self.assertEqual((latitude, longitude), (51.123456, -3.123456))
            settings = load_settings(config)
            self.assertEqual(settings.latitude, 51.123456)
            self.assertEqual(settings.longitude, -3.123456)
            self.assertFalse(settings.demo_on_failure)

    def test_existing_live_config_cannot_omit_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text('{"radius_nm": 25}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(config)

    def test_route_map_centres_full_route_with_equal_padding(self):
        self.assertIn(b'id="route-map"', PAGE)
        self.assertIn(b"centredRouteViewport", PAGE)
        self.assertIn(b"const centrePixel=L.point((minX+maxX)/2,(minY+maxY)/2)", PAGE)
        self.assertIn(b"Math.log2(availableWidth/spanX)", PAGE)
        self.assertIn(b"Math.log2(availableHeight/spanY)", PAGE)
        self.assertIn(b"routeMap.setView(viewport.centre,viewport.zoom", PAGE)
        self.assertIn(b"centredRouteViewport(routeMap,[origin,centre,destination])", PAGE)
        self.assertIn(b"dashArray:'2 7'", PAGE)
        self.assertIn(b"routeAircraftIcon", PAGE)
        self.assertIn(b"OpenStreetMap contributors", PAGE)

    def test_airline_logo_or_name_sits_above_route_map(self):
        self.assertIn(b'<div class="brand"><img class="brand-logo" id="brand-logo" alt="Over-Head"></div>', PAGE)
        self.assertIn(b'class="route-brand" id="route-brand"', PAGE)
        self.assertIn(b'id="route-brand-name"', PAGE)
        self.assertIn(b'class="route-brand-logo" id="operator-logo"', PAGE)
        self.assertIn(b"const airlineLabel=d.airline_name||''", PAGE)
        self.assertIn(b"brandName.textContent=airlineLabel", PAGE)

    def test_route_brand_matches_edited_composition_without_affecting_layout(self):
        self.assertIn(b".flight-heading { position:relative", PAGE)
        self.assertIn(b".route-visual { position:absolute; top:clamp(-126px,-13.5vh,-112px); right:0", PAGE)
        self.assertIn(b".route-brand { position:absolute; top:0", PAGE)
        self.assertIn(b"background:linear-gradient(90deg", PAGE)
        self.assertIn(b".route-map { display:none; position:absolute; top:clamp(90.882px,10.3275vh,99.144px)", PAGE)
        self.assertIn(b"left:calc(50% + clamp(18px,1.2vw,24px))", PAGE)
        self.assertIn(b"async function fitLogoArtwork(img)", PAGE)

    def test_route_map_remains_ten_percent_larger_and_route_has_25_percent_padding(self):
        self.assertIn(b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px)", PAGE)
        self.assertIn(b"height:clamp(277.2px,33vh,376.2px)", PAGE)
        self.assertIn(b".wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px); left:auto; right:0; transform:none; width:clamp(900px,64vw,1220px)", PAGE)
        self.assertIn(b"height:clamp(316.8px,38.28vh,415.8px)", PAGE)
        self.assertIn(b"const horizontalPadding=.25;", PAGE)
        self.assertIn(b"const verticalPadding=radarOpen?.25:.22;", PAGE)
        self.assertIn(b"size.x*(1-horizontalPadding*2)", PAGE)
        self.assertIn(b"size.y*(1-verticalPadding*2)", PAGE)

    def test_route_map_uses_soft_four_edge_feather(self):
        self.assertIn(b"opacity:.78", PAGE)
        self.assertIn(b"brightness(.62)", PAGE)
        self.assertIn(b"linear-gradient(to right,#000207", PAGE)
        self.assertIn(b"linear-gradient(to bottom,#02050a", PAGE)
        self.assertIn(b"rgba(2,5,10,0) 31%", PAGE)
        self.assertIn(b"border:0", PAGE)
        self.assertIn(b"box-shadow:none", PAGE)

    def test_route_flag_size_does_not_expand_when_radar_is_hidden(self):
        self.assertIn(b".callsign.route-known { font-size:clamp(64px,7.2vw,138px)", PAGE)
        self.assertIn(b"heading.classList.toggle('route-known',hasAircraft&&routeKnown)", PAGE)

    def test_desktop_radar_still_defaults_open_while_mobile_defaults_to_flight(self):
        self.assertIn(b"overhead-radar-v2", PAGE)
        self.assertIn(b"overhead-mobile-radar-v1", PAGE)
        self.assertIn(
            b"setRadar(isMobileViewport()?initialRadarPreference==='open':initialRadarPreference!=='closed');",
            PAGE,
        )

    def test_ui_revision_identifies_header_logo_map_layout(self):
        self.assertIn(b'overhead-ui-revision" content="mobile-footer-three-lines-v52"', PAGE)
        self.assertIn(b'<div class="brand"><img class="brand-logo" id="brand-logo" alt="Over-Head"></div>', PAGE)
        self.assertIn(b'class="route-brand-logo" id="operator-logo"', PAGE)
        self.assertNotIn(b'id="logo-box"', PAGE)

    def test_identity_line_never_wraps_and_shrinks_to_fit(self):
        self.assertIn(b".registration { display:block; width:100%; min-width:0; overflow:hidden; white-space:nowrap", PAGE)
        self.assertIn(b"function fitSingleLine(element,minSize=18)", PAGE)
        self.assertIn(b"while(element.scrollWidth>available&&size>minSize)", PAGE)
        self.assertIn(b"requestAnimationFrame(()=>fitSingleLine(byId('registration'),18))", PAGE)

    def test_radar_toggle_refits_identity_and_map(self):
        self.assertIn(b"fitSingleLine(byId('registration'),18); const logo=byId('operator-logo'); if(logo&&logo.complete&&logo.naturalWidth)fitLogoArtwork(logo); if(routeMap)routeMap.invalidateSize(false)", PAGE)

    def test_logo_visible_artwork_is_normalised_for_padded_assets(self):
        self.assertIn(b"fitLogoArtwork", PAGE)
        self.assertIn(b"getImageData", PAGE)
        self.assertIn(b"pixels[(y*canvas.width+x)*4+3]>12", PAGE)
        self.assertIn(b"Math.min(4.5", PAGE)

    def test_header_uses_actual_over_head_logo_asset(self):
        self.assertIn(b'class="brand-logo" id="brand-logo" alt="Over-Head"', PAGE)
        self.assertIn(b".brand-logo { display:block; visibility:hidden; max-width:100%; max-height:100%", PAGE)
        self.assertNotIn(b"OverHead Acherus Militant", PAGE)

    def test_main_panel_allows_airline_logo_to_cross_border(self):
        self.assertIn(b"main { position:relative; min-height:0; border:1px solid var(--line)", PAGE)
        self.assertIn(b"overflow:visible", PAGE)
        self.assertIn(b".route-brand { position:absolute", PAGE)
        self.assertIn(b"z-index:20", PAGE)

    def test_logo_store_prominent_branding_is_vector_only(self):
        class RasterOnlyLogoStore(LogoStore):
            def _soaring(self, code):
                return None

            def _simple_icons(self, airline_name):
                return None

            def _high_resolution(self, code):
                raise AssertionError("raster fallback must not be used")

            def _jxck(self, code):
                raise AssertionError("raster fallback must not be used")

        with tempfile.TemporaryDirectory() as tmp:
            store = RasterOnlyLogoStore(Path(tmp))
            self.assertIsNone(store.get("EZY", "easyJet"))

    def test_logo_store_does_not_use_symbol_only_fallback_when_wordmark_missing(self):
        class NoWordmarkLogoStore(LogoStore):
            def _soaring(self, code):
                return None

            def _simple_icons(self, airline_name):
                raise AssertionError("symbol-only Simple Icons fallback must not be used")

        with tempfile.TemporaryDirectory() as tmp:
            store = NoWordmarkLogoStore(Path(tmp))
            self.assertIsNone(store.get("AAL", "American Airlines"))

    def test_simple_icons_slug_normalises_airline_name(self):
        self.assertEqual(LogoStore._simple_icon_slug("easyJet"), "easyjet")
        self.assertEqual(LogoStore._simple_icon_slug("American Airlines"), "americanairlines")

    def test_prominent_logo_pipeline_requires_full_wordmark(self):
        import inspect
        get_source = inspect.getsource(LogoStore.get)
        soaring_source = inspect.getsource(LogoStore._soaring)
        self.assertNotIn("self._simple_icons(airline_name)", get_source)
        self.assertNotIn("self._high_resolution", get_source)
        self.assertNotIn("self._jxck", get_source)
        self.assertIn('filename="logo.svg"', soaring_source)
        self.assertNotIn('"icon.svg"', soaring_source)
        self.assertIn("prominent airline branding must be SVG", get_source)

    def test_unknown_airline_hides_branding_and_repairs_panel_outline(self):
        self.assertIn(b"id=\"route-brand\"", PAGE)
        self.assertIn(b"brandBox.style.display=airlineLabel?'flex':'none'", PAGE)
        self.assertIn(b"brandName.style.display=airlineLabel?'block':'none'", PAGE)
        self.assertNotIn(b"d.airline_name||d.operator_code||'Over-Head'", PAGE)

    def test_over_head_logo_asset_accepts_common_extensions_and_extensionless_svg(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
            (root / "Over-Head_Logo.svg").write_bytes(svg)
            self.assertEqual(overhead_logo_asset(root), (svg, "image/svg+xml"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
            (root / "Over-Head_Logo").write_bytes(svg)
            self.assertEqual(overhead_logo_asset(root), (svg, "image/svg+xml"))

    def test_map_keeps_horizontal_position_brightness_and_vertical_feathering(self):
        self.assertIn(b"top:clamp(90.882px,10.3275vh,99.144px)", PAGE)
        self.assertIn(b"left:calc(50% + clamp(18px,1.2vw,24px))", PAGE)
        self.assertIn(b"linear-gradient(to right,#000207", PAGE)
        self.assertIn(b"linear-gradient(to bottom,#02050a", PAGE)
        self.assertIn(b"opacity:.78", PAGE)
        self.assertIn(b"brightness(.62)", PAGE)

    def test_header_logo_greys_with_contrast_and_pulses_colour_with_bell(self):
        self.assertIn(b"transition:filter 900ms ease,opacity 900ms ease", PAGE)
        self.assertIn(b".wall.aircraft-active .brand-logo", PAGE)
        self.assertIn(b"grayscale(1) saturate(0) brightness(.82) contrast(1.62)", PAGE)
        self.assertIn(b".wall.aircraft-active.brand-pulse .brand-logo", PAGE)
        self.assertIn(b"grayscale(0) saturate(1.08) brightness(1.04) contrast(1.18)", PAGE)
        self.assertIn(b"function pulseBrandLogo()", PAGE)
        self.assertIn(b"brandPulseTimer=setTimeout(()=>wall.classList.remove('brand-pulse'),1500)", PAGE)
        self.assertIn(b"classList.toggle('aircraft-active',hasAircraft)", PAGE)
        self.assertIn(b"if(soundEnabled)pulseBrandLogo(); playTone();", PAGE)

    def test_route_map_progressively_brightens_toward_centre(self):
        self.assertIn(b".route-map::before", PAGE)
        self.assertIn(b"radial-gradient(ellipse at center", PAGE)
        self.assertIn(b"rgba(232,246,255,.28) 0%", PAGE)
        self.assertIn(b"rgba(108,154,172,.16) 28%", PAGE)
        self.assertIn(b"rgba(108,154,172,0) 70%", PAGE)
        self.assertIn(b"mix-blend-mode:screen", PAGE)

    def test_route_viewport_keeps_quarter_padding_with_radar_open(self):
        self.assertIn(b"const horizontalPadding=.25;", PAGE)
        self.assertIn(b"const verticalPadding=radarOpen?.25:.22;", PAGE)
        self.assertIn(b"size.x*(1-horizontalPadding*2)", PAGE)
        self.assertIn(b"size.y*(1-verticalPadding*2)", PAGE)
        self.assertIn(b"const centrePixel=L.point((minX+maxX)/2,(minY+maxY)/2)", PAGE)

    def test_branding_never_displays_operator_code_as_airline_name(self):
        self.assertIn(b"const airlineLabel=d.airline_name||''", PAGE)
        self.assertNotIn(b"d.airline_name||d.operator_code", PAGE)
        self.assertIn(b"logo.alt=(airlineLabel||'Airline')+' logo'", PAGE)

    def test_airline_logos_receive_consistent_brightness_lift(self):
        self.assertIn(b"brightness(1.35) saturate(1.22) contrast(1.06)", PAGE)

    def test_runtime_contributions_are_credited_in_footer(self):
        self.assertIn(b"attributionControl:false", PAGE)
        self.assertNotIn(b".route-map .leaflet-control-attribution", PAGE)
        for credit in (
            b"ADSB.lol", b"ADSBDB", b"OurAirports", b"Leaflet", b"OpenStreetMap",
            b"Soaring Symbols", b"country-flag-icons",
            b"arunangshubanerjee", b"Pixabay", b"TheFlightWall",
        ):
            self.assertIn(credit, PAGE)
        self.assertIn(b"https://www.openstreetmap.org/copyright", PAGE)

    def test_soaring_prominent_branding_requests_logo_not_icon(self):
        import inspect
        source = inspect.getsource(LogoStore._soaring)
        self.assertIn('filename="logo.svg"', source)
        self.assertNotIn('"icon.svg"', source)

    def test_initial_loading_state_says_scanning_the_skies(self):
        self.assertIn(b'class="wall no-aircraft loading" id="wall"', PAGE)
        self.assertIn(b'id="scan-message">Scanning the skies&hellip;</div>', PAGE)
        self.assertIn(b".wall.loading .scan-message,.wall.no-aircraft .scan-message { display:flex; }", PAGE)
        self.assertIn(b".wall.loading .information { visibility:hidden; }", PAGE)
        self.assertIn(b"byId('wall').classList.remove('loading');", PAGE)

    def test_route_map_is_under_other_panel_content(self):
        self.assertIn(b".route-visual { position:absolute", PAGE)
        self.assertIn(b"overflow:visible; z-index:0; pointer-events:none", PAGE)
        self.assertIn(b".flight-copy { position:relative; z-index:2", PAGE)
        self.assertIn(b".metrics { position:relative; z-index:2", PAGE)
        self.assertIn(b"pointer-events:auto; z-index:0", PAGE)

    def test_left_map_feather_extends_further_than_right(self):
        self.assertIn(b"rgba(0,2,7,.72) 12%", PAGE)
        self.assertIn(b"rgba(0,2,7,.30) 24%", PAGE)
        self.assertIn(b"rgba(0,2,7,0) 38%", PAGE)
        self.assertIn(b"rgba(2,5,10,0) 69%", PAGE)
        self.assertIn(b"rgba(2,5,10,.28) 81%", PAGE)

    def test_header_logo_is_preloaded_without_visible_placeholder(self):
        self.assertIn(b'class="brand-logo" id="brand-logo" alt="Over-Head"', PAGE)
        self.assertNotIn(b'class="brand-logo" src="/over-head-logo"', PAGE)
        self.assertIn(b".brand-logo { display:block; visibility:hidden", PAGE)
        self.assertIn(b".brand-logo.ready { visibility:visible; }", PAGE)
        self.assertIn(b"function loadHeaderLogo()", PAGE)
        self.assertIn(b"const preload=new Image()", PAGE)
        self.assertIn(b"preload.src='/over-head-logo'", PAGE)
        self.assertIn(b"target.src=preload.src", PAGE)
        self.assertIn(b"target.classList.add('ready')", PAGE)
        self.assertIn(b"loadHeaderLogo();", PAGE)


    def test_route_uses_live_aircraft_track_and_animated_direction_flow(self):
        self.assertIn(b"function routeAircraftIcon(track)", PAGE)
        self.assertIn(b"Number.isFinite(Number(track))?Number(track):0", PAGE)
        self.assertIn(b"className:'route-direction-flow'", PAGE)
        self.assertIn(b"flowPath.setAttribute('pathLength','100')", PAGE)
        self.assertIn(b"flowPath.setAttribute('stroke-dasharray','100 100')", PAGE)
        self.assertIn(b"routeMapLayers.push(flow)", PAGE)
        self.assertIn(b"zIndexOffset:1000", PAGE)



    def test_route_direction_is_shown_by_ten_second_brightness_sweep(self):
        self.assertIn(b".route-direction-flow { pointer-events:none; animation:route-flow 10s linear infinite", PAGE)
        self.assertIn(b"@keyframes route-flow { from { stroke-dashoffset:100; } to { stroke-dashoffset:0; } }", PAGE)
        self.assertNotIn(b"route-direction-arrow", PAGE)
        self.assertNotIn(b"addRouteDirectionArrows", PAGE)


    def test_map_uses_stronger_luminance_separation_for_land_and_water(self):
        self.assertIn(b"opacity:.78", PAGE)
        self.assertIn(b"brightness(.62)", PAGE)
        self.assertIn(b"contrast(1.62)", PAGE)

    def test_header_has_fullscreen_control_next_to_bell(self):
        self.assertIn(b'id="sound-toggle"', PAGE)
        self.assertIn(b'id="fullscreen-toggle"', PAGE)
        self.assertIn(b'aria-label="Enter full screen"', PAGE)
        self.assertIn(b"async function toggleFullscreen()", PAGE)
        self.assertIn(b"document.addEventListener('fullscreenchange',updateFullscreenButton)", PAGE)

    def test_settings_cog_replaces_radar_panel(self):
        self.assertIn(b'id="settings-toggle"', PAGE)
        self.assertIn(b'class="settings-panel"', PAGE)
        self.assertIn(b".wall.settings-open .radar-panel { display:none; }", PAGE)
        self.assertIn(b".wall.settings-open .settings-panel { display:grid; }", PAGE)
        self.assertIn(b".wall.radar-open .content,.wall.settings-open .content", PAGE)
        self.assertIn(b"function setSettings(open)", PAGE)

    def test_settings_panel_controls_existing_display_settings(self):
        self.assertIn(b'id="settings-sound"', PAGE)
        self.assertIn(b'id="settings-location"', PAGE)
        self.assertIn(b'id="settings-fullscreen"', PAGE)
        self.assertNotIn(b'id="settings-radar-range"', PAGE)
        self.assertIn(b"byId('settings-sound').addEventListener('click',toggleSound)", PAGE)
        self.assertIn(b"byId('settings-fullscreen').addEventListener('click',toggleFullscreen)", PAGE)
        self.assertIn(b"byId('settings-location').addEventListener('click',openLocationSelector)", PAGE)

    def test_route_brightness_sweep_reaches_full_destination(self):
        self.assertIn(b"stroke-dasharray','100 100'", PAGE)
        self.assertIn(b"stroke-dashoffset','100'", PAGE)
        self.assertIn(b"from { stroke-dashoffset:100; } to { stroke-dashoffset:0; }", PAGE)

    def test_active_over_head_logo_greys_with_map_like_contrast(self):
        self.assertIn(b".wall.aircraft-active .brand-logo { filter:grayscale(1) saturate(0) brightness(.82) contrast(1.62)", PAGE)
        self.assertIn(b".wall.aircraft-active.brand-pulse .brand-logo { filter:grayscale(0)", PAGE)

    def test_current_location_selector_uses_native_high_accuracy_geolocation_and_osm(self):
        self.assertIn(b"CURRENT LOCATION", PAGE)
        self.assertIn(b"SELECT LOCATION", PAGE)
        self.assertIn(b"navigator.geolocation.getCurrentPosition", PAGE)
        self.assertIn(b"enableHighAccuracy:true", PAGE)
        self.assertIn(b"id=\"location-map\"", PAGE)
        self.assertIn(b"https://tile.openstreetmap.org/{z}/{x}/{y}.png", PAGE)
        self.assertIn(b"Reported accuracy is approximately", PAGE)

    def test_live_location_update_preserves_other_config_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "latitude": 1.0,
                "longitude": 2.0,
                "radius_nm": 40,
                "refresh_seconds": 7,
                "custom_future_setting": "keep-me",
            }), encoding="utf-8")
            settings = Settings(latitude=1.0, longitude=2.0, radius_nm=40, refresh_seconds=7)
            latitude, longitude = update_live_location_config(path, settings, 51.5, -0.12)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((latitude, longitude), (51.5, -0.12))
            self.assertEqual((settings.latitude, settings.longitude), (51.5, -0.12))
            self.assertEqual(saved["radius_nm"], 40)
            self.assertEqual(saved["refresh_seconds"], 7)
            self.assertEqual(saved["custom_future_setting"], "keep-me")

    def test_footer_uses_space_for_credits_not_contact_count(self):
        self.assertIn(b".footer-credits { min-width:0; flex:1 1 auto; }", PAGE)
        self.assertIn(b"byId('footer-status').textContent='Updated '+d.updated", PAGE)
        self.assertNotIn(b"d.total+' CONTACTS  /  UPDATED '", PAGE)

    def test_route_direction_animation_survives_status_refreshes(self):
        self.assertIn(b"let routeFlowKey='',routeFlowStartedAt=Date.now()", PAGE)
        self.assertIn(b"const nextFlowKey=String(d.aircraft_id||'')+'|'+String(d.origin||'')+'|'+String(d.destination||'')", PAGE)
        self.assertIn(b"if(nextFlowKey!==routeFlowKey){ routeFlowKey=nextFlowKey; routeFlowStartedAt=Date.now(); }", PAGE)
        self.assertIn(b"const elapsed=(Date.now()-routeFlowStartedAt)%10000", PAGE)
        self.assertIn(b"flowPath.style.animationDelay='-'+elapsed+'ms'", PAGE)
        self.assertIn(b"clearRouteMap(); routeFlowKey=''; return;", PAGE)

    def test_location_selector_initialises_leaflet_with_valid_view(self):
        self.assertIn(b"function initialiseLocationMap(latitude,longitude)", PAGE)
        self.assertIn(b".setView([lat,lon],12)", PAGE)
        self.assertIn(b"const currentZoom=locationMap.getZoom()", PAGE)
        self.assertIn(b"const zoom=Number.isFinite(currentZoom)?Math.max(currentZoom,13):13", PAGE)
        self.assertNotIn(b"Math.max(locationMap.getZoom(),13)", PAGE)

    def test_location_selector_fetches_saved_home_when_needed(self):
        self.assertIn(b"async function openLocationSelector()", PAGE)
        self.assertIn(b"const response=await fetch('/status?t='+Date.now(),{cache:'no-store'})", PAGE)
        self.assertIn(b"lat=Number(status.home_latitude); lon=Number(status.home_longitude)", PAGE)
        self.assertNotIn(b"51.5074", PAGE)
        self.assertNotIn(b"-0.1278", PAGE)

    def test_location_save_refreshes_ui_immediately(self):
        self.assertIn(b"closeLocationSelector();\n        update();", PAGE)

    def test_location_selector_fits_viewport_without_scrolling(self):
        self.assertIn(b".location-backdrop { display:none; position:fixed; inset:0;", PAGE)
        self.assertIn(b"overflow:hidden; background:rgba(0,2,7,.84)", PAGE)
        self.assertIn(b".location-dialog { width:min(1060px,96vw); height:min(94vh,820px); min-height:0; overflow:hidden;", PAGE)
        self.assertIn(b"display:grid; grid-template-rows:auto minmax(220px,1fr) auto auto auto", PAGE)
        self.assertIn(b"#location-map { width:100%; height:100%; min-height:0;", PAGE)
        self.assertNotIn(b".location-dialog { width:min(920px,94vw); max-height:92vh; overflow:auto", PAGE)

    def test_location_selector_compacts_on_short_screens(self):
        self.assertIn(b"@media (max-height:700px)", PAGE)
        self.assertIn(b"height:calc(100vh - 16px)", PAGE)
        self.assertIn(b"grid-template-rows:auto minmax(170px,1fr) auto auto auto", PAGE)

    def test_no_aircraft_uses_scanning_message_not_giant_hyphen(self):
        self.assertIn(b'id="scan-message">Scanning the skies&hellip;</div>', PAGE)
        self.assertIn(b".wall.loading .scan-message,.wall.no-aircraft .scan-message { display:flex; }", PAGE)
        self.assertIn(b"if(!hasAircraft){ heading.textContent=''; return; }", PAGE)
        self.assertNotIn(b"if(!hasAircraft){ heading.textContent='-'; return; }", PAGE)
        self.assertNotIn(b".wall.no-aircraft .callsign { text-align:center; font-size:clamp(100px,15vw,260px)", PAGE)

    def test_footer_credits_flightaware_conditionally_and_ourairports(self):
        self.assertIn(b'id="flightaware-credit" hidden', PAGE)
        self.assertIn(b"FlightAware", PAGE)
        self.assertIn(b"https://www.flightaware.com/commercial/aeroapi/", PAGE)
        self.assertIn(b">OurAirports<", PAGE)
        self.assertIn(b"https://ourairports.com/data/", PAGE)

    def test_status_payload_contains_route_source_and_commercial_flight(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('"route_source": route.get("route_source", "")', source)
        self.assertIn('"commercial_flight": route.get("commercial_flight", "")', source)

    def test_flightaware_api_key_never_appears_in_page(self):
        self.assertNotIn(b"FLIGHTAWARE_AEROAPI_KEY", PAGE)
        self.assertNotIn(b"flightaware_api_key", PAGE)

    def test_radar_closed_map_moves_up_fifteen_percent_only(self):
        self.assertIn(b".route-map { display:none; position:absolute; top:clamp(90.882px,10.3275vh,99.144px)", PAGE)
        self.assertIn(b".wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px)", PAGE)
        self.assertIn(b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px)", PAGE)

    def test_unknown_route_is_always_regular_weight(self):
        self.assertIn(b".callsign.route-unknown { color:var(--muted); font-weight:400; letter-spacing:0; }", PAGE)
        self.assertIn(b"N/A to N/A", PAGE)

    def test_altitude_uses_red_amber_green_bands(self):
        self.assertIn(b"--green:#54d889", PAGE)
        self.assertIn(b".metric-value.altitude-low { color:var(--red) !important; }", PAGE)
        self.assertIn(b".metric-value.altitude-mid { color:var(--amber) !important; }", PAGE)
        self.assertIn(b".metric-value.altitude-high { color:var(--green) !important; }", PAGE)
        self.assertIn(b"function renderAltitude(value)", PAGE)
        self.assertIn(b"const numeric=Number(value);", PAGE)
        self.assertIn(b"numeric<5000?'altitude-low':numeric<15000?'altitude-mid':'altitude-high'", PAGE)
        self.assertIn(b"renderAltitude(d.altitude)", PAGE)

    def test_left_map_feather_blends_into_panel_without_vertical_seam(self):
        self.assertIn(b"linear-gradient(to right,#000207 0%,rgba(0,2,7,.96) 3%", PAGE)
        self.assertIn(b".route-map.leaflet-container { background:transparent; outline:0; border:0; box-shadow:none; }", PAGE)
        self.assertIn(b"rgba(2,5,10,0) 69%", PAGE)

    def test_footer_uses_normal_capitalization(self):
        self.assertIn(b"Data: ", PAGE)
        self.assertIn(b"Map: ", PAGE)
        self.assertIn(b"Inspired by: ", PAGE)
        self.assertIn(b">FlightAware<", PAGE)
        self.assertIn(b">OpenStreetMap<", PAGE)
        self.assertIn(b">TheFlightWall<", PAGE)
        self.assertIn(b'For Sam <span class="api-heart api-unhealthy" id="api-heart"', PAGE)
        self.assertIn(b">Connecting<", PAGE)
        self.assertIn(b"'Updated '+d.updated", PAGE)

    def test_settings_restores_radar_state_on_close(self):
        self.assertIn(b"let radarWasOpenBeforeSettings=false", PAGE)
        self.assertIn(b"radarWasOpenBeforeSettings=wall.classList.contains('radar-open')", PAGE)
        self.assertIn(b"wall.classList.toggle('radar-open',radarWasOpenBeforeSettings)", PAGE)
        self.assertIn(b"localStorage.setItem(radarPreferenceKey(),radarWasOpenBeforeSettings?'open':'closed')", PAGE)

    def test_escape_closes_settings_and_restores_previous_panel(self):
        self.assertIn(b"else if(byId('wall').classList.contains('settings-open'))setSettings(false)", PAGE)

    def test_use_my_location_has_immediate_animated_busy_state(self):
        self.assertIn(b"#location-use-device.locating", PAGE)
        self.assertIn(b"animation:location-button-pulse 1.6s ease-in-out infinite", PAGE)
        self.assertNotIn(b"location-button-spin", PAGE)
        self.assertIn(b"function setLocationButtonBusy(busy)", PAGE)
        self.assertIn(b"button.textContent=busy?'LOCATING...':'USE MY LOCATION'", PAGE)
        self.assertIn(b"button.setAttribute('aria-busy',String(busy))", PAGE)
        self.assertIn(b"setLocationButtonBusy(true);", PAGE)

    def test_location_button_busy_state_clears_on_success_failure_and_close(self):
        self.assertIn(b"position=>{\n          setLocationButtonBusy(false);", PAGE)
        self.assertIn(b"error=>{\n          setLocationButtonBusy(false);", PAGE)
        self.assertIn(b"function closeLocationSelector(){\n      setLocationButtonBusy(false);\n      byId('location-backdrop').classList.remove('open');\n      setRadar(true);", PAGE)

    def test_wrong_crl_to_opo_route_is_rejected_for_aircraft_over_wales(self):
        route = {
            "origin": "CRL", "destination": "OPO",
            "origin_latitude": 50.4592, "origin_longitude": 4.4538,
            "destination_latitude": 41.2481, "destination_longitude": -8.6814,
        }
        plane = {"lat": 51.690057, "lon": -3.358753}
        accepted, detail = FlightRouteStore._plausibility(route, plane)
        self.assertFalse(accepted)
        self.assertGreater(detail["route_excess_nm"], detail["allowed_excess_nm"])

    def test_real_tfs_to_ncl_route_is_accepted_for_same_aircraft_position(self):
        route = {
            "origin": "TFS", "destination": "NCL",
            "origin_latitude": 28.0445, "origin_longitude": -16.5725,
            "destination_latitude": 55.0375, "destination_longitude": -1.6917,
        }
        plane = {"lat": 51.690057, "lon": -3.358753}
        accepted, detail = FlightRouteStore._plausibility(route, plane)
        self.assertTrue(accepted)
        self.assertLess(detail["route_excess_nm"], detail["allowed_excess_nm"])

    def test_adsblol_plausible_route_normalises_tfs_to_ncl(self):
        class StubAirports:
            def get(self, code):
                return {}
        store = FlightRouteStore(Path(tempfile.gettempdir()) / "unused-route-test.json", airports=StubAirports())
        raw = {
            "callsign": "RYR6TQ",
            "number": "417",
            "airline_code": "RYR",
            "airport_codes": "GCTS-EGNT",
            "_airport_codes_iata": "TFS-NCL",
            "plausible": True,
            "_airports": [
                {"name": "Tenerife South", "icao": "GCTS", "iata": "TFS", "countryiso2": "ES", "lat": 28.0445, "lon": -16.5725},
                {"name": "Newcastle", "icao": "EGNT", "iata": "NCL", "countryiso2": "GB", "lat": 55.0375, "lon": -1.6917},
            ],
        }
        route = store._normalise_adsblol(raw)
        self.assertEqual(route["route"], "TFS to NCL")
        self.assertEqual(route["origin_country"], "ES")
        self.assertEqual(route["destination_country"], "GB")
        self.assertEqual(route["route_source"], "ADSB.lol route + live validation")

    def test_adsblol_provider_plausible_flag_does_not_override_our_validator(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FlightRouteStore(Path(tmp) / "routes.json")
            raw = {
                "callsign": "BAW703",
                "airport_codes": "LTFE-EGLL",
                "_airport_codes_iata": "BJV-LHR",
                "plausible": False,
                "_airports": [
                    {"icao": "LTFE", "iata": "BJV", "countryiso2": "TR", "lat": 37.2506, "lon": 27.6643},
                    {"icao": "EGLL", "iata": "LHR", "countryiso2": "GB", "lat": 51.4700, "lon": -0.4543},
                ],
            }
            route = store._normalise_adsblol(raw)
            self.assertEqual(route["route"], "BJV to LHR")
            self.assertFalse(route["provider_plausible"])
            accepted, detail = store._plausibility(
                route,
                {"lat": 51.35, "lon": -1.15, "alt_baro": 5600},
            )
            self.assertTrue(accepted, detail)

    def test_route_resolver_uses_adsblol_not_adsbdb_for_fallback(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('ADSBIM_ROUTESET_URL = "https://adsb.im/api/0/routeset"', source)
        self.assertIn("ADSLOL_ROUTESET_URL", source)
        self.assertIn("https://api.adsb.lol/api/0/routeset", source)
        self.assertIn("ADSLOL_ROUTE_URL", source)
        self.assertIn("/api/0/route/{callsign}/{latitude}/{longitude}", source)
        self.assertNotIn("https://api.adsbdb.com/v0/callsign/", source)

    def test_route_debug_endpoint_exposes_decision_without_api_key(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('elif path == "/route-debug":', source)
        self.assertIn('"flightaware_configured": bool(self.api_key)', source)
        self.assertIn('"attempts": []', source)
        self.assertNotIn('"x-apikey": self.api_key,\\n                    "debug"', source)

    def test_route_cache_version_is_bumped_and_short_lived(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('cache/flight-routes-v6.json', source)
        self.assertIn('ROUTE_CACHE_TTL_SECONDS = 900', source)
        self.assertIn('ROUTE_FAILURE_RETRY_SECONDS = 30', source)

    def test_locating_button_uses_gentle_pulse_only(self):
        self.assertIn(b"#location-use-device.locating { color:var(--white)", PAGE)
        self.assertIn(b"animation:location-button-pulse 1.6s ease-in-out infinite", PAGE)
        self.assertIn(b"button.textContent=busy?'LOCATING...':'USE MY LOCATION'", PAGE)
        self.assertNotIn(b"#location-use-device.locating::after", PAGE)
        self.assertNotIn(b"location-button-spin", PAGE)

    def test_browser_title_updates_from_live_route_and_callsign(self):
        self.assertIn(b"function updateBrowserTitle(d)", PAGE)
        self.assertIn(b"document.title=prefix+' '+origin+' to '+destination", PAGE)
        self.assertIn(b"updateBrowserTitle(d);", PAGE)

    def test_browser_title_fallbacks_are_explicit(self):
        self.assertIn(b"if(!hasAircraft){ document.title=prefix; return; }", PAGE)
        self.assertIn(b"const origin=String(d.origin||'N/A').trim().toUpperCase()||'N/A'", PAGE)
        self.assertIn(b"const destination=String(d.destination||'N/A').trim().toUpperCase()||'N/A'", PAGE)

    def test_every_location_selector_exit_returns_to_radar(self):
        self.assertIn(
            b"function closeLocationSelector(){\n      setLocationButtonBusy(false);\n      byId('location-backdrop').classList.remove('open');\n      setRadar(true);",
            PAGE,
        )
        self.assertIn(b"byId('location-close').addEventListener('click',closeLocationSelector)", PAGE)
        self.assertIn(b"byId('location-cancel').addEventListener('click',closeLocationSelector)", PAGE)
        self.assertIn(b"if(event.target===byId('location-backdrop'))closeLocationSelector()", PAGE)
        self.assertIn(b"if(byId('location-backdrop').classList.contains('open'))closeLocationSelector()", PAGE)

    def test_saving_location_returns_to_radar_via_close(self):
        self.assertIn(b"closeLocationSelector();\n        update();", PAGE)
        self.assertIn(b"setRadar(true);", PAGE)

    def test_radar_closed_moves_entire_header_control_cluster_up(self):
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-38px,-3.55vh,-27px)); }",
            PAGE,
        )
        self.assertIn(
            b".header-tools { display:flex; align-items:center; gap:clamp(14px,1.5vw,28px); transition:transform 220ms ease; }",
            PAGE,
        )

    def test_radar_open_and_settings_header_position_remain_unchanged(self):
        self.assertNotIn(b".wall.radar-open .header-tools { transform:", PAGE)
        self.assertNotIn(b".wall.settings-open .header-tools { transform:", PAGE)

    def test_altitude_semantic_colours_override_generic_metric_colour(self):
        self.assertIn(b".metric-value.altitude-low { color:var(--red) !important; }", PAGE)
        self.assertIn(b".metric-value.altitude-mid { color:var(--amber) !important; }", PAGE)
        self.assertIn(b".metric-value.altitude-high { color:var(--green) !important; }", PAGE)
        self.assertIn(b"const numeric=Number(value);", PAGE)
        self.assertIn(b"numeric<5000?'altitude-low':numeric<15000?'altitude-mid':'altitude-high'", PAGE)

    def test_closed_radar_map_is_wider_but_same_height(self):
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px); left:auto; right:0; transform:none; width:clamp(900px,64vw,1220px); height:clamp(316.8px,38.28vh,415.8px); border-radius:0; }",
            PAGE,
        )
        self.assertIn(
            b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }",
            PAGE,
        )

    def test_closed_radar_route_fit_reduces_vertical_dead_space_only(self):
        self.assertIn(b"const radarOpen=byId('wall').classList.contains('radar-open');", PAGE)
        self.assertIn(b"const horizontalPadding=.25;", PAGE)
        self.assertIn(b"const verticalPadding=radarOpen?.25:.22;", PAGE)
        self.assertIn(b"size.x*(1-horizontalPadding*2)", PAGE)
        self.assertIn(b"size.y*(1-verticalPadding*2)", PAGE)

    def test_map_left_separator_is_explicitly_covered(self):
        self.assertIn(b"linear-gradient(to right,#000207 0%,rgba(0,2,7,.96) 3%", PAGE)
        self.assertIn(b".route-map.leaflet-container { background:transparent; outline:0; border:0; box-shadow:none; }", PAGE)
        self.assertIn(b"border-radius:0;", PAGE)

    def test_radar_closed_header_moves_up_an_additional_ten_percent(self):
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-38px,-3.55vh,-27px)); }",
            PAGE,
        )
        self.assertNotIn(b".wall.radar-open .header-tools { transform:", PAGE)
        self.assertNotIn(b".wall.settings-open .header-tools { transform:", PAGE)

    def test_closed_map_has_no_manual_left_shift_or_visible_box_edge(self):
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px); left:auto; right:0; transform:none;",
            PAGE,
        )
        self.assertIn(b"border-radius:0;", PAGE)
        self.assertNotIn(b"left:calc(50% - clamp(70px,5.8vw,110px))", PAGE)
        self.assertNotIn(b"0 4px,rgba(2,5,10,0) 4px", PAGE)

    def test_radar_open_map_remains_unchanged_after_closed_map_fix(self):
        self.assertIn(
            b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }",
            PAGE,
        )

    def test_expansive_footer_and_sam_dedication(self):
        self.assertIn(b'For Sam <span class="api-heart api-unhealthy" id="api-heart"', PAGE)
        self.assertIn(b"Data: ", PAGE)
        self.assertIn(b">ADSB.lol<", PAGE)
        self.assertIn(b">ADSBDB<", PAGE)
        self.assertIn(b">OurAirports<", PAGE)
        self.assertIn(b"Map: ", PAGE)
        self.assertIn(b">Leaflet<", PAGE)
        self.assertIn(b">OpenStreetMap<", PAGE)
        self.assertIn(b"Logos: ", PAGE)
        self.assertIn(b">Soaring Symbols<", PAGE)
        self.assertIn(b"Flags: ", PAGE)
        self.assertIn(b">country-flag-icons<", PAGE)
        self.assertIn(b"Sound: arunangshubanerjee via ", PAGE)
        self.assertIn(b">Pixabay<", PAGE)
        self.assertIn(b"Inspired by: ", PAGE)
        self.assertIn(b">TheFlightWall<", PAGE)
        self.assertIn(b'id="flightaware-credit" hidden', PAGE)
        self.assertIn(b"byId('flightaware-credit').hidden=!Boolean(d.flightaware_configured)", PAGE)

    def test_for_sam_is_first_footer_credit(self):
        footer_start = PAGE.index(b'<footer><span class="footer-credits">')
        self.assertTrue(
            PAGE[footer_start:].startswith(
                b'<footer><span class="footer-credits"><span class="footer-credit-line footer-credit-line-1">For Sam <span class="api-heart'
            )
        )

    def test_for_sam_has_no_separator_after_heart(self):
        footer = PAGE[PAGE.index(b'<footer><span class="footer-credits">'):]
        heart_end = footer.index(b'</span>&nbsp;&nbsp;&nbsp;&nbsp;Data:')
        self.assertGreater(heart_end, 0)
        self.assertNotIn(b'</span>&nbsp;&nbsp;/&nbsp;&nbsp;Data:', footer)

    def test_for_sam_stays_first_with_expanded_credits(self):
        footer = PAGE[PAGE.index(b'<footer><span class="footer-credits">'):]
        self.assertTrue(footer.startswith(
            b'<footer><span class="footer-credits"><span class="footer-credit-line footer-credit-line-1">For Sam '
        ))
        self.assertIn(b'&nbsp;&nbsp;&nbsp;&nbsp;Data:', footer)
        self.assertNotIn(b'&nbsp;&nbsp;/&nbsp;&nbsp;Data:', footer)

    def test_browser_title_keeps_over_head_brand_and_plane_first(self):
        self.assertIn(b"<title>Over-Head &#x2708;&#xFE0F;</title>", PAGE)
        self.assertIn(b"const prefix='Over-Head '+String.fromCodePoint(0x2708,0xFE0F);", PAGE)
        self.assertIn(b"document.title=prefix+' '+origin+' to '+destination", PAGE)

    def test_browser_title_idle_state_is_over_head_plane(self):
        self.assertIn(b"if(!hasAircraft){ document.title=prefix; return; }", PAGE)

    def test_browser_title_does_not_include_callsign(self):
        self.assertIn(b"const hasAircraft=Boolean(d&&d.aircraft_id);", PAGE)
        self.assertIn(b"document.title=prefix+' '+origin+' to '+destination;", PAGE)
        self.assertNotIn(b"document.title=prefix+' '+origin+' to '+destination+' - '+callsign", PAGE)

    def test_browser_title_unknown_route_still_shows_na_to_na(self):
        self.assertIn(b"const origin=String(d.origin||'N/A').trim().toUpperCase()||'N/A'", PAGE)
        self.assertIn(b"const destination=String(d.destination||'N/A').trim().toUpperCase()||'N/A'", PAGE)

    def test_baw703_bodrum_to_heathrow_is_accepted_near_heathrow(self):
        route = {
            "origin": "BJV", "destination": "LHR",
            "origin_latitude": 37.2506, "origin_longitude": 27.6643,
            "destination_latitude": 51.4700, "destination_longitude": -0.4543,
        }
        plane = {"lat": 51.35, "lon": -1.15, "alt_baro": 5600}
        accepted, detail = FlightRouteStore._plausibility(route, plane)
        self.assertTrue(accepted, detail)
        self.assertLess(detail["nearest_endpoint_nm"], 120)

    def test_low_aircraft_far_from_both_endpoints_is_rejected_even_on_corridor(self):
        route = {
            "origin": "BJV", "destination": "LHR",
            "origin_latitude": 37.2506, "origin_longitude": 27.6643,
            "destination_latitude": 51.4700, "destination_longitude": -0.4543,
        }
        plane = {"lat": 44.4, "lon": 13.0, "alt_baro": 5600}
        accepted, detail = FlightRouteStore._plausibility(route, plane)
        self.assertFalse(accepted)
        self.assertEqual(detail["reason"], "low aircraft too far from route endpoints")

    def test_adsblol_routeset_posts_callsign_and_live_position(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('ADSLOL_ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"', source)
        self.assertIn('"planes": [{', source)
        self.assertIn('"callsign": callsign', source)
        self.assertIn('"lat": latitude', source)
        self.assertIn('"lng": longitude', source)
        self.assertIn('("adsb.im routeset", self._adsbim_routeset)', source)
        self.assertIn('("ADSB.lol routeset", self._adsblol_routeset)', source)

    def test_adsblol_provider_flag_is_diagnostic_not_authority(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertNotIn('if payload.get("plausible") is not True:', source)
        self.assertIn('"provider_plausible": payload.get("plausible")', source)
        self.assertIn("Accepted {source_name} after independent live-position validation", source)

    def test_radar_closed_header_has_more_clearance_above_airline_logo(self):
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-38px,-3.55vh,-27px)); }",
            PAGE,
        )
        self.assertNotIn(b".wall.radar-open .header-tools { transform:", PAGE)

    def test_adsbim_is_first_free_route_provider(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('ADSBIM_ROUTESET_URL = "https://adsb.im/api/0/routeset"', source)
        adsbim = source.index('("adsb.im routeset", self._adsbim_routeset)')
        adsblol = source.index('("ADSB.lol routeset", self._adsblol_routeset)')
        legacy = source.index('("ADSB.lol route", self._adsblol)')
        self.assertLess(adsbim, adsblol)
        self.assertLess(adsblol, legacy)

    def test_adsbim_routeset_uses_same_live_position_payload(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn("def _routeset(self, url: str, callsign:", source)
        self.assertIn("return self._routeset(ADSBIM_ROUTESET_URL, callsign, plane)", source)
        self.assertIn('"callsign": callsign', source)
        self.assertIn('"lat": latitude', source)
        self.assertIn('"lng": longitude', source)

    def test_baw703_adsbim_payload_normalises_to_bjv_lhr(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FlightRouteStore(Path(tmp) / "routes.json")
            raw = {
                "_airport_codes_iata": "BJV-LHR",
                "_airports": [
                    {"iata": "BJV", "icao": "LTFE", "countryiso2": "TR", "lat": 37.2506, "lon": 27.6643},
                    {"iata": "LHR", "icao": "EGLL", "countryiso2": "GB", "lat": 51.4700, "lon": -0.4543},
                ],
                "airline_code": "BAW",
                "callsign": "BAW703",
                "number": "703",
                "plausible": True,
            }
            route = store._normalise_routeset(raw, "adsb.im routeset")
            self.assertEqual(route["route"], "BJV to LHR")
            self.assertEqual(route["route_source"], "adsb.im routeset + live validation")
            accepted, detail = store._plausibility(
                route,
                {"lat": 51.35, "lon": -1.15, "alt_baro": 5600},
            )
            self.assertTrue(accepted, detail)

    def test_failed_free_route_lookup_retries_after_thirty_seconds(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn("ROUTE_FAILURE_RETRY_SECONDS = 30", source)

    def test_radar_replaces_home_centre_with_flight_tracking_controls(self):
        self.assertNotIn(b"HOME CENTRE", PAGE)
        self.assertIn(b'id="track-flight"', PAGE)
        self.assertIn(b'placeholder="BA703"', PAGE)
        self.assertIn(b'id="track-button"', PAGE)
        self.assertIn(b">TRACK</button>", PAGE)

    def test_track_button_pulses_slowly_while_tracking(self):
        self.assertIn(b".track-button.tracking { animation:track-button-pulse 2.6s ease-in-out infinite; }", PAGE)
        self.assertIn(b"button.classList.toggle('tracking',flightTrackingActive)", PAGE)

    def test_track_mode_disables_manual_radar_zoom_and_labels_auto_range(self):
        self.assertIn(b"(trackingActive?'AUTO ':'')+number(radius,0)+' NM'", PAGE)
        self.assertIn(b"byId('zoom-in').disabled=Boolean(trackingActive)||rangeIndex===0", PAGE)
        self.assertIn(b"if(flightTrackingActive)return;", PAGE)

    def test_iata_flight_number_maps_to_adsb_icao_callsign(self):
        store = LogoStore.__new__(LogoStore)
        store._index = {
            "BAW": {"name": "British Airways", "iata": "BA", "icao": "BAW", "slug": "british-airways"},
            "RYR": {"name": "Ryanair", "iata": "FR", "icao": "RYR", "slug": "ryanair"},
        }
        self.assertEqual(store.tracking_callsigns("BA703"), ("BAW703", "BA703"))
        self.assertEqual(store.tracking_callsigns("FR417"), ("RYR417", "FR417"))
        self.assertEqual(store.tracking_callsigns("BAW703"), ("BAW703",))

    def test_tracking_query_validation(self):
        self.assertEqual(normalise_flight_query(" ba 703 "), "BA703")
        self.assertEqual(normalise_flight_query("BAW-703"), "BAW703")
        with self.assertRaises(ValueError):
            normalise_flight_query("703")

    def test_auto_tracking_radius_expands_and_contracts_by_distance(self):
        self.assertEqual(automatic_tracking_radius(4), 5)
        self.assertEqual(automatic_tracking_radius(20), 25)
        self.assertEqual(automatic_tracking_radius(80), 100)
        self.assertEqual(automatic_tracking_radius(120), 150)
        self.assertEqual(automatic_tracking_radius(210), 250)
        self.assertEqual(automatic_tracking_radius(700), 1000)
        self.assertGreater(TRACK_RADAR_RANGES[-1], RADAR_RANGES[-1])

    def test_tracking_state_persists_until_explicit_stop(self):
        tracker = FlightTrackingState(25)
        tracker.start("BA703", ("BAW703", "BA703"))
        first = tracker.snapshot()
        self.assertTrue(first["active"])
        self.assertEqual(first["query"], "BA703")
        tracker.remember({"hex": "407abc", "flight": "BAW703"}, "BAW703")
        second = tracker.snapshot()
        self.assertEqual(second["resolved_callsign"], "BAW703")
        self.assertEqual(second["last_plane"]["flight"], "BAW703")
        restored = tracker.stop()
        self.assertEqual(restored, 25)
        self.assertFalse(tracker.snapshot()["active"])

    def test_explicit_track_overrides_nearest_aircraft_selection(self):
        class EmptyStore:
            def get(self, *args):
                return None

        nearest = {
            "hex": "near01", "flight": "NEAR1", "lat": 51.51, "lon": -0.13,
            "seen_pos": 0.1, "alt_baro": 12000, "gs": 220, "track": 90,
        }
        target = {
            "hex": "track1", "flight": "BAW703", "r": "G-TTNJ", "t": "A20N",
            "lat": 51.35, "lon": -1.15, "seen_pos": 0.1, "alt_baro": 5600,
            "gs": 226, "track": 80,
        }
        tracker = FlightTrackingState(25)
        tracker.start("BA703", ("BAW703", "BA703"))
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                latitude=51.5074, longitude=-0.1278, radius_nm=25,
                output_rgb=str(Path(tmp) / "frame.rgb"),
                output_png=str(Path(tmp) / "frame.png"),
            )
            state = FrameState()
            with patch("monitor.fetch_aircraft", return_value=[nearest]), patch(
                "monitor.fetch_tracked_aircraft", return_value=(target, "BAW703")
            ):
                update_state(state, settings, False, EmptyStore(), EmptyStore(), tracker=tracker)
            self.assertEqual(state.payload["callsign"], "BAW703")
            self.assertTrue(state.payload["tracking_active"])
            self.assertTrue(state.payload["tracking_found"])
            self.assertEqual(state.payload["tracking_query"], "BA703")
            self.assertEqual(state.payload["tracking_resolved_callsign"], "BAW703")
            self.assertNotEqual(state.payload["callsign"], "NEAR1")
            self.assertGreaterEqual(state.payload["radar_radius_nm"], state.payload["distance_nm"] * 1.18)

    def test_tracking_keeps_last_target_when_live_lookup_temporarily_misses(self):
        class EmptyStore:
            def get(self, *args):
                return None

        target = {
            "hex": "track1", "flight": "BAW703", "lat": 51.35, "lon": -1.15,
            "seen_pos": 0.1, "alt_baro": 5600, "gs": 226, "track": 80,
        }
        tracker = FlightTrackingState(25)
        tracker.start("BA703", ("BAW703", "BA703"))
        tracker.remember(target, "BAW703")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                latitude=51.5074, longitude=-0.1278, radius_nm=25,
                output_rgb=str(Path(tmp) / "frame.rgb"),
                output_png=str(Path(tmp) / "frame.png"),
            )
            state = FrameState()
            with patch("monitor.fetch_aircraft", return_value=[]), patch(
                "monitor.fetch_tracked_aircraft", return_value=(None, "")
            ):
                update_state(state, settings, False, EmptyStore(), EmptyStore(), tracker=tracker)
            self.assertEqual(state.payload["callsign"], "BAW703")
            self.assertTrue(state.payload["tracking_active"])
            self.assertTrue(state.payload["tracking_found"])

    def test_tracked_aircraft_is_merged_into_radar_even_outside_point_query(self):
        local = [{"hex": "near", "flight": "NEAR1"}]
        tracked = {"hex": "far", "flight": "BAW703"}
        merged = merge_tracked_aircraft(local, tracked)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[-1]["flight"], "BAW703")

    def test_point_query_is_capped_at_250_nm_during_long_distance_tracking(self):
        source = Path(__file__).with_name("overhead.py").read_text(encoding="utf-8")
        self.assertIn("query_radius = min(250.0, max(1.0, float(settings.radius_nm)))", source)
        self.assertIn('f"{settings.latitude}/{settings.longitude}/{query_radius:g}"', source)

    def test_track_api_is_backend_owned_and_nearest_selection_cannot_overwrite_it(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('if path == "/track":', source)
        self.assertIn("tracker.start(query, candidates)", source)
        self.assertIn("def do_DELETE(self) -> None:", source)
        self.assertIn("restored_radius = tracker.stop()", source)
        self.assertIn("if tracking.get(\"active\") and not demo:", source)
        self.assertIn("else:\n        plane, distance = select_nearest(aircraft, settings)", source)

    def test_tracking_input_updates_without_status_refresh_overwriting_user_typing(self):
        self.assertIn(b"if(flightTrackingActive&&document.activeElement!==input)input.value=flightTrackingQuery", PAGE)
        self.assertIn(b"byId('radar-track-form').addEventListener('submit',submitFlightTracking)", PAGE)

    def test_icao_callsign_is_not_misread_as_two_letter_iata_number(self):
        store = LogoStore.__new__(LogoStore)
        store._index = {
            "BAW": {"name": "British Airways", "iata": "BA", "icao": "BAW", "slug": "british-airways"},
        }
        self.assertEqual(store.tracking_callsigns("BAW703"), ("BAW703",))

    def test_mobile_layout_is_scoped_to_phone_viewports_only(self):
        self.assertIn(
            b"@media (max-width:700px), (max-width:950px) and (max-height:500px) {",
            PAGE,
        )
        self.assertIn(b"height:100dvh;", PAGE)
        self.assertIn(b"overflow:hidden;", PAGE)

    def test_mobile_radar_replaces_flight_panel_instead_of_sitting_beside_it(self):
        self.assertIn(b".wall.radar-open main,", PAGE)
        self.assertIn(b".wall.settings-open main { display:none; }", PAGE)
        self.assertIn(b".wall.radar-open .radar-panel,", PAGE)
        self.assertIn(b"width:100%;", PAGE)
        self.assertIn(b"grid-template-columns:minmax(0,1fr);", PAGE)

    def test_mobile_main_view_compacts_route_map_and_metrics_without_scrolling(self):
        self.assertIn(b"grid-template-rows:minmax(0,1fr) auto;", PAGE)
        self.assertIn(b"grid-template-columns:repeat(2,minmax(0,1fr));", PAGE)
        self.assertIn(b"height:calc(100% - 31px);", PAGE)
        self.assertIn(b".footer-credits {", PAGE)
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertNotIn(b"text-overflow:ellipsis;", mobile.split(b".location-backdrop", 1)[0])

    def test_mobile_radar_toggle_is_flight_switch_and_has_separate_preference(self):
        self.assertIn(
            b"const radarPreferenceKey=()=>isMobileViewport()?'overhead-mobile-radar-v1':'overhead-radar-v2';",
            PAGE,
        )
        self.assertIn(
            b"radarButton.textContent=radarOpen&&!settingsOpen?(isMobileViewport()?'FLIGHT':'HIDE RADAR'):'RADAR';",
            PAGE,
        )
        self.assertIn(
            b"setRadar(isMobileViewport()?initialRadarPreference==='open':initialRadarPreference!=='closed');",
            PAGE,
        )

    def test_mobile_track_returns_to_flight_view_without_cancelling_track(self):
        self.assertIn(b"if(isMobileViewport()&&data.tracking)setRadar(false);", PAGE)
        self.assertIn(b"tracker.start(query, candidates)", Path(__file__).with_name("monitor.py").read_bytes())

    def test_desktop_radar_geometry_rules_are_still_present_unchanged(self):
        self.assertIn(
            b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }",
            PAGE,
        )
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px); left:auto; right:0; transform:none; width:clamp(900px,64vw,1220px); height:clamp(316.8px,38.28vh,415.8px); border-radius:0; }",
            PAGE,
        )
        self.assertIn(
            b".wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-38px,-3.55vh,-27px)); }",
            PAGE,
        )

    def test_mobile_flight_information_fills_entire_main_panel(self):
        self.assertIn(b"main {\n        position:relative;", PAGE)
        self.assertIn(b"width:100%;\n        height:100%;", PAGE)
        self.assertIn(b".information {\n        position:absolute;\n        inset:0;", PAGE)
        self.assertIn(b"width:100%;\n        height:100%;", PAGE)

    def test_mobile_route_and_metrics_explicitly_use_full_width(self):
        self.assertIn(b".flight-heading {\n        width:100%;", PAGE)
        self.assertIn(b"width:100%;\n        max-width:none;", PAGE)
        self.assertIn(
            b".wall.settings-open .metrics {\n        width:100%;",
            PAGE,
        )
        self.assertIn(b".metric { width:100%; min-width:0; padding-top:5px; }", PAGE)

    def test_desktop_layout_remains_outside_mobile_fill_fix(self):
        # Original desktop rules are still byte-for-byte present.
        self.assertIn(
            b"main { position:relative; min-height:0; border:1px solid var(--line); border-radius:clamp(16px,2vw,32px); background:rgba(0,2,7,.72); display:grid; grid-template-columns:1fr; overflow:visible;",
            PAGE,
        )
        self.assertIn(
            b".information { min-width:0; padding:clamp(26px,4vw,76px); display:flex; flex-direction:column; justify-content:space-between; }",
            PAGE,
        )

    def test_mobile_information_grid_has_explicit_full_width_column(self):
        self.assertIn(
            b".information {\n        position:absolute;\n        inset:0;",
            PAGE,
        )
        self.assertIn(
            b"display:grid;\n        grid-template-columns:minmax(0,1fr);\n        grid-template-rows:minmax(0,1fr) auto;",
            PAGE,
        )

    def test_mobile_footer_uses_three_explicit_credit_lines_without_truncation(self):
        self.assertEqual(PAGE.count(b'class="footer-credit-line footer-credit-line-'), 3)
        self.assertIn(b"grid-template-rows:34px minmax(0,1fr) 34px;", PAGE)
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertIn(b"grid-template-rows:repeat(3,minmax(0,1fr));", mobile)
        self.assertIn(b"font-size:clamp(6.2px,1.85vw,7.5px);", mobile)
        self.assertIn(b".footer-credit-line-1 { grid-column:1 / -1; grid-row:1; }", mobile)
        self.assertIn(b".footer-credit-line-2 { grid-column:1 / -1; grid-row:2; }", mobile)
        self.assertIn(b".footer-credit-line-3 { grid-column:1; grid-row:3; }", mobile)
        self.assertIn(b"overflow:visible;", mobile)
        self.assertNotIn(b"text-overflow:ellipsis;", mobile.split(b".location-backdrop", 1)[0])

    def test_footer_heart_has_health_states_and_pulse_animation(self):
        self.assertIn(b'id="api-heart"', PAGE)
        self.assertIn(b".api-heart.api-unhealthy", PAGE)
        self.assertIn(b".api-heart.api-pulse { animation:api-heart-pulse 1.45s ease-out 1; }", PAGE)
        self.assertIn(b"@keyframes api-heart-pulse", PAGE)
        self.assertIn(b"function syncApiHeartbeat(d)", PAGE)
        self.assertIn(b"if(healthy){", PAGE)

    def test_api_health_monitor_checks_four_public_services(self):
        monitor = APIHealthMonitor(Settings(latitude=51.5, longitude=-3.3))
        with patch.object(monitor, "_probe_adsblol", return_value=True), \
             patch.object(monitor, "_probe_adsbim", return_value=True), \
             patch.object(monitor, "_probe_adsbdb", return_value=True), \
             patch.object(monitor, "_probe_ourairports", return_value=True):
            snapshot = monitor.check()
        self.assertTrue(snapshot["ok"])
        self.assertEqual(
            snapshot["services"],
            {"ADSB.lol": True, "adsb.im": True, "ADSBDB": True, "OurAirports": True},
        )

    def test_api_health_goes_unhealthy_if_any_core_service_fails(self):
        monitor = APIHealthMonitor(Settings(latitude=51.5, longitude=-3.3))
        with patch.object(monitor, "_probe_adsblol", return_value=True), \
             patch.object(monitor, "_probe_adsbim", return_value=False), \
             patch.object(monitor, "_probe_adsbdb", return_value=True), \
             patch.object(monitor, "_probe_ourairports", return_value=True):
            snapshot = monitor.check()
        self.assertFalse(snapshot["ok"])
        self.assertFalse(snapshot["services"]["adsb.im"])

    def test_api_health_interval_is_thirty_seconds_and_flightaware_is_not_synthetically_probed(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn("API_HEALTH_INTERVAL_SECONDS = 30", source)
        health_class = source.split("class APIHealthMonitor:", 1)[1].split("class LogoStore:", 1)[0]
        self.assertNotIn("FLIGHTAWARE_AEROAPI_URL", health_class)
        self.assertIn("time.sleep(API_HEALTH_INTERVAL_SECONDS)", source)

    def test_status_payload_exposes_api_health_to_browser(self):
        source = Path(__file__).with_name("monitor.py").read_text(encoding="utf-8")
        self.assertIn('"api_health_ok": bool(api_health.get("ok"))', source)
        self.assertIn('"api_health_checked_at": int(api_health.get("checked_at") or 0)', source)
        self.assertIn('"api_health_services": dict(api_health.get("services") or {})', source)

    def test_readme_documents_mobile_single_view_and_api_heartbeat(self):
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("## Mobile view", readme)
        self.assertIn("single non-scrolling screen", readme)
        self.assertIn("Radar", readme)
        self.assertIn("core API heartbeat", readme)
        self.assertIn("Every 30 seconds", readme)
        self.assertIn("FlightAware AeroAPI is deliberately not synthetically probed", readme)

    def test_mobile_identity_is_large_centered_and_prominent(self):
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertIn(b".flight-copy {", mobile)
        self.assertIn(b"align-items:center;", mobile)
        self.assertIn(b"text-align:center;", mobile)
        self.assertIn(b"justify-content:center;", mobile)
        self.assertIn(b"font-size:clamp(45px,14.4vw,62px);", mobile)
        self.assertIn(b".callsign.route-known { font-size:clamp(40px,13.2vw,57px); }", mobile)
        self.assertIn(b"font-size:clamp(17px,5.3vw,23px);", mobile)
        self.assertIn(b"font-size:clamp(14px,4.35vw,19px);", mobile)

    def test_mobile_airline_logo_and_map_are_reduced_to_make_identity_room(self):
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertIn(b"height:31px;", mobile)
        self.assertIn(b".route-brand-logo { max-height:31px; }", mobile)
        self.assertIn(b"left:5%;", mobile)
        self.assertIn(b"right:5%;", mobile)
        self.assertIn(b"width:90%;", mobile)
        self.assertIn(b"top:31px;", mobile)

    def test_mobile_flight_heading_reserves_space_for_identity(self):
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertIn(b"grid-template-rows:minmax(118px,30%) minmax(0,1fr);", mobile)

    def test_desktop_airline_brand_and_map_sizes_remain_unchanged(self):
        self.assertIn(
            b".route-brand { position:absolute; top:0; left:50%; transform:translateX(-50%); width:100%; height:clamp(92px,10.5vh,108px);",
            PAGE,
        )
        self.assertIn(
            b".wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }",
            PAGE,
        )

    def test_mobile_footer_balances_credit_groups_across_three_rows(self):
        footer = PAGE[PAGE.index(b"<footer>"):PAGE.index(b"</footer>") + len(b"</footer>")]
        self.assertIn(b'footer-credit-line-1">For Sam ', footer)
        self.assertIn(b'Data: ', footer)
        self.assertIn(b'footer-credit-line-2">Map: ', footer)
        self.assertIn(b'Logos: ', footer)
        self.assertIn(b'Flags: ', footer)
        self.assertIn(b'footer-credit-line-3">Sound: ', footer)
        self.assertIn(b'Inspired by: ', footer)

    def test_mobile_update_timestamp_shares_third_footer_row(self):
        mobile = PAGE.split(b"/* Mobile is a single-view application", 1)[1]
        self.assertIn(b"#footer-status {", mobile)
        self.assertIn(b"grid-column:2;", mobile)
        self.assertIn(b"grid-row:3;", mobile)

    def test_readme_documents_three_line_mobile_footer(self):
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("three compact acknowledgement", readme)


if __name__ == "__main__":
    unittest.main()
