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
                constructor(element) { this.element = element; this.zoom = 12; this.layers = new Set(); this.handlers = {}; }
    setView(_point, zoom) { if (Number.isFinite(zoom)) this.zoom = zoom; return this; }
    getZoom() { return this.zoom; }
        getSize() { const box = this.element.getBoundingClientRect(); return {x: box.width, y: box.height}; }
        project(point) { return {x: point.lng * 256, y: point.lat * 256}; }
        unproject(point) { return {lat: point.y / 256, lng: point.x / 256}; }
    invalidateSize() { return this; }
    on(name, handler) { (this.handlers[name] ||= []).push(handler); return this; }
    fire(name, event) { for (const handler of this.handlers[name] || []) handler(event); return this; }
    addLayer(layer) { this.layers.add(layer); return this; }
    removeLayer(layer) { this.layers.delete(layer); return this; }
    hasLayer(layer) { return this.layers.has(layer); }
  }
  class Layer {
        constructor(point) { this.point = point; this.map = null; this.element = document.createElementNS('http://www.w3.org/2000/svg', 'path'); }
    addTo(map) { this.map = map; map.addLayer(this); return this; }
    on() { return this; }
    remove() { if (this.map) this.map.removeLayer(this); return this; }
    setLatLng(point) { this.point = point; return this; }
    getLatLng() { return {lat: this.point[0], lng: this.point[1]}; }
        bindTooltip() { return this; }
        getElement() { return this.element; }
  }
  window.L = {
        map: id => { const map = new Map(document.getElementById(id)); maps.push(map); return map; },
    tileLayer: () => new Layer([0, 0]),
    marker: point => new Layer(point),
    circle: point => new Layer(point),
        circleMarker: point => new Layer(point),
        polyline: points => new Layer(points[0]),
        divIcon: options => options,
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
        self.targeted_requests: list[str] = []
        self.diagnostic_events: list[dict[str, Any]] = []

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
            self.diagnostic_events.append({
                "at": time.monotonic(), "kind": "geographic", "mode": mode,
                "latitude": settings.latitude, "longitude": settings.longitude,
                "radius_nm": radius_nm,
            })
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
        target = _callsigns[0].upper()
        with self.lock:
            self.targeted_requests.append(target)
            mode = self.mode
            self.diagnostic_events.append({
                "at": time.monotonic(), "kind": "tracking", "mode": mode,
                "target": target,
            })
        if target in {"HEX:ABC123", "HEX:DEF456"}:
            if mode == "timeout":
                raise TimeoutError("fixture timeout")
            if mode == "rate_limited":
                raise AircraftFeedRateLimited(120)
            if mode == "empty":
                return None, ""
            second = target == "HEX:DEF456"
            return ({"hex": "def456" if second else "abc123",
                     "flight": "TST456" if second else "TST123",
                     "lat": self.settings.latitude + (.1 if second else 0),
                     "lon": self.settings.longitude + (0 if second else .25),
                     "seen_pos": .1, "alt_baro": 18000, "gs": 250, "track": 90},
                    "TST456" if second else "TST123")
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
                self.sessions, self.identities, self.flags,
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
        self.diagnostic_events: list[dict[str, Any]] = []
        self.diagnostic_lock = threading.Lock()
        self.diagnostic_enabled = False
        self.diagnostic_page: Page | None = None

    def record_diagnostic(self, event: str, **details: Any) -> None:
        if not self.diagnostic_enabled:
            return
        record = {"at": time.monotonic(), "event": event, **details}
        with self.diagnostic_lock:
            self.diagnostic_events.append(record)

    def scheduler_diagnostic_snapshot(self) -> dict[str, Any]:
        with self.sessions.lock:
            return {
                "at": time.monotonic(),
                "next_acquisition_at": self.sessions.next_acquisition_at,
                "initial_priority_streak": self.sessions.initial_priority_streak,
                "sessions": [
                    {
                        "session_id_suffix": session.session_id[-6:],
                        "active": session.lease.active(),
                        "lease_revision": session.lease.revision(),
                        "active_lease_revision": session.active_lease_revision,
                        "last_fetch_at": session.last_fetch_at,
                        "last_tracking_at": session.last_tracking_at,
                        "refresh_reasons": sorted(session.refresh_reasons),
                        "refresh_requested_at": session.refresh_requested_at,
                        "initial_acquisition_granted": session.initial_acquisition_granted,
                        "acquisition_kind": session.acquisition_kind,
                        "status": session.state.payload.get("acquisition_status"),
                        "retry_in_seconds": session.state.payload.get("acquisition_retry_in_seconds"),
                        "tracker": session.tracker.snapshot(),
                    }
                    for session in self.sessions.sessions.values()
                ],
            }

    def session_id_suffix_for(self, state: Any) -> str | None:
        with self.sessions.lock:
            return next((session.session_id[-6:] for session in self.sessions.sessions.values()
                         if session.state is state), None)

    def install_claim_due_diagnostics(self) -> None:
        original = self.sessions.claim_due

        def diagnostic_claim_due(*args, **kwargs):
            selected, retry_in = original(*args, **kwargs)
            with self.sessions.lock:
                record = {
                    "at": time.monotonic(),
                    "event": "scheduler_claim",
                    "selected_session_id_suffix": selected.session_id[-6:] if selected else None,
                    "selected_kind": selected.acquisition_kind if selected else None,
                    "retry_in": retry_in,
                    "next_acquisition_at": self.sessions.next_acquisition_at,
                    "initial_priority_streak": self.sessions.initial_priority_streak,
                    "sessions": [
                        {
                            "session_id_suffix": item.session_id[-6:],
                            "active": item.lease.active(),
                            "lease_revision": item.lease.revision(),
                            "active_lease_revision": item.active_lease_revision,
                            "last_fetch_at": item.last_fetch_at,
                            "last_tracking_at": item.last_tracking_at,
                            "refresh_reasons": sorted(item.refresh_reasons),
                            "refresh_requested_at": item.refresh_requested_at,
                            "initial_acquisition_granted": item.initial_acquisition_granted,
                            "acquisition_kind": item.acquisition_kind,
                            "retry_in_seconds": item.state.payload.get("acquisition_retry_in_seconds"),
                        }
                        for item in self.sessions.sessions.values()
                    ],
                }
            self.record_diagnostic("scheduler_claim", **{k: v for k, v in record.items() if k != "event"})
            return selected, retry_in

        self.sessions.claim_due = diagnostic_claim_due
        self.addCleanup(setattr, self.sessions, "claim_due", original)

    def start_scheduler_diagnostics(self) -> threading.Event:
        stop = threading.Event()

        def sample() -> None:
            previous = None
            while not stop.is_set():
                snapshot = self.scheduler_diagnostic_snapshot()
                comparable = {key: value for key, value in snapshot.items() if key != "at"}
                signature = json.dumps(comparable, sort_keys=True, default=str)
                if signature != previous:
                    self.record_diagnostic("scheduler", **snapshot)
                    previous = signature
                stop.wait(0.025)

        threading.Thread(target=sample, daemon=True).start()
        return stop

    def install_location_diagnostics(self, page: Page) -> None:
        request_sessions: dict[int, str | None] = {}

        def on_request(request) -> None:
            if request.url.split("?", 1)[0] != self.base_url + "/location":
                return
            session_suffix = None
            cookie_header = request.headers.get("cookie", "")
            cookie = next((part.partition("=")[2] for part in cookie_header.split(";")
                           if part.strip().startswith("overhead_session=")), None)
            if cookie:
                session_suffix = cookie[-6:]
            request_sessions[id(request)] = session_suffix
            try:
                body = request.post_data_json
            except Exception:
                body = None
            self.record_diagnostic("location_post_submitted", session_id_suffix=session_suffix, body=body)

        def on_response(response) -> None:
            if response.url.split("?", 1)[0] != self.base_url + "/location":
                return
            try:
                result = response.json()
            except Exception as exc:
                result = {"response_body_error": type(exc).__name__}
            request_id = id(response.request)
            session_suffix = request_sessions.get(request_id)
            cookie_header = response.request.headers.get("cookie", "")
            cookie = next((part.partition("=")[2] for part in cookie_header.split(";")
                           if part.strip().startswith("overhead_session=")), None)
            if cookie:
                session_suffix = cookie[-6:]
            matching_session = next(
                (item for item in self.sessions.sessions.values() if item.session_id[-6:] == session_suffix),
                None,
            )
            if matching_session is not None:
                with matching_session.state.lock:
                    server_state = {
                        "latitude": matching_session.settings.latitude,
                        "longitude": matching_session.settings.longitude,
                        "location_revision": matching_session.state.location_revision,
                    }
            else:
                server_state = None
            self.record_diagnostic(
                "location_post_response", session_id_suffix=session_suffix,
                status=response.status, result=result, server_state_at_response=server_state,
            )

        page.on("request", on_request)
        page.on("response", on_response)

    def install_status_diagnostics(self, page: Page) -> None:
        def on_response(response) -> None:
            if "/status" not in response.url:
                return
            try:
                payload = response.json()
                keys = (
                    "mode", "tracking_active", "tracking_query", "tracking_resolved_callsign",
                    "tracking_acquisition_outcome", "acquisition_status",
                    "acquisition_retry_in_seconds", "location_revision", "home_latitude",
                    "home_longitude",
                )
                payload = {key: payload.get(key) for key in keys if key in payload}
            except Exception as exc:
                payload = {"response_body_error": type(exc).__name__}
            self.record_diagnostic("browser_status_response", status=response.status, payload=payload)

        page.on("response", on_response)

    def install_page_lifecycle_diagnostics(self, page: Page, label: str) -> None:
        def on_request(request) -> None:
            if "/status?active=" in request.url:
                self.record_diagnostic(
                    "lease_touch_request", label=label,
                    active="/status?active=1" in request.url,
                )

        def on_response(response) -> None:
            if "/status?active=" not in response.url:
                return
            self.record_diagnostic(
                "lease_touch_response", label=label, status=response.status,
                scheduler=self.scheduler_diagnostic_snapshot(),
            )

        page.on("request", on_request)
        page.on("response", on_response)

    def diagnostic_failure(self, message: str) -> str:
        with self.diagnostic_lock:
            events = list(self.diagnostic_events)
        return f"{message}; diagnostics={json.dumps(events, sort_keys=True, default=str)}"

    def tearDown(self) -> None:
        if self.diagnostic_enabled:
            with self.diagnostic_lock:
                diagnostic_output = json.dumps(self.diagnostic_events, sort_keys=True, default=str)
            print(f"ACCEPTANCE_DIAGNOSTICS {diagnostic_output}", flush=True)
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
        reduced_motion: str | None = None, install_clock: bool = False,
        confirm_location: bool = True,
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
        if install_clock:
            page.clock.install()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.on("console", lambda message: self.console_errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: self.failed_requests.append(f"{request.url}: {request.failure}"))
        page.bring_to_front()
        try:
            for session in self.sessions.sessions.values():
                session.last_fetch_at = time.monotonic()
            interval_patch = patch.object(monitor, "NORMAL_ACQUISITION_INTERVAL_SECONDS", 0.05)
            interval_patch.start()
            page.goto(self.base_url + "/", wait_until="domcontentloaded")
            page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
            if confirm_location:
                if geolocation:
                    page.locator("#location-use-device").click()
                    self.wait_for_input_value(
                        page, "#location-latitude",
                        lambda value: bool(value) and abs(float(value) - geolocation["latitude"]) < 0.0001,
                    )
                page.locator("#location-save").click()
                self.wait_for_class(page, "#location-backdrop", "open", present=False)
            try:
                expected_modes = {"live", "cached observations", "scanning", "error"} if confirm_location else {"starting"}
                self.wait_for_text(page, "#mode", lambda text: text.lower() in expected_modes, timeout=10)
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
        finally:
            if "interval_patch" in locals():
                interval_patch.stop()

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
        session_cookie = next(cookie["value"] for cookie in page.context.cookies(self.base_url) if cookie["name"] == "overhead_session")
        session = self.sessions.sessions[session_cookie]
        with self.sessions.lock:
            session.refresh_reasons.clear()
            session.last_fetch_at = time.monotonic()
            self.sessions.next_acquisition_at = time.monotonic() + 60
        requests_before = self.provider.count()
        contact = page.locator("#radar-contacts .contact").first
        position_before = contact.get_attribute("transform")

        page.locator("#zoom-out").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "30 NM")
        page.wait_for_function(
            "(previous) => document.querySelector('#radar-contacts .contact')?.getAttribute('transform') !== previous",
            arg=position_before,
        )
        position_after = page.locator("#radar-contacts .contact").first.get_attribute("transform")
        self.assertNotEqual(position_before, position_after)
        for _ in range(4):
            page.locator("#zoom-out").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "50 NM")
        self.assertEqual(self.provider.count(), requests_before)
        self.assertGreaterEqual(session.settings.query_radius_nm, 50)
        self.assertIn("radar_range", session.refresh_reasons)

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

    def test_radar_range_immediately_reselects_nearest_eligible_aircraft(self) -> None:
        page = self.new_page(1280, 800)
        settings = self.settings
        current_observations = []
        monitor.fetch_aircraft.side_effect = lambda *_args, **_kwargs: list(current_observations)

        def aircraft_at(distance, hex_code, flight):
            latitude, longitude = monitor.destination_point(
                settings.latitude, settings.longitude, 90, distance,
            )
            return {
                "hex": hex_code, "flight": flight, "r": hex_code.upper(),
                "lat": latitude, "lon": longitude, "seen_pos": 0.1, "alt_baro": 20000,
            }

        def set_observations(*planes):
            current_observations[:] = planes
            with self.state.lock:
                self.state.last_live_aircraft = list(planes)
                self.state.last_live_aircraft_received_at = time.monotonic()
                self.state.last_live_aircraft_query_radius_nm = 100

        def reproject_range(radius):
            return page.evaluate("""radius => fetch('/radar-range?radius='+radius, {cache:'no-store'})
              .then(response => response.json())""", radius)

        def wait_for_aircraft(hex_code):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                with self.state.lock:
                    if self.state.payload.get("aircraft_id") == hex_code:
                        return
                time.sleep(0.025)
            self.fail(f"Expected selected aircraft {hex_code}, got {self.state.payload.get('aircraft_id')!r}")

        def wait_for_none(allowed_modes=("scanning",)):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                with self.state.lock:
                    if not self.state.payload.get("aircraft_id"):
                        self.assertIn(self.state.payload["mode"], allowed_modes)
                        self.assertEqual(self.state.payload["radar_contacts"], [])
                        return
                time.sleep(0.025)
            self.fail(f"Out-of-range aircraft remained selected: {self.state.payload.get('aircraft_id')!r}")

        outside = aircraft_at(8.9, "aabb81", "SFY8DU")
        set_observations(outside)
        for expected in ("20 NM", "15 NM", "10 NM"):
            page.locator("#zoom-in").click()
            self.wait_for_text(page, "#radar-range", lambda text, expected=expected: text == expected)
        wait_for_aircraft("AABB81")
        self.assertEqual(self.state.payload["callsign"], "SFY8DU")

        page.locator("#zoom-in").click()
        self.wait_for_text(page, "#radar-range", lambda text: text == "5 NM")
        wait_for_none()

        nearer = aircraft_at(3.2, "aabb82", "NEAR32")
        farther = aircraft_at(4.4, "aabb83", "NEAR44")
        set_observations(outside, farther, nearer)
        reproject_range(5)
        wait_for_aircraft("AABB82")
        self.assertLessEqual(self.state.payload["distance_nm"], 5)

        moved_outside = aircraft_at(6.2, "aabb82", "NEAR32")
        set_observations(moved_outside, farther, outside)
        reproject_range(5)
        wait_for_aircraft("AABB83")
        self.assertEqual(self.state.payload["callsign"], "NEAR44")
        self.assertLessEqual(self.state.payload["distance_nm"], 5)

        set_observations(outside)
        reproject_range(5)
        wait_for_none()

        set_observations(moved_outside, outside)
        reproject_range(10)
        wait_for_aircraft("AABB82")
        self.assertAlmostEqual(self.state.payload["distance_nm"], 6.2, delta=0.1)

        response = page.evaluate("""async () => {
          const response=await fetch('/location',{method:'POST',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({latitude:40,longitude:-75})});
          return response.json();
        }""")
        self.assertTrue(response["saved"])
        wait_for_none(("starting", "scanning"))
        relocated = aircraft_at(3, "aabb84", "NEAR30")
        set_observations(relocated)
        reproject_range(5)
        wait_for_aircraft("AABB84")

        with self.state.lock:
            self.state.position_observed_at_monotonic = time.monotonic() - monitor.POSITION_FRESHNESS_SECONDS - 1
            for contact in self.state.payload["radar_contacts"]:
                contact["position_age_seconds"] = monitor.POSITION_FRESHNESS_SECONDS + 1
                contact["position_state"] = "last_known"
        stale_status = page.evaluate("""async () => {
          const response=await fetch('/status?active=1',{cache:'no-store'});
          return response.json();
        }""")
        # The position is 21 seconds old: retain its unchanged coordinates and
        # selection as last-known, rather than treating it as live or erasing it.
        self.assertEqual(stale_status["aircraft_id"], "AABB84")
        self.assertEqual(stale_status["mode"], "last_known")
        self.assertEqual(stale_status["position_state"], "last_known")
        self.assertAlmostEqual(stale_status["aircraft_latitude"], relocated["lat"])
        self.assertEqual(len(stale_status["radar_contacts"]), 1)
        self.assertEqual(stale_status["radar_contacts"][0]["position_state"], "last_known")
        page.evaluate("update()")
        self.wait_for_text(page, "#mode", lambda text: text == "Last known position")
        self.assertFalse(page.locator("#wall").evaluate("element => element.classList.contains('no-aircraft')"))

        # Once the bounded retention window expires, status and UI must both
        # remove the coordinates and radar contact rather than extend retention.
        retention_deadline = time.monotonic() - monitor.retained_position_seconds(settings.refresh_seconds) - 1
        with self.state.lock:
            self.state.position_observed_at_monotonic = retention_deadline
            for contact in self.state.payload["radar_contacts"]:
                contact["position_age_seconds"] = monitor.retained_position_seconds(settings.refresh_seconds) + 1
        expired_status = page.evaluate("""async () => {
          const response=await fetch('/status?active=1',{cache:'no-store'});
          return response.json();
        }""")
        self.assertEqual(expired_status["aircraft_id"], "")
        self.assertEqual(expired_status["mode"], "scanning")
        self.assertEqual(expired_status["radar_contacts"], [])
        page.evaluate("update()")
        self.wait_for_text(page, "#mode", lambda text: text == "Scanning")
        self.wait_for_class(page, "#wall", "no-aircraft")
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_automatic_route_resolution_displays_pending_then_unresolved(self) -> None:
        page = self.new_page(1280, 800)
        previous_future = self.state.last_enrichment_future
        if previous_future is not None:
            previous_future.result(timeout=5)
        route_started = threading.Event()
        finish_route = threading.Event()
        original_get = self.routes.get

        def delayed_route(_plane):
            route_started.set()
            if not finish_route.wait(5):
                raise TimeoutError("route fixture was not released")
            return {}

        self.routes.get = delayed_route
        try:
            self.update_now()
            future = self.state.last_enrichment_future
            self.assertIsNotNone(future)
            self.assertTrue(route_started.wait(2), "Automatic aircraft selection did not start route lookup")
            self.assertFalse(self.state.payload["tracking_active"])
            self.wait_for_text(page, "#route-lookup-status", lambda text: text == "Looking up route…")
            self.assertEqual(page.locator("#callsign").text_content(), "")
            finish_route.set()
            future.result(timeout=5)
            self.wait_for_text(page, "#callsign", lambda text: text == "N/A to N/A")
            self.assertTrue(page.locator("#route-lookup-status").is_hidden())
            self.assertEqual(self.state.payload["route_resolution_status"], "unresolved")
        finally:
            finish_route.set()
            self.routes.get = original_get

    def test_continuous_automatic_acquisition_survives_freshness_boundary(self) -> None:
        page = self.new_page(390, 844)
        page.locator("#radar-toggle").click()
        started = time.monotonic()
        while time.monotonic() - started < 36:
            page.wait_for_timeout(1000)
            self.assertEqual(page.locator("#mode").text_content(), "Live")
            self.assertGreater(page.locator("#radar-contacts .contact").count(), 0)
            self.assertEqual(page.locator("#track-button").get_attribute("aria-pressed"), "false")
        self.assertGreaterEqual(len(self.provider.targeted_requests), 2)
        directory = self.ARTIFACT_ROOT / "continuous"
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / "mobile-after-36-seconds.png"), full_page=True)
        self.assertEqual(self.page_errors, [])

    def test_mobile_footer_and_operational_status_wrap_without_overlap(self) -> None:
        for width in (320, 375, 390):
            with self.subTest(width=width):
                page = self.new_page(width, 844 if width > 320 else 568)
                page.locator("#radar-toggle").click()
                self.wait_for_class(page, "#wall", "radar-open")
                payload = page.request.get(self.base_url + "/status?active=0").json()
                scenarios = [
                    dict(mode="queued", position_state="unavailable", acquisition_status="queued",
                         acquisition_queue_position=64, acquisition_queue_length=128,
                         acquisition_retry_in_seconds=3840),
                    dict(mode="provider_unavailable", tracking_acquisition_outcome="provider_unavailable"),
                    dict(mode="rate_limited", tracking_acquisition_outcome="rate_limited"),
                    dict(mode="position_unavailable", acquisition_outcome="stale"),
                    dict(mode="cached", position_state="last_known", position_age_seconds=45),
                    dict(mode="reconnecting"),
                ]
                for index, scenario in enumerate(scenarios):
                    fixture = {**payload, "flightaware_configured": True, "acquisition_status": "ready",
                               "tracking_acquisition_outcome": "", "acquisition_outcome": "", **scenario}
                    page.route("**/status?*", lambda route: route.fulfill(json=fixture))
                    page.wait_for_timeout(2300)
                    status = page.locator("#footer-status")
                    if index == 0:
                        self.assertIn("waiting in shared aircraft queue (64 of 128)", status.text_content())
                        self.assertGreater(status.bounding_box()["height"], 40)
                    else:
                        self.assertEqual(status.get_attribute("data-warning"), "true")
                    contrast = status.evaluate(r"""e => {
                        const luminance = color => {
                            const rgb = color.match(/[\d.]+/g).slice(0,3).map(Number).map(v => {
                                v /= 255; return v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4;
                            });
                            return rgb[0]*.2126 + rgb[1]*.7152 + rgb[2]*.0722;
                        };
                        const css = getComputedStyle(e);
                        return (luminance(css.color)+.05)/(luminance(css.backgroundColor)+.05);
                    }""")
                    self.assertGreaterEqual(contrast, 4.5)
                    self.assertGreaterEqual(status.evaluate("e => parseFloat(getComputedStyle(e).fontSize)"), 13)
                    for selector in ("#footer-status", "footer", ".footer-credit-line"):
                        for element in page.locator(selector).all():
                            self.assertTrue(element.evaluate("e => e.scrollWidth <= e.clientWidth + 1 && e.scrollHeight <= e.clientHeight + 1"))
                    status_box = status.bounding_box()
                    for selector in ("#track-flight", "#track-button", "#zoom-in", "#zoom-out"):
                        box = page.locator(selector).bounding_box()
                        self.assertLessEqual(box["y"] + box["height"], status_box["y"])
                    links = page.locator("footer a")
                    self.assertEqual(links.count(), 11)
                    for link in links.all():
                        link.scroll_into_view_if_needed()
                        self.assertTrue(link.evaluate("e => {const b=e.getBoundingClientRect(); return b.left>=0 && b.right<=innerWidth && !!e.href && e.getClientRects().length>0;}"))
                        link.focus()
                        self.assertTrue(link.evaluate("e => e===document.activeElement"))
                    self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                    directory = self.ARTIFACT_ROOT / f"mobile-status-{width}"
                    directory.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(directory / f"status-{index}.png"), full_page=True)
                    page.unroute("**/status?*")
                page.locator("#track-button").click(trial=True)
                page.locator("#track-flight").fill("TST703")
                self.assertEqual(page.locator("#track-flight").input_value(), "TST703")
                before = page.locator("#radar-range").text_content()
                page.locator("#zoom-out").click()
                self.wait_for_text(page, "#radar-range", lambda value: value != before)
                self.assertEqual(self.page_errors, [])

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
                self.assertEqual(page.locator("header").evaluate("element => getComputedStyle(element).zIndex"), "1000")
                self.assertEqual(page.locator("#brand-logo").evaluate("element => getComputedStyle(element).transform"), "matrix(1, 0, 0, 1, 0, 0)")
                for selector in ("#sound-toggle", "#radar-toggle", "#camera-toggle", "#snapshots-toggle", "#fullscreen-toggle", "#settings-toggle"):
                    button = page.locator(selector)
                    if button.is_visible():
                        self.assertTrue(button.evaluate("element => { const box=element.getBoundingClientRect(); const hit=document.elementFromPoint(box.x+box.width/2,box.y+box.height/2); return hit===element||element.contains(hit); }"), selector)
                page.locator("#settings-toggle").click()
                self.wait_for_class(page, "#wall", "settings-open")
                page.locator("#settings-toggle").click()
                self.wait_for_class(page, "#wall", "settings-open", present=False)
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
                    # Compare rendered CSS pixels, avoiding a 1/64px layout-rounding failure.
                    self.assertLessEqual(round(bounds["x"] + bounds["width"]), round(wall_bounds["x"] + wall_bounds["width"]) + 1)
                self.assertIn(page.locator("#mode").text_content(), {"Live", "Cached observations"})
                page.screenshot(path=str(screenshot_dir / "populated.png"), full_page=False)
                self.assertEqual(self.page_errors, [])

    def test_location_chooser_every_load_and_responsive_confirmation_layout(self) -> None:
        viewports = (
            ("location-desktop", 1366, 768),
            ("location-mobile-320", 320, 568),
            ("location-mobile-375", 375, 812),
            ("location-mobile-390", 390, 844),
            ("location-landscape", 844, 390),
        )
        for label, width, height in viewports:
            with self.subTest(viewport=label):
                page = self.new_page(width, height, confirm_location=False)
                dialog = page.locator("#location-dialog") if page.locator("#location-dialog").count() else page.locator(".location-dialog")
                self.assertTrue(page.locator("#location-backdrop").is_visible())
                geometry = page.evaluate("""() => {
                  const selectors=['.location-dialog','#location-map','.location-details','.location-status','#location-use-device','#location-cancel','#location-save'];
                  const boxes=Object.fromEntries(selectors.map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return [selector,{x:r.x,y:r.y,right:r.right,bottom:r.bottom,width:r.width,height:r.height}]}));
                  return {width:innerWidth,height:innerHeight,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,boxes};
                }""")
                self.assertLessEqual(geometry["scrollWidth"], width + 1)
                self.assertLessEqual(geometry["scrollHeight"], height + 1)
                for selector, box in geometry["boxes"].items():
                    self.assertGreater(box["width"], 0, selector)
                    self.assertGreater(box["height"], 0, selector)
                    self.assertGreaterEqual(box["x"], -1, selector)
                    self.assertGreaterEqual(box["y"], -1, selector)
                    self.assertLessEqual(box["right"], width + 1, selector)
                    self.assertLessEqual(box["bottom"], height + 1, selector)
                self.assertTrue(page.locator("#location-save").is_visible())
                artifact = self.ARTIFACT_ROOT / label / "location-chooser.png"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(artifact), full_page=False)
                page.reload(wait_until="domcontentloaded")
                page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
                self.assertTrue(page.locator("#location-save").is_visible())
                self.assertEqual(self.page_errors, [])

    def test_location_consent_gates_acquisition_and_focus_events_are_debounced(self) -> None:
        page = self.new_page(
            390, 844, {"latitude": 34.0522, "longitude": -118.2437},
            confirm_location=False,
        )
        page.wait_for_timeout(250)
        self.assertEqual(self.provider.count(), 0)
        self.assertAlmostEqual(float(page.locator("#location-latitude").input_value()), 51.5, places=3)
        page.locator("#location-use-device").click()
        self.wait_for_input_value(page, "#location-latitude", lambda value: bool(value) and abs(float(value) - 34.0522) < .0001)
        confirmed_at = time.monotonic()
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        self.wait_for_calls(1)
        first_request_at = self.provider.diagnostic_events[0]["at"]
        self.assertLess(first_request_at - confirmed_at, 1.0)
        requests = []
        page.on("request", lambda request: requests.append(request.url) if "/status?active=1" in request.url else None)
        page.evaluate("() => { for(let index=0; index<12; index++) window.dispatchEvent(new Event('focus')); }")
        page.wait_for_timeout(1000)
        self.assertLessEqual(len(requests), 2)
        self.assertEqual(self.page_errors, [])

    def test_last_known_contact_is_counted_labelled_and_expires(self) -> None:
        page = self.new_page(1280, 800)
        session = next(item for item in self.sessions.sessions.values() if item.state is self.state)
        with self.sessions.lock:
            session.refresh_reasons.clear()
            session.refresh_requested_at = None
            session.last_fetch_at = time.monotonic() + 600
            self.sessions.next_acquisition_at = time.monotonic() + 600

        observed_at = time.monotonic() - 30
        with self.state.lock:
            for plane in self.state.last_live_aircraft:
                plane["_position_observed_at_monotonic"] = observed_at
                plane["seen_pos"] = 30
            self.state.last_live_aircraft_received_at = time.monotonic()
            self.state.position_observed_at_monotonic = observed_at
        self.provider.set_mode("empty")
        self.update_now()
        page.evaluate("update()")
        self.wait_for_text(page, "#mode", lambda text: text == "Last known position")
        count = page.locator("#radar-count").text_content() or ""
        self.assertIn("LAST KNOWN", count)
        self.assertGreater(page.locator("#radar-contacts .contact.last-known").count(), 0)
        self.assertIn("Last known position", page.locator("#footer-status").text_content() or "")
        artifact = self.ARTIFACT_ROOT / "continuity-desktop" / "last-known-contact.png"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(artifact), full_page=False)

        expired_at = time.monotonic() - monitor.retained_position_seconds(self.settings.refresh_seconds) - 1
        with self.state.lock:
            for plane in self.state.last_live_aircraft:
                plane["_position_observed_at_monotonic"] = expired_at
                plane["seen_pos"] = monitor.retained_position_seconds(self.settings.refresh_seconds) + 1
            self.state.last_live_aircraft_received_at = time.monotonic()
            self.state.position_observed_at_monotonic = expired_at
        self.update_now()
        page.evaluate("update()")
        self.wait_for_text(page, "#mode", lambda text: text == "Scanning")
        self.assertEqual(page.locator("#radar-count").text_content(), "0 CONTACTS")
        self.assertEqual(page.locator("#radar-contacts .contact").count(), 0)
        self.assertEqual(self.page_errors, [])

    def test_operator_name_fits_complete_text_across_identity_and_layout_changes(self) -> None:
        page = self.new_page(1366, 768)
        session = next(item for item in self.sessions.sessions.values() if item.state is self.state)
        with self.sessions.lock:
            session.refresh_reasons.clear()
            session.refresh_requested_at = None
            session.last_fetch_at = time.monotonic() + 600
            self.sessions.next_acquisition_at = time.monotonic() + 600

        def publish(name: str, aircraft_id: str) -> None:
            with self.state.lock:
                self.state.logo = b""
                self.state.position_aircraft_id = aircraft_id
                self.state.position_observed_at_monotonic = time.monotonic()
                self.state.payload.update({
                    "mode": "live", "aircraft_id": aircraft_id,
                    "callsign": aircraft_id, "registration": aircraft_id,
                    "flight_details": aircraft_id, "airline_name": name,
                    "logo_available": False, "logo_revision": "",
                    "aircraft_latitude": self.settings.latitude,
                    "aircraft_longitude": self.settings.longitude,
                    "distance_nm": 0.0,
                })
            page.evaluate("update()")
            self.wait_for_text(page, "#route-brand-name", lambda text: text == name)
            page.wait_for_timeout(80)

        def metrics() -> dict[str, Any]:
            return page.locator("#route-brand-name").evaluate("""element => {
              const range=document.createRange(); range.selectNodeContents(element);
              const box=element.parentElement.getBoundingClientRect();
              const text=range.getBoundingClientRect();
              return {fontSize:parseFloat(getComputedStyle(element).fontSize),
                textWidth:text.width,textHeight:text.height,availableWidth:box.width*.92,
                availableHeight:box.height*.82,title:element.getAttribute('title'),
                overflow:getComputedStyle(element).textOverflow};
            }""")

        artifact_root = self.ARTIFACT_ROOT / "operator-fit"
        artifact_root.mkdir(parents=True, exist_ok=True)
        publish("KLM", "ABC123")
        short = metrics()
        self.assertIsNone(short["title"])
        page.screenshot(path=str(artifact_root / "desktop-short.png"), full_page=False)

        publish("British Airways", "DEF456")
        medium = metrics()
        self.assertLessEqual(medium["fontSize"], short["fontSize"] + .1)
        self.assertLessEqual(medium["textWidth"], medium["availableWidth"] + 1)

        publish("Capital Air Services", "ABC123")
        capital = metrics()
        self.assertIsNone(capital["title"])
        self.assertLessEqual(capital["textWidth"], capital["availableWidth"] + 1)
        self.assertLessEqual(capital["textHeight"], capital["availableHeight"] + 1)
        page.screenshot(path=str(artifact_root / "desktop-capital-radar-closed.png"), full_page=False)
        if not page.locator("#wall").evaluate("element => element.classList.contains('radar-open')"):
            page.locator("#radar-toggle").click()
            self.wait_for_class(page, "#wall", "radar-open")
        page.wait_for_timeout(80)
        self.assertLessEqual(metrics()["textWidth"], metrics()["availableWidth"] + 1)
        page.screenshot(path=str(artifact_root / "desktop-capital-radar-open.png"), full_page=False)

        exceptionally_long = "The Exceptionally Long International Helicopter Charter and Specialist Aviation Services Company"
        publish(exceptionally_long, "DEF456")
        long_metrics = metrics()
        self.assertGreaterEqual(long_metrics["fontSize"], 18)
        if long_metrics["textWidth"] > long_metrics["availableWidth"] + 1:
            self.assertEqual(long_metrics["title"], exceptionally_long)
            self.assertEqual(long_metrics["overflow"], "ellipsis")

        for label, width, height in (
            ("tablet", 768, 1024), ("mobile-portrait", 390, 844),
            ("mobile-landscape", 844, 390),
        ):
            with self.subTest(viewport=label):
                page.set_viewport_size({"width": width, "height": height})
                if page.locator("#wall").evaluate("element => element.classList.contains('radar-open')"):
                    page.locator("#radar-toggle").click()
                    self.wait_for_class(page, "#wall", "radar-open", present=False)
                publish("Capital Air Services", "ABC123" if label != "tablet" else "DEF456")
                fitted = metrics()
                self.assertIsNone(fitted["title"])
                self.assertLessEqual(fitted["textWidth"], fitted["availableWidth"] + 1)
                self.assertLessEqual(fitted["textHeight"], fitted["availableHeight"] + 1)
                page.screenshot(path=str(artifact_root / f"{label}-capital.png"), full_page=False)
        self.assertEqual(self.page_errors, [])

    def test_map_photo_rotation_identity_reset_missing_photo_and_responsive_area(self) -> None:
        svg_photo = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200" viewBox="0 0 400 200"><rect width="400" height="200" fill="#14ecff"/><path d="M0 180L180 20 400 160" fill="none" stroke="#02050a" stroke-width="12"/></svg>'

        def publish_photo(revision: str, available: bool = True) -> None:
            with self.state.lock:
                self.state.aircraft_photo = svg_photo if available else b""
                self.state.aircraft_photo_type = "image/svg+xml"
                self.state.payload["aircraft_photo_available"] = available
                self.state.payload["aircraft_photo_revision"] = revision if available else ""
                self.state.payload.update({
                    "route": "LHR to JFK", "origin": "LHR", "destination": "JFK",
                    "aircraft_latitude": 51.6, "aircraft_longitude": -0.1, "track": 90,
                    "origin_latitude": 51.47, "origin_longitude": -0.45,
                    "destination_latitude": 40.64, "destination_longitude": -73.78,
                })

        def wait_until(page: Page, predicate, timeout: float = 3.0) -> None:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.025)
            self.fail("Timed out waiting for the local photo fixture state")

        def assert_photo_layout(page: Page) -> None:
            photo_layout = page.locator("#aircraft-photo").evaluate("""image => {
              const imageBox=image.getBoundingClientRect();
              const mapBox=document.querySelector('#route-map').getBoundingClientRect();
              const panelBox=image.parentElement.getBoundingClientRect();
              const scale=Math.min(imageBox.width/image.naturalWidth,imageBox.height/image.naturalHeight);
              return {imageX:imageBox.x,imageY:imageBox.y,imageWidth:imageBox.width,imageHeight:imageBox.height,
                mapX:mapBox.x,mapY:mapBox.y,mapWidth:mapBox.width,mapHeight:mapBox.height,
                panelX:panelBox.x,panelY:panelBox.y,panelWidth:panelBox.width,panelHeight:panelBox.height,
                renderedWidth:image.naturalWidth*scale,mask:getComputedStyle(image).maskImage,
                objectFit:getComputedStyle(image).objectFit,background:getComputedStyle(image.parentElement).backgroundColor};
            }""")
            for image_key, frame_key in (("imageX", "mapX"), ("imageY", "mapY"),
                                         ("imageWidth", "mapWidth"), ("imageHeight", "mapHeight"),
                                         ("panelX", "mapX"), ("panelY", "mapY"),
                                         ("panelWidth", "mapWidth"), ("panelHeight", "mapHeight")):
                self.assertAlmostEqual(photo_layout[image_key], photo_layout[frame_key], delta=1)
            expected_width = min(photo_layout["mapWidth"], photo_layout["mapHeight"] * 2)
            self.assertAlmostEqual(photo_layout["renderedWidth"], expected_width, delta=1)
            self.assertNotEqual(photo_layout["mask"], "none")
            self.assertEqual(photo_layout["objectFit"], "contain")
            self.assertEqual(photo_layout["background"], "rgb(5, 8, 13)")
            self.assertTrue(page.locator(".aircraft-photo-credit").is_visible())

        viewports = (("desktop", 1366, 768), ("tablet", 768, 1024),
                     ("mobile-320", 320, 568), ("mobile-375", 375, 812), ("mobile-390", 390, 844))
        for label, width, height in viewports:
            with self.subTest(viewport=label):
                screenshot_dir = self.ARTIFACT_ROOT / label
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                page = self.new_page(width, height, install_clock=True)
                page.clock.pause_at(page.evaluate("Date.now()") + 100)
                publish_photo("fixture-one")
                page.clock.fast_forward(2000)
                wait_until(page, lambda: page.locator("#aircraft-photo").evaluate(
                    "image => image.complete && image.naturalWidth === 400"
                ) and page.evaluate("typeof routePhotoTimer !== 'undefined' && routePhotoTimer !== null"))
                wait_until(page, lambda: page.locator("#route-photo-panel").is_hidden())
                self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                self.assertTrue(page.locator("#route-map").evaluate("element => element.classList.contains('visible')"))
                self.assertGreater(page.evaluate("window.__overheadMaps[0].layers.size"), 1)
                map_width = page.locator("#route-map").evaluate("element => element.getBoundingClientRect().width")
                map_instances = page.evaluate("window.__overheadMaps.length")
                information_box = page.locator(".information").evaluate("element => {const r=element.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}")

                if label == "desktop":
                    page.clock.fast_forward(9999)
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    page.clock.fast_forward(1)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden(), page.evaluate("""() => ({
                      hidden:document.querySelector('#route-photo-panel').hidden,
                      source:document.querySelector('#aircraft-photo').getAttribute('src'),
                      complete:document.querySelector('#aircraft-photo').complete,
                      naturalWidth:document.querySelector('#aircraft-photo').naturalWidth,
                      aircraft:document.querySelector('#callsign').textContent,
                      visibility:document.visibilityState,
                      panelStyle:getComputedStyle(document.querySelector('#route-photo-panel')).cssText,
                      panelDisplay:getComputedStyle(document.querySelector('#route-photo-panel')).display,
                      panelVisibility:getComputedStyle(document.querySelector('#route-photo-panel')).visibility,
                      panelRect:document.querySelector('#route-photo-panel').getBoundingClientRect().toJSON(),
                      mapClass:document.querySelector('#route-map').className,
                      mapRect:document.querySelector('#route-map').getBoundingClientRect().toJSON()
                    })"""))
                    photo_size = page.locator("#aircraft-photo").evaluate("image => ({width:image.getBoundingClientRect().width,height:image.getBoundingClientRect().height,naturalWidth:image.naturalWidth,naturalHeight:image.naturalHeight,objectFit:getComputedStyle(image).objectFit})")
                    self.assertAlmostEqual(photo_size["width"], map_width, delta=1)
                    self.assertEqual(photo_size["objectFit"], "contain")
                    self.assertEqual(photo_size["naturalWidth"] / photo_size["naturalHeight"], 2)
                    assert_photo_layout(page)
                    self.assertEqual(page.locator(".information").evaluate("element => {const r=element.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}"), information_box)
                    page.screenshot(path=str(screenshot_dir / "route-photo.png"), full_page=False)
                    page.clock.fast_forward(9999)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden())
                    page.clock.fast_forward(1)
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    page.screenshot(path=str(screenshot_dir / "route-map-after-photo.png"), full_page=False)
                    page.clock.fast_forward(10000)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden(), page.evaluate("""() => ({
                      hidden:document.querySelector('#route-photo-panel').hidden,
                      source:document.querySelector('#aircraft-photo').getAttribute('src'),
                      complete:document.querySelector('#aircraft-photo').complete,
                      naturalWidth:document.querySelector('#aircraft-photo').naturalWidth,
                      aircraft:document.querySelector('#callsign').textContent,
                      visibility:document.visibilityState,
                      panelDisplay:getComputedStyle(document.querySelector('#route-photo-panel')).display,
                      panelVisibility:getComputedStyle(document.querySelector('#route-photo-panel')).visibility,
                      panelRect:document.querySelector('#route-photo-panel').getBoundingClientRect().toJSON(),
                      mapClass:document.querySelector('#route-map').className,
                      mapDisplay:getComputedStyle(document.querySelector('#route-map')).display,
                      mapRect:document.querySelector('#route-map').getBoundingClientRect().toJSON()
                    })"""))
                    page.evaluate("window.__stalePhotoLoad = document.querySelector('#aircraft-photo').onload")
                    with self.state.lock:
                        self.state.payload["aircraft_id"] = "DEF456"
                        self.state.payload["callsign"] = "TST456"
                    publish_photo("fixture-two")
                    page.clock.fast_forward(2000)
                    wait_until(page, lambda: page.locator("#aircraft-photo").get_attribute("src").endswith("fixture-two"))
                    page.evaluate("window.__stalePhotoLoad && window.__stalePhotoLoad()")
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    self.assertFalse(page.locator("#route-map").evaluate("element => element.classList.contains('photo-active')"))
                else:
                    for _ in range(25):
                        if not page.locator("#route-photo-panel").is_hidden():
                            break
                        page.clock.fast_forward(500)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden(), page.evaluate("""() => ({
                      hidden:document.querySelector('#route-photo-panel').hidden,
                      source:document.querySelector('#aircraft-photo').getAttribute('src'),
                      complete:document.querySelector('#aircraft-photo').complete,
                      naturalWidth:document.querySelector('#aircraft-photo').naturalWidth,
                      aircraft:document.querySelector('#callsign').textContent,
                      visibility:document.visibilityState,
                      panelDisplay:getComputedStyle(document.querySelector('#route-photo-panel')).display,
                      panelVisibility:getComputedStyle(document.querySelector('#route-photo-panel')).visibility,
                      panelRect:document.querySelector('#route-photo-panel').getBoundingClientRect().toJSON(),
                      mapClass:document.querySelector('#route-map').className,
                      mapDisplay:getComputedStyle(document.querySelector('#route-map')).display,
                      mapRect:document.querySelector('#route-map').getBoundingClientRect().toJSON()
                    })"""))
                    assert_photo_layout(page)
                    self.assertEqual(page.locator(".information").evaluate("element => {const r=element.getBoundingClientRect();return [r.x,r.y,r.width,r.height]}"), information_box)
                    page.screenshot(path=str(screenshot_dir / "route-photo.png"), full_page=False)
                    # The next map transition is due exactly ten seconds after
                    # the image-load callback armed the rotation timer.
                    page.clock.fast_forward(9999)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden())
                    page.clock.fast_forward(1)
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    self.assertTrue(page.locator("#route-map").evaluate("element => element.classList.contains('visible')"))
                    page.screenshot(path=str(screenshot_dir / "route-map-after-photo.png"), full_page=False)

                self.assertEqual(page.evaluate("window.__overheadMaps.length"), map_instances)
                self.assertEqual(page.locator("#route-map").evaluate("element => element.getBoundingClientRect().width"), map_width)
                if label.startswith("mobile-"):
                    page.clock.fast_forward(10000)
                    self.assertFalse(page.locator("#route-photo-panel").is_hidden())
                    publish_photo("", available=False)
                    page.clock.fast_forward(2000)
                    wait_until(page, lambda: page.locator("#route-photo-panel").is_hidden())
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    page.clock.fast_forward(10000)
                    self.assertTrue(page.locator("#route-photo-panel").is_hidden())
                    page.screenshot(path=str(screenshot_dir / "route-map-no-photo.png"), full_page=False)
                self.assertEqual(self.page_errors, [])
                self.assertEqual(self.external_requests, [])

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
        self.assertEqual(page.evaluate("document.activeElement.id"), "settings-location")
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
            self.wait_for_class(page, "#location-backdrop", "open", present=False)
            page.locator("#settings-toggle").click()
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
        page_b.locator("#radar-toggle").click()
        page_b.locator("#track-flight").fill("MAN42")
        page_b.locator("#track-button").click()
        self.wait_for_text(page_a, "#track-button", lambda text: text == "STOP")
        self.wait_for_text(page_b, "#track-button", lambda text: text == "STOP")
        self.assertEqual(session_a.tracker.snapshot()["query"], "TST703")
        self.assertEqual(session_b.tracker.snapshot()["query"], "MAN42")

        page_a.reload(wait_until="domcontentloaded")
        page_a.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        page_a.locator("#location-save").click()
        self.wait_for_class(page_a, "#location-backdrop", "open", present=False)
        self.wait_for_text(page_a, "#track-button", lambda text: text == "STOP")
        self.assertEqual(session_a.tracker.snapshot()["query"], "TST703")
        self.assertEqual(session_b.tracker.snapshot()["query"], "MAN42")
        self.assertEqual(json.loads(self.config.read_text(encoding="utf-8"))["latitude"], 51.5)
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def test_dynamic_location_prompts_on_load_and_manual_choice_remains_available(self) -> None:
        page = self.new_page(1280, 800, confirm_location=False)
        self.assertTrue(page.locator("#location-backdrop").is_visible())
        self.assertIn(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), (None, "dynamic"))
        self.assertTrue(page.locator("#location-latitude").is_visible())
        self.assertTrue(page.locator("#location-use-device").is_visible())
        self.assertEqual(self.external_requests, [])

    def test_previously_granted_location_is_applied_automatically(self) -> None:
        page = self.new_page(1280, 800, {"latitude": 48.8566, "longitude": 2.3522})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and abs(self.settings.latitude - 48.8566) > 0.0001:
            time.sleep(0.025)
        self.assertAlmostEqual(self.settings.latitude, 48.8566, places=4)
        self.assertAlmostEqual(self.settings.longitude, 2.3522, places=4)
        self.assertTrue(page.evaluate("localStorage.getItem('overhead-location-mode-v1') === 'dynamic'"))

    def test_denied_location_does_not_repeat_and_manual_fallback_works(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','dynamic');
          localStorage.setItem('overhead-location-denied-v1','1');
          Object.defineProperty(navigator,'permissions',{value:{query:()=>Promise.resolve({state:'denied'})}});
                    window.__locationCalls=0;
                    Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:()=>window.__locationCalls++}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        self.bind_page_session(page)
        self.wait_for_input_value(page, "#location-latitude", lambda value: value == "51.500000")
        self.wait_for_input_value(page, "#location-longitude", lambda value: value == "-0.100000")
        self.assertEqual(page.evaluate("window.__locationCalls"), 0)
        page.locator("#location-latitude").fill("35")
        page.locator("#location-longitude").fill("-120")
        page.locator("#location-save").click()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            self.settings.latitude != 35
            or self.settings.longitude != -120
            or self.state.location_revision < 1
        ):
            time.sleep(0.025)
        self.assertEqual(
            (self.settings.latitude, self.settings.longitude), (35, -120),
            f"manual save did not update visitor session: revision={self.state.location_revision}; "
            f"sessions={[(s.session_id, s.settings.latitude, s.settings.longitude) for s in self.sessions.sessions.values()]!r}",
        )
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), "manual")
        self.assertEqual(self.page_errors, [])

    def test_geolocation_timeout_or_unavailable_coordinates_preserve_manual_fallback(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','dynamic');
          Object.defineProperty(navigator,'permissions',{value:{query:()=>Promise.resolve({state:'granted'})}});
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:(success,error)=>error({code:3,message:'timeout'})}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        self.bind_page_session(page)
        page.locator("#location-use-device").click()
        self.wait_for_text(page, "#location-status", lambda text: "timed out" in text.lower())
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        self.assertEqual(self.page_errors, [])

    def test_explicit_manual_mode_is_preserved_for_returning_visitor(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','manual');
          Object.defineProperty(navigator,'permissions',{value:{query:()=>Promise.resolve({state:'granted'})}});
          window.__locationCalls=0;
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:()=>window.__locationCalls++}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.wait_for_timeout(100)
        self.assertEqual(page.evaluate("window.__locationCalls"), 0)
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), "manual")

    def test_location_permission_denial_keeps_dynamic_preference_and_explicit_activation_can_resume(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','dynamic');
          window.__locationCalls=0;
          Object.defineProperty(navigator,'permissions',{value:{query:()=>Promise.resolve({state:'prompt'})}});
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:(success,error)=>{
            window.__locationCalls++;
            if(window.__locationCalls===1)error({code:1,message:'denied'});
            else success({coords:{latitude:35,longitude:-120,accuracy:10}});
          }}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        self.bind_page_session(page)
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), "dynamic")
        page.locator("#location-use-device").click()
        self.wait_for_text(page, "#location-status", lambda text: "declined" in text.lower())
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-denied-v1')"), "1")
        page.locator("#location-use-device").click()
        self.wait_for_input_value(page, "#location-latitude", lambda value: value == "35.000000")
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        self.assertEqual(self.settings.latitude, 35)
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), "dynamic")
        self.assertIsNone(page.evaluate("localStorage.getItem('overhead-location-denied-v1')"))

    def test_permissions_api_unavailable_attempts_geolocation_without_blocking(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','dynamic');
          Object.defineProperty(navigator,'permissions',{value:undefined});
          window.__locationCalls=0;
                    window.__pendingGeolocationSuccess=null;
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:(success)=>{
            window.__locationCalls++;
                        window.__pendingGeolocationSuccess=success;
          }}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        self.assertEqual(page.evaluate("window.__locationCalls"), 0)
        page.locator("#location-use-device").click()
        self.assertEqual(page.evaluate("window.__locationCalls"), 1)
        page.evaluate("window.__pendingGeolocationSuccess({coords:{latitude:35,longitude:-120,accuracy:25}})")
        self.bind_page_session(page)
        self.wait_for_input_value(page, "#location-latitude", lambda value: value == "35.000000")
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        self.assertEqual(self.settings.latitude, 35)
        self.assertEqual(self.page_errors, [])

    def test_stale_geolocation_callback_cannot_override_manual_save(self) -> None:
        context = self.browser.new_context(viewport={"width": 1280, "height": 800})
        self.contexts.append(context)
        context.add_init_script(LEAFLET_SHIM)
        context.add_init_script("""
          localStorage.setItem('overhead-location-mode-v1','dynamic');
          Object.defineProperty(navigator,'permissions',{value:{query:()=>Promise.resolve({state:'granted'})}});
          window.__pendingGeolocationSuccess=null;
          Object.defineProperty(navigator,'geolocation',{value:{getCurrentPosition:(success)=>{window.__pendingGeolocationSuccess=success;}}});
        """)
        context.route("**/*", self._offline_route)
        page = context.new_page()
        self.diagnostic_enabled = True
        self.install_location_diagnostics(page)
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        page.goto(self.base_url + "/", wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        page.locator("#location-use-device").click()
        self.assertTrue(page.evaluate("typeof window.__pendingGeolocationSuccess === 'function'"))
        page.evaluate("document.getElementById('location-activation').hidden=false")
        self.bind_page_session(page)
        self.install_location_diagnostics(page)
        location_post = {"submitted": False, "response_received": False}
        location_response = threading.Event()

        def record_location_request(request) -> None:
            if request.url.split("?", 1)[0] == self.base_url + "/location":
                location_post["submitted"] = True

        def record_location_response(response) -> None:
            if response.url.split("?", 1)[0] == self.base_url + "/location":
                location_post["response_received"] = True
                location_post["status"] = response.status
                try:
                    location_post["result"] = response.json()
                except Exception as exc:
                    location_post["result_error"] = type(exc).__name__
                location_response.set()

        page.on("request", record_location_request)
        page.on("response", record_location_response)
        page.evaluate("() => { locationRequestRevision++; locationLookupInFlight=false; setLocationButtonBusy(false); pendingLocationMode='manual'; }")
        page.locator("#location-latitude").fill("10")
        page.locator("#location-longitude").fill("20")
        page.locator("#location-save").click()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            (self.settings.latitude, self.settings.longitude) != (10, 20)
            or self.state.location_revision < 1
        ):
            time.sleep(0.025)
        self.record_diagnostic(
            "manual_save_assertion_state",
            post=dict(location_post),
            response_event_set=location_response.is_set(),
            session_id_suffix=self.session_id_suffix_for(self.state),
            server_location={"latitude": self.settings.latitude, "longitude": self.settings.longitude,
                             "location_revision": self.state.location_revision},
        )
        self.assertEqual(
            (self.settings.latitude, self.settings.longitude), (10, 20),
            self.diagnostic_failure(
                f"manual save failed: revision={self.state.location_revision}; "
                f"server_location=({self.settings.latitude},{self.settings.longitude}); "
                f"session_id_suffix={self.session_id_suffix_for(self.state)}; "
                f"location_post={location_post!r}; response_event_set={location_response.is_set()}; "
                f"page_errors={self.page_errors!r}; button_disabled={page.locator('#location-save').is_disabled()}"
            ),
        )
        page.evaluate("window.__pendingGeolocationSuccess({coords:{latitude:35,longitude:-120,accuracy:5}})")
        page.wait_for_timeout(100)
        self.assertEqual((self.settings.latitude, self.settings.longitude), (10, 20))
        self.assertEqual(page.evaluate("localStorage.getItem('overhead-location-mode-v1')"), "manual")
        self.assertEqual(self.page_errors, [])

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
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            self.state.location_revision < 1
            or abs(self.state.payload.get("home_latitude", 0) - 34.0522) > 0.0001
        ):
            time.sleep(0.025)
        page.locator("#settings-toggle").click()
        page.locator("#settings-location").click()
        self.wait_for_input_value(page, "#location-latitude", lambda value: abs(float(value) - 34.0522) < 0.0001)
        revision_before_location = self.state.location_revision
        page.locator("#location-latitude").fill("34.0522")
        page.locator("#location-longitude").fill("-118.2437")
        with self.sessions.lock:
            self.sessions.next_acquisition_at = 0
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            self.state.location_revision < revision_before_location + 1
            or
            abs(self.settings.latitude - 34.0522) > 0.0001
            or abs(self.settings.longitude + 118.2437) > 0.0001
            or not str(self.state.payload.get("callsign") or "").startswith("NEW")
        ):
            time.sleep(0.025)
        self.assertEqual((self.settings.latitude, self.settings.longitude), (34.0522, -118.2437))
        self.assertEqual(
            self.state.location_revision, revision_before_location + 1,
            f"manual mobile save did not advance revision: baseline={revision_before_location}; "
            f"sessions={[(s.session_id, s.settings.latitude, s.settings.longitude, s.state.location_revision) for s in self.sessions.sessions.values()]!r}",
        )
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
        self.diagnostic_enabled = True
        stop_diagnostics = self.start_scheduler_diagnostics()
        self.addCleanup(stop_diagnostics.set)
        interval_patch = patch.object(monitor, "NORMAL_ACQUISITION_INTERVAL_SECONDS", 0.05)
        interval_patch.start()
        self.addCleanup(interval_patch.stop)
        self.install_claim_due_diagnostics()
        self.provider.set_mode("empty")
        page = self.new_page(1280, 800, {"latitude": 34.0522, "longitude": -118.2437})
        self.install_status_diagnostics(page)
        self.install_page_lifecycle_diagnostics(page, "initial")
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
        if self.state.payload.get("tracking_resolved_callsign") != "TST703":
            self.update_now()
        self.assertEqual(self.state.payload["tracking_resolved_callsign"], "TST703")
        self.wait_for_text(page, "#radar-range", lambda text: text.startswith("AUTO "))
        self.assertTrue(self.state.payload["tracking_active"])
        self.assertGreaterEqual(self.state.payload["radar_radius_nm"], self.state.payload["distance_nm"] * 1.18)
        page.reload(wait_until="domcontentloaded")
        page.locator("#location-backdrop.open").wait_for(state="visible", timeout=5000)
        page.locator("#location-save").click()
        self.wait_for_class(page, "#location-backdrop", "open", present=False)
        self.wait_for_text(page, "#track-button", lambda text: text == "STOP")
        self.assertEqual(self.state.payload["tracking_resolved_callsign"], "TST703")

        page.locator("#settings-toggle").click()
        page.locator("#settings-location").click()
        page.locator("#location-use-device").click()
        self.wait_for_input_value(page, "#location-latitude", lambda value: abs(float(value) - 34.0522) < 0.0001)
        before_location = self.provider.count()
        revision_before_location = self.state.location_revision
        with self.sessions.lock:
            self.sessions.next_acquisition_at = 0
        self.state.payload.update({"tracking_active": True, "tracking_query": "TST703", "tracking_resolved_callsign": "TST703"})
        page.locator("#location-save").click()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and self.state.location_revision == revision_before_location:
            time.sleep(0.025)
        self.assertGreaterEqual(self.state.location_revision, revision_before_location + 1)
        # This is the same covered geographic area, so a fresh shared-cache hit
        # is valid and should not require an additional provider request.
        self.assertGreaterEqual(self.provider.count(), before_location)
        self.assertEqual((self.settings.latitude, self.settings.longitude), (34.0522, -118.2437))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (
            self.tracker.snapshot()["last_plane"] is None
            or abs(self.tracker.snapshot()["last_plane"]["lat"] - 35.0522) > 0.0001
        ):
            time.sleep(0.025)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and "tracking_active" not in self.state.payload:
            time.sleep(0.025)
        self.assertIn("tracking_active", self.state.payload, f"location update state did not refresh: {self.state.payload!r}")
        self.assertTrue(self.state.payload["tracking_active"])
        tracked_plane = self.tracker.snapshot()["last_plane"]
        self.assertIsNotNone(tracked_plane)
        self.assertAlmostEqual(tracked_plane["lat"], self.settings.latitude + 1.0)
        self.assertAlmostEqual(self.state.payload["home_latitude"], 34.0522)
        expected_distance = monitor.haversine_nm(
            self.settings.latitude, self.settings.longitude,
            tracked_plane["lat"], tracked_plane["lon"],
        )
        self.assertAlmostEqual(self.state.payload["distance_nm"], expected_distance, delta=0.1)
        self.wait_for_text(page, "#radar-range", lambda text: text.startswith("AUTO "))

        self.record_diagnostic(
            "before_lease_expiry",
            session_id_suffix=self.session_id_suffix_for(self.state),
            tracker=self.tracker.snapshot(), scheduler=self.scheduler_diagnostic_snapshot(),
            provider_events=list(self.provider.diagnostic_events),
        )

        before_hidden = self.provider.count()
        self.force_inactive_status = True
        with self.lease.lock:
            self.lease.last_seen = time.monotonic() - 13
        deadline = time.monotonic() + 14
        while time.monotonic() < deadline and self.lease.active():
            time.sleep(0.05)
        self.assertFalse(self.lease.active())
        self.record_diagnostic(
            "lease_expired",
            session_id_suffix=self.session_id_suffix_for(self.state),
            tracker=self.tracker.snapshot(), scheduler=self.scheduler_diagnostic_snapshot(),
            provider_events=list(self.provider.diagnostic_events),
        )
        self.assertEqual(self.provider.count(), before_hidden)
        self.force_inactive_status = False
        self.record_diagnostic("before_reactivation", scheduler=self.scheduler_diagnostic_snapshot())
        with self.sessions.lock:
            self.sessions.next_acquisition_at = 0
        page.evaluate("fetch('/status?active=1').then(response=>response.json())")
        self.record_diagnostic("after_reactivation_request", scheduler=self.scheduler_diagnostic_snapshot())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.sessions.initial_priority_streak == 0:
            time.sleep(0.025)
        with self.sessions.lock:
            self.sessions.next_acquisition_at = 0
        self.wait_for_calls(before_hidden + 1)

        count_before_close = self.provider.count()
        browser_context = page.context
        page.close()
        with self.lease.lock:
            self.lease.last_seen = time.monotonic() - 13
        with self.sessions.lock:
            self.sessions.next_acquisition_at = 0
        reopened = browser_context.new_page()
        self.install_status_diagnostics(reopened)
        self.install_page_lifecycle_diagnostics(reopened, "reopened")
        reopened.on("pageerror", lambda error: self.page_errors.append(str(error)))
        reopened.goto(self.base_url + "/", wait_until="domcontentloaded")
        self.wait_for_text(reopened, "#mode", lambda text: text.lower() == "live", timeout=10)
        self.bind_page_session(reopened)
        self.record_diagnostic(
            "reopened_session_bound",
            session_id_suffix=self.session_id_suffix_for(self.state),
            tracker=self.tracker.snapshot(),
            scheduler=self.scheduler_diagnostic_snapshot(),
            provider_events=list(self.provider.diagnostic_events),
        )
        self.wait_for_calls(count_before_close + 1)
        reopened.locator("#track-button").wait_for()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and "tracking_active" not in self.state.payload:
            time.sleep(0.025)
        self.record_diagnostic(
            "reopen_tracking_assertion",
            session_id_suffix=self.session_id_suffix_for(self.state),
            state_payload={key: self.state.payload.get(key) for key in (
                "mode", "tracking_active", "tracking_query", "tracking_resolved_callsign",
                "acquisition_status", "acquisition_retry_in_seconds",
            )},
            tracker=self.tracker.snapshot(), scheduler=self.scheduler_diagnostic_snapshot(),
            provider_events=list(self.provider.diagnostic_events),
        )
        stop_diagnostics.set()
        self.assertTrue(
            self.state.payload.get("tracking_active"),
            self.diagnostic_failure("returning session tracking state missing"),
        )
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
