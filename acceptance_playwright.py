#!/usr/bin/env python3
"""Offline desktop/mobile browser acceptance checks for Over-Head.

Run with ``python3 acceptance_playwright.py`` after installing the requirements
and Playwright Chromium system dependencies. No public aircraft or map service
is contacted; provider responses and Leaflet are local fixtures.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import patch

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

import monitor
from overhead import AircraftFeedRateLimited, Settings, load_settings


LEAFLET_SHIM = r"""
(() => {
    const maps = [];
  class Map {
        constructor() { this.zoom = 12; this.layers = new Set(); this.handlers = {}; }
    setView(_point, zoom) { if (Number.isFinite(zoom)) this.zoom = zoom; return this; }
    getZoom() { return this.zoom; }
    invalidateSize() { return this; }
    on(name, handler) { (this.handlers[name] ||= []).push(handler); return this; }
    fire(name, event) { for (const handler of this.handlers[name] || []) handler(event); return this; }
    addLayer(layer) { this.layers.add(layer); return this; }
    removeLayer(layer) { this.layers.delete(layer); return this; }
    hasLayer(layer) { return this.layers.has(layer); }
  }
  class Layer {
    constructor(point) { this.point = point; this.map = null; }
    addTo(map) { this.map = map; map.addLayer(this); return this; }
    on() { return this; }
    remove() { if (this.map) this.map.removeLayer(this); return this; }
    setLatLng(point) { this.point = point; return this; }
    getLatLng() { return {lat: this.point[0], lng: this.point[1]}; }
  }
  window.L = {
    map: () => { const map = new Map(); maps.push(map); return map; },
    tileLayer: () => new Layer([0, 0]),
    marker: point => new Layer(point),
    circle: point => new Layer(point),
    latLng: (lat, lng) => ({lat, lng}),
    point: (x, y) => ({x, y})
  };
    window.__overheadMaps = maps;
})();
"""


class EmptyStore:
    def get(self, *_args: Any) -> dict[str, Any]:
        return {}


class EmptyLogos(EmptyStore):
    def get(self, *_args: Any) -> None:
        return None

    def tracking_callsigns(self, callsign: str) -> tuple[str, ...]:
        return (callsign,)


class EmptyRoutes(EmptyStore):
    api_key = ""


class ProviderFixture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "live"
        self.requests: list[tuple[float, float, float | None]] = []

    def set_mode(self, mode: str) -> None:
        with self.lock:
            self.mode = mode

    def count(self) -> int:
        with self.lock:
            return len(self.requests)

    def fetch(self, settings: Settings, radius_nm: float | None = None) -> list[dict[str, Any]]:
        with self.lock:
            mode = self.mode
            self.requests.append((settings.latitude, settings.longitude, radius_nm))
        if mode == "empty":
            return []
        if mode == "rate_limited":
            raise AircraftFeedRateLimited(120)
        if mode == "timeout":
            raise TimeoutError("fixture timeout")
        if mode == "http_error":
            raise urllib.error.HTTPError("fixture://aircraft", 503, "unavailable", {}, None)
        if mode != "live":
            raise RuntimeError("unknown fixture mode")
        prefix = "NEW" if settings.latitude < 40 else "TST"
        return [
            {
                "hex": "abc123", "flight": prefix + "123", "r": "N123AB", "t": "A320",
                "lat": settings.latitude, "lon": settings.longitude + 0.25,
                "seen_pos": 0.1, "alt_baro": 12000, "gs": 220, "track": 90,
            },
            {
                "hex": "def456", "flight": prefix + "456", "r": "N456CD", "t": "B738",
                "lat": settings.latitude + 0.1, "lon": settings.longitude,
                "seen_pos": 0.2, "alt_baro": 18000, "gs": 300, "track": 180,
            },
        ]

    def tracked(self, _callsigns: tuple[str, ...] | list[str]) -> tuple[dict[str, Any], str]:
        return (
            {
                "hex": "789abc", "flight": "TST703", "r": "N703XY", "t": "A320",
                "lat": self.settings.latitude + 1.0, "lon": self.settings.longitude,
                "seen_pos": 0.1, "alt_baro": 25000, "gs": 400, "track": 90,
            },
            "TST703",
        )

    settings: Settings


class BrowserAcceptanceTests(unittest.TestCase):
    ARTIFACT_ROOT = Path(__file__).with_name("test-artifacts") / "browser"

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        try:
            cls.browser: Browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            raise unittest.SkipTest(f"Playwright Chromium cannot launch: {exc}") from exc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="overhead-browser-")
        root = Path(self.temp.name)
        self.config = root / "config.json"
        self.config.write_text('{"latitude": 51.5, "longitude": -0.1, "radius_nm": 25, "refresh_seconds": 300}\n', encoding="utf-8")
        self.settings = Settings(
            latitude=51.5, longitude=-0.1, radius_nm=25, refresh_seconds=300,
            output_rgb=str(root / "frame.rgb"), output_png=str(root / "frame.png"),
        )
        self.provider = ProviderFixture()
        self.provider.settings = self.settings
        self.state = monitor.FrameState()
        self.tracker = monitor.FlightTrackingState(self.settings.radius_nm)
        self.lease = monitor.ActiveMonitorLease()
        self.refresh_event = threading.Event()
        self.routes = EmptyRoutes()
        self.logos = EmptyLogos()
        self.identities = EmptyStore()
        self.flags = EmptyStore()
        self.snapshots = monitor.SnapshotStore(root / "screenshots")
        self.sessions = monitor.VisitorSessionStore(
            self.settings, root / "visitor-snapshots", global_min_interval_seconds=0,
        )
        self.server = monitor.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            monitor.handler_factory(
                self.state, self.settings, self.refresh_event, Path(__file__).with_name("beep-tone.mp3"),
                self.config, self.routes, self.logos, self.tracker, self.snapshots, False, self.lease,
                self.sessions,
            ),
        )
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.fetch_patch = patch.object(monitor, "fetch_aircraft", side_effect=self.provider.fetch)
        self.track_patch = patch.object(monitor, "fetch_tracked_aircraft", side_effect=self.provider.tracked)
        self.fetch_patch.start()
        self.track_patch.start()
        self.refresh_thread = threading.Thread(
            target=monitor.visitor_refresh_loop,
            args=(self.sessions, False, self.logos, self.identities, self.routes, self.flags),
            daemon=True,
        )
        self.refresh_thread.start()
        self.contexts: list[BrowserContext] = []
        self.page_errors: list[str] = []
        self.console_errors: list[str] = []
        self.failed_requests: list[str] = []
        self.external_requests: list[str] = []
        self.force_inactive_status = False

    def tearDown(self) -> None:
        for context in self.contexts:
            context.close()
        for session in self.sessions.sessions.values():
            with session.lease.lock:
                session.lease.last_seen = time.monotonic() - 20
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.track_patch.stop()
        self.fetch_patch.stop()
        self.temp.cleanup()

    def new_page(
        self, width: int, height: int, geolocation: dict[str, float] | None = None,
        reduced_motion: str | None = None,
    ) -> Page:
        options: dict[str, Any] = {"viewport": {"width": width, "height": height}}
        if reduced_motion:
            options["reduced_motion"] = reduced_motion
        if geolocation:
            options["geolocation"] = geolocation
        context = self.browser.new_context(**options)
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
                    if(localStorage.getItem('overhead-distance-unit')===null)localStorage.setItem('overhead-distance-unit','nm');
                    if(localStorage.getItem('overhead-radar-range-nm')===null)localStorage.setItem('overhead-radar-range-nm','25');
                    if(localStorage.getItem('overhead-radar-range-mi')===null)localStorage.setItem('overhead-radar-range-mi','35');
                    if(localStorage.getItem('overhead-radar-range-km')===null)localStorage.setItem('overhead-radar-range-km','45');
                    if(localStorage.getItem('overhead-sound')===null)localStorage.setItem('overhead-sound','muted');
        """)
        if geolocation:
            context.grant_permissions(["geolocation"], origin=self.base_url)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("console", lambda message: self.console_errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: self.failed_requests.append(f"{request.url}: {request.failure}"))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        try:
            self.wait_for_text(page, "#mode", lambda text: text.lower() in {"live", "error"}, timeout=10)
        except AssertionError as exc:
            self.fail(f"Initial display did not settle: {exc}; page errors={self.page_errors}; console errors={self.console_errors}; failed requests={self.failed_requests}")
        if (page.locator("#mode").text_content() or "").lower() == "error":
            cookies = page.context.cookies(self.base_url)
            session_id = next((cookie["value"] for cookie in cookies if cookie["name"] == "overhead_session"), None)
            session = self.sessions.sessions.get(session_id)
            server_error = session.state.payload.get("error") if session else "session was not created"
            self.fail(f"Initial display entered Error: {server_error}; page errors={self.page_errors}; console errors={self.console_errors}; failed requests={self.failed_requests}")
        self.bind_page_session(page)
        return page

    def wait_for_text(self, page: Page, selector: str, predicate, timeout: float = 5) -> str:
        locator = page.locator(selector)
        locator.wait_for(state="attached", timeout=int(timeout * 1000))
        deadline = time.monotonic() + timeout
        last_text = ""
        while time.monotonic() < deadline:
            last_text = locator.text_content() or ""
            if predicate(last_text):
                return last_text
            time.sleep(0.025)
        self.fail(f"Timed out waiting for {selector} text; last value was {last_text!r}")

    def wait_for_input_value(self, page: Page, selector: str, predicate, timeout: float = 5) -> str:
        locator = page.locator(selector)
        locator.wait_for(state="attached", timeout=int(timeout * 1000))
        deadline = time.monotonic() + timeout
        last_value = ""
        while time.monotonic() < deadline:
            last_value = locator.input_value()
            if predicate(last_value):
                return last_value
            time.sleep(0.025)
        self.fail(f"Timed out waiting for {selector} value; last value was {last_value!r}")

    def wait_for_class(self, page: Page, selector: str, class_name: str, present: bool = True, timeout: float = 5) -> None:
        locator = page.locator(selector)
        locator.wait_for(state="attached", timeout=int(timeout * 1000))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            classes = set((locator.get_attribute("class") or "").split())
            if (class_name in classes) == present:
                return
            time.sleep(0.025)
        self.fail(f"Timed out waiting for class {class_name!r} on {selector}")

    def bind_page_session(self, page: Page) -> None:
        cookies = page.context.cookies(self.base_url)
        session_cookie = next((cookie["value"] for cookie in cookies if cookie["name"] == "overhead_session"), None)
        self.assertIsNotNone(session_cookie)
        session = self.sessions.sessions[session_cookie]
        self.settings = session.settings
        self.provider.settings = session.settings
        self.state = session.state
        self.tracker = session.tracker
        self.lease = session.lease

    def _offline_route(self, route) -> None:
        url = route.request.url
        if url.startswith(self.base_url + "/"):
            if self.force_inactive_status and "/status?active=1" in url:
                route.continue_(url=url.replace("active=1", "active=0", 1))
                return
            if url.split("?", 1)[0] == self.base_url + "/":
                response = route.fetch()
                html = response.text()
                html = __import__("re").sub(r'<link[^>]*leaflet\.css[^>]*>', "", html)
                html = __import__("re").sub(r'<script[^>]*leaflet\.js[^>]*></script>', "", html)
                route.fulfill(response=response, body=html)
            else:
                route.continue_()
        else:
            self.external_requests.append(url)
            route.abort()

    def wait_for_calls(self, count: int, timeout: float = 4) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.provider.count() >= count:
                return
            time.sleep(0.025)
        self.fail(f"Expected at least {count} fixture provider requests; got {self.provider.count()}")

    def update_now(self) -> None:
        monitor.update_state(
            self.state, self.settings, False, self.logos, self.identities,
            self.routes, self.flags, self.tracker,
        )

    def test_desktop_radar_ranges_units_reprojection_and_limits(self) -> None:
        page = self.new_page(1280, 800)
        self.wait_for_text(page, "#radar-range", lambda text: text == "25 NM")
        requests_before = self.provider.count()
        contact = page.locator("#radar-contacts .contact").first
        position_before = contact.get_attribute("transform")

        page.locator("#zoom-out").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "30 NM")
        position_after = page.locator("#radar-contacts .contact").first.get_attribute("transform")
        self.assertNotEqual(position_before, position_after)
        for _ in range(4):
            page.locator("#zoom-out").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "50 NM")
        self.assertEqual(self.provider.count(), requests_before)

        page.locator("#settings-toggle").click()
        distance = page.locator("#settings-distance-unit")
        distance.select_option("mi")
        self.wait_for_text(page, "#radar-range", lambda text: text == "35 MI")
        distance.select_option("km")
        self.wait_for_text(page, "#radar-range", lambda text: text == "45 KM")
        distance.select_option("nm")
        self.wait_for_text(page, "#radar-range", lambda text: text == "50 NM")
        preferences = page.evaluate("""() => ['nm','mi','km'].map(unit =>
          localStorage.getItem('overhead-radar-range-'+unit))""")
        self.assertEqual(preferences, ["50", "35", "45"])
        page.locator("#settings-toggle").click()

        for _ in range(9):
            page.locator("#zoom-in").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "5 NM")
        self.assertTrue(page.locator("#zoom-in").is_disabled())
        for expected_range in range(10, 101, 5):
            page.locator("#zoom-out").click()
            self.wait_for_text(page, "#radar-range", lambda text, expected=expected_range: text == f"{expected} NM")
        session_cookie = next(cookie["value"] for cookie in page.context.cookies(self.base_url) if cookie["name"] == "overhead_session")
        self.assertEqual(self.sessions.sessions[session_cookie].settings.radius_nm, 100)
        self.assertEqual(self.sessions.sessions[session_cookie].state.payload["radar_radius_nm"], 100)
        self.wait_for_text(page, "#radar-range", lambda text: text == "100 NM")
        self.assertTrue(page.locator("#zoom-out").is_disabled())
        self.assertEqual(self.provider.count(), requests_before)
        page.reload(wait_until="domcontentloaded")
        self.wait_for_text(page, "#radar-range", lambda text: text == "100 NM")
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_layout_and_controls_across_five_viewports(self) -> None:
        viewports = (
            ("small-mobile", 320, 568),
            ("mobile", 390, 844),
            ("tablet", 768, 1024),
            ("laptop", 1366, 768),
            ("desktop", 1920, 1080),
        )
        for label, width, height in viewports:
            with self.subTest(viewport=label):
                page = self.new_page(width, height)
                screenshot_dir = self.ARTIFACT_ROOT / label
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot_dir / "initial.png"), full_page=False)
                if not page.locator("#wall").evaluate("element => element.classList.contains('radar-open')"):
                    page.locator("#radar-toggle").click()
                    self.wait_for_class(page, "#wall", "radar-open")
                page.screenshot(path=str(screenshot_dir / "radar.png"), full_page=False)
                dimensions = page.evaluate("""() => ({
                  width: innerWidth, height: innerHeight,
                  scrollWidth: document.documentElement.scrollWidth,
                  wall: document.querySelector('#wall').getBoundingClientRect().toJSON()
                })""")
                self.assertLessEqual(dimensions["scrollWidth"], width + 1)
                wall_bounds = dimensions["wall"]
                for selector in ("#track-button", "#radar-count", "#zoom-out"):
                    bounds = page.locator(selector).bounding_box()
                    self.assertIsNotNone(bounds)
                    self.assertGreater(bounds["width"], 0)
                    self.assertGreaterEqual(bounds["x"], 0)
                    self.assertLessEqual(bounds["x"] + bounds["width"], wall_bounds["x"] + wall_bounds["width"] + 1)
                self.assertEqual(page.locator("#mode").text_content(), "Live")
                page.screenshot(path=str(screenshot_dir / "populated.png"), full_page=False)
                self.assertEqual(self.page_errors, [])

    def test_keyboard_shortcut_and_modal_focus_behaviour(self) -> None:
        page = self.new_page(1280, 800)
        wall = page.locator("#wall")
        initial_radar = wall.evaluate("element => element.classList.contains('radar-open')")
        flight_input = page.locator("#track-flight")
        flight_input.focus()
        page.keyboard.press("r")
        self.assertEqual(flight_input.input_value(), "r")
        self.assertEqual(wall.evaluate("element => element.classList.contains('radar-open')"), initial_radar)

        page.locator("#snapshots-toggle").focus()
        page.keyboard.press("Enter")
        self.wait_for_class(page, "#snapshot-backdrop", "open")
        self.assertEqual(page.evaluate("document.activeElement.id"), "snapshot-close")
        page.keyboard.press("Shift+Tab")
        self.assertEqual(page.evaluate("document.activeElement.id"), "snapshot-close")
        page.keyboard.press("Escape")
        self.wait_for_class(page, "#snapshot-backdrop", "open", present=False)
        self.assertEqual(page.evaluate("document.activeElement.id"), "snapshots-toggle")

        page.locator("#settings-toggle").click()
        page.locator("#settings-location").click()
        self.wait_for_class(page, "#location-backdrop", "open")
        self.assertEqual(page.evaluate("document.activeElement.id"), "location-close")
        page.keyboard.press("Escape")
        self.assertEqual(page.evaluate("document.activeElement.id"), "settings-toggle")
        self.assertEqual(self.page_errors, [])

    def test_reduced_motion_disables_repeating_tracking_animation(self) -> None:
        page = self.new_page(390, 844, reduced_motion="reduce")
        page.locator("#radar-toggle").click()
        self.wait_for_class(page, "#wall", "radar-open")
        page.locator("#track-flight").fill("TST703")
        page.locator("#track-button").click()
        self.wait_for_text(page, "#track-button", lambda text: text == "STOP")
        values = page.locator("#track-button").evaluate("element => ({iterations:getComputedStyle(element).animationIterationCount,duration:getComputedStyle(element).animationDuration})")
        self.assertTrue(page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        self.assertEqual(values["iterations"], "1")
        self.assertNotEqual(values["duration"], "2.6s")
        self.assertEqual(self.page_errors, [])

    def test_snapshot_capture_and_gallery_use_local_render_fixture(self) -> None:
        from PIL import Image

        page = self.new_page(1280, 800)

        def render_fixture(_store, _snapshot_id, _radar, _base_url, desktop_path, mobile_path, _visitor_session_id=None):
            Image.new("RGB", (1, 1), "black").save(desktop_path)
            Image.new("RGB", (1, 1), "black").save(mobile_path)

        with patch.object(monitor.SnapshotStore, "_render_pair", render_fixture):
            page.locator("#camera-toggle").click()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and page.locator("#snapshot-grid .snapshot-card").count() != 1:
                time.sleep(0.025)
            self.assertEqual(page.locator("#snapshot-grid .snapshot-card").count(), 1)
        self.assertEqual(len(self.sessions.sessions), 1)
        session = next(iter(self.sessions.sessions.values()))
        self.assertEqual(len(session.snapshots.list()), 1)
        self.assertTrue(session.snapshots.image(session.snapshots.list()[0]["id"], "desktop"))
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_two_browser_contexts_keep_locations_tracking_and_ranges_independent(self) -> None:
        page_a = self.new_page(1280, 800, {"latitude": 41.8781, "longitude": -87.6298})
        cookies_a = page_a.context.cookies(self.base_url)
        cookie_a = next(cookie["value"] for cookie in cookies_a if cookie["name"] == "overhead_session")
        session_a = self.sessions.sessions[cookie_a]

        page_b = self.new_page(390, 844, {"latitude": 34.0522, "longitude": -118.2437})
        cookies_b = page_b.context.cookies(self.base_url)
        cookie_b = next(cookie["value"] for cookie in cookies_b if cookie["name"] == "overhead_session")
        session_b = self.sessions.sessions[cookie_b]
        self.assertNotEqual(cookie_a, cookie_b)

        for page in (page_a, page_b):
            page.locator("#settings-toggle").click()
            page.locator("#settings-location").click()
            page.locator("#location-use-device").click()
            page.locator("#location-save").click()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            abs(session_a.settings.latitude - 41.8781) > 0.0001
            or abs(session_b.settings.latitude - 34.0522) > 0.0001
        ):
            time.sleep(0.025)
        self.assertAlmostEqual(session_a.settings.latitude, 41.8781, places=4)
        self.assertAlmostEqual(session_b.settings.latitude, 34.0522, places=4)
        self.assertNotEqual(session_a.settings.longitude, session_b.settings.longitude)

        page_a.locator("#zoom-out").click()
        self.wait_for_text(page_a, "#radar-range", lambda text: text == "30 NM")
        self.assertEqual(session_a.settings.radius_nm, 30)
        self.assertEqual(session_b.settings.radius_nm, 25)

        page_a.locator("#track-flight").fill("TST703")
        page_a.locator("#track-button").click()
        page_b.locator("#track-flight").fill("MAN42")
        page_b.locator("#track-button").click()
        self.wait_for_text(page_a, "#track-button", lambda text: text == "STOP")
        self.wait_for_text(page_b, "#track-button", lambda text: text == "STOP")
        self.assertEqual(session_a.tracker.snapshot()["query"], "TST703")
        self.assertEqual(session_b.tracker.snapshot()["query"], "MAN42")

        page_a.reload(wait_until="domcontentloaded")
        self.wait_for_text(page_a, "#track-button", lambda text: text == "STOP")
        self.assertEqual(session_a.tracker.snapshot()["query"], "TST703")
        self.assertEqual(session_b.tracker.snapshot()["query"], "MAN42")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["latitude"], 51.5)
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_mobile_layout_location_updates_and_restart_persistence(self) -> None:
        page = self.new_page(390, 844, {"latitude": 34.0522, "longitude": -118.2437})
        dimensions = page.evaluate("""() => ({
          viewportWidth: innerWidth, viewportHeight: innerHeight,
          pageWidth: document.documentElement.scrollWidth,
          pageHeight: document.documentElement.scrollHeight
        })""")
        self.assertLessEqual(dimensions["pageWidth"], dimensions["viewportWidth"] + 1)
        self.assertLessEqual(dimensions["pageHeight"], dimensions["viewportHeight"] + 1)
        screenshot = Path(self.temp.name) / "mobile.png"
        page.screenshot(path=str(screenshot))
        self.assertGreater(screenshot.stat().st_size, 1000)
        page.locator("#settings-toggle").click()
        page.locator("#settings-location").click()
        page.locator("#location-use-device").click()
        self.wait_for_text(page, "#location-latitude", lambda text: abs(float(text) - 34.0522) < 0.0001)
        page.locator("#location-save").click()
        self.wait_for_calls(2)
        self.assertEqual((self.settings.latitude, self.settings.longitude), (34.0522, -118.2437))
        self.assertEqual((self.state.payload["home_latitude"], self.state.payload["home_longitude"]), (34.0522, -118.2437))
        self.assertTrue(self.state.payload["callsign"].startswith("NEW"))
        reloaded = load_settings(self.config)
        self.assertEqual((reloaded.latitude, reloaded.longitude), (51.5, -0.1))

        responses = page.evaluate("""async () => {
          const saves = [[10,20],[30,40]].map(([latitude,longitude]) => fetch('/location', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({latitude,longitude})
          }).then(response => response.json()));
          return Promise.all(saves);
        }""")
        latest = max(responses, key=lambda item: item["location_revision"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (self.settings.latitude, self.settings.longitude) != (latest["latitude"], latest["longitude"]):
            time.sleep(0.025)
        self.assertEqual((self.settings.latitude, self.settings.longitude), (latest["latitude"], latest["longitude"]))
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual((saved["latitude"], saved["longitude"]), (51.5, -0.1))
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_scanning_rate_limit_timeout_recovery_tracking_and_reopen(self) -> None:
        page = self.new_page(1280, 800, {"latitude": 34.0522, "longitude": -118.2437})
        self.provider.set_mode("empty")
        self.update_now()
        self.wait_for_text(page, "#mode", lambda text: text == "Scanning")
        self.assertNotEqual(self.state.payload["mode"], "demo")

        self.provider.set_mode("rate_limited")
        self.update_now()
        self.wait_for_text(page, "#mode", lambda text: text == "Rate Limited")
        self.provider.set_mode("timeout")
        self.update_now()
        self.wait_for_text(page, "#mode", lambda text: text == "Reconnecting")
        self.provider.set_mode("http_error")
        self.update_now()
        self.wait_for_text(page, "#mode", lambda text: text == "Reconnecting")
        self.provider.set_mode("live")
        self.update_now()
        self.wait_for_text(page, "#mode", lambda text: text == "Live")

        page.locator("#track-flight").fill("TST703")
        page.locator("#track-button").click()
        self.wait_for_text(page, "#track-button", lambda text: text == "STOP")
        self.update_now()
        self.wait_for_text(page, "#radar-range", lambda text: text.startswith("AUTO "))
        self.assertTrue(self.state.payload["tracking_active"])
        self.assertGreaterEqual(self.state.payload["radar_radius_nm"], self.state.payload["distance_nm"] * 1.18)
        page.reload(wait_until="domcontentloaded")
        self.wait_for_text(page, "#track-button", lambda text: text == "STOP")
        self.assertEqual(self.state.payload["tracking_resolved_callsign"], "TST703")

        page.locator("#settings-toggle").click()
        page.locator("#settings-location").click()
        page.locator("#location-use-device").click()
        self.wait_for_text(page, "#location-latitude", lambda text: abs(float(text) - 34.0522) < 0.0001)
        before_location = self.provider.count()
        page.locator("#location-save").click()
        self.wait_for_calls(before_location + 1)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            self.tracker.snapshot()["last_plane"] is None
            or abs(self.tracker.snapshot()["last_plane"]["lat"] - 35.0522) > 0.0001
        ):
            time.sleep(0.025)
        self.assertTrue(self.state.payload["tracking_active"])
        tracked_plane = self.tracker.snapshot()["last_plane"]
        self.assertIsNotNone(tracked_plane)
        self.assertAlmostEqual(tracked_plane["lat"], 52.5)
        self.assertAlmostEqual(self.state.payload["home_latitude"], 34.0522)
        expected_distance = monitor.haversine_nm(
            self.settings.latitude, self.settings.longitude,
            tracked_plane["lat"], tracked_plane["lon"],
        )
        self.assertAlmostEqual(self.state.payload["distance_nm"], expected_distance, delta=0.1)
        self.wait_for_text(page, "#radar-range", lambda text: text.startswith("AUTO "))

        before_hidden = self.provider.count()
        self.force_inactive_status = True
        with self.lease.lock:
            self.lease.last_seen = time.monotonic() - 13
        page.wait_for_timeout(2200)
        self.assertFalse(self.lease.active())
        self.assertEqual(self.provider.count(), before_hidden)
        self.force_inactive_status = False
        page.evaluate("fetch('/status?active=1')")
        self.wait_for_calls(before_hidden + 1)

        count_before_close = self.provider.count()
        browser_context = page.context
        page.close()
        with self.lease.lock:
            self.lease.last_seen = time.monotonic() - 13
        reopened = browser_context.new_page()
        reopened.on("pageerror", lambda error: self.page_errors.append(str(error)))
        reopened.goto(self.base_url + "/", wait_until="domcontentloaded")
        self.wait_for_text(reopened, "#mode", lambda text: text.lower() == "live", timeout=10)
        self.bind_page_session(reopened)
        self.wait_for_calls(count_before_close + 1)
        reopened.locator("#track-button").wait_for()
        self.assertEqual(reopened.locator("#mode").text_content(), "Live")
        self.assertTrue(self.state.payload["tracking_active"])
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)