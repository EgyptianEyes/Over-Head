#!/usr/bin/env python3
"""Serve Over-Head as a sharp, full-screen monitor display."""

from __future__ import annotations

import argparse
import csv
import copy
import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from http.cookies import SimpleCookie
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from xml.etree import ElementTree

from overhead import _retry_after_seconds
from route_failures import RouteFailureLog, configured_log
from overhead import AircraftFeedRateLimited, AircraftFeedUnavailable, AircraftPositionInvalid, AircraftPositionStale, DEMO, NORMAL_ACQUISITION_INTERVAL_SECONDS, Settings, aircraft_provider_status, fetch_provider_aircraft, fetch_aircraft, haversine_nm, load_settings, render, save_frame, select_nearest, validate_location


SOARING_INDEX_URL = (
    "https://raw.githubusercontent.com/soaring-symbols/soaring-symbols/"
    "main/airlines.json"
)
SOARING_ASSET_URL = (
    "https://raw.githubusercontent.com/soaring-symbols/soaring-symbols/"
    "main/assets/{slug}/{filename}"
)
SIMPLE_ICONS_URL = "https://cdn.simpleicons.org/{slug}"
JXCK_LOGO_URLS = (
    "https://raw.githubusercontent.com/Jxck-S/airline-logos/main/fr24_banners/{code}.png",
    "https://raw.githubusercontent.com/Jxck-S/airline-logos/main/radarbox_banners/{code}.png",
    "https://raw.githubusercontent.com/Jxck-S/airline-logos/main/flightaware_logos/{code}.png",
)
HIGH_RES_LOGO_URL = (
    "https://raw.githubusercontent.com/sexym0nk3y/airline-logos/"
    "master/logos/{code}.png"
)
COUNTRY_FLAG_URL = (
    "https://purecatamphetamine.github.io/country-flag-icons/"
    "{ratio}/{code}.svg"
)
SQUARE_FLAG_COUNTRIES = frozenset({"CH", "VA"})
USER_AGENT = "Over-Head/0.2 (+personal wall display)"
SECURITY_RESPONSE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(self), fullscreen=(self)",
    "Content-Security-Policy": "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; script-src 'self' 'unsafe-inline' https://unpkg.com; style-src 'self' 'unsafe-inline' https://unpkg.com; img-src 'self' data: blob: https://tile.openstreetmap.org https://unpkg.com; font-src 'self' data: https://unpkg.com; connect-src 'self'; media-src 'self'",
}
MAX_LOGO_BYTES = 1_000_000
MAX_AIRCRAFT_PHOTO_BYTES = 8_000_000
AIRCRAFT_PHOTO_RETRY_SECONDS = 300.0
AIRCRAFT_PHOTO_METADATA_RETRY_SECONDS = 6 * 60 * 60
HOUSE_GREY = "#6c9aac"
RADAR_RANGES = (5, 10, 15, 25, 40, 60, 100)
TRACK_RADAR_RANGES = (5, 10, 15, 25, 40, 60, 100, 150, 250, 400, 600, 1000, 1500, 2500, 4000, 6000, 10000)
MAX_TRACKING_CALLSIGN_CANDIDATES = 3
FLIGHTAWARE_AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi"
ADSLOL_ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
ADSLOL_ROUTE_URL = "https://api.adsb.lol/api/0/route/{callsign}/{latitude}/{longitude}"
ADSLOL_CALLSIGN_URL = "https://api.adsb.lol/v2/callsign/{callsign}"
ADSLOL_HEX_URL = "https://api.adsb.lol/v2/hex/{hex_code}"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
ROUTE_CACHE_TTL_SECONDS = 900
ROUTE_FAILURE_RETRY_SECONDS = 300
OURAIRPORTS_TTL_SECONDS = 7 * 24 * 60 * 60
API_HEALTH_INTERVAL_SECONDS = 30
METRIC_HISTORY_AGES_SECONDS = (30, 60, 90)
METRIC_HISTORY_STEP_SECONDS = 30
COMMENTARY_SAMPLE_WINDOW_SECONDS = 300
COMMENTARY_TREND_WINDOW_SECONDS = 35
COMMENTARY_MIN_TREND_SPAN_SECONDS = 8
COMMENTARY_MIN_TREND_CHANGE_NM = 0.12
COMMENTARY_DIRECT_OVERHEAD_NM = 0.05
COMMENTARY_ALMOST_OVERHEAD_NM = 0.25
COMMENTARY_ONE_STATUTE_MILE_NM = 1.0 / 1.150779448
COMMENTARY_PROJECTION_MAX_DISTANCE_NM = 100.0
COMMENTARY_PROJECTION_MAX_SECONDS = 600.0
COMMENTARY_PROJECTION_MIN_SPEED_KT = 30.0
COMMENTARY_PROJECTION_MAX_MISS_NM = 10.0
COMMENTARY_PROJECTION_TRACK_STABILITY_DEGREES = 8.0
COMMENTARY_PROJECTION_MOTION_TRACK_TOLERANCE_DEGREES = 15.0
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NEARBY_PLACE_MAX_DISTANCE_MILES = 1.0
NEARBY_PLACE_CELL_DEGREES = 0.01
NEARBY_PLACE_RETRY_SECONDS = 60.0
NEARBY_PLACE_REQUEST_INTERVAL_SECONDS = 1.1
ADSBDB_HEALTH_URL = "https://api.adsbdb.com/v0/aircraft/G-STBH"

PRECISE_MODEL_NAMES = {
    ("BOMBARDIER", "CHALLENGER 650"): "BOMBARDIER CL-600-2B16 CHALLENGER 650",
}

AIRCRAFT_NAMES = {
    "A306": "AIRBUS A300-600", "A319": "AIRBUS A319", "A320": "AIRBUS A320",
    "A321": "AIRBUS A321", "A20N": "AIRBUS A320NEO", "A21N": "AIRBUS A321NEO",
    "A332": "AIRBUS A330-200", "A333": "AIRBUS A330-300", "A339": "AIRBUS A330-900NEO",
    "A343": "AIRBUS A340-300", "A359": "AIRBUS A350-900", "A35K": "AIRBUS A350-1000",
    "A388": "AIRBUS A380-800", "AT43": "ATR 42-300", "AT45": "ATR 42-500",
    "AT72": "ATR 72-200", "AT75": "ATR 72-500", "AT76": "ATR 72-600",
    "B712": "BOEING 717-200", "B733": "BOEING 737-300", "B734": "BOEING 737-400",
    "B735": "BOEING 737-500", "B736": "BOEING 737-600", "B737": "BOEING 737-700",
    "B738": "BOEING 737-800", "B739": "BOEING 737-900", "B38M": "BOEING 737 MAX 8",
    "B39M": "BOEING 737 MAX 9", "B744": "BOEING 747-400", "B748": "BOEING 747-8",
    "B752": "BOEING 757-200", "B753": "BOEING 757-300",
    "B762": "BOEING 767-200", "B763": "BOEING 767-300",
    "B764": "BOEING 767-400ER", "B76F": "BOEING 767 FREIGHTER",
    "B772": "BOEING 777-200", "B77L": "BOEING 777-200LR", "B77W": "BOEING 777-300ER",
    "B788": "BOEING 787-8", "B789": "BOEING 787-9", "B78X": "BOEING 787-10",
    "C152": "CESSNA 152", "C172": "CESSNA 172", "C182": "CESSNA 182",
    "CL60": "BOMBARDIER CHALLENGER 600 SERIES",
    "CRJ7": "BOMBARDIER CRJ700", "CRJ9": "BOMBARDIER CRJ900", "DH8D": "DE HAVILLAND DASH 8-400",
    "E170": "EMBRAER E170", "E175": "EMBRAER E175", "E190": "EMBRAER E190",
    "E195": "EMBRAER E195", "E290": "EMBRAER E190-E2", "E295": "EMBRAER E195-E2",
    "P28A": "PIPER PA-28 CHEROKEE", "PC12": "PILATUS PC-12",
}


def aircraft_name(type_code: Any, description: Any = None) -> str:
    """Expand a common ICAO aircraft designator into a readable model name."""
    if isinstance(description, str) and description.strip():
        return description.strip().upper()
    code = str(type_code or "").strip().upper()
    return AIRCRAFT_NAMES.get(code, "MODEL NOT IDENTIFIED" if code else "AIRCRAFT NOT IDENTIFIED")


def operator_code(plane: dict[str, Any] | None) -> str:
    """Return a three-letter ICAO operator code when the callsign has one."""
    if not plane:
        return ""
    callsign = re.sub(r"[^A-Z0-9]", "", str(plane.get("flight") or "").upper())
    registration = re.sub(r"[^A-Z0-9]", "", str(plane.get("r") or "").upper())
    if len(callsign) < 4 or not callsign[:3].isalpha() or callsign == registration:
        return ""
    return callsign[:3]


def aircraft_identity(plane: dict[str, Any] | None) -> str:
    """Return a stable identity used to detect a change of tracked aircraft."""
    if not plane:
        return ""
    for field in ("hex", "r", "flight"):
        value = re.sub(r"[^A-Z0-9]", "", str(plane.get(field) or "").upper())
        if value:
            return value
    return ""


def normalise_flight_query(value: Any) -> str:
    """Normalise a user-entered commercial flight number or ATC callsign."""
    clean = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if not 3 <= len(clean) <= 10 or not any(ch.isalpha() for ch in clean) or not any(ch.isdigit() for ch in clean):
        raise ValueError("Enter a flight number such as BA703 or BAW703")
    return clean


def normalise_adsb_hex(value: Any) -> str:
    """Normalise an ADS-B ICAO or non-ICAO hex identifier."""
    raw = str(value or "").strip().upper()
    prefix = "~" if raw.startswith("~") else ""
    digits = re.sub(r"[^0-9A-F]", "", raw)
    return prefix + digits if len(digits) == 6 else ""


def tracking_distance_nm(plane: dict[str, Any] | None, settings) -> float | None:
    """Return a tracked aircraft's distance from home."""
    if not plane:
        return None
    lat, lon = plane.get("lat"), plane.get("lon")
    try:
        latitude, longitude = validate_location(lat, lon)
    except ValueError:
        return None
    return haversine_nm(settings.latitude, settings.longitude, latitude, longitude)


def automatic_tracking_radius(distance_nm: float | None) -> float:
    """Choose a stable home-centred radar range with useful headroom."""
    if distance_nm is None or not math.isfinite(float(distance_nm)):
        return 25.0
    required = max(5.0, float(distance_nm) * 1.18)
    for radius in TRACK_RADAR_RANGES:
        if radius >= required:
            return float(radius)
    return float(math.ceil(required / 1000.0) * 1000.0)


POSITION_FRESHNESS_SECONDS = 20.0
TRACKED_POSITION_MAX_REPORTED_AGE_SECONDS = 60.0
POSITION_RETENTION_MAX_SECONDS = 900.0


def aircraft_position_observed_at(plane: dict[str, Any] | None, received_at: float) -> float | None:
    if not plane:
        return None
    try:
        age = float(plane.get("seen_pos"))
        if plane.get("observed_at") is not None:
            age = max(age, time.time() - float(plane["observed_at"]))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(age) or age < 0 or age > TRACKED_POSITION_MAX_REPORTED_AGE_SECONDS:
        return None
    return max(0.0, float(received_at) - age)


def retained_position_seconds(refresh_seconds: float) -> float:
    try:
        interval = float(refresh_seconds)
    except (TypeError, ValueError):
        interval = 300.0
    if not math.isfinite(interval) or interval < 0:
        interval = 300.0
    interval = max(interval, GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS)
    return min(POSITION_RETENTION_MAX_SECONDS, max(120.0, interval * 2.0))


def metric_history_retention_seconds(refresh_seconds: float) -> float:
    try:
        interval = float(refresh_seconds)
    except (TypeError, ValueError):
        interval = GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS
    if not math.isfinite(interval) or interval < 0:
        interval = GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS
    interval = max(interval, GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS)
    return min(3600.0, max(90.0, interval * 3.0))


def age_aircraft_positions(aircraft: list[dict[str, Any]], elapsed_seconds: float) -> list[dict[str, Any]]:
    elapsed = max(0.0, float(elapsed_seconds))
    aged: list[dict[str, Any]] = []
    for plane in aircraft:
        if not isinstance(plane, dict):
            continue
        record = dict(plane)
        try:
            age = float(record.get("seen_pos", 999))
        except (TypeError, ValueError):
            age = 999.0
        record["seen_pos"] = age + elapsed if math.isfinite(age) and age >= 0 else 999.0
        aged.append(record)
    return aged


def aircraft_within_radius(aircraft: list[dict[str, Any]], settings, radius_nm: float) -> list[dict[str, Any]]:
    radius = min(250.0, max(1.0, float(radius_nm)))
    filtered: list[dict[str, Any]] = []
    for plane in aircraft:
        if not isinstance(plane, dict):
            continue
        try:
            latitude, longitude = validate_location(plane.get("lat"), plane.get("lon"))
        except ValueError:
            continue
        distance = haversine_nm(settings.latitude, settings.longitude, latitude, longitude)
        if distance < radius and not math.isclose(distance, radius, rel_tol=0.0, abs_tol=1e-9):
            filtered.append(plane)
    return filtered


def select_nearest_within_radius(
    aircraft: list[dict[str, Any]], settings, max_position_age_seconds: float,
) -> tuple[dict[str, Any] | None, float | None]:
    eligible = aircraft_within_radius(aircraft, settings, settings.radius_nm)
    return select_nearest(
        eligible, settings, min(float(max_position_age_seconds), POSITION_FRESHNESS_SECONDS),
    )


def remove_position_data(plane: dict[str, Any]) -> dict[str, Any]:
    result = dict(plane)
    for field in (
        "lat", "lon", "track", "true_heading", "mag_heading", "alt_baro", "alt_geom",
        "gs", "ias", "tas", "mach", "baro_rate", "geom_rate", "track_rate", "roll",
        "wind_speed", "wind_dir", "ws", "wd", "oat", "tat", "squawk", "emergency",
        "seen", "seen_pos",
    ):
        result[field] = None
    result.pop("nav_modes", None)
    return result


def merge_tracked_aircraft(aircraft: list[dict[str, Any]], tracked: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Ensure an explicitly tracked aircraft is present exactly once."""
    if not tracked:
        return list(aircraft)
    tracked_id = aircraft_identity(tracked)
    merged = [plane for plane in aircraft if not tracked_id or aircraft_identity(plane) != tracked_id]
    merged.append(tracked)
    return merged


def fetch_tracked_aircraft(callsigns: tuple[str, ...] | list[str]) -> tuple[dict[str, Any] | None, str]:
    """Resolve an explicit tracking target by exact live ADS-B callsign or ICAO hex."""
    stale_position = False
    for callsign in callsigns:
        raw_candidate = str(callsign or "").strip()
        target_hex = ""
        if raw_candidate.lower().startswith("hex:"):
            target_hex = normalise_adsb_hex(raw_candidate[4:])
            if not target_hex:
                continue
            candidate = target_hex
            url = ADSLOL_HEX_URL.format(hex_code=quote(target_hex, safe=""))
        else:
            candidate = normalise_flight_query(raw_candidate)
            url = ADSLOL_CALLSIGN_URL.format(callsign=quote(candidate, safe=""))
        # Tracking lookups use the same provider cooldowns as the local area feed.
        path = urlsplit(url).path
        planes = fetch_provider_aircraft(path)
        fresh: list[tuple[float, dict[str, Any]]] = []
        for plane in planes if isinstance(planes, list) else []:
            if not isinstance(plane, dict):
                continue
            if target_hex:
                plane_hex = normalise_adsb_hex(plane.get("hex"))
                if plane_hex != target_hex:
                    continue
            else:
                flight = re.sub(r"[^A-Z0-9]", "", str(plane.get("flight") or "").upper())
                if flight != candidate:
                    continue
            lat, lon = plane.get("lat"), plane.get("lon")
            try:
                validate_location(lat, lon)
            except ValueError:
                continue
            try:
                age = float(plane.get("seen_pos", plane.get("seen", 999)))
            except (TypeError, ValueError):
                age = 999.0
            if math.isfinite(age) and 0 <= age <= 60:
                fresh.append((age, plane))
            elif math.isfinite(age) and age > 60:
                stale_position = True
        if fresh:
            identities = {normalise_adsb_hex(record.get("hex")) for _, record in fresh}
            if len(identities) != 1 or not next(iter(identities)):
                continue
            _, plane = min(fresh, key=lambda item: item[0])
            resolved = re.sub(r"[^A-Z0-9]", "", str(plane.get("flight") or "").upper()) or candidate
            return plane, resolved
    if stale_position:
        raise AircraftPositionStale("Only stale aircraft positions were returned")
    return None, ""


class FlightTrackingState:
    """Thread-safe persistent flight lock for the live display."""

    def __init__(self, manual_radius: float = 25.0) -> None:
        self.lock = threading.Lock()
        self.active = False
        self.query = ""
        self.candidates: tuple[str, ...] = ()
        self.resolved_callsign = ""
        self.last_plane: dict[str, Any] | None = None
        self.last_position_observed_at: float | None = None
        self.manual_radius = float(manual_radius)
        self.revision = 0

    def start(self, query: str, candidates: tuple[str, ...] | list[str], seed_plane: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.revision += 1
            self.active = True
            self.query = query
            self.candidates = tuple(candidates)
            self.resolved_callsign = re.sub(r"[^A-Z0-9]", "", str((seed_plane or {}).get("flight") or "").upper())
            self.last_plane = dict(seed_plane) if seed_plane else None
            self.last_position_observed_at = aircraft_position_observed_at(seed_plane, time.monotonic()) if seed_plane else None

    def stop(self) -> float:
        with self.lock:
            self.revision += 1
            self.active = False
            self.query = ""
            self.candidates = ()
            self.resolved_callsign = ""
            self.last_plane = None
            self.last_position_observed_at = None
            return self.manual_radius

    def set_manual_radius(self, radius: float) -> None:
        with self.lock:
            if not self.active:
                self.manual_radius = float(radius)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "active": self.active,
                "query": self.query,
                "candidates": self.candidates,
                "resolved_callsign": self.resolved_callsign,
                "last_plane": dict(self.last_plane) if self.last_plane else None,
                "last_position_observed_at": self.last_position_observed_at,
                "manual_radius": self.manual_radius,
                "revision": self.revision,
            }

    def remember(self, plane: dict[str, Any], resolved_callsign: str, observed_at: float | None = None) -> None:
        with self.lock:
            if not self.active:
                return
            self.last_plane = dict(plane)
            self.last_position_observed_at = (
                observed_at if observed_at is not None else aircraft_position_observed_at(plane, time.monotonic())
            )
            self.resolved_callsign = resolved_callsign

    def invalidate_position(self) -> None:
        with self.lock:
            self.last_plane = None
            self.last_position_observed_at = None
            self.resolved_callsign = ""


def flight_details(plane: dict[str, Any] | None, registration: str = "") -> str:
    """Return callsign and registration without repeating equivalent values."""
    if not plane:
        return ""
    values = (str(plane.get("flight") or "").strip(), registration or str(plane.get("r") or "").strip())
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalised = re.sub(r"[^A-Z0-9]", "", value.upper())
        if value and normalised and normalised not in seen:
            unique.append(value)
            seen.add(normalised)
    return "  \u00b7  ".join(unique)


def safe_svg(data: bytes) -> bytes | None:
    """Reject active or externally-referencing SVG content before serving it."""
    if len(data) > MAX_LOGO_BYTES:
        return None
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject", "iframe", "object", "embed"}:
            return None
        for name, value in node.attrib.items():
            attr = name.rsplit("}", 1)[-1].lower()
            if attr.startswith("on"):
                return None
            if attr == "href" and value and not value.startswith("#"):
                return None
    return data


def radar_contacts(aircraft: list[dict[str, Any]], settings, selected: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Project current aircraft positions onto a north-up circular radar."""
    selected_hex = str((selected or {}).get("hex") or "")
    latitude_scale = 60.0405
    longitude_scale = latitude_scale * math.cos(math.radians(settings.latitude))
    contacts: list[dict[str, Any]] = []
    for plane in aircraft_within_radius(aircraft, settings, settings.radius_nm):
        if not isinstance(plane, dict):
            continue
        lat, lon = plane.get("lat"), plane.get("lon")
        try:
            latitude, longitude = validate_location(lat, lon)
        except ValueError:
            continue
        try:
            position_age = float(plane.get("seen_pos", 999))
            if not math.isfinite(position_age) or position_age < 0 or position_age > POSITION_FRESHNESS_SECONDS:
                continue
        except (TypeError, ValueError):
            continue
        east = (longitude - settings.longitude) * longitude_scale
        north = (latitude - settings.latitude) * latitude_scale
        distance = math.hypot(east, north)
        flight = str(plane.get("flight") or "").strip()
        callsign = flight or str(plane.get("r") or plane.get("hex") or "").strip()
        hex_code = normalise_adsb_hex(plane.get("hex"))
        contacts.append({
            "x": round(50 + east / settings.radius_nm * 46, 2),
            "y": round(50 - north / settings.radius_nm * 46, 2),
            "flight": flight,
            "callsign": callsign,
            "aircraft_hex": hex_code,
            "track": plane.get("track") or 0,
            "altitude": plane.get("alt_baro"),
            "position_age_seconds": position_age,
            "selected": bool(selected_hex and str(plane.get("hex") or "") == selected_hex),
            "emergency": str(plane.get("squawk") or "") in {"7500", "7600", "7700"},
        })
    return contacts


class APIHealthMonitor:
    """Lightweight heartbeat for the public data services used by Over-Head."""

    SERVICES = ("ADSB.lol", "ADSBDB", "OurAirports")

    def __init__(self, settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "ok": False,
            "checked_at": 0,
            "services": {service: False for service in self.SERVICES},
        }

    @staticmethod
    def _open_ok(request: urllib.request.Request, timeout: float = 5.0) -> bool:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                # Consume only a small response body when one is returned.
                response.read(256)
                return 200 <= status < 400
        except urllib.error.HTTPError as exc:
            # A 404 still proves that the service and API endpoint are answering.
            return exc.code == 404
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return False

    def _probe_adsblol(self) -> bool:
        url = (
            "https://api.adsb.lol/v2/point/"
            f"{float(self.settings.latitude):.6f}/{float(self.settings.longitude):.6f}/1"
        )
        return self._open_ok(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}))

    def _probe_adsbdb(self) -> bool:
        request = urllib.request.Request(ADSBDB_HEALTH_URL, headers={"User-Agent": USER_AGENT})
        return self._open_ok(request)

    def _probe_ourairports(self) -> bool:
        request = urllib.request.Request(
            OURAIRPORTS_URL,
            headers={"User-Agent": USER_AGENT},
            method="HEAD",
        )
        return self._open_ok(request)

    def check(self) -> dict[str, Any]:
        probes = {
            "ADSB.lol": self._probe_adsblol,
            "ADSBDB": self._probe_adsbdb,
            "OurAirports": self._probe_ourairports,
        }
        services = {name: bool(probe()) for name, probe in probes.items()}
        snapshot = {
            "ok": all(services.values()),
            "checked_at": int(time.time()),
            "services": services,
        }
        with self.lock:
            self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "ok": bool(self._snapshot.get("ok")),
                "checked_at": int(self._snapshot.get("checked_at") or 0),
                "services": dict(self._snapshot.get("services") or {}),
            }


def api_health_loop(health: APIHealthMonitor, refresh_event: threading.Event) -> None:
    while True:
        try:
            health.check()
        except Exception:
            # A heartbeat failure must never bring down the monitor.
            with health.lock:
                health._snapshot = {
                    "ok": False,
                    "checked_at": int(time.time()),
                    "services": {service: False for service in health.SERVICES},
                }
        time.sleep(API_HEALTH_INTERVAL_SECONDS)



class LogoStore:
    """Resolve logos once, cache them locally, and remember missing operators."""

    def __init__(self, cache_dir: Path = Path("cache/logos-v5")) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = cache_dir / "soaring-airlines.json"
        self._index: dict[str, dict[str, Any]] | None = None
        self._missing: set[str] = set()
        self._lock = threading.Lock()

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=6) as response:
            if response.status != 200:
                raise OSError(f"logo response {response.status}")
            data = response.read(MAX_LOGO_BYTES + 1)
        if len(data) > MAX_LOGO_BYTES:
            raise ValueError("logo response too large")
        return data

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        raw: bytes
        try:
            raw = self.index_path.read_bytes()
            if time.time() - self.index_path.stat().st_mtime > 7 * 86400:
                raise OSError("stale index")
        except OSError:
            raw = self._download(SOARING_INDEX_URL)
            self.index_path.write_bytes(raw)
        records = json.loads(raw)
        self._index = {
            str(record.get("icao") or "").upper(): record
            for record in records
            if isinstance(record, dict) and record.get("icao")
        }
        return self._index

    def tracking_callsigns(self, value: Any) -> tuple[str, ...]:
        """Return likely ADS-B callsigns for an IATA flight number or ICAO callsign."""
        query = normalise_flight_query(value)
        candidates: list[str] = [query]
        match = re.fullmatch(r"([A-Z0-9]{2})([0-9][A-Z0-9]{0,7})", query)
        if match:
            iata, suffix = match.groups()
            try:
                records = list(self._load_index().values())
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
                records = []
            for record in records:
                entries = [record]
                subsidiaries = record.get("subsidiaries") if isinstance(record, dict) else None
                if isinstance(subsidiaries, list):
                    entries.extend(item for item in subsidiaries if isinstance(item, dict))
                for entry in entries:
                    if str(entry.get("iata") or "").upper() != iata:
                        continue
                    icao = re.sub(r"[^A-Z0-9]", "", str(entry.get("icao") or "").upper())
                    if icao:
                        candidates.insert(0, icao + suffix)
        unique = tuple(dict.fromkeys(candidates))
        mapped = tuple(candidate for candidate in unique if candidate != query)
        return mapped[:MAX_TRACKING_CALLSIGN_CANDIDATES - 1] + (query,)

    def _soaring(self, code: str) -> tuple[bytes, str, str] | None:
        record = self._load_index().get(code)
        if not record or not record.get("slug"):
            return None
        slug = str(record["slug"])
        try:
            data = safe_svg(self._download(SOARING_ASSET_URL.format(slug=slug, filename="logo.svg")))
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return None
        if data:
            return data, "image/svg+xml", "Soaring Symbols wordmark"
        return None

    @staticmethod
    def _simple_icon_slug(airline_name: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(airline_name or "").lower())

    def _simple_icons(self, airline_name: str) -> tuple[bytes, str, str] | None:
        slug = self._simple_icon_slug(airline_name)
        if not slug:
            return None
        try:
            data = safe_svg(self._download(SIMPLE_ICONS_URL.format(slug=slug)))
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return None
        if data:
            return data, "image/svg+xml", "Simple Icons"
        return None

    def _jxck(self, code: str) -> tuple[bytes, str, str] | None:
        for url in JXCK_LOGO_URLS:
            try:
                data = self._download(url.format(code=code))
            except (OSError, ValueError, urllib.error.URLError, TimeoutError):
                continue
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return data, "image/png", "Jxck-S airline-logos"
        return None

    def _high_resolution(self, code: str) -> tuple[bytes, str, str] | None:
        try:
            data = self._download(HIGH_RES_LOGO_URL.format(code=code))
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return None
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, "image/png", "sexym0nk3y airline-logos"
        return None

    def get(self, code: str, airline_name: str = "") -> tuple[bytes, str, str] | None:
        if not re.fullmatch(r"[A-Z]{3}", code):
            return None
        cache_key = f"{code}-{self._simple_icon_slug(airline_name) or 'unknown'}"
        if cache_key in self._missing:
            return None
        with self._lock:
            svg_path = self.cache_dir / f"{code}-wordmark.svg"
            if svg_path.is_file():
                return svg_path.read_bytes(), "image/svg+xml", "Vector cache"

            try:
                vector = self._soaring(code)
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
                vector = None

            if vector:
                data, content_type, source = vector
                if content_type != "image/svg+xml":
                    raise ValueError("prominent airline branding must be SVG")
                svg_path.write_bytes(data)
                return data, content_type, source

            # Never use a symbol-only or raster fallback in the prominent branding position.
            # The browser will show the full airline name instead.
            self._missing.add(cache_key)
            return None


class FlagStore:
    """Retrieve standardized 3:2 country flags as safe SVGs and cache them locally."""

    def __init__(self, cache_dir: Path = Path("cache/flags-v2")) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._missing: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _codepoints(country_code: str) -> str:
        return "-".join(f"{0x1F1E6 + ord(character) - ord('A'):x}" for character in country_code)

    def get(self, country_code: str) -> bytes | None:
        code = str(country_code or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", code) or code in self._missing:
            return None
        path = self.cache_dir / f"{code}.svg"
        with self._lock:
            try:
                return path.read_bytes()
            except OSError:
                pass
            ratio = "1x1" if code in SQUARE_FLAG_COUNTRIES else "3x2"
            request = urllib.request.Request(
                COUNTRY_FLAG_URL.format(ratio=ratio, code=code),
                headers={"User-Agent": USER_AGENT},
            )
            try:
                with urllib.request.urlopen(request, timeout=6) as response:
                    data = safe_svg(response.read(MAX_LOGO_BYTES + 1))
            except (OSError, ValueError, urllib.error.URLError, TimeoutError):
                data = None
            if not data:
                self._missing.add(code)
                return None
            path.write_bytes(data)
            return data


class AircraftIdentityStore:
    """Enrich tracked aircraft once and retain successful identities on disk."""

    def __init__(
        self,
        cache_path: Path = Path("cache/aircraft-identities.json"),
        photo_cache_dir: Path | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.photo_cache_dir = photo_cache_dir or (self.cache_path.parent / "aircraft-photos")
        self.photo_cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._photo_lock = threading.Lock()
        self._unavailable: set[str] = set()
        self._photo_retry_after: dict[str, float] = {}
        self._photo_download_retry_after: dict[str, float] = {}
        self._photo_inflight: set[str] = set()
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            self._records = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._records: dict[str, dict[str, str]] = {}

    @staticmethod
    def _keys(plane: dict[str, Any]) -> list[str]:
        keys = []
        registration = re.sub(r"[^A-Z0-9]", "", str(plane.get("r") or "").upper())
        mode_s = re.sub(r"[^A-F0-9]", "", str(plane.get("hex") or "").upper())
        if registration:
            keys.append(f"registration:{registration}")
        if mode_s:
            keys.append(f"mode_s:{mode_s}")
        return keys

    def _fetch(self, identifier: str) -> dict[str, Any]:
        url = f"https://api.adsbdb.com/v0/aircraft/{identifier}"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.load(response)
        aircraft = payload.get("response", {}).get("aircraft", {})
        return aircraft if isinstance(aircraft, dict) else {}

    @staticmethod
    def _photo_url(value: Any) -> str:
        """Accept only HTTPS aircraft photographs supplied by airport-data.com via ADSBDB."""
        url = str(value or "").strip()
        if not url:
            return ""
        try:
            parsed = urlsplit(url)
        except ValueError:
            return ""
        hostname = str(parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or not (hostname == "airport-data.com" or hostname.endswith(".airport-data.com")):
            return ""
        return url

    @staticmethod
    def _photo_content_type(data: bytes) -> str:
        """Return a safe browser image MIME type from the downloaded bytes."""
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _photo_cache_key(self, identity: dict[str, str], plane: dict[str, Any] | None, url: str) -> str:
        registration = re.sub(r"[^A-Z0-9]", "", str(identity.get("registration") or (plane or {}).get("r") or "").upper())
        mode_s = re.sub(r"[^A-F0-9]", "", str(identity.get("mode_s") or (plane or {}).get("hex") or "").upper())
        aircraft_key = registration or mode_s or "AIRCRAFT"
        return f"{aircraft_key}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"

    def _cached_photo(self, key: str) -> tuple[bytes, str, str] | None:
        path = self.photo_cache_dir / f"{key}.img"
        try:
            data = path.read_bytes()
        except OSError:
            return None
        content_type = self._photo_content_type(data)
        if not content_type or len(data) > MAX_AIRCRAFT_PHOTO_BYTES:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return data, content_type, hashlib.sha256(data).hexdigest()[:12]

    def _download_photo(self, key: str, url: str) -> None:
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://www.airport-data.com/",
                    "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5",
                },
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read(MAX_AIRCRAFT_PHOTO_BYTES + 1)
            content_type = self._photo_content_type(data)
            if not content_type or not data or len(data) > MAX_AIRCRAFT_PHOTO_BYTES:
                raise ValueError("unsupported aircraft photo")
            path = self.photo_cache_dir / f"{key}.img"
            temporary = path.with_suffix(".img.tmp")
            temporary.write_bytes(data)
            temporary.replace(path)
            with self._photo_lock:
                self._photo_download_retry_after.pop(key, None)
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            with self._photo_lock:
                self._photo_download_retry_after[key] = time.monotonic() + AIRCRAFT_PHOTO_RETRY_SECONDS
        finally:
            with self._photo_lock:
                self._photo_inflight.discard(key)

    def photo_asset(
        self, identity: dict[str, str] | None, plane: dict[str, Any] | None = None
    ) -> tuple[bytes, str, str] | None:
        """Return a locally cached aircraft photograph, downloading it asynchronously once."""
        identity = identity or {}
        url = str(identity.get("photo_url") or identity.get("photo_thumbnail_url") or "").strip()
        if not url:
            return None
        url = self._photo_url(url)
        if not url:
            return None
        key = self._photo_cache_key(identity, plane, url)
        cached = self._cached_photo(key)
        if cached is not None:
            return cached
        now = time.monotonic()
        with self._photo_lock:
            if key in self._photo_inflight or now < self._photo_download_retry_after.get(key, 0.0):
                return None
            self._photo_inflight.add(key)
        threading.Thread(target=self._download_photo, args=(key, url), daemon=True).start()
        return None

    def photo_debug(self, identity: dict[str, str] | None, plane: dict[str, Any] | None = None) -> dict[str, Any]:
        """Describe aircraft-photo metadata/cache state without triggering another download."""
        identity = identity or {}
        plane = plane or {}
        registration = str(identity.get("registration") or plane.get("r") or "").strip().upper()
        mode_s = str(identity.get("mode_s") or plane.get("hex") or "").strip().upper()
        full_url = str(identity.get("photo_url") or "").strip()
        thumb_url = str(identity.get("photo_thumbnail_url") or "").strip()
        selected_url = full_url or thumb_url
        metadata_checked_at = str(identity.get("photo_checked_at") or "")
        metadata_status = str(identity.get("_photo_metadata_status") or "")
        metadata_attempts = identity.get("_photo_metadata_attempts")
        result: dict[str, Any] = {
            "registration": registration,
            "mode_s": mode_s,
            "metadata_checked_at": metadata_checked_at,
            "metadata_status": metadata_status or ("checked" if metadata_checked_at else "not-checked"),
            "metadata_attempts": list(metadata_attempts) if isinstance(metadata_attempts, list) else [],
            "photo_url": full_url,
            "photo_thumbnail_url": thumb_url,
            "selected_url": selected_url,
            "cache_directory": str(self.photo_cache_dir),
            "cache_filename": "",
            "cache_bytes": 0,
            "content_type": "",
            "revision": "",
            "status": "no-photo-url" if metadata_checked_at else "metadata-not-checked",
            "download_inflight": False,
            "retry_in_seconds": 0,
        }
        if not selected_url:
            if metadata_status == "lookup-failed":
                result["status"] = "metadata-lookup-failed"
            elif metadata_status == "lookup-deferred":
                result["status"] = "metadata-retry-wait"
            return result
        safe_url = self._photo_url(selected_url)
        if not safe_url:
            result["status"] = "photo-url-rejected"
            return result
        key = self._photo_cache_key(identity, plane, safe_url)
        result["cache_filename"] = f"{key}.img"
        cached = self._cached_photo(key)
        if cached is not None:
            data, content_type, revision = cached
            result.update({
                "status": "cached",
                "cache_bytes": len(data),
                "content_type": content_type,
                "revision": revision,
            })
            return result
        now = time.monotonic()
        with self._photo_lock:
            inflight = key in self._photo_inflight
            retry_after = self._photo_download_retry_after.get(key, 0.0)
        result["download_inflight"] = inflight
        result["retry_in_seconds"] = max(0, int(math.ceil(retry_after - now))) if retry_after > now else 0
        result["status"] = "downloading" if inflight else ("download-retry-wait" if retry_after > now else "not-cached")
        return result

    @classmethod
    def _normalise(cls, record: dict[str, Any]) -> dict[str, str]:
        manufacturer = str(record.get("manufacturer") or "").strip()
        model = str(record.get("type") or "").strip()
        precise_name = PRECISE_MODEL_NAMES.get((manufacturer.upper(), model.upper()))
        if precise_name:
            name = precise_name
        elif manufacturer and model and not model.casefold().startswith(manufacturer.casefold()):
            name = f"{manufacturer} {model}"
        else:
            name = model or manufacturer
        return {
            "aircraft_name": name.upper(),
            "aircraft_type": str(record.get("icao_type") or "").strip().upper(),
            "registration": str(record.get("registration") or "").strip().upper(),
            "airline_name": str(record.get("registered_owner") or "").strip(),
            "operator_code": str(record.get("registered_owner_operator_flag_code") or "").strip().upper(),
            "mode_s": str(record.get("mode_s") or "").strip().upper(),
            "photo_url": cls._photo_url(record.get("url_photo")),
            "photo_thumbnail_url": cls._photo_url(record.get("url_photo_thumbnail")),
            "photo_checked_at": str(int(time.time())),
        }

    def _save(self) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)

    def get(self, plane: dict[str, Any] | None) -> dict[str, str]:
        if not plane:
            return {}
        keys = self._keys(plane)
        with self._lock:
            legacy_cached: dict[str, str] | None = None
            for key in keys:
                cached = self._records.get(key)
                if isinstance(cached, dict):
                    has_photo_fields = "photo_url" in cached and "photo_thumbnail_url" in cached
                    has_photo = bool(cached.get("photo_url") or cached.get("photo_thumbnail_url"))
                    try:
                        photo_checked_at = float(cached.get("photo_checked_at") or 0)
                    except (TypeError, ValueError):
                        photo_checked_at = 0.0
                    photo_check_fresh = photo_checked_at > 0 and time.time() - photo_checked_at < AIRCRAFT_PHOTO_METADATA_RETRY_SECONDS
                    if has_photo_fields and (has_photo or photo_check_fresh):
                        return cached
                    legacy_cached = cached

            if not keys:
                return {}

            now_monotonic = time.monotonic()
            attempts: list[dict[str, str]] = []
            deferred = True
            for lookup_key in keys:
                retry_after = self._photo_retry_after.get(lookup_key, 0.0)
                if legacy_cached is not None and now_monotonic < retry_after:
                    attempts.append({
                        "key": lookup_key,
                        "result": "retry-wait",
                        "retry_in_seconds": str(max(0, int(math.ceil(retry_after - now_monotonic)))),
                    })
                    continue
                deferred = False
                identifier = lookup_key.split(":", 1)[1]
                try:
                    fetched = self._fetch(identifier)
                except urllib.error.HTTPError as exc:
                    attempts.append({"key": lookup_key, "result": f"http-{exc.code}"})
                    self._photo_retry_after[lookup_key] = now_monotonic + (300.0 if exc.code >= 500 or exc.code == 429 else AIRCRAFT_PHOTO_METADATA_RETRY_SECONDS)
                    continue
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError) as exc:
                    attempts.append({"key": lookup_key, "result": type(exc).__name__})
                    self._photo_retry_after[lookup_key] = now_monotonic + 60.0
                    continue

                if not fetched:
                    attempts.append({"key": lookup_key, "result": "empty-response"})
                    self._photo_retry_after[lookup_key] = now_monotonic + 300.0
                    continue

                result = self._normalise(fetched)
                if not any(value for key, value in result.items() if key != "photo_checked_at"):
                    attempts.append({"key": lookup_key, "result": "empty-aircraft"})
                    self._photo_retry_after[lookup_key] = now_monotonic + 300.0
                    continue

                attempts.append({
                    "key": lookup_key,
                    "result": "photo-found" if result.get("photo_url") or result.get("photo_thumbnail_url") else "aircraft-found-no-photo",
                })
                for key in keys:
                    self._photo_retry_after.pop(key, None)
                if not (result.get("photo_url") or result.get("photo_thumbnail_url")):
                    for key in keys:
                        self._photo_retry_after[key] = now_monotonic + AIRCRAFT_PHOTO_METADATA_RETRY_SECONDS
                cache_keys = set(keys)
                if result["registration"]:
                    cache_keys.add(f"registration:{re.sub(r'[^A-Z0-9]', '', result['registration'])}")
                if result["mode_s"]:
                    cache_keys.add(f"mode_s:{re.sub(r'[^A-F0-9]', '', result['mode_s'])}")
                for key in cache_keys:
                    self._records[key] = result
                self._save()
                return result

            if legacy_cached is not None:
                result = dict(legacy_cached)
                result["_photo_metadata_status"] = "lookup-deferred" if deferred else "lookup-failed"
                result["_photo_metadata_attempts"] = attempts
                return result
            return {
                "_photo_metadata_status": "lookup-deferred" if deferred else "lookup-failed",
                "_photo_metadata_attempts": attempts,
            }



class AirportStore:
    """Resolve airport code metadata from the public-domain OurAirports dataset."""

    def __init__(
        self,
        cache_path: Path = Path("cache/ourairports-airports.csv"),
        ttl_seconds: int = OURAIRPORTS_TTL_SECONDS,
    ) -> None:
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] | None = None

    def _download(self) -> None:
        request = urllib.request.Request(OURAIRPORTS_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read()
        if len(data) < 100_000:
            raise ValueError("OurAirports dataset download was unexpectedly small")
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(self.cache_path)

    def _ensure_loaded(self) -> None:
        if self._records is not None:
            return
        with self._lock:
            if self._records is not None:
                return
            stale = (
                not self.cache_path.is_file()
                or time.time() - self.cache_path.stat().st_mtime > self.ttl_seconds
            )
            if stale:
                try:
                    self._download()
                except (OSError, ValueError, urllib.error.URLError, TimeoutError):
                    if not self.cache_path.is_file():
                        self._records = {}
                        return
            records: dict[str, dict[str, Any]] = {}
            try:
                with self.cache_path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        try:
                            latitude = float(row.get("latitude_deg") or "")
                            longitude = float(row.get("longitude_deg") or "")
                        except (TypeError, ValueError):
                            continue
                        if not (math.isfinite(latitude) and math.isfinite(longitude)):
                            continue
                        record = {
                            "country_iso_name": str(row.get("iso_country") or "").strip().upper(),
                            "latitude": latitude,
                            "longitude": longitude,
                            "iata_code": str(row.get("iata_code") or "").strip().upper(),
                            "icao_code": str(row.get("ident") or "").strip().upper(),
                            "name": str(row.get("name") or "").strip(),
                        }
                        for key in (
                            record["iata_code"],
                            record["icao_code"],
                            str(row.get("gps_code") or "").strip().upper(),
                        ):
                            if key:
                                records[key] = record
            except OSError:
                records = {}
            self._records = records

    def get(self, code: str) -> dict[str, Any]:
        code = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
        if not code:
            return {}
        self._ensure_loaded()
        return dict((self._records or {}).get(code, {}))


class FlightRouteStore:
    """Resolve current routes without trusting stale callsign mappings."""

    def __init__(
        self,
        cache_path: Path = Path("cache/flight-routes-v6.json"),
        ttl_seconds: int = ROUTE_CACHE_TTL_SECONDS,
        api_key: str = "",
        airports: AirportStore | None = None,
    ) -> None:
        self.cache_path = cache_path
        self.ttl_seconds = ttl_seconds
        self.api_key = str(api_key or "").strip()
        self.airports = airports or AirportStore()
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._failures: dict[str, float] = {}
        self._flightaware_disabled_until = 0.0
        self._adsblol_disabled_until = 0.0
        self._lookup_local = threading.local()
        self._debug: dict[str, Any] = {
            "decision": "No route lookup has run yet",
            "flightaware_configured": bool(self.api_key),
            "attempts": [],
        }
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            self._records = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._records: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _clean(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())

    @classmethod
    def callsign(cls, plane: dict[str, Any] | None) -> str:
        if not plane:
            return ""
        callsign = cls._clean(plane.get("flight"))
        registration = cls._clean(plane.get("r"))
        return callsign if callsign and callsign != registration else ""

    @classmethod
    def registration(cls, plane: dict[str, Any] | None) -> str:
        return cls._clean(plane.get("r")) if plane else ""

    @staticmethod
    def _alternative_callsign(callsign: str) -> str:
        match = re.fullmatch(r"([A-Z]+)(0[0-9]+)", callsign)
        if not match:
            return ""
        prefix, number = match.groups()
        return prefix + (number.lstrip("0") or "0")

    @classmethod
    def _cache_key(cls, plane: dict[str, Any] | None) -> str:
        return f"{cls.callsign(plane)}|{cls.registration(plane)}"

    @staticmethod
    def _coordinate(value: Any, minimum: float, maximum: float) -> float | None:
        try:
            coordinate = float(value)
        except (TypeError, ValueError):
            return None
        return coordinate if math.isfinite(coordinate) and minimum <= coordinate <= maximum else None

    @staticmethod
    def _plane_position(plane: dict[str, Any] | None) -> tuple[float, float] | None:
        if not plane:
            return None
        try:
            latitude = float(plane.get("lat"))
            longitude = float(plane.get("lon"))
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(latitude) and math.isfinite(longitude)):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude

    @staticmethod
    def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_nm = 3440.065
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))

    @classmethod
    def _plausibility(cls, route: dict[str, Any], plane: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
        position = cls._plane_position(plane)
        if position is None:
            return False, {"reason": "aircraft has no valid live position"}

        coordinates = (
            route.get("origin_latitude"),
            route.get("origin_longitude"),
            route.get("destination_latitude"),
            route.get("destination_longitude"),
        )
        if any(value is None for value in coordinates):
            return False, {"reason": "route has no complete airport coordinates"}

        lat, lon = position
        o_lat, o_lon, d_lat, d_lon = (float(value) for value in coordinates)
        direct = cls._haversine_nm(o_lat, o_lon, d_lat, d_lon)
        from_origin = cls._haversine_nm(o_lat, o_lon, lat, lon)
        to_destination = cls._haversine_nm(lat, lon, d_lat, d_lon)

        if direct < 5:
            nearest = min(from_origin, to_destination)
            accepted = nearest <= 80
            return accepted, {
                "reason": "near-airport route" if accepted else "aircraft too far from route airport",
                "direct_nm": round(direct, 1),
                "nearest_endpoint_nm": round(nearest, 1),
                "allowed_nm": 80,
            }

        # Triangle excess is zero on the great-circle path and rises as the
        # aircraft moves away from the origin-destination corridor.
        excess = max(0.0, from_origin + to_destination - direct)
        allowed = max(60.0, min(180.0, direct * 0.10))
        accepted = excess <= allowed

        # A low aircraft should normally be close to either its departure or
        # arrival airport. This gives us extra protection against a stale route
        # that happens to cross the aircraft's present position.
        try:
            altitude = float((plane or {}).get("alt_baro"))
        except (TypeError, ValueError):
            altitude = math.nan
        nearest_endpoint = min(from_origin, to_destination)
        if accepted and math.isfinite(altitude) and altitude < 10000 and nearest_endpoint > 120:
            accepted = False
            reason = "low aircraft too far from route endpoints"
        else:
            reason = "within route corridor" if accepted else "outside route corridor"

        return accepted, {
            "reason": reason,
            "direct_nm": round(direct, 1),
            "from_origin_nm": round(from_origin, 1),
            "to_destination_nm": round(to_destination, 1),
            "nearest_endpoint_nm": round(nearest_endpoint, 1),
            "route_excess_nm": round(excess, 1),
            "allowed_excess_nm": round(allowed, 1),
        }

    @classmethod
    def _route_is_plausible(cls, route: dict[str, Any], plane: dict[str, Any] | None) -> bool:
        return cls._plausibility(route, plane)[0]

    def _aeroapi(self, ident: str) -> list[dict[str, Any]]:
        if not self.api_key or time.time() < self._flightaware_disabled_until:
            return []
        url = (
            f"{FLIGHTAWARE_AEROAPI_URL}/flights/{quote(ident, safe='')}"
            f"?{urlencode({'max_pages': 1})}"
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "x-apikey": self.api_key},
        )
        event = {"callsign": ident}
        if not hasattr(self._lookup_local, "aeroapi_requests"):
            self._lookup_local.aeroapi_requests = []
        self._lookup_local.aeroapi_requests.append(event)
        try:
            self._lookup_local.request_count = getattr(self._lookup_local, "request_count", 0) + 1
            with urllib.request.urlopen(request, timeout=8) as response:
                self._lookup_local.http_status = getattr(response, "status", None)
                event["http_status"] = self._lookup_local.http_status
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            self._lookup_local.http_status = exc.code
            event["http_status"] = exc.code
            if exc.code in (401, 402, 403):
                self._flightaware_disabled_until = time.time() + 3600
            raise
        flights = payload.get("flights", [])
        return [flight for flight in flights if isinstance(flight, dict)] if isinstance(flights, list) else []

    @staticmethod
    def _epoch(value: Any) -> float | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        try:
            return parsed.timestamp()
        except (OverflowError, OSError):
            return None

    @classmethod
    def _candidate_score(
        cls,
        flight: dict[str, Any],
        plane: dict[str, Any] | None,
        callsign: str,
        registration: str,
    ) -> float:
        if flight.get("cancelled"):
            return -1_000
        origin = flight.get("origin")
        destination = flight.get("destination")
        if not isinstance(origin, dict) or not isinstance(destination, dict):
            return -1_000
        if not (origin.get("code") or origin.get("code_iata") or origin.get("code_icao")):
            return -1_000
        if not (destination.get("code") or destination.get("code_iata") or destination.get("code_icao")):
            return -1_000

        score = 0.0
        atc_ident = cls._clean(flight.get("atc_ident"))
        ident = cls._clean(flight.get("ident"))
        ident_icao = cls._clean(flight.get("ident_icao"))
        candidate_registration = cls._clean(flight.get("registration"))

        if callsign and atc_ident == callsign:
            score += 180
        if callsign and callsign in {ident, ident_icao}:
            score += 120
        if registration and candidate_registration == registration:
            score += 100

        actual_off = cls._epoch(flight.get("actual_off"))
        actual_on = cls._epoch(flight.get("actual_on"))
        actual_in = cls._epoch(flight.get("actual_in"))
        if actual_off and not actual_on and not actual_in:
            score += 80
        elif actual_off and actual_on:
            score -= 30

        position = cls._plane_position(plane)
        last = flight.get("last_position")
        if position and isinstance(last, dict):
            try:
                last_lat = float(last.get("latitude"))
                last_lon = float(last.get("longitude"))
            except (TypeError, ValueError):
                last_lat = last_lon = math.nan
            if math.isfinite(last_lat) and math.isfinite(last_lon):
                distance = cls._haversine_nm(position[0], position[1], last_lat, last_lon)
                if distance <= 15:
                    score += 140
                elif distance <= 60:
                    score += 100
                elif distance <= 150:
                    score += 50
                elif distance > 350:
                    score -= 150
            last_time = cls._epoch(last.get("timestamp"))
            if last_time:
                age = abs(time.time() - last_time)
                if age <= 15 * 60:
                    score += 50
                elif age <= 60 * 60:
                    score += 20

        return score

    def _select_flightaware_flight(self, plane: dict[str, Any] | None) -> dict[str, Any]:
        callsign = self.callsign(plane)
        registration = self.registration(plane)
        identifiers = []
        if registration:
            identifiers.append(registration)
        if callsign and callsign not in identifiers:
            identifiers.append(callsign)
        candidates: list[dict[str, Any]] = []
        for ident in identifiers:
            candidates.extend(self._aeroapi(ident))
            if any(
                self._candidate_score(flight, plane, callsign, registration) >= 230
                for flight in candidates
            ):
                break
        if not candidates:
            return {}
        ranked = sorted(
            candidates,
            key=lambda flight: self._candidate_score(flight, plane, callsign, registration),
            reverse=True,
        )
        winner = ranked[0]
        return winner if self._candidate_score(winner, plane, callsign, registration) >= 130 else {}

    @staticmethod
    def _airport_code(airport: dict[str, Any]) -> str:
        return str(
            airport.get("code_iata")
            or airport.get("code_icao")
            or airport.get("code")
            or ""
        ).strip().upper()

    def _normalise_flightaware(self, flight: dict[str, Any]) -> dict[str, Any]:
        origin = flight.get("origin") if isinstance(flight.get("origin"), dict) else {}
        destination = flight.get("destination") if isinstance(flight.get("destination"), dict) else {}
        origin_code = self._airport_code(origin)
        destination_code = self._airport_code(destination)
        if not origin_code or not destination_code:
            return {}

        origin_meta = self.airports.get(origin_code)
        destination_meta = self.airports.get(destination_code)
        return {
            "route": f"{origin_code} to {destination_code}",
            "origin": origin_code,
            "destination": destination_code,
            "origin_name": str(origin_meta.get("name") or "").strip(),
            "destination_name": str(destination_meta.get("name") or "").strip(),
            "origin_country": str(origin_meta.get("country_iso_name") or "").strip().upper(),
            "destination_country": str(destination_meta.get("country_iso_name") or "").strip().upper(),
            "origin_latitude": self._coordinate(origin_meta.get("latitude"), -90, 90),
            "origin_longitude": self._coordinate(origin_meta.get("longitude"), -180, 180),
            "destination_latitude": self._coordinate(destination_meta.get("latitude"), -90, 90),
            "destination_longitude": self._coordinate(destination_meta.get("longitude"), -180, 180),
            "commercial_flight": str(
                flight.get("ident_iata")
                or flight.get("ident_icao")
                or flight.get("ident")
                or ""
            ).strip().upper(),
            "atc_ident": str(flight.get("atc_ident") or "").strip().upper(),
            "route_source": "FlightAware AeroAPI",
            "cached_at": time.time(),
        }

    def _routeset(self, url: str, callsign: str, plane: dict[str, Any] | None) -> dict[str, Any]:
        position = self._plane_position(plane)
        if position is None:
            return {}
        latitude, longitude = position
        body = json.dumps({
            "planes": [{
                "callsign": callsign,
                "lat": latitude,
                "lng": longitude,
            }]
        }).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self._lookup_local.request_count = getattr(self._lookup_local, "request_count", 0) + 1
        with urllib.request.urlopen(request, timeout=6) as response:
            self._lookup_local.http_status = getattr(response, "status", None)
            body = response.read()
        if not body.strip():
            return {}
        payload = json.loads(body)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return payload if isinstance(payload, dict) else {}

    def _adsblol_routeset(self, callsign: str, plane: dict[str, Any] | None) -> dict[str, Any]:
        return self._routeset(ADSLOL_ROUTESET_URL, callsign, plane)

    def _adsblol(self, callsign: str, plane: dict[str, Any] | None) -> dict[str, Any]:
        position = self._plane_position(plane)
        if position is None:
            return {}
        latitude, longitude = position
        url = ADSLOL_ROUTE_URL.format(
            callsign=quote(callsign, safe=""),
            latitude=f"{latitude:.6f}",
            longitude=f"{longitude:.6f}",
        )
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        self._lookup_local.request_count = getattr(self._lookup_local, "request_count", 0) + 1
        with urllib.request.urlopen(request, timeout=6) as response:
            self._lookup_local.http_status = getattr(response, "status", None)
            payload = json.load(response)
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _vrs_airport_code(airport: dict[str, Any]) -> str:
        return str(airport.get("iata") or airport.get("icao") or "").strip().upper()

    def _normalise_routeset(self, payload: dict[str, Any], source: str) -> dict[str, Any]:
        airports = payload.get("_airports")
        if not isinstance(airports, list) or len(airports) < 2:
            return {}
        origin = airports[0] if isinstance(airports[0], dict) else {}
        destination = airports[-1] if isinstance(airports[-1], dict) else {}
        origin_code = self._vrs_airport_code(origin)
        destination_code = self._vrs_airport_code(destination)
        if not origin_code or not destination_code:
            return {}
        number = str(payload.get("number") or "").strip()
        airline_code = str(payload.get("airline_code") or "").strip().upper()
        origin_meta = self.airports.get(origin_code)
        destination_meta = self.airports.get(destination_code)
        origin_latitude = self._coordinate(origin.get("lat"), -90, 90)
        origin_longitude = self._coordinate(origin.get("lon"), -180, 180)
        destination_latitude = self._coordinate(destination.get("lat"), -90, 90)
        destination_longitude = self._coordinate(destination.get("lon"), -180, 180)
        if origin_latitude is None:
            origin_latitude = self._coordinate(origin_meta.get("latitude"), -90, 90)
        if origin_longitude is None:
            origin_longitude = self._coordinate(origin_meta.get("longitude"), -180, 180)
        if destination_latitude is None:
            destination_latitude = self._coordinate(destination_meta.get("latitude"), -90, 90)
        if destination_longitude is None:
            destination_longitude = self._coordinate(destination_meta.get("longitude"), -180, 180)
        return {
            "route": f"{origin_code} to {destination_code}",
            "origin": origin_code,
            "destination": destination_code,
            "origin_name": str(origin_meta.get("name") or "").strip(),
            "destination_name": str(destination_meta.get("name") or "").strip(),
            "origin_country": str(origin.get("countryiso2") or origin_meta.get("country_iso_name") or "").strip().upper(),
            "destination_country": str(destination.get("countryiso2") or destination_meta.get("country_iso_name") or "").strip().upper(),
            "origin_latitude": origin_latitude,
            "origin_longitude": origin_longitude,
            "destination_latitude": destination_latitude,
            "destination_longitude": destination_longitude,
            "commercial_flight": f"{airline_code}{number}" if airline_code and number and number != "unknown" else "",
            "route_source": f"{source} + live validation",
            "provider_plausible": payload.get("plausible"),
            "cached_at": time.time(),
        }

    def _normalise_adsblol(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._normalise_routeset(payload, "ADSB.lol route")

    def _save(self) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)

    def _set_debug(self, debug: dict[str, Any]) -> None:
        started_at = debug.pop("_started_at", None)
        if started_at is not None:
            debug["elapsed_seconds"] = round(max(0.0, time.monotonic() - started_at), 3)
        for attempt in debug.get("attempts", []):
            if attempt.get("source") == "FlightAware AeroAPI" and attempt.get("requested"):
                attempt["requests"] = getattr(self._lookup_local, "aeroapi_requests", [])
        if any(attempt.get("requested") for attempt in debug.get("attempts", [])):
            debug["lookup_id"] = secrets.token_hex(16)
        self._debug = json.loads(json.dumps(debug, default=str))
        self._lookup_local.debug = self._debug

    def _current_lookup_debug(self) -> dict[str, Any]:
        return getattr(self._lookup_local, "debug", {})

    def debug_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._debug, default=str))

    def cached_route(self, plane: dict[str, Any] | None) -> dict[str, Any]:
        callsign = self.callsign(plane)
        debug: dict[str, Any] = {
            "callsign": callsign,
            "original_callsign": callsign,
            "alternative_callsign": self._alternative_callsign(callsign),
            "registration": self.registration(plane),
            "attempts": [],
            "_started_at": time.monotonic(),
        }
        if not callsign:
            debug["decision"] = "No usable ATC callsign"
            self._set_debug(debug)
            return {}
        key = self._cache_key(plane)
        with self._lock:
            cached = self._records.get(key)
            if not isinstance(cached, dict) or not cached.get("origin") or not cached.get("destination"):
                return {}
            plausible, detail = self._plausibility(cached, plane)
            age = time.time() - float(cached.get("cached_at", 0))
            debug["attempts"].append({
                "source": "cache",
                "callsign": callsign,
                "airport_information_returned": True,
                "geographical_validation_passed": plausible,
                "route": cached.get("route"),
                "route_source": cached.get("route_source"),
                "age_seconds": round(age, 1),
                "accepted": age < self.ttl_seconds and plausible,
                "validation": detail,
            })
            if age >= self.ttl_seconds or not plausible:
                debug["decision"] = "Cached route is expired or geographically implausible"
                self._set_debug(debug)
                return {}
            route = dict(cached)
        route["route"] = f"{route['origin']} to {route['destination']}"
        if not route.get("origin_name"):
            route["origin_name"] = str(self.airports.get(route["origin"]).get("name") or "").strip()
        if not route.get("destination_name"):
            route["destination_name"] = str(self.airports.get(route["destination"]).get("name") or "").strip()
        debug["decision"] = f"Accepted cached {route.get('route_source', 'route')}"
        self._set_debug(debug)
        return route

    def get(self, plane: dict[str, Any] | None) -> dict[str, Any]:
        self._lookup_local.request_count = 0
        self._lookup_local.http_status = None
        self._lookup_local.aeroapi_requests = []
        callsign = self.callsign(plane)
        registration = self.registration(plane)
        position = self._plane_position(plane)
        debug: dict[str, Any] = {
            "callsign": callsign,
            "original_callsign": callsign,
            "alternative_callsign": self._alternative_callsign(callsign),
            "registration": registration,
            "position": {"latitude": position[0], "longitude": position[1]} if position else None,
            "flightaware_configured": bool(self.api_key),
            "attempts": [],
            "_started_at": time.monotonic(),
        }
        if not callsign:
            debug["decision"] = "No usable ATC callsign"
            self._set_debug(debug)
            return {}

        key = self._cache_key(plane)
        with self._lock:
            cached = self._records.get(key)
            if isinstance(cached, dict) and cached.get("origin") and cached.get("destination"):
                plausible, detail = self._plausibility(cached, plane)
                age = time.time() - float(cached.get("cached_at", 0))
                debug["attempts"].append({
                    "source": "cache",
                    "callsign": callsign,
                    "airport_information_returned": True,
                    "geographical_validation_passed": plausible,
                    "route": cached.get("route"),
                    "route_source": cached.get("route_source"),
                    "age_seconds": round(age, 1),
                    "accepted": age < self.ttl_seconds and plausible,
                    "validation": detail,
                })
                if age < self.ttl_seconds and plausible:
                    cached["route"] = f"{cached['origin']} to {cached['destination']}"
                    if not cached.get("origin_name"):
                        cached["origin_name"] = str(self.airports.get(cached["origin"]).get("name") or "").strip()
                    if not cached.get("destination_name"):
                        cached["destination_name"] = str(self.airports.get(cached["destination"]).get("name") or "").strip()
                    debug["decision"] = f"Accepted cached {cached.get('route_source', 'route')}"
                    self._set_debug(debug)
                    return cached

            failed_at = self._failures.get(key, 0.0)
            if failed_at and time.time() - failed_at < ROUTE_FAILURE_RETRY_SECONDS:
                debug["decision"] = "Recent route lookup failed; waiting before retry"
                self._set_debug(debug)
                return {}

            if self.api_key:
                try:
                    flight = self._select_flightaware_flight(plane)
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                    flight = {}
                    debug["attempts"].append({
                        "source": "FlightAware AeroAPI", "callsign": callsign,
                        "airport_information_returned": False,
                        "geographical_validation_passed": None, "accepted": False,
                        "error": type(exc).__name__, "reason": "provider request failed",
                        "requested": bool(self._lookup_local.request_count),
                        "request_count": self._lookup_local.request_count,
                        "http_status": self._lookup_local.http_status,
                    })
                route = self._normalise_flightaware(flight) if flight else {}
                if route:
                    plausible, detail = self._plausibility(route, plane)
                    debug["attempts"].append({
                        "source": "FlightAware AeroAPI",
                        "callsign": callsign,
                        "airport_information_returned": True,
                        "geographical_validation_passed": plausible,
                        "route": route.get("route"),
                        "commercial_flight": route.get("commercial_flight"),
                        "accepted": plausible,
                        "validation": detail,
                        "reason": detail["reason"],
                        "requested": bool(self._lookup_local.request_count),
                        "request_count": self._lookup_local.request_count,
                        "http_status": self._lookup_local.http_status,
                    })
                    if plausible:
                        self._records[key] = route
                        self._save()
                        debug["decision"] = "Accepted FlightAware AeroAPI route"
                        self._set_debug(debug)
                        return route
                elif flight:
                    debug["attempts"].append({
                        "source": "FlightAware AeroAPI", "callsign": callsign,
                        "airport_information_returned": bool(flight.get("origin") or flight.get("destination")),
                        "geographical_validation_passed": None, "accepted": False,
                        "reason": "flight had no usable airport pair",
                        "requested": bool(self._lookup_local.request_count),
                        "request_count": self._lookup_local.request_count,
                        "http_status": self._lookup_local.http_status,
                    })
                elif not any(attempt.get("source") == "FlightAware AeroAPI" for attempt in debug["attempts"]):
                    debug["attempts"].append({
                        "source": "FlightAware AeroAPI", "callsign": callsign,
                        "requested": bool(self._lookup_local.request_count),
                        "request_count": self._lookup_local.request_count,
                        "http_status": self._lookup_local.http_status, "accepted": False,
                        "reason": "no matching flight returned",
                    })
            else:
                debug["attempts"].append({"source": "FlightAware AeroAPI", "skipped": True, "reason": "no API key configured"})

            free_route_attempts = (
                ("ADSB.lol routeset", self._adsblol_routeset),
                ("ADSB.lol route", self._adsblol),
            )
            lookup_callsigns = [callsign]
            if debug["alternative_callsign"]:
                lookup_callsigns.append(debug["alternative_callsign"])
            for lookup_callsign in lookup_callsigns:
                for source_name, lookup in free_route_attempts:
                    attempt: dict[str, Any] = {
                        "source": source_name,
                        "callsign": lookup_callsign,
                        "airport_information_returned": False,
                        "geographical_validation_passed": None,
                        "accepted": False,
                    }
                    debug["attempts"].append(attempt)
                    if time.time() < self._adsblol_disabled_until:
                        attempt.update(skipped=True, reason="ADSB.lol provider cooldown")
                        continue
                    try:
                        self._lookup_local.request_count = 0
                        self._lookup_local.http_status = None
                        raw = lookup(lookup_callsign, plane)
                    except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
                        attempt.update(error=type(exc).__name__, reason="provider request failed")
                        if isinstance(exc, urllib.error.HTTPError):
                            attempt["http_status"] = exc.code
                            retry = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                            if exc.code in (403, 429) or retry is not None:
                                minimum = 300 if exc.code == 403 else ROUTE_FAILURE_RETRY_SECONDS
                                self._adsblol_disabled_until = time.time() + max(minimum, retry or 0)
                        continue
                    finally:
                        attempt["requested"] = bool(self._lookup_local.request_count)
                        attempt["request_count"] = self._lookup_local.request_count
                        if self._lookup_local.http_status is not None:
                            attempt["http_status"] = self._lookup_local.http_status
                    if not raw:
                        attempt["reason"] = "provider returned no route data"
                        continue
                    route = self._normalise_routeset(raw, source_name)
                    airport_codes = (
                        raw.get("airport_codes"), raw.get("_airport_codes_iata"),
                    )
                    airports = raw.get("_airports")
                    attempt["airport_information_returned"] = bool(
                        any(str(value or "").strip().casefold() not in ("", "unknown", "n/a", "none")
                            for value in airport_codes)
                        or isinstance(airports, list) and any(
                            isinstance(airport, dict) and (airport.get("iata") or airport.get("icao"))
                            for airport in airports
                        )
                    )
                    attempt["raw"] = {
                        "airport_codes": raw.get("airport_codes"),
                        "airport_codes_iata": raw.get("_airport_codes_iata"),
                        "provider_plausible": raw.get("plausible"),
                        "airports": [
                            {field: str(airport.get(field) or "")[:128] for field in ("iata", "icao")}
                            for airport in raw.get("_airports", [])[:16] if isinstance(airport, dict)
                        ] if isinstance(raw.get("_airports"), list) else [],
                    }
                    if not route:
                        attempt["reason"] = f"{source_name} returned no usable airport pair"
                        continue
                    plausible, detail = self._plausibility(route, plane)
                    attempt.update(
                        route=route.get("route"), accepted=plausible,
                        geographical_validation_passed=plausible, validation=detail,
                        reason=detail["reason"],
                    )
                    if plausible:
                        self._records[key] = route
                        self._save()
                        debug["decision"] = f"Accepted {source_name} after independent live-position validation"
                        self._set_debug(debug)
                        return route

            self._failures[key] = time.time()
            debug["decision"] = "No trustworthy route. Display N/A to N/A."
            self._set_debug(debug)
            return {}


CONFIG_REQUIRED_PAGE = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Over-Head - Set Home Location</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    :root { color-scheme:dark; --bg:#02050a; --panel:#000207; --line:#123846; --cyan:#14ecff; --white:#e8f6ff; --muted:#6c9aac; --red:#ff3e52; }
    * { box-sizing:border-box; }
    html,body { width:100%; min-height:100%; margin:0; background:var(--bg); }
    body { padding:clamp(18px,3vw,42px); color:var(--white); font-family:"Segoe UI",Arial,sans-serif; background:radial-gradient(circle at 75% 35%,rgba(20,236,255,.07),transparent 28%),linear-gradient(145deg,#06121a 0%,var(--bg) 52%); }
    main { width:min(1100px,100%); margin:auto; padding:clamp(24px,4vw,52px); border:1px solid var(--line); border-radius:28px; background:rgba(0,2,7,.9); box-shadow:inset 0 0 60px rgba(20,236,255,.025),0 28px 80px rgba(0,0,0,.34); }
    h1 { margin:0; color:var(--cyan); font-size:clamp(38px,5.5vw,76px); letter-spacing:-.055em; line-height:.95; }
    h2 { margin:20px 0 8px; font-size:clamp(22px,2.5vw,34px); }
    p { margin:0 0 20px; color:var(--muted); font-size:clamp(14px,1.2vw,18px); line-height:1.5; }
    #setup-map { width:100%; height:clamp(330px,55vh,560px); border:1px solid var(--line); border-radius:20px; overflow:hidden; background:#06121a; }
    .leaflet-control-attribution { font-size:10px; }
    .leaflet-marker-icon.home-marker { border-radius:50%; background:var(--cyan); border:3px solid var(--panel); box-shadow:0 0 0 2px var(--cyan),0 0 18px rgba(20,236,255,.55); }
    .controls { margin-top:20px; display:grid; grid-template-columns:1fr 1fr auto auto; gap:12px; align-items:end; }
    label { display:grid; gap:7px; color:var(--muted); font-size:12px; font-weight:750; letter-spacing:.12em; }
    input { width:100%; padding:12px 14px; border:1px solid var(--line); border-radius:12px; outline:none; background:#061018; color:var(--white); font:650 16px/1.2 "Segoe UI",sans-serif; }
    input:focus { border-color:var(--cyan); box-shadow:0 0 0 2px rgba(20,236,255,.12); }
    button { min-height:46px; padding:0 18px; border:1px solid var(--line); border-radius:12px; cursor:pointer; background:rgba(20,236,255,.055); color:var(--cyan); font:750 13px/1 "Segoe UI",sans-serif; letter-spacing:.1em; }
    button:hover,button:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    button.primary { background:var(--cyan); color:#001018; border-color:var(--cyan); }
    button:disabled { opacity:.45; cursor:not-allowed; }
    #setup-status { min-height:22px; margin:14px 0 0; color:var(--muted); font-size:14px; }
    #setup-status.error { color:var(--red); }
    @media (max-width:800px) { .controls { grid-template-columns:1fr 1fr; } .controls button { width:100%; } }
  </style>
</head>
<body>
  <main>
    <h1>OVER-HEAD</h1>
    <h2>SET HOME LOCATION</h2>
    <p>LOCATION NOT CONFIGURED. No default location is used. Click the map, drag the marker, enter coordinates manually, or use your browser location. Live aircraft tracking remains disabled until you save.</p>
    <div id="setup-map" aria-label="Choose the home centre on a map"></div>
    <div class="controls">
      <label>LATITUDE<input id="latitude" type="number" min="-90" max="90" step="0.000001" inputmode="decimal" placeholder="Enter latitude"></label>
      <label>LONGITUDE<input id="longitude" type="number" min="-180" max="180" step="0.000001" inputmode="decimal" placeholder="Enter longitude"></label>
      <button id="use-location" type="button">USE MY LOCATION</button>
      <button id="save-location" class="primary" type="button" disabled>SAVE LOCATION</button>
    </div>
    <p id="setup-status">Select a point to continue.</p>
  </main>
  <script>
    const latInput=document.getElementById('latitude'),lonInput=document.getElementById('longitude'),saveButton=document.getElementById('save-location'),status=document.getElementById('setup-status');
    let map=null,marker=null;
    const valid=()=>{ const lat=Number(latInput.value),lon=Number(lonInput.value); return Number.isFinite(lat)&&Number.isFinite(lon)&&lat>=-90&&lat<=90&&lon>=-180&&lon<=180; };
    function setStatus(message,error=false){ status.textContent=message; status.classList.toggle('error',error); }
    function updateSave(){ saveButton.disabled=!valid(); }
    function setLocation(lat,lon,recenter=true){ lat=Number(lat); lon=Number(lon); if(!Number.isFinite(lat)||!Number.isFinite(lon)||lat<-90||lat>90||lon<-180||lon>180)return; latInput.value=lat.toFixed(6); lonInput.value=lon.toFixed(6); updateSave(); if(map){ if(!marker){ marker=L.marker([lat,lon],{draggable:true,icon:L.divIcon({className:'home-marker',html:'',iconSize:[18,18],iconAnchor:[9,9]})}).addTo(map); marker.on('dragend',()=>{ const point=marker.getLatLng(); setLocation(point.lat,point.lng,false); }); } else marker.setLatLng([lat,lon]); if(recenter)map.setView([lat,lon],Math.max(map.getZoom(),13)); } setStatus('HOME CENTRE: '+lat.toFixed(6)+', '+lon.toFixed(6)); }
    if(typeof L!=='undefined'){
      map=L.map('setup-map',{worldCopyJump:true}).setView([20,0],2);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'}).addTo(map);
      map.on('click',event=>setLocation(event.latlng.lat,event.latlng.lng,false));
    }else setStatus('Map library could not load. You can still enter latitude and longitude manually.',true);
    [latInput,lonInput].forEach(input=>{ input.addEventListener('input',updateSave); input.addEventListener('change',()=>{ if(valid())setLocation(latInput.value,lonInput.value,false); }); });
    document.getElementById('use-location').addEventListener('click',()=>{ if(!navigator.geolocation){ setStatus('Browser location is not available. Enter coordinates manually.',true); return; } setStatus('Finding your location...'); navigator.geolocation.getCurrentPosition(position=>setLocation(position.coords.latitude,position.coords.longitude,true),error=>setStatus('Could not get your browser location: '+error.message,true),{enableHighAccuracy:true,timeout:12000,maximumAge:60000}); });
    async function waitForLive(){ for(let attempt=0;attempt<120;attempt++){ await new Promise(resolve=>setTimeout(resolve,500)); try{ const response=await fetch('/status?active='+(document.visibilityState==='visible'?'1':'0')+'&t='+Date.now(),{cache:'no-store'}); if(response.ok){ location.replace('/'); return; } }catch(_){} } setStatus('Location saved, but the live display did not start. Restart Over-Head.',true); }
    saveButton.addEventListener('click',async()=>{ if(!valid())return; saveButton.disabled=true; setStatus('Saving location...'); try{ const response=await fetch('/setup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude:Number(latInput.value),longitude:Number(lonInput.value)})}); const data=await response.json(); if(!response.ok)throw new Error(data.error||'Could not save location'); setStatus('Location saved. Starting live display...'); waitForLive(); }catch(error){ setStatus(error.message||'Could not save location',true); updateSave(); } });
  </script>
</body>
</html>
"""


PAGE = b"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Over-Head &#x2708;&#xFE0F;</title>
  <meta name="overhead-ui-revision" content="commentary-hide-radar-v91">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    :root { color-scheme: dark; --bg:#02050a; --line:#123846; --cyan:#14ecff; --white:#e8f6ff; --muted:#6c9aac; --amber:#ffb020; --green:#54d889; --red:#ff3e52; }
    * { box-sizing: border-box; }
    html,body { width:100%; height:100%; margin:0; overflow:hidden; background:var(--bg); }
    body { color:var(--white); font-family:"Segoe UI",Arial,sans-serif; cursor:default; -webkit-font-smoothing:antialiased; text-rendering:geometricPrecision; }
    html.cursor-hidden,html.cursor-hidden * { cursor:none !important; }
    .wall { position:relative; width:100vw; height:100vh; padding:clamp(18px,2.4vw,52px); display:grid; grid-template-rows:auto 1fr auto; gap:clamp(14px,2vh,28px); background:radial-gradient(circle at 76% 48%,rgba(20,236,255,.075),transparent 28%),linear-gradient(145deg,#06121a 0%,var(--bg) 52%); }
    header,footer { display:flex; align-items:center; justify-content:space-between; }
    header { position:relative; z-index:1000; }
    .header-tools { display:flex; align-items:center; gap:clamp(14px,1.5vw,28px); transition:transform 220ms ease; }
    .wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-46px,-4.15vh,-34px)); }
    .wall.radar-open .header-tools { transform:translateY(calc(-15% - 7px)); }
    .wall.settings-open .header-tools { transform:translateY(calc(-15% - 7px)); }
    .radar-toggle { cursor:pointer; border:1px solid var(--line); border-radius:999px; padding:.62em 1.05em; background:rgba(20,236,255,.055); color:var(--muted); font:750 clamp(11px,1vw,18px)/1 "Segoe UI",sans-serif; letter-spacing:.14em; }
    .radar-toggle:hover,.radar-toggle:focus-visible,.wall.radar-open .radar-toggle { color:var(--cyan); border-color:rgba(20,236,255,.48); outline:none; }
    .sound-toggle,.commentary-toggle { width:clamp(38px,2.7vw,50px); height:clamp(38px,2.7vw,50px); padding:9px; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); }
    .sound-toggle { display:grid; }
    .commentary-toggle { display:none; }
    .wall.radar-open:not(.settings-open) .commentary-toggle { display:grid; }
    .sound-toggle:hover,.sound-toggle:focus-visible,.commentary-toggle:hover,.commentary-toggle:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .commentary-toggle.active { color:var(--green); border-color:rgba(84,216,137,.68); background:rgba(84,216,137,.10); }
    .commentary-toggle .commentary-slash { display:block; }
    .commentary-toggle.active .commentary-slash { display:none; }
    .sound-toggle svg,.commentary-toggle svg { width:100%; height:100%; }
    .sound-toggle .mute-slash { display:none; }
    .sound-toggle.muted { color:var(--muted); }
    .sound-toggle.muted .mute-slash { display:block; }
    .sound-toggle.blocked { color:var(--amber); border-color:rgba(255,176,32,.65); }
    .camera-toggle,.snapshots-toggle,.fullscreen-toggle,.settings-toggle { width:clamp(38px,2.7vw,50px); height:clamp(38px,2.7vw,50px); padding:9px; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); }
    .camera-toggle:disabled { color:#34525e; border-color:#18303a; cursor:default; opacity:.7; }
    .camera-toggle.capturing { color:var(--amber); border-color:rgba(255,176,32,.72); animation:snapshot-camera-pulse 1s ease-in-out infinite; }
    .camera-toggle.saved { color:var(--green); border-color:rgba(84,216,137,.72); }
    .camera-toggle.failed { color:var(--red); border-color:rgba(255,62,82,.75); }
    .camera-toggle:not(:disabled):hover,.camera-toggle:not(:disabled):focus-visible,.snapshots-toggle:hover,.snapshots-toggle:focus-visible,.fullscreen-toggle:hover,.fullscreen-toggle:focus-visible,.settings-toggle:hover,.settings-toggle:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .camera-toggle svg,.snapshots-toggle svg,.fullscreen-toggle svg,.settings-toggle svg { width:100%; height:100%; }
    .fullscreen-toggle .fullscreen-exit { display:none; }
    .fullscreen-toggle.active .fullscreen-enter { display:none; }
    .fullscreen-toggle.active .fullscreen-exit { display:block; }
    .snapshots-toggle.active,.settings-toggle.active { color:var(--amber); border-color:rgba(255,176,32,.65); background:rgba(255,176,32,.08); }
    @keyframes snapshot-camera-pulse { 0%,100% { box-shadow:0 0 0 rgba(255,176,32,0); } 50% { box-shadow:0 0 16px rgba(255,176,32,.28); } }
    .snapshot-flash { position:fixed; inset:0; z-index:7000; pointer-events:none; opacity:0; background:rgba(232,246,255,.92); }
    .snapshot-flash.active { animation:snapshot-flash 420ms ease-out 1; }
    @keyframes snapshot-flash { 0% { opacity:.82; } 100% { opacity:0; } }
    .snapshot-countdown { position:fixed; inset:0; z-index:7050; pointer-events:none; display:grid; place-items:center; opacity:0; background:rgba(2,5,10,.16); transition:opacity 140ms ease; }
    .snapshot-countdown.show { opacity:1; }
    .snapshot-countdown-panel { min-width:min(72vw,420px); padding:clamp(18px,2.4vw,32px) clamp(24px,3.2vw,44px); text-align:center; border:1px solid rgba(20,236,255,.24); border-radius:clamp(18px,2vw,28px); background:rgba(2,5,10,.74); box-shadow:0 18px 60px rgba(0,0,0,.34),inset 0 0 36px rgba(20,236,255,.035); backdrop-filter:blur(5px); }
    .snapshot-countdown-label { display:block; color:var(--muted); font:750 clamp(10px,1vw,16px)/1.2 "Segoe UI",sans-serif; letter-spacing:.18em; }
    .snapshot-countdown-number { display:block; min-height:1em; margin-top:.08em; color:var(--white); font:800 clamp(72px,12vw,180px)/.9 "Segoe UI",sans-serif; letter-spacing:-.04em; text-shadow:0 0 28px rgba(20,236,255,.24); }
    .snapshot-countdown-number.tick { animation:snapshot-countdown-tick 820ms ease-out 1; }
    .snapshot-countdown.working .snapshot-countdown-panel { min-width:min(82vw,680px); min-height:clamp(180px,22vh,260px); display:grid; place-items:center; border-color:rgba(255,176,32,.34); }
    .snapshot-countdown.working .snapshot-countdown-number { display:none; }
    .snapshot-countdown.working .snapshot-countdown-label { max-width:28ch; margin:0; color:var(--amber); font-size:clamp(30px,3.4vw,54px); font-weight:800; line-height:1.15; letter-spacing:.01em; text-transform:none; text-align:center; }
    .snapshot-countdown.working .snapshot-countdown-label.message-pulse { animation:snapshot-working-pulse 2s ease-out 1; }
    @keyframes snapshot-countdown-tick { 0% { opacity:.1; transform:scale(.72); } 26% { opacity:1; transform:scale(1.06); } 100% { opacity:.76; transform:scale(1); } }
    @keyframes snapshot-working-pulse { 0% { opacity:.58; transform:scale(.96); } 30% { opacity:1; transform:scale(1.035); } 100% { opacity:1; transform:scale(1); } }
    .snapshot-toast { position:fixed; z-index:7100; left:50%; bottom:clamp(44px,6vh,76px); transform:translateX(-50%) translateY(12px); opacity:0; pointer-events:none; padding:.65em 1em; border:1px solid rgba(20,236,255,.42); border-radius:999px; background:rgba(2,5,10,.92); color:var(--white); font:750 clamp(10px,.85vw,14px)/1 "Segoe UI",sans-serif; letter-spacing:.1em; transition:opacity 180ms ease,transform 180ms ease; }
    .snapshot-toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
    .snapshot-toast.error { color:var(--red); border-color:rgba(255,62,82,.6); }

    .brand { width:clamp(220px,24vw,440px); height:clamp(52px,4.4vw,82px); display:flex; align-items:center; justify-content:flex-start; }
    .brand-logo { display:block; visibility:hidden; max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; object-position:left center; transform:translateY(0px); filter:grayscale(0) saturate(1) brightness(1); opacity:1; transition:filter 900ms ease,opacity 900ms ease; }
    .brand-logo.ready { visibility:visible; }
    .wall.aircraft-active .brand-logo { filter:grayscale(1) saturate(0) brightness(.82) contrast(1.62); opacity:.82; }
    .wall.aircraft-active.brand-pulse .brand-logo { filter:grayscale(0) saturate(1.08) brightness(1.04) contrast(1.18); opacity:1; }
    .live { display:flex; align-items:center; gap:.7em; color:var(--muted); font-size:clamp(14px,1.45vw,26px); font-weight:700; line-height:1; letter-spacing:.16em; }
    .live #mode { display:block; line-height:1; transform:translateY(-.04em); }
    .live-dot { width:.62em; height:.62em; flex:0 0 auto; border-radius:50%; background:var(--cyan); box-shadow:0 0 1em var(--cyan); }
    .live.demo .live-dot { background:var(--amber); box-shadow:0 0 1em var(--amber); }
    .live.error .live-dot { background:var(--red); box-shadow:0 0 1em var(--red); }
    .live.rate_limited .live-dot, .live.provider_unavailable .live-dot, .live.reconnecting .live-dot { background:var(--amber); box-shadow:0 0 1em var(--amber); transform-origin:center; animation:reconnecting-led-pulse 1.25s ease-in-out infinite; }
    .live.last_known .live-dot, .live.position_unavailable .live-dot, .live.queued .live-dot { background:var(--amber); box-shadow:0 0 1em var(--amber); }
    @keyframes reconnecting-led-pulse { 0%,100% { transform:scale(.72); opacity:.72; box-shadow:0 0 .5em rgba(255,176,32,.52); } 50% { transform:scale(1.18); opacity:1; box-shadow:0 0 1.4em rgba(255,176,32,.98); } }
    .content { min-height:0; display:grid; grid-template-columns:minmax(0,1fr); gap:clamp(14px,1.3vw,24px); }
    .wall.radar-open .content,.wall.settings-open .content { grid-template-columns:minmax(0,1fr) clamp(320px,29vw,560px); }
    main { position:relative; min-height:0; border:1px solid var(--line); border-radius:clamp(16px,2vw,32px); background:rgba(0,2,7,.72); display:grid; grid-template-columns:1fr; overflow:visible; box-shadow:inset 0 0 60px rgba(20,236,255,.025),0 28px 80px rgba(0,0,0,.34); }
    .information { position:relative; min-width:0; padding:clamp(26px,4vw,76px); display:flex; flex-direction:column; justify-content:space-between; }
    .flight-heading { position:relative; min-width:0; display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:clamp(28px,3.4vw,64px); }
    .flight-copy { position:relative; z-index:2; min-width:0; }
    .callsign { overflow:visible; color:var(--white); font-size:clamp(64px,10.5vw,198px); font-weight:800; letter-spacing:-.065em; line-height:.88; white-space:nowrap; display:flex; align-items:center; gap:.12em; }
    .callsign.route-known { font-size:clamp(64px,7.2vw,138px); }
    .callsign.route-unknown { color:var(--muted); font-weight:400; letter-spacing:0; }
    .route-lookup-status { margin-top:clamp(7px,.8vh,12px); color:var(--muted); font-size:clamp(11px,.85vw,15px); font-weight:600; letter-spacing:.06em; line-height:1.2; }
    .route-lookup-status[hidden] { display:none; }
    .route-airport { width:1.32em; height:.88em; flex:0 0 1.32em; min-width:1.32em; min-height:.88em; max-width:1.32em; max-height:.88em; overflow:visible; filter:drop-shadow(0 0 8px rgba(108,154,172,.16)); }
    .route-airport-outline { fill:none; stroke:#6c9aac; stroke-width:2.1; stroke-linejoin:round; paint-order:stroke; vector-effect:non-scaling-stroke; }
    .route-join { color:var(--muted); font-size:.45em; font-weight:650; letter-spacing:0; }
    .destination-summary { min-height:1.25em; margin-top:clamp(10px,1.2vh,17px); color:var(--muted); font-size:clamp(17px,1.35vw,25px); font-weight:650; letter-spacing:.035em; line-height:1.2; }
    .destination-summary[hidden] { display:none; }
    .identity { margin-top:clamp(12px,1.45vh,23px); min-width:0; width:100%; display:flex; flex-direction:column; gap:.24em; }
    .registration { display:block; width:100%; min-width:0; overflow:hidden; white-space:nowrap; color:var(--muted); font-size:clamp(22px,2.5vw,44px); font-weight:700; letter-spacing:.1em; }
    .aircraft-name { color:var(--white); font-size:clamp(17px,1.55vw,28px); font-weight:650; letter-spacing:.065em; }
    .route-visual { position:absolute; top:clamp(-126px,-13.5vh,-112px); right:0; width:clamp(530px,29.7vw,575px); height:clamp(430px,51vh,460px); overflow:visible; z-index:0; pointer-events:none; }
    .route-brand { position:absolute; top:0; left:50%; transform:translateX(-50%); width:100%; height:clamp(92px,10.5vh,108px); display:flex; align-items:center; justify-content:center; overflow:hidden; pointer-events:none; z-index:20; background:linear-gradient(90deg,rgba(2,5,10,0) 0%,#02050a 5%,#02050a 95%,rgba(2,5,10,0) 100%); }
    .route-brand-name { display:none; max-width:92%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); font-size:clamp(34px,3.4vw,58px); font-weight:750; letter-spacing:.015em; line-height:1; text-align:center; }
    .route-brand-logo { display:none; width:100%; height:100%; object-fit:contain; transform-origin:center; filter:brightness(1.35) saturate(1.22) contrast(1.06) drop-shadow(0 0 14px rgba(108,154,172,.20)); }
    .route-map { display:none; position:absolute; top:clamp(90.882px,10.3275vh,99.144px); left:calc(50% + clamp(18px,1.2vw,24px)); transform:translateX(-60%); width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); overflow:hidden; border:0; border-radius:clamp(14px,1.5vw,22px); background:transparent; box-shadow:none; pointer-events:auto; z-index:0; }
    .route-map.visible { display:block; }
    .route-map::before { content:""; position:absolute; inset:0; z-index:250; pointer-events:none; border-radius:inherit; background:radial-gradient(ellipse at center,rgba(232,246,255,.28) 0%,rgba(108,154,172,.16) 28%,rgba(108,154,172,.075) 48%,rgba(108,154,172,0) 70%); mix-blend-mode:screen; }
    .route-map::after { content:""; position:absolute; inset:-1px; z-index:500; pointer-events:none; border-radius:inherit; background:
      linear-gradient(to right,#000207 0%,rgba(0,2,7,.96) 3%,rgba(0,2,7,.72) 12%,rgba(0,2,7,.30) 24%,rgba(0,2,7,0) 38%,rgba(2,5,10,0) 69%,rgba(2,5,10,.28) 81%,rgba(2,5,10,.68) 90%,rgba(2,5,10,.94) 97%,#02050a 100%),
      linear-gradient(to bottom,#02050a 0%,rgba(2,5,10,.90) 3%,rgba(2,5,10,.56) 11%,rgba(2,5,10,.22) 20%,rgba(2,5,10,0) 31%,rgba(2,5,10,0) 69%,rgba(2,5,10,.22) 80%,rgba(2,5,10,.56) 89%,rgba(2,5,10,.90) 97%,#02050a 100%); }
    .route-map.leaflet-container { background:transparent; outline:0; border:0; box-shadow:none; cursor:default; }

    .route-map .leaflet-tile-pane { opacity:.78; filter:grayscale(1) sepia(.14) hue-rotate(132deg) saturate(.58) brightness(.62) contrast(1.62); }
    .route-map .leaflet-tooltip.route-label { padding:2px 5px; border:1px solid rgba(108,154,172,.5); border-radius:5px; background:rgba(2,5,10,.82); box-shadow:none; color:#e8f6ff; font:750 10px/1 "Segoe UI",sans-serif; letter-spacing:.06em; }
    .route-map .leaflet-tooltip.route-label::before { display:none; }
    .route-map.photo-active { background:#02050a; }
    .route-map.photo-active > :not(.route-photo-panel),.route-map.photo-active::before,.route-map.photo-active::after { visibility:hidden; }
    .route-photo-panel { position:absolute; inset:0; z-index:900; overflow:hidden; background:transparent; pointer-events:none; }
    .route-photo-panel[hidden] { display:none; }
    .route-photo-panel img { position:absolute; top:38%; right:0; display:block; width:44%; height:62%; object-fit:contain; object-position:right center; filter:saturate(1.06) brightness(1.03) contrast(1.03); mask-image:linear-gradient(to right,transparent 0%,#000 24%,#000 82%,transparent 100%),linear-gradient(to bottom,transparent 0%,#000 24%,#000 76%,transparent 100%); mask-composite:intersect; }
    .aircraft-photo-credit { position:absolute; right:8px; bottom:8px; z-index:2; padding:.36em .55em; border-radius:5px; background:rgba(2,5,10,.62); color:rgba(232,246,255,.90); font:700 clamp(6px,.48vw,8px)/1.1 "Segoe UI",sans-serif; letter-spacing:.07em; }
    .route-direction-flow { pointer-events:none; animation:route-flow 10s linear infinite; filter:drop-shadow(0 0 4px rgba(232,246,255,.95)) drop-shadow(0 0 8px rgba(20,236,255,.7)); }
    .snapshot-render .route-direction-flow { animation:none; }
    @keyframes route-flow { from { stroke-dashoffset:100; } to { stroke-dashoffset:0; } }
    .route-aircraft-icon { background:none!important; border:0!important; }
    .route-aircraft-icon svg { width:24px; height:24px; overflow:visible; filter:drop-shadow(0 0 5px rgba(20,236,255,.9)); }
    .metrics { position:relative; z-index:2; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:clamp(12px,2vw,30px); margin-top:clamp(24px,5vh,70px); }
    .metric { min-width:0; padding-top:clamp(12px,2vh,24px); border-top:1px solid var(--line); }
    .metric-label { color:var(--muted); font-size:clamp(11px,1vw,18px); font-weight:700; letter-spacing:.18em; }
    .metric-value { margin-top:.18em; color:var(--white); font-size:clamp(27px,3.5vw,64px); font-weight:750; letter-spacing:-.035em; white-space:nowrap; }
    .metric:first-child .metric-value { color:var(--white); }
    .metric-value.altitude-low { color:var(--red) !important; }
    .metric-value.altitude-mid { color:var(--amber) !important; }
    .metric-value.altitude-high { color:var(--green) !important; }
    .metric-history { margin-top:clamp(7px,.8vh,12px); display:grid; gap:clamp(2px,.28vh,4px); color:var(--muted); font-size:clamp(9px,.7vw,13px); font-weight:650; line-height:1.18; }
    .metric-history-row { min-width:0; display:grid; grid-template-columns:3.2em minmax(0,1fr); align-items:baseline; gap:.6em; white-space:nowrap; }
    .metric-history-age { color:rgba(108,154,172,.66); font-size:.88em; font-weight:750; letter-spacing:.10em; text-transform:lowercase; }
    .metric-history-value { min-width:0; overflow:hidden; text-overflow:ellipsis; color:rgba(108,154,172,.94); letter-spacing:.025em; }
    .radar-panel { display:none; min-width:0; min-height:0; padding:clamp(18px,1.8vw,30px); border:1px solid var(--line); border-radius:clamp(16px,2vw,32px); background:rgba(0,2,7,.78); grid-template-rows:auto minmax(0,1fr) auto; overflow:hidden; box-shadow:inset 0 0 60px rgba(20,236,255,.025),0 28px 80px rgba(0,0,0,.28); }
    .wall.radar-open .radar-panel { display:grid; }
    .wall.settings-open .radar-panel { display:none; }
    .settings-panel { display:none; min-width:0; min-height:0; padding:clamp(18px,1.8vw,30px); border:1px solid var(--line); border-radius:clamp(16px,2vw,32px); background:rgba(0,2,7,.78); grid-template-rows:auto 1fr auto; overflow:hidden; box-shadow:inset 0 0 60px rgba(20,236,255,.025),0 28px 80px rgba(0,0,0,.28); }
    .wall.settings-open .settings-panel { display:grid; }
    .settings-heading { display:flex; align-items:end; justify-content:space-between; }
    .settings-heading strong { color:var(--white); font-size:clamp(18px,1.5vw,28px); letter-spacing:.08em; }
    .settings-heading span { color:var(--muted); font-size:clamp(10px,.82vw,14px); font-weight:700; letter-spacing:.14em; }
    .settings-list { align-self:center; display:grid; gap:clamp(14px,2.2vh,24px); }
    .setting-row { display:grid; gap:.55em; padding:clamp(13px,1.7vh,19px) 0; border-top:1px solid var(--line); }
    .setting-row:last-child { border-bottom:1px solid var(--line); }
    .setting-label { color:var(--muted); font-size:clamp(10px,.88vw,15px); font-weight:700; letter-spacing:.16em; }
    .setting-control { display:flex; align-items:center; justify-content:space-between; gap:12px; color:var(--white); font-size:clamp(16px,1.35vw,24px); font-weight:700; }
    .setting-action { cursor:pointer; border:1px solid var(--line); border-radius:999px; padding:.6em 1em; background:rgba(20,236,255,.055); color:var(--cyan); font:750 clamp(10px,.9vw,15px)/1 "Segoe UI",sans-serif; letter-spacing:.12em; }
    .setting-action:hover,.setting-action:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .setting-select { min-width:clamp(142px,11vw,190px); cursor:pointer; border:1px solid var(--line); border-radius:999px; padding:.55em 2.2em .55em .9em; background:#061017; color:var(--cyan); font:750 clamp(10px,.9vw,15px)/1.1 "Segoe UI",sans-serif; letter-spacing:.07em; color-scheme:dark; }
    .setting-select:hover,.setting-select:focus-visible { border-color:var(--cyan); outline:none; background:#081820; }
    .settings-location-value { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--white); font-size:clamp(13px,1vw,18px); letter-spacing:.02em; }
    .snapshot-backdrop { display:none; position:fixed; inset:0; z-index:5400; align-items:center; justify-content:center; padding:clamp(10px,1.5vw,22px); overflow:hidden; background:rgba(0,2,7,.90); backdrop-filter:blur(8px); }
    .snapshot-backdrop.open { display:flex; }
    .snapshot-dialog { position:relative; width:min(1240px,96vw); height:min(92vh,900px); min-height:0; padding:clamp(16px,1.8vw,26px); display:grid; grid-template-rows:auto minmax(0,1fr); gap:clamp(12px,1.4vh,18px); border:1px solid var(--line); border-radius:clamp(18px,2vw,30px); background:#02050a; box-shadow:0 35px 120px rgba(0,0,0,.72),inset 0 0 70px rgba(20,236,255,.025); }
    .snapshot-dialog-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; }
    .snapshot-dialog-head strong { color:var(--white); font-size:clamp(22px,2vw,34px); letter-spacing:.07em; }
    .snapshot-dialog-head p { margin:.35em 0 0; color:var(--muted); font-size:clamp(11px,.9vw,15px); line-height:1.4; }
    .snapshot-close { flex:0 0 auto; width:2.5em; height:2.5em; padding:0; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); font:700 20px/1 "Segoe UI",sans-serif; }
    .snapshot-close:hover,.snapshot-close:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .snapshot-grid { min-height:0; overflow:auto; display:grid; grid-template-columns:repeat(auto-fit,minmax(310px,1fr)); align-content:start; gap:14px; padding-right:4px; }
    .snapshot-empty { grid-column:1/-1; min-height:220px; display:grid; place-items:center; color:var(--muted); font:700 clamp(15px,1.3vw,21px)/1.4 "Segoe UI",sans-serif; letter-spacing:.06em; text-align:center; }
    .snapshot-card { min-width:0; padding:10px; border:1px solid var(--line); border-radius:14px; background:rgba(20,236,255,.025); }
    .snapshot-card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:10px; margin-bottom:8px; }
    .snapshot-card-title { min-width:0; }
    .snapshot-card-time { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--white); font-size:13px; font-weight:750; letter-spacing:.055em; }
    .snapshot-card-description { display:block; margin-top:3px; color:var(--muted); font-size:10px; line-height:1.35; letter-spacing:.045em; }
    .snapshot-card-detail { display:block; margin-top:2px; color:var(--muted); opacity:.8; font-size:9px; line-height:1.3; letter-spacing:.06em; }
    .snapshot-delete { flex:0 0 auto; padding:.48em .66em; cursor:pointer; border:1px solid rgba(255,62,82,.35); border-radius:999px; background:rgba(255,62,82,.04); color:#d46b78; font:750 9px/1 "Segoe UI",sans-serif; letter-spacing:.09em; }
    .snapshot-delete:hover,.snapshot-delete:focus-visible { color:var(--red); border-color:rgba(255,62,82,.7); outline:none; }
    .snapshot-pair { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; align-items:start; }
    .snapshot-variant { min-width:0; display:grid; gap:6px; }
    .snapshot-thumb { position:relative; min-width:0; height:96px; padding:0; overflow:hidden; cursor:pointer; border:1px solid rgba(108,154,172,.30); border-radius:9px; background:#000207; }
    .snapshot-thumb:hover,.snapshot-thumb:focus-visible { border-color:rgba(20,236,255,.65); outline:none; }
    .snapshot-thumb img { display:block; width:100%; height:100%; object-fit:contain; background:#000207; }
    .snapshot-thumb span { position:absolute; left:5px; bottom:5px; padding:.32em .5em; border-radius:999px; background:rgba(2,5,10,.88); color:var(--white); font:750 8px/1 "Segoe UI",sans-serif; letter-spacing:.09em; }
    .snapshot-actions { display:flex; gap:5px; }
    .snapshot-action { flex:1 1 0; min-width:0; padding:.55em .35em; cursor:pointer; text-align:center; text-decoration:none; border:1px solid rgba(108,154,172,.34); border-radius:999px; background:rgba(20,236,255,.035); color:var(--cyan); font:750 8px/1 "Segoe UI",sans-serif; letter-spacing:.08em; }
    .snapshot-action:hover,.snapshot-action:focus-visible { border-color:rgba(20,236,255,.72); background:rgba(20,236,255,.10); outline:none; }
    .snapshot-viewer { display:none; position:absolute; inset:0; z-index:2; grid-template-rows:auto minmax(0,1fr); gap:10px; padding:clamp(12px,1.5vw,20px); border-radius:inherit; background:#02050a; }
    .snapshot-viewer.open { display:grid; }
    .snapshot-viewer-head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
    .snapshot-viewer-title { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.08em; }
    .snapshot-viewer-stage { min-height:0; display:grid; place-items:center; overflow:auto; }
    .snapshot-viewer-stage img { display:block; max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; box-shadow:0 14px 50px rgba(0,0,0,.42); }
    .location-backdrop { display:none; position:fixed; inset:0; z-index:5000; align-items:center; justify-content:center; padding:clamp(10px,1.5vw,22px); overflow:hidden; background:rgba(0,2,7,.84); backdrop-filter:blur(8px); }
    .location-backdrop.open { display:flex; }
    .location-dialog { width:min(1060px,96vw); height:min(94vh,820px); min-height:0; overflow:hidden; padding:clamp(16px,1.8vw,26px); display:grid; grid-template-rows:auto minmax(220px,1fr) auto auto auto; gap:clamp(8px,1.2vh,14px); border:1px solid var(--line); border-radius:clamp(18px,2vw,30px); background:#02050a; box-shadow:0 35px 120px rgba(0,0,0,.65),inset 0 0 70px rgba(20,236,255,.025); }
    .location-dialog-head { display:flex; align-items:flex-start; justify-content:space-between; gap:18px; min-height:0; }
    .location-dialog-head strong { color:var(--white); font-size:clamp(22px,2vw,34px); letter-spacing:.07em; }
    .location-dialog-head p { margin:.35em 0 0; max-width:58em; color:var(--muted); font-size:clamp(11px,.9vw,15px); line-height:1.4; }
    .location-close { flex:0 0 auto; width:2.5em; height:2.5em; padding:0; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); font:700 20px/1 "Segoe UI",sans-serif; }
    .location-close:hover,.location-close:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    #location-map { width:100%; height:100%; min-height:0; border:1px solid var(--line); border-radius:18px; overflow:hidden; background:#02050a; }
    #location-map .leaflet-tile-pane { filter:grayscale(.15) brightness(.82) contrast(1.08); }
    .location-details { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; min-height:0; }
    .location-coordinate { padding:clamp(9px,1.2vh,12px) 14px; border:1px solid var(--line); border-radius:12px; background:rgba(20,236,255,.025); }
    .location-coordinate span { display:block; margin-bottom:4px; color:var(--muted); font-size:10px; font-weight:700; letter-spacing:.16em; }
    .location-coordinate input { display:block; width:100%; min-width:0; padding:0; border:0; outline:none; background:transparent; color:var(--white); font:700 clamp(15px,1.2vw,20px)/1.15 "Segoe UI",sans-serif; font-variant-numeric:tabular-nums; }
    .location-coordinate input:focus-visible { outline:1px solid var(--cyan); outline-offset:3px; }
    .location-status { min-height:1.35em; color:var(--muted); font-size:clamp(11px,.9vw,14px); }
    .location-status.error { color:var(--amber); }
    .location-actions { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:10px; min-height:0; }
    .location-primary { border-color:rgba(20,236,255,.55); background:rgba(20,236,255,.10); }
    #location-use-device.locating { color:var(--white); border-color:rgba(20,236,255,.58); background:rgba(20,236,255,.07); animation:location-button-pulse 1.6s ease-in-out infinite; cursor:wait; }
    #location-use-device.locating:disabled { opacity:1; }
    @keyframes location-button-pulse {
      0%,100% { border-color:rgba(20,236,255,.42); box-shadow:0 0 0 rgba(20,236,255,0); }
      50% { border-color:rgba(20,236,255,.9); box-shadow:0 0 12px rgba(20,236,255,.18); }
    }

    @media (max-height:700px) {
      .location-backdrop { padding:8px; }
      .location-dialog { width:min(1060px,98vw); height:calc(100vh - 16px); padding:12px 16px; grid-template-rows:auto minmax(170px,1fr) auto auto auto; gap:7px; }
      .location-dialog-head strong { font-size:clamp(19px,1.8vw,28px); }
      .location-dialog-head p { margin-top:.2em; font-size:11px; line-height:1.25; }
      .location-coordinate { padding:7px 12px; }
      .location-actions .setting-action { padding:.5em .85em; }
    }
    .settings-foot { color:#52717e; font-size:clamp(9px,.75vw,13px); font-weight:650; letter-spacing:.1em; }

    .radar-heading { display:flex; align-items:end; justify-content:space-between; }
    .radar-heading strong { color:var(--white); font-size:clamp(18px,1.5vw,28px); letter-spacing:.08em; }
    .radar-heading span,.radar-foot { color:var(--muted); font-size:clamp(10px,.82vw,14px); font-weight:700; letter-spacing:.14em; }
    .radar-stage { position:relative; min-height:0; display:grid; place-items:center; }
    #radar { width:min(100%,52vh); aspect-ratio:1; overflow:visible; }

    /* The radar, commentary and photograph occupy one rotating display stage. */
    .radar-commentary { position:absolute; left:50%; top:50%; z-index:4; width:min(92%,560px); max-width:calc(100% - 12px); transform:translate(-50%,-50%) scale(.985); opacity:0; display:flex; align-items:center; justify-content:center; padding:clamp(12px,1.15vw,22px); border-radius:clamp(12px,1vw,18px); background:rgba(2,5,10,.76); box-shadow:0 0 38px rgba(2,5,10,.76); color:rgba(232,246,255,.99); font:760 clamp(26px,2.2vw,42px)/1.16 "Segoe UI",sans-serif; letter-spacing:.003em; text-align:center; white-space:pre-line; overflow:visible; pointer-events:none; transition:opacity 180ms ease,transform 180ms ease; }
    .wall.commentary-view .radar-commentary.visible { opacity:1; transform:translate(-50%,-50%) scale(1); }
    .wall.commentary-view #radar { visibility:hidden; }
    .wall.commentary-view #radar-count,
    .wall.commentary-view .radar-zoom { visibility:hidden; }
    .radar-grid { fill:rgba(20,236,255,.018); stroke:rgba(20,236,255,.18); stroke-width:.35; vector-effect:non-scaling-stroke; }
    .radar-axis { stroke:rgba(20,236,255,.09); stroke-width:.3; vector-effect:non-scaling-stroke; }
    .radar-home { fill:var(--white); filter:drop-shadow(0 0 2px var(--cyan)); }
    .radar-sweep { opacity:0; transform-origin:center; transform-box:view-box; pointer-events:none; }
    .radar-sweep-beam { fill:rgba(20,236,255,.075); stroke:rgba(20,236,255,.32); stroke-width:.28; vector-effect:non-scaling-stroke; }
    .radar-sweep-line { stroke:rgba(20,236,255,.84); stroke-width:.55; vector-effect:non-scaling-stroke; filter:drop-shadow(0 0 1.6px var(--cyan)); }
    .wall.loading .radar-sweep,.wall.no-aircraft .radar-sweep { opacity:1; animation:radar-scan 3.2s linear infinite; }
    @keyframes radar-scan { to { transform:rotate(360deg); } }
    .contact { color:var(--muted); transition:transform 800ms ease; }
    .contact.trackable { cursor:pointer; outline:none; }
    .contact.trackable:hover path,.contact.trackable:focus path,.contact.trackable:focus-visible path { fill:var(--green); filter:drop-shadow(0 0 1.8px var(--green)); }
    .contact.trackable:focus,.contact.trackable:focus-visible { outline:none; }
    .contact.selected { color:var(--cyan); filter:drop-shadow(0 0 1.6px var(--cyan)); }
    .contact.emergency { color:var(--red); }
    .contact path { fill:currentColor; }
    .contact text { fill:currentColor; font:700 3.2px "Segoe UI",sans-serif; letter-spacing:.02em; }
    .radar-foot { display:flex; align-items:center; justify-content:space-between; gap:clamp(10px,1vw,16px); }
    .radar-track { min-width:0; flex:1 1 auto; display:flex; align-items:center; gap:clamp(7px,.6vw,10px); }
    .track-input-wrap { position:relative; min-width:0; width:clamp(92px,8.8vw,150px); }
    .track-input { min-width:0; width:100%; height:2.15em; padding:0 2.15em 0 .7em; border:1px solid var(--line); border-radius:999px; outline:none; background:rgba(20,236,255,.035); color:var(--white); font:700 clamp(10px,.82vw,14px)/1 "Segoe UI",sans-serif; letter-spacing:.08em; text-transform:uppercase; }
    .track-clear { position:absolute; top:50%; right:.28em; transform:translateY(-50%); width:1.62em; height:1.62em; padding:.28em; display:grid; place-items:center; cursor:pointer; border:0; border-radius:50%; background:transparent; color:var(--muted); }
    .track-clear:hover,.track-clear:focus-visible { color:var(--cyan); background:rgba(20,236,255,.10); outline:none; }
    .track-clear[hidden] { display:none; }
    .track-clear svg { width:100%; height:100%; display:block; }
    .track-input::placeholder { color:#46636f; }
    .track-input:focus { border-color:rgba(20,236,255,.72); box-shadow:0 0 10px rgba(20,236,255,.10); }
    .track-button { height:2.15em; padding:0 .9em; cursor:pointer; border:1px solid var(--line); border-radius:999px; background:rgba(20,236,255,.055); color:var(--cyan); font:750 clamp(10px,.82vw,14px)/1 "Segoe UI",sans-serif; letter-spacing:.12em; }
    .track-button:hover,.track-button:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .track-button.tracking,.track-button.ready { animation:track-button-pulse 2.6s ease-in-out infinite; }
    @keyframes track-button-pulse {
      0%,100% { border-color:rgba(20,236,255,.38); box-shadow:0 0 0 rgba(20,236,255,0); background:rgba(20,236,255,.055); }
      50% { border-color:rgba(20,236,255,.95); box-shadow:0 0 14px rgba(20,236,255,.22); background:rgba(20,236,255,.12); }
    }
    .wall.radar-open .metric-value { font-size:clamp(26px,2.75vw,52px); }
    .wall.radar-open .callsign { font-size:clamp(64px,7.2vw,138px); }
    .wall.radar-open .route-visual { width:clamp(530px,29.7vw,575px); }
    .wall.radar-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }
    .wall.settings-open .metric-value { font-size:clamp(26px,2.75vw,52px); }
    .wall.settings-open .callsign { font-size:clamp(64px,7.2vw,138px); }
    .wall.settings-open .route-visual { width:clamp(530px,29.7vw,575px); }
    .wall.settings-open .route-map { width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); }

    .wall:not(.radar-open) .route-visual { width:clamp(650px,40vw,760px); }
    .wall:not(.radar-open):not(.settings-open) .route-map { top:clamp(77.25px,8.78vh,84.27px); left:auto; right:0; transform:none; width:clamp(900px,64vw,1220px); height:clamp(316.8px,38.28vh,415.8px); border-radius:0; }
    .radar-zoom { display:flex; align-items:center; gap:clamp(8px,.7vw,12px); }
    .zoom-button { width:2.15em; height:2.15em; padding:0; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); font:700 clamp(15px,1.2vw,21px)/1 "Segoe UI",sans-serif; }
    .zoom-button svg { width:1em; height:1em; display:block; overflow:visible; }
    .zoom-button:hover,.zoom-button:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .wall.no-aircraft.radar-open #zoom-out:not(:disabled) { animation:radar-range-hint 2.4s ease-in-out infinite; }
    @keyframes radar-range-hint { 0%,100% { transform:scale(1); box-shadow:0 0 0 rgba(20,236,255,0); } 50% { transform:scale(1.12); border-color:rgba(20,236,255,.95); box-shadow:0 0 18px rgba(20,236,255,.28); background:rgba(20,236,255,.12); } }
    .zoom-button:disabled { color:#34525e; border-color:#18303a; cursor:default; }
    .wall.no-aircraft .information { justify-content:center; }
    .wall.no-aircraft .flight-heading,.wall.no-aircraft .identity,.wall.no-aircraft .route-visual,.wall.no-aircraft .metrics { display:none; }
    .idle-message,.tracking-message { display:none; position:absolute; inset:0; align-items:center; justify-content:center; color:var(--muted); font:700 clamp(20px,2vw,36px)/1.2 "Segoe UI",sans-serif; letter-spacing:.06em; text-align:center; pointer-events:none; }
    .wall.loading .idle-message,.wall.no-aircraft:not(.tracking-wait) .idle-message { display:flex; }
    .wall.tracking-wait .tracking-message { display:flex; }
    .wall.loading .information { visibility:hidden; }
    footer { gap:clamp(12px,1.2vw,22px); color:#52717e; font:600 clamp(9px,.72vw,13px)/1.25 "Segoe UI",sans-serif; letter-spacing:.075em; }
    .footer-credits { min-width:0; flex:1 1 auto; }
    .footer-credit-line { display:inline; }
    .footer-credit-line-2::before,.footer-credit-line-3::before { content:"  /  "; white-space:pre; }
    .api-heart { display:inline-flex; align-items:center;vertical-align:middle;transform-origin:center; color:#FF8000; transition:color 300ms ease,opacity 300ms ease; }
    .api-heart svg { display:block;width:1em;height:1em;fill:currentColor; }
    .api-heart.api-unhealthy { color:#888; opacity:.75; }
    .api-heart.api-pulse { animation:api-heart-pulse 1.45s ease-out 1; }
    @keyframes api-heart-pulse {
      0%,100% { transform:scale(1); filter:none; }
      28% { transform:scale(1.42); filter:drop-shadow(0 0 5px rgba(255,140,48,.85)); }
      55% { transform:scale(.94); filter:drop-shadow(0 0 2px rgba(255,140,48,.45)); }
      76% { transform:scale(1.16); filter:drop-shadow(0 0 4px rgba(255,140,48,.65)); }
    }
    .footer-attribution { color:inherit; text-decoration:none; white-space:nowrap; }
    .footer-attribution:hover { color:var(--muted); }
    #footer-status { position:absolute; right:clamp(18px,2.4vw,52px); bottom:calc(clamp(18px,2.4vw,52px) + clamp(16px,1.4vw,24px)); max-width:min(44vw,560px); overflow:hidden; color:#52717e; font:600 clamp(8px,.65vw,11px)/1.2 "Segoe UI",sans-serif; letter-spacing:.04em; text-align:right; text-overflow:ellipsis; white-space:nowrap; pointer-events:none; }
    @media (max-width:1150px) { .wall.radar-open .content,.wall.settings-open .content { grid-template-columns:minmax(0,1fr) minmax(280px,36vw); } .wall.radar-open .metrics,.wall.settings-open .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } }
        @media (min-width:701px) and (max-width:950px) {
            .wall.radar-open .radar-foot { flex-wrap:wrap; gap:8px; }
            .wall.radar-open .radar-track { flex:1 1 100%; }
            .wall.radar-open .radar-zoom { width:100%; justify-content:flex-end; }
            .wall.radar-open .radar-heading span { font-size:9px; letter-spacing:.06em; }
        }
    @media (max-aspect-ratio:4/3) { .callsign { font-size:clamp(56px,15vw,130px); } }
    @media (min-width:701px) and (max-height:780px) {
      .information { padding-bottom:22px; }
    }
  
    /* Mobile is a single-view application: flight, radar and settings replace
       one another instead of being squeezed side-by-side. Desktop rules above
       remain untouched. */
    @media (max-width:700px), (max-width:950px) and (max-height:500px) {
      html,body { width:100%; height:100%; min-height:100%; overflow:hidden; }
      body { cursor:auto; }
      .wall {
        width:100vw;
        height:100vh;
        height:100dvh;
        padding:max(8px,env(safe-area-inset-top)) max(8px,env(safe-area-inset-right)) max(7px,env(safe-area-inset-bottom)) max(8px,env(safe-area-inset-left));
        grid-template-rows:34px minmax(0,1fr) 34px;
        gap:7px;
      }
      header { min-width:0; gap:6px; }
      .brand { flex:0 1 auto; width:clamp(94px,28vw,132px); height:32px; min-width:0; }
      .header-tools { min-width:0; gap:5px; transform:none !important; }
      .sound-toggle,.commentary-toggle,.camera-toggle,.snapshots-toggle,.fullscreen-toggle,.settings-toggle { width:29px; height:29px; padding:6px; flex:0 0 29px; }
      .radar-toggle { min-height:29px; padding:.52em .68em; flex:0 0 auto; font-size:9px; letter-spacing:.09em; }
      .live { flex:0 0 auto; gap:.35em; font-size:9px; letter-spacing:.06em; }
      .live #mode { display:none; }
      .live-dot { width:8px; height:8px; }
      .content,
      .wall.radar-open .content,
      .wall.settings-open .content {
        min-height:0;
        display:grid;
        grid-template-columns:minmax(0,1fr);
        gap:0;
      }
      main {
        position:relative;
        min-width:0;
        min-height:0;
        width:100%;
        height:100%;
        overflow:hidden;
        border-radius:14px;
      }
      .information {
        position:absolute;
        inset:0;
        width:100%;
        height:100%;
        min-width:0;
        min-height:0;
        padding:11px 12px;
        display:grid;
        grid-template-columns:minmax(0,1fr);
        grid-template-rows:minmax(0,1fr) auto auto;
        gap:6px;
      }
      .flight-heading {
        width:100%;
        min-width:0;
        min-height:0;
        display:grid;
        grid-template-columns:minmax(0,1fr);
        grid-template-rows:minmax(118px,30%) minmax(0,1fr);
        align-items:stretch;
        gap:7px;
      }
      .flight-copy {
        width:100%;
        min-width:0;
        display:flex;
        flex-direction:column;
        align-items:center;
        justify-content:center;
        text-align:center;
        padding:2px 0 3px;
      }
      .callsign,
      .wall.radar-open .callsign,
      .wall.settings-open .callsign {
        width:100%;
        justify-content:center;
        font-size:clamp(45px,14.4vw,62px);
        line-height:.88;
      }
      .callsign.route-known { font-size:clamp(40px,13.2vw,57px); }
      .callsign.route-known .route-airport {
        width:1.18em;
        height:.78em;
        flex-basis:1.18em;
        min-width:1.18em;
        min-height:.78em;
        max-width:1.18em;
        max-height:.78em;
      }
      .route-join { font-size:.40em; }
      .destination-summary { min-height:1.15em; margin-top:5px; font-size:clamp(10px,3.15vw,13px); letter-spacing:.02em; }
      .identity {
        width:100%;
        margin-top:5px;
        align-items:center;
        gap:2px;
      }
      .registration {
        width:100%;
        text-align:center;
        font-size:clamp(17px,5.3vw,23px);
        letter-spacing:.06em;
      }
      .aircraft-name {
        width:100%;
        text-align:center;
        font-size:clamp(14px,4.35vw,19px);
        letter-spacing:.035em;
      }

      .route-visual,
      .wall.radar-open .route-visual,
      .wall.settings-open .route-visual,
      .wall:not(.radar-open) .route-visual {
        position:relative;
        top:auto;
        right:auto;
        left:auto;
        width:100%;
        max-width:none;
        height:100%;
        min-height:0;
        overflow:hidden;
      }
      .route-brand {
        top:0;
        width:100%;
        height:31px;
        background:linear-gradient(90deg,rgba(2,5,10,0) 0%,#02050a 7%,#02050a 93%,rgba(2,5,10,0) 100%);
      }
      .route-brand-name { font-size:clamp(15px,4.8vw,20px); }
      .route-brand-logo { max-height:31px; }
      .route-map,
      .wall.radar-open .route-map,
      .wall.settings-open .route-map,
      .wall:not(.radar-open):not(.settings-open) .route-map {
        top:31px;
        left:5%;
        right:5%;
        transform:none;
        width:90%;
        height:calc(100% - 31px);
        min-height:0;
        border-radius:10px;
      }
      .route-map .leaflet-tooltip.route-label { font-size:8px; padding:2px 4px; }
    .aircraft-photo-credit { right:5px; bottom:5px; font-size:5px; padding:.35em .45em; }

      .metrics,
      .wall.radar-open .metrics,
      .wall.settings-open .metrics {
        width:100%;
        margin-top:0;
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:5px 12px;
      }
      .metric { width:100%; min-width:0; padding-top:5px; }
      .metric-label { font-size:8px; letter-spacing:.12em; }
      .metric-value,
      .wall.radar-open .metric-value,
      .wall.settings-open .metric-value {
        margin-top:.08em;
        font-size:clamp(16px,5.1vw,22px);
      }
      .metric-history {
        margin-top:3px;
        display:grid;
        grid-template-columns:repeat(3,minmax(0,1fr));
        gap:4px;
        font-size:clamp(6px,1.75vw,8px);
        line-height:1.05;
      }
      .metric-history-row {
        min-width:0;
        display:grid;
        grid-template-columns:minmax(0,1fr);
        gap:1px;
        align-items:start;
        white-space:nowrap;
      }
      .metric-history-age {
        font-size:.82em;
        letter-spacing:.08em;
      }
      .metric-history-value {
        min-width:0;
        overflow:hidden;
        font-size:1em;
        letter-spacing:0;
      }
      .idle-message,.tracking-message { padding:12px; font-size:18px; }

      /* Radar and Settings become alternate full-size views on mobile. */
      .wall.radar-open main,
      .wall.settings-open main { display:none; }
      .wall.radar-open .radar-panel,
      .wall.settings-open .settings-panel {
        width:100%;
        height:100%;
        min-height:0;
        border-radius:14px;
        padding:11px 12px;
      }
      .wall.radar-open .radar-panel { grid-template-rows:auto minmax(0,1fr) auto; }
      .radar-heading strong { font-size:15px; }
      .radar-heading span { font-size:9px; }
      .radar-stage { min-height:0; overflow:hidden; }
      .radar-commentary { width:min(94%,430px); max-width:calc(100% - 8px); padding:10px; font-size:clamp(22px,6.4vw,32px); line-height:1.14; }
      #radar {
        width:min(100%,calc(100dvh - 150px));
        max-width:100%;
        max-height:100%;
      }
      .radar-foot { display:flex; justify-content:space-between; gap:7px; font-size:9px; letter-spacing:.08em; }
      .radar-track { gap:5px; }
      .track-input-wrap { width:clamp(78px,25vw,112px); }
      .track-input { width:100%; height:29px; padding:0 26px 0 8px; font-size:10px; }
      .track-button { height:29px; padding:0 8px; font-size:9px; }
      .radar-zoom { grid-column:auto; justify-self:auto; gap:5px; }
      .zoom-button { width:29px; height:29px; font-size:15px; }

      .wall.settings-open .settings-panel { grid-template-rows:auto minmax(0,1fr) auto; }
      .settings-heading strong { font-size:15px; }
      .settings-heading span { font-size:9px; }
      .settings-list { align-self:center; gap:5px; }
      .setting-row { gap:5px; padding:7px 0; }
      .setting-label { font-size:8px; }
      .setting-control { gap:7px; font-size:13px; }
      .setting-action { padding:.55em .75em; font-size:9px; }
      .setting-select { min-width:128px; padding:.5em 1.8em .5em .7em; font-size:9px; }
      .settings-location-value { font-size:11px; }
      .settings-foot { font-size:8px; }

      footer {
        position:relative;
        min-width:0;
        min-height:0;
        display:grid;
        grid-template-columns:minmax(0,1fr) auto;
        grid-template-rows:auto auto auto;
        column-gap:7px;
        row-gap:2px;
        overflow:hidden;
        align-items:center;
        font-size:clamp(6.2px,1.85vw,7.5px);
        line-height:1.08;
        letter-spacing:.015em;
      }
      .footer-credits {
        display:contents;
        min-width:0;
        white-space:normal;
      }
      .footer-credit-line {
        display:block;
        min-width:0;
        white-space:normal;
        overflow:visible;
      }
      .footer-credit-line-2::before,.footer-credit-line-3::before { content:none; }
      .footer-credit-line-1 { grid-column:1 / -1; grid-row:1; }
      .footer-credit-line-2 { grid-column:1 / -1; grid-row:2; }
      .footer-credit-line-3 { grid-column:1; grid-row:3; }
      #footer-status {
        right:10px;
        bottom:calc(8px + 8em);
        max-width:58vw;
        font-size:clamp(5.8px,1.65vw,6.8px);
        line-height:1;
      }

      .snapshot-backdrop { padding:6px; }
      .snapshot-dialog { width:calc(100vw - 12px); height:calc(100dvh - 12px); padding:9px 10px; gap:7px; border-radius:14px; }
      .snapshot-dialog-head { gap:8px; }
      .snapshot-dialog-head strong { font-size:16px; }
      .snapshot-dialog-head p { display:none; }
      .snapshot-close { width:30px; height:30px; font-size:16px; }
      .snapshot-grid { grid-template-columns:1fr; gap:8px; padding-right:2px; }
      .snapshot-card { padding:8px; border-radius:11px; }
      .snapshot-card-time { font-size:11px; }
      .snapshot-card-description { font-size:8px; }
      .snapshot-card-detail { font-size:7px; }
      .snapshot-pair { grid-template-columns:repeat(2,minmax(0,1fr)); gap:5px; }
      .snapshot-thumb { height:72px; }
      .snapshot-action { font-size:7px; padding:.5em .25em; }
      .snapshot-viewer { padding:8px; }
      .snapshot-toast { bottom:42px; font-size:9px; }

      /* Keep the location picker inside the mobile viewport too. */
      .location-backdrop { padding:6px; }
      .location-dialog {
        width:calc(100vw - 12px);
        height:calc(100dvh - 12px);
        padding:9px 10px;
        grid-template-rows:auto minmax(120px,1fr) auto auto auto;
        gap:6px;
        border-radius:14px;
      }
      .location-dialog-head { gap:8px; }
      .location-dialog-head strong { font-size:16px; }
      .location-dialog-head p { display:none; }
      .location-close { width:30px; height:30px; font-size:16px; }
      #location-map { border-radius:10px; }
      .location-details { gap:6px; }
      .location-coordinate { padding:6px 8px; }
      .location-coordinate span { font-size:7px; }
    .location-coordinate input { font-size:12px; }
      .location-status { min-height:1.2em; font-size:9px; }
      .location-actions { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:5px; }
      .location-actions .setting-action { min-width:0; padding:.55em .35em; font-size:8px; letter-spacing:.06em; }
    }
    @media (max-width:360px) {
      .brand { width:clamp(78px,24vw,96px); }
      .header-tools { gap:3px; }
      .sound-toggle,.commentary-toggle,.camera-toggle,.snapshots-toggle,.fullscreen-toggle,.settings-toggle { width:27px; height:27px; padding:5px; flex-basis:27px; }
      .radar-toggle { min-height:27px; padding:.48em .52em; font-size:8px; }
    }
        @media (prefers-reduced-motion:reduce) {
            *,*::before,*::after { scroll-behavior:auto !important; animation-duration:.01ms !important; animation-iteration-count:1 !important; transition-duration:.01ms !important; }
        }

  </style>
</head>
<body>
  <section class="wall no-aircraft loading" id="wall">
    <header><div class="brand"><img class="brand-logo" id="brand-logo" alt="Over-Head"></div><div class="header-tools"><button class="sound-toggle" id="sound-toggle" type="button" aria-label="Mute aircraft change sound" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6m-5 2h4M6.5 16.5h11c-1.6-1.7-2.2-3.5-2.2-6.2A3.3 3.3 0 0 0 12 7a3.3 3.3 0 0 0-3.3 3.3c0 2.7-.6 4.5-2.2 6.2Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path class="mute-slash" d="M4 4l16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button><button class="commentary-toggle" id="commentary-toggle" type="button" aria-label="Disable commentary" aria-pressed="true" title="Disable commentary"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 5.5h15v10h-8l-4.5 3v-3H4.5Z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M8 9h8M8 12h5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><path class="commentary-slash" d="M4 4l16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button><button class="camera-toggle" id="camera-toggle" type="button" aria-label="Save desktop and mobile flight snapshots" title="Save flight snapshots" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 7.5h3l1.4-2h6.2l1.4 2h3A1.5 1.5 0 0 1 21 9v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18V9a1.5 1.5 0 0 1 1.5-1.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="12" cy="13" r="3.4" fill="none" stroke="currentColor" stroke-width="1.6"/></svg></button><button class="snapshots-toggle" id="snapshots-toggle" type="button" aria-label="Open saved flight snapshots" aria-pressed="false" title="Open flight snapshots"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3.5 6.5h6l1.8 2h9.2v8.8A1.7 1.7 0 0 1 18.8 19H5.2a1.7 1.7 0 0 1-1.7-1.7V6.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M3.5 9h17" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></button><button class="fullscreen-toggle" id="fullscreen-toggle" type="button" aria-label="Enter full screen" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><g class="fullscreen-enter"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g><g class="fullscreen-exit"><path d="M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g></svg></button><button class="settings-toggle" id="settings-toggle" type="button" aria-label="Open settings" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.7 3.4h4.6l.6 2a7.4 7.4 0 0 1 1.6.9l2-.5 2.3 4-1.5 1.5a7.9 7.9 0 0 1 0 1.8l1.5 1.5-2.3 4-2-.5a7.4 7.4 0 0 1-1.6.9l-.6 2H9.7l-.6-2a7.4 7.4 0 0 1-1.6-.9l-2 .5-2.3-4 1.5-1.5a7.9 7.9 0 0 1 0-1.8L3.2 9.8l2.3-4 2 .5a7.4 7.4 0 0 1 1.6-.9l.6-2Z" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linejoin="round"/><circle cx="12" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.55"/></svg></button><button class="radar-toggle" id="radar-toggle" type="button" aria-pressed="false">RADAR</button><div class="live" id="live"><span class="live-dot"></span><span id="mode">STARTING</span></div></div></header>
    <div class="content">
    <main>
      <div class="idle-message" id="idle-message">Looking out&hellip;</div>
      <div class="tracking-message" id="tracking-message"></div>
      <section class="information">
        <div class="flight-heading"><div class="flight-copy"><div class="callsign" id="callsign">-</div><div class="route-lookup-status" id="route-lookup-status" aria-live="polite" hidden></div><div class="identity"><span class="registration" id="registration"></span><span class="aircraft-name" id="aircraft-name"></span></div><div class="destination-summary" id="destination-summary" hidden></div></div><div class="route-visual"><div class="route-brand" id="route-brand"><span class="route-brand-name" id="route-brand-name"></span><img class="route-brand-logo" id="operator-logo" alt=""></div><div class="route-map" id="route-map" aria-label="Route map centred on the tracked aircraft"><div class="route-photo-panel" id="route-photo-panel" hidden><img id="aircraft-photo" alt=""><span class="aircraft-photo-credit">PHOTO &middot; AIRPORT-DATA.COM via ADSBDB</span></div></div></div></div>
        <div class="metrics">
          <div class="metric"><div class="metric-label">ALTITUDE</div><div class="metric-value" id="altitude">--</div><div class="metric-history" aria-label="Previous altitude values"><div class="metric-history-row"><span class="metric-history-age">30s</span><span class="metric-history-value" id="altitude-history-30">--</span></div><div class="metric-history-row"><span class="metric-history-age">60s</span><span class="metric-history-value" id="altitude-history-60">--</span></div><div class="metric-history-row"><span class="metric-history-age">90s</span><span class="metric-history-value" id="altitude-history-90">--</span></div></div></div>
          <div class="metric"><div class="metric-label">GROUND SPEED</div><div class="metric-value" id="speed">--</div><div class="metric-history" aria-label="Previous ground speed values"><div class="metric-history-row"><span class="metric-history-age">30s</span><span class="metric-history-value" id="speed-history-30">--</span></div><div class="metric-history-row"><span class="metric-history-age">60s</span><span class="metric-history-value" id="speed-history-60">--</span></div><div class="metric-history-row"><span class="metric-history-age">90s</span><span class="metric-history-value" id="speed-history-90">--</span></div></div></div>
          <div class="metric"><div class="metric-label">VERTICAL</div><div class="metric-value" id="vertical">--</div><div class="metric-history" aria-label="Previous vertical rate values"><div class="metric-history-row"><span class="metric-history-age">30s</span><span class="metric-history-value" id="vertical-history-30">--</span></div><div class="metric-history-row"><span class="metric-history-age">60s</span><span class="metric-history-value" id="vertical-history-60">--</span></div><div class="metric-history-row"><span class="metric-history-age">90s</span><span class="metric-history-value" id="vertical-history-90">--</span></div></div></div>
          <div class="metric metric-distance"><div class="metric-label">DISTANCE</div><div class="metric-value"><span id="distance-main">--</span> <span id="distance-unit">NM</span></div><div class="metric-history" aria-label="Previous distance values"><div class="metric-history-row"><span class="metric-history-age">30s</span><span class="metric-history-value" id="distance-history-30">--</span></div><div class="metric-history-row"><span class="metric-history-age">60s</span><span class="metric-history-value" id="distance-history-60">--</span></div><div class="metric-history-row"><span class="metric-history-age">90s</span><span class="metric-history-value" id="distance-history-90">--</span></div></div></div>
        </div>
      </section>
    </main>
    <aside class="radar-panel" aria-label="Nearby aircraft radar">
      <div class="radar-heading"><strong id="radar-heading-title">WIDER AREA</strong><span id="radar-count">0 CONTACTS</span></div>
      <div class="radar-stage"><svg id="radar" viewBox="0 0 100 100" role="img" aria-label="North-up radar of nearby aircraft">
        <circle class="radar-grid" cx="50" cy="50" r="46"/><circle class="radar-grid" cx="50" cy="50" r="30.7"/><circle class="radar-grid" cx="50" cy="50" r="15.3"/>
        <path class="radar-axis" d="M50 4V96M4 50H96"/><text x="50" y="2.8" text-anchor="middle" fill="#6c9aac" font-size="3">N</text>
        <g class="radar-sweep" aria-hidden="true"><path class="radar-sweep-beam" d="M50 50 L50 4 A46 46 0 0 1 72 9.6 Z"/><line class="radar-sweep-line" x1="50" y1="50" x2="50" y2="4"/></g>
        <g id="radar-contacts"></g><circle class="radar-home" cx="50" cy="50" r="1.25"/>
    </svg><div class="radar-commentary" id="radar-commentary" aria-live="polite"></div></div>
      <div class="radar-foot"><form class="radar-track" id="radar-track-form"><div class="track-input-wrap"><input class="track-input" id="track-flight" type="text" maxlength="10" autocomplete="off" spellcheck="false" aria-label="Flight number or callsign to track" placeholder="BA703"><button class="track-clear" id="track-clear" type="button" aria-label="Clear flight selection" title="Clear" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button></div><button class="track-button" id="track-button" type="submit" aria-pressed="false">TRACK</button></form><div class="radar-zoom"><button class="zoom-button" id="zoom-in" type="button" aria-label="Zoom in and reduce tracking range"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button><span id="radar-range">25 NM</span><button class="zoom-button" id="zoom-out" type="button" aria-label="Zoom out and increase tracking range"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12M12 6v12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button></div></div>
    </aside>
    <aside class="settings-panel" aria-label="Display settings">
      <div class="settings-heading"><strong>SETTINGS</strong><span>DISPLAY</span></div>
      <div class="settings-list">
        <div class="setting-row"><span class="setting-label">SPEED UNITS</span><div class="setting-control"><span>GROUND SPEED</span><select class="setting-select" id="settings-speed-unit" aria-label="Ground speed units"><option value="kt">KNOTS (KT)</option><option value="mph">MPH</option><option value="kmh">KM/H</option></select></div></div>
        <div class="setting-row"><span class="setting-label">DISTANCE UNITS</span><div class="setting-control"><span>RANGE</span><select class="setting-select" id="settings-distance-unit" aria-label="Distance units"><option value="nm">NAUTICAL MILES (NM)</option><option value="mi">MILES (MI)</option><option value="km">KILOMETRES (KM)</option></select></div></div>
        <div class="setting-row"><span class="setting-label">CURRENT LOCATION</span><div class="setting-control"><span class="settings-location-value" id="settings-location-value">LOADING</span><button class="setting-action" id="settings-location" type="button">SELECT LOCATION</button></div></div>
      </div>
      <div class="settings-foot">SETTINGS ARE SAVED ON THIS DISPLAY</div>
    </aside>
    </div>
    <footer><span class="footer-credits"><span class="footer-credit-line footer-credit-line-1">For Sam <span class="api-heart" id="api-heart" aria-label="API heartbeat normal" title="API heartbeat: waiting for first response"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg></span>&nbsp;&nbsp;&nbsp;&nbsp;Data: <a class="footer-attribution" href="https://adsb.lol/" target="_blank" rel="noopener">ADSB.lol</a> + <a class="footer-attribution" href="https://opendata.adsb.fi/" target="_blank" rel="noopener">ADSB.fi</a><span id="flightaware-credit" hidden> + <a class="footer-attribution" href="https://www.flightaware.com/commercial/aeroapi/" target="_blank" rel="noopener">FlightAware</a></span> + <a class="footer-attribution" href="https://www.adsbdb.com/" target="_blank" rel="noopener">ADSBDB</a> + <a class="footer-attribution" href="https://ourairports.com/data/" target="_blank" rel="noopener">OurAirports</a></span><span class="footer-credit-line footer-credit-line-2">Map: <a class="footer-attribution" href="https://leafletjs.com/" target="_blank" rel="noopener">Leaflet</a> + &copy; <a class="footer-attribution" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>&nbsp;&nbsp;/&nbsp;&nbsp;Places: <a class="footer-attribution" href="https://nominatim.org/" target="_blank" rel="noopener">Nominatim</a>&nbsp;&nbsp;/&nbsp;&nbsp;Logos: <a class="footer-attribution" href="https://github.com/soaring-symbols/soaring-symbols" target="_blank" rel="noopener">Soaring Symbols</a>&nbsp;&nbsp;/&nbsp;&nbsp;Flags: <a class="footer-attribution" href="https://github.com/catamphetamine/country-flag-icons" target="_blank" rel="noopener">country-flag-icons</a></span><span class="footer-credit-line footer-credit-line-3">Sound: arunangshubanerjee via <a class="footer-attribution" href="https://pixabay.com/" target="_blank" rel="noopener">Pixabay</a></span></span></footer>
    <div id="footer-status" aria-live="polite">Connecting</div>
  </section>
  <div class="snapshot-flash" id="snapshot-flash" aria-hidden="true"></div>
  <div class="snapshot-countdown" id="snapshot-countdown" aria-hidden="true"><div class="snapshot-countdown-panel"><span class="snapshot-countdown-label" id="snapshot-countdown-label">SNAPSHOT IN</span><strong class="snapshot-countdown-number" id="snapshot-countdown-number"></strong></div></div>
  <div class="snapshot-toast" id="snapshot-toast" role="status" aria-live="polite"></div>
  <div class="snapshot-backdrop" id="snapshot-backdrop" role="dialog" aria-modal="true" aria-labelledby="snapshot-title">
    <div class="snapshot-dialog">
      <div class="snapshot-dialog-head"><div><strong id="snapshot-title">FLIGHT SNAPSHOTS</strong><p>Every capture preserves the complete Over-Head screen as both a 1920&times;1080 desktop image and a 390&times;844 mobile image.</p></div><button class="snapshot-close" id="snapshot-close" type="button" aria-label="Close snapshots">&times;</button></div>
      <div class="snapshot-grid" id="snapshot-grid"><div class="snapshot-empty">No flight snapshots saved yet.</div></div>
      <div class="snapshot-viewer" id="snapshot-viewer"><div class="snapshot-viewer-head"><span class="snapshot-viewer-title" id="snapshot-viewer-title"></span><button class="snapshot-close" id="snapshot-viewer-close" type="button" aria-label="Back to snapshots">&times;</button></div><div class="snapshot-viewer-stage"><img id="snapshot-viewer-image" alt="Saved Over-Head snapshot"></div></div>
    </div>
  </div>
  <div class="location-backdrop" id="location-backdrop" role="dialog" aria-modal="true" aria-labelledby="location-title">
    <div class="location-dialog">
      <div class="location-dialog-head"><div><strong id="location-title">SELECT CURRENT LOCATION</strong><p>Use your device's high-accuracy browser location, or click and drag the marker to set the exact home centre used for aircraft tracking.</p></div><button class="location-close" id="location-close" type="button" aria-label="Close location selector">&times;</button></div>
      <div id="location-map" aria-label="Map for selecting the Over-Head home location"></div>
    <div class="location-details"><label class="location-coordinate"><span>LATITUDE</span><input id="location-latitude" type="number" min="-90" max="90" step="any" inputmode="decimal" aria-label="Home latitude" placeholder="-90 to 90"></label><label class="location-coordinate"><span>LONGITUDE</span><input id="location-longitude" type="number" min="-180" max="180" step="any" inputmode="decimal" aria-label="Home longitude" placeholder="-180 to 180"></label></div>
      <div class="location-status" id="location-status">Choose a point on the map or use your device location.</div>
      <div class="location-actions"><button class="setting-action" id="location-use-device" type="button">USE MY LOCATION</button><button class="setting-action" id="location-cancel" type="button">CANCEL</button><button class="setting-action location-primary" id="location-save" type="button">SAVE LOCATION</button></div>
    </div>
  </div>
  <audio id="alert-tone" preload="auto" src="/beep-tone.mp3"></audio>
  <script>
    const byId=id=>document.getElementById(id);
        let modalReturnFocus=null,viewerReturnFocus=null;
        function focusModalControl(selector){ const control=byId(selector); if(control)control.focus(); }
        function trapModalTab(event){
            if(event.key!=='Tab')return;
            const viewer=byId('snapshot-viewer');
            const location=byId('location-backdrop');
            const gallery=byId('snapshot-backdrop');
            const dialog=viewer.classList.contains('open')?viewer:location.classList.contains('open')?location:gallery.classList.contains('open')?gallery:null;
            if(!dialog)return;
            const controls=[...dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(item=>item.getClientRects().length);
            if(!controls.length){ event.preventDefault(); return; }
            const first=controls[0],last=controls[controls.length-1];
            if(event.shiftKey&&(document.activeElement===first||!dialog.contains(document.activeElement))){ event.preventDefault(); last.focus(); }
            else if(!event.shiftKey&&(document.activeElement===last||!dialog.contains(document.activeElement))){ event.preventDefault(); first.focus(); }
        }
    const snapshotParams=new URLSearchParams(location.search);
    const snapshotId=snapshotParams.get('snapshot')||'';
    const snapshotRadar=snapshotParams.get('radar')==='1';
    const snapshotAssetSuffix=snapshotId?'&snapshot='+encodeURIComponent(snapshotId):'';
    window.__overheadSnapshotReady=false;
    window.__overheadSnapshotError='';
    function updateBrowserTitle(d){
      const prefix='Over-Head '+String.fromCodePoint(0x2708,0xFE0F);
      const hasAircraft=Boolean(d&&d.aircraft_id);
      if(!hasAircraft){ document.title=prefix; return; }
      const origin=String(d.origin||'N/A').trim().toUpperCase()||'N/A';
      const destination=String(d.destination||'N/A').trim().toUpperCase()||'N/A';
      document.title=prefix+' '+origin+' to '+destination;
    }
    const svgNS='http://www.w3.org/2000/svg';
    const radarRanges=Array.from({length:20},(_,i)=>(i+1)*5); let currentRadarRange=25;
    let radarRangeRequest=Promise.resolve(),radarRangePending=null,radarManualOverride=false,radarRangeRevision=0,statusRequestRevision=0;
    const radarRangeKey=()=> 'overhead-radar-range-'+distanceUnit;
    const radarRangeUser=()=>Math.round((currentRadarRange*distanceUnitSpec().factor)/5)*5;
    const nearestRadarRange=(value)=>radarRanges.reduce((a,b)=>Math.abs(b-value)<Math.abs(a-value)?b:a,radarRanges[0]);
    const radarRangeNm=(value)=>value/distanceUnitSpec().factor;
    function requestRadarRange(radius){
      // Serialize updates: a slow earlier request must not overwrite a later click.
      const requested=radius;
    const revision=++radarRangeRevision;
      radarRangePending=requested;
      radarManualOverride=true;
      radarRangeRequest=radarRangeRequest.catch(()=>{}).then(async()=>{
        const response=await fetch('/radar-range?radius='+encodeURIComponent(requested),{cache:'no-store'});
        if(!response.ok)throw new Error('Radar range update failed ('+response.status+')');
            const data=await response.json();
            if(revision===radarRangeRevision)drawRadar(data.radar_contacts,data.radius_nm,false);
      }).catch(error=>{console.warn(error);}).then(()=>{
            if(revision===radarRangeRevision)radarRangePending=null;
      });
      return radarRangeRequest;
    }
    function displayRadarRangeValue(nm,auto=false){return (auto?'AUTO ':'')+Math.round(Number(nm)*distanceUnitSpec().factor/5)*5+' '+distanceUnitSpec().label;}
    function updateRadarRangeControls(){
      const current=radarRangeUser(),idx=radarRanges.indexOf(nearestRadarRange(current));
      byId('zoom-in').disabled=flightTrackingActive||idx===0;
      byId('zoom-out').disabled=flightTrackingActive||idx===radarRanges.length-1;
    }
    let flightTrackingActive=false,flightTrackingQuery='',pendingTrackHex='',pendingTrackFlight='',suppressNextAircraftTone=false;
    let apiHealthRevision='';
    let apiHeartPulseTimer=null;
    function loadHeaderLogo(){
      const target=byId('brand-logo');
      if(!target)return;
      const preload=new Image();
      preload.onload=()=>{
        target.onload=()=>target.classList.add('ready');
        target.onerror=()=>{ target.classList.remove('ready'); target.removeAttribute('src'); };
        target.src=preload.src;
        if(target.complete&&target.naturalWidth)target.classList.add('ready');
      };
      preload.onerror=()=>{ target.classList.remove('ready'); target.removeAttribute('src'); };
      preload.src='/over-head-logo';
    }
    let soundEnabled=localStorage.getItem('overhead-sound')!=='muted'; let audioBlocked=false; let trackedAircraftId=''; let brandPulseTimer=null;
    let commentaryEnabled=localStorage.getItem('overhead-commentary')!=='off';
    const validSpeedUnits=new Set(['kt','mph','kmh']),validDistanceUnits=new Set(['nm','mi','km']);
    let speedUnit=localStorage.getItem('overhead-speed-unit')||'kt'; if(!validSpeedUnits.has(speedUnit))speedUnit='kt';
    let distanceUnit=localStorage.getItem('overhead-distance-unit')||'nm'; if(!validDistanceUnits.has(distanceUnit))distanceUnit='nm';
    let lastDisplayStatus=null;
    let statusFailureCount=0;
    let radarWasOpenBeforeSettings=false;
    const number=(value,digits=0)=>value==null?'--':Number(value).toFixed(digits);
    function formatObservationAge(value){ const seconds=Math.max(0,Math.floor(Number(value)||0)); if(seconds<60)return seconds+'s'; const minutes=Math.floor(seconds/60),remaining=seconds%60; return remaining?minutes+'m '+remaining+'s':minutes+'m'; }
    const speedUnitSpec=()=>speedUnit==='mph'?{factor:1.150779448,label:'MPH'}:speedUnit==='kmh'?{factor:1.852,label:'KM/H'}:{factor:1,label:'KT'};
    const distanceUnitSpec=()=>distanceUnit==='mi'?{factor:1.150779448,label:'MI',name:'miles'}:distanceUnit==='km'?{factor:1.852,label:'KM',name:'kilometres'}:{factor:1,label:'NM',name:'nautical miles'};
    function speedFromKnots(value){ if(value===null||value===undefined||value==='')return null; const numeric=Number(value); return Number.isFinite(numeric)?numeric*speedUnitSpec().factor:null; }
    function distanceFromNm(value){ if(value===null||value===undefined||value==='')return null; const numeric=Number(value); return Number.isFinite(numeric)?numeric*distanceUnitSpec().factor:null; }
    function factNumber(value){ if(value===null||value===undefined||value==='')return Number.NaN; const numeric=Number(value); return Number.isFinite(numeric)?numeric:Number.NaN; }
    function formatSpeedKnots(value,digits=0){ const converted=speedFromKnots(value); return converted==null?'--':converted.toFixed(digits)+' '+speedUnitSpec().label; }
    function formatDistanceNm(value,digits=1){ const converted=distanceFromNm(value); return converted==null?'--':converted.toFixed(digits)+' '+distanceUnitSpec().label; }
    function formatDistanceValue(value,digits=1){ const converted=distanceFromNm(value); return converted==null?'--':converted.toFixed(digits); }
    function smartDistanceNm(value,precise=false){ const converted=distanceFromNm(value); if(converted==null)return ''; const digits=precise||converted<1?2:converted<10?1:0; return converted.toFixed(digits)+' '+distanceUnitSpec().label; }
    const routeMapElement=byId('route-map'); let routeMap=null,routeMapLayers=[],routeMapRenderKey='';
    let routePhotoAircraftId='',routePhotoRevision='',routePhotoTimer=null,routePhotoRequest=0,routePhotoInPhotoView=false;
    let routeFlowKey='',routeFlowStartedAt=Date.now();
    let currentHomeLatitude=null,currentHomeLongitude=null,currentLocationRevision=0;
    let locationMap=null,locationMarker=null,locationAccuracyCircle=null,locationDialogRevision=0;
    if(typeof L!=='undefined'){
      routeMap=L.map('route-map',{zoomControl:false,dragging:false,scrollWheelZoom:false,doubleClickZoom:false,boxZoom:false,keyboard:false,touchZoom:false,worldCopyJump:false,attributionControl:false,zoomSnap:.25,zoomDelta:.25,minZoom:0,maxZoom:8});
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{minZoom:0,maxZoom:8,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'}).addTo(routeMap);
    }
    async function fitLogoArtwork(img){
      img.style.transform='none';
      const container=img.parentElement;
      if(!img.naturalWidth||!img.naturalHeight||!container)return;
      const cw=Math.max(1,Math.round(container.clientWidth)),ch=Math.max(1,Math.round(container.clientHeight));
      const canvas=document.createElement('canvas'); canvas.width=Math.min(800,cw); canvas.height=Math.min(220,ch);
      const ctx=canvas.getContext('2d',{willReadFrequently:true}); if(!ctx)return;
      const ratio=Math.min(canvas.width/img.naturalWidth,canvas.height/img.naturalHeight);
      const dw=img.naturalWidth*ratio,dh=img.naturalHeight*ratio,dx=(canvas.width-dw)/2,dy=(canvas.height-dh)/2;
      try{ ctx.clearRect(0,0,canvas.width,canvas.height); ctx.drawImage(img,dx,dy,dw,dh); }catch(_){ return; }
      let pixels; try{ pixels=ctx.getImageData(0,0,canvas.width,canvas.height).data; }catch(_){ return; }
      let minX=canvas.width,minY=canvas.height,maxX=-1,maxY=-1;
      for(let y=0;y<canvas.height;y++)for(let x=0;x<canvas.width;x++){ if(pixels[(y*canvas.width+x)*4+3]>12){ if(x<minX)minX=x;if(x>maxX)maxX=x;if(y<minY)minY=y;if(y>maxY)maxY=y; } }
      if(maxX<minX||maxY<minY)return;
      const visibleW=maxX-minX+1,visibleH=maxY-minY+1;
      const scale=Math.max(1,Math.min(4.5,Math.min(canvas.width/visibleW,canvas.height/visibleH)*.9));
      img.style.transform='scale('+scale+')';
    }
    const finiteCoordinate=value=>typeof value==='number'&&Number.isFinite(value);
    function unwrapLongitude(longitude,centreLongitude){ let value=Number(longitude); while(value-centreLongitude>180)value-=360; while(value-centreLongitude<-180)value+=360; return value; }
    function clearRouteMap(){ for(const layer of routeMapLayers)layer.remove(); routeMapLayers=[]; routeMapRenderKey=''; routeMapElement.classList.remove('visible'); }
    function centredRouteViewport(map,points){
      const size=map.getSize();
      const radarOpen=byId('wall').classList.contains('radar-open');
      const horizontalPadding=.25;
      const verticalPadding=radarOpen?.25:.22;
      const projected=points.map(point=>map.project(point,0));
      const minX=Math.min(...projected.map(point=>point.x)),maxX=Math.max(...projected.map(point=>point.x));
      const minY=Math.min(...projected.map(point=>point.y)),maxY=Math.max(...projected.map(point=>point.y));
      const spanX=Math.max(1,maxX-minX),spanY=Math.max(1,maxY-minY);
      const availableWidth=Math.max(40,size.x*(1-horizontalPadding*2));
      const availableHeight=Math.max(40,size.y*(1-verticalPadding*2));
      const zoomX=Math.log2(availableWidth/spanX),zoomY=Math.log2(availableHeight/spanY);
      const zoom=Math.max(0,Math.min(8,Math.floor(Math.min(zoomX,zoomY)*4)/4));
      const centrePixel=L.point((minX+maxX)/2,(minY+maxY)/2);
      return {centre:map.unproject(centrePixel,0),zoom};
    }
    function routeAircraftIcon(track){
      const angle=Number.isFinite(Number(track))?Number(track):0;
      return L.divIcon({className:'route-aircraft-icon',iconSize:[24,24],iconAnchor:[12,12],html:'<svg viewBox="-12 -12 24 24" aria-hidden="true" style="transform:rotate('+angle+'deg)"><path d="M0-10L2-2 9 1 9 3 2.5 2 2 8 5 10 5 11 0 9-5 11-5 10-2 8-2.5 2-9 3-9 1-2-2Z" fill="#14ecff" stroke="#02050a" stroke-width="1.2" stroke-linejoin="round"/></svg>'});
    }
    function renderRouteMap(d){
      const values=[d.aircraft_latitude,d.aircraft_longitude,d.origin_latitude,d.origin_longitude,d.destination_latitude,d.destination_longitude];
      if(!routeMap||!values.every(finiteCoordinate)||!d.origin||!d.destination){ clearRouteMap(); routeFlowKey=''; return; }
    const renderKey=JSON.stringify([...values,d.aircraft_id,d.origin,d.destination,d.track]);
    if(renderKey===routeMapRenderKey)return;
    routeMapRenderKey=renderKey;
      const nextFlowKey=String(d.aircraft_id||'')+'|'+String(d.origin||'')+'|'+String(d.destination||'');
      if(nextFlowKey!==routeFlowKey){ routeFlowKey=nextFlowKey; routeFlowStartedAt=Date.now(); }
      for(const layer of routeMapLayers)layer.remove(); routeMapLayers=[]; routeMapElement.classList.add('visible');
      const centre=L.latLng(d.aircraft_latitude,d.aircraft_longitude);
      const origin=L.latLng(d.origin_latitude,unwrapLongitude(d.origin_longitude,centre.lng));
      const destination=L.latLng(d.destination_latitude,unwrapLongitude(d.destination_longitude,centre.lng));
      routeMapLayers.push(L.polyline([origin,centre,destination],{color:'#14ecff',weight:2,opacity:.68,dashArray:'2 7',lineCap:'round'}).addTo(routeMap));
      const flow=L.polyline([origin,centre,destination],{className:'route-direction-flow',color:'#e8f6ff',weight:3.2,opacity:.95,lineCap:'round'}).addTo(routeMap);
      const flowPath=flow.getElement();
      if(flowPath){
        const elapsed=(Date.now()-routeFlowStartedAt)%10000;
        flowPath.setAttribute('pathLength','100');
        flowPath.setAttribute('stroke-dasharray','100 100');
        flowPath.setAttribute('stroke-dashoffset','100');
        flowPath.style.animationDelay='-'+elapsed+'ms';
      }
      routeMapLayers.push(flow);
      const airportStyle={radius:4.5,color:'#e8f6ff',weight:1.35,fillColor:'#02050a',fillOpacity:1};
      routeMapLayers.push(L.circleMarker(origin,airportStyle).bindTooltip(d.origin,{permanent:true,direction:'top',offset:[0,-5],className:'route-label'}).addTo(routeMap));
      routeMapLayers.push(L.circleMarker(destination,airportStyle).bindTooltip(d.destination,{permanent:true,direction:'top',offset:[0,-5],className:'route-label'}).addTo(routeMap));
      routeMapLayers.push(L.marker(centre,{icon:routeAircraftIcon(d.track),interactive:false,zIndexOffset:1000}).addTo(routeMap));
      requestAnimationFrame(()=>{
        routeMap.invalidateSize(false);
        const viewport=centredRouteViewport(routeMap,[origin,centre,destination]);
        routeMap.setView(viewport.centre,viewport.zoom,{animate:false});
      });
    }
    const ROTATION_INTERVAL_MS=15000;
    const prefersReducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const rotationViews=['radar','commentary'];
    let rotationView='radar',rotationStartedAt=Date.now();
    function availableRotationViews(){
      const d=lastDisplayStatus||{};
      const views=['radar'];
      if(commentaryEnabled&&d.aircraft_id&&String(byId('radar-commentary').textContent||'').trim())views.push('commentary');
      return views;
    }
    function advanceRotation(){
            if(snapshotId||prefersReducedMotion||document.visibilityState!=='visible')return;
      const wall=byId('wall');
      if(!wall||!wall.classList.contains('radar-open')||wall.classList.contains('settings-open'))return;
      const views=availableRotationViews();
      if(!views.includes(rotationView))rotationView='radar';
      else if(Date.now()-rotationStartedAt>=ROTATION_INTERVAL_MS){
        const currentIndex=rotationViews.indexOf(rotationView);
        rotationView=rotationViews.slice(currentIndex+1).concat(rotationViews.slice(0,currentIndex+1)).find(view=>views.includes(view))||'radar';
        rotationStartedAt=Date.now();
      }
      syncCommentaryPresentation();
    }
    let commentaryCountdownTemplate='',commentaryCountdownDeadline=0,commentaryFallbackText='',commentaryFacts=null;
    function commentaryIsShowing(){
      const wall=byId('wall'),element=byId('radar-commentary');
      return Boolean(
        rotationView==='commentary'&&commentaryEnabled&&wall&&wall.classList.contains('radar-open')&&!wall.classList.contains('settings-open')&&
        element&&String(element.textContent||'').trim()
      );
    }
    let commentaryFitFrame=0;
    function fitRadarCommentary(){
      const element=byId('radar-commentary'),stage=element&&element.closest('.radar-stage');
      if(!element||!stage||!String(element.textContent||'').trim())return;
      element.style.fontSize='';
      element.style.lineHeight='';
      const computed=getComputedStyle(element);
      const preferred=Math.max(12,parseFloat(computed.fontSize)||32);
      const availableWidth=Math.max(80,Math.min(stage.clientWidth*.92,560));
      const availableHeight=Math.max(70,stage.clientHeight-12);
      element.style.width=Math.max(80,availableWidth)+'px';
      let size=preferred;
      const minimum=9;
      while(size>minimum){
        element.style.fontSize=size+'px';
        element.style.lineHeight='1.14';
        if(element.scrollHeight<=availableHeight&&element.scrollWidth<=availableWidth+1)break;
        size-=1;
      }
      if(element.scrollHeight>availableHeight){
        const ratio=Math.max(.55,availableHeight/Math.max(1,element.scrollHeight));
        element.style.fontSize=Math.max(8,size*ratio)+'px';
        element.style.lineHeight='1.1';
      }
    }
    function scheduleCommentaryFit(){
      cancelAnimationFrame(commentaryFitFrame);
      commentaryFitFrame=requestAnimationFrame(()=>{ commentaryFitFrame=requestAnimationFrame(fitRadarCommentary); });
    }
    function syncCommentaryPresentation(){
      const wall=byId('wall'),button=byId('commentary-toggle');
      const showing=commentaryIsShowing();
    if(wall)wall.classList.toggle('commentary-view',showing);
      if(button){
        button.classList.toggle('active',commentaryEnabled);
        button.setAttribute('aria-pressed',String(commentaryEnabled));
        button.setAttribute('aria-label',commentaryEnabled?'Disable commentary':'Enable commentary');
        button.title=commentaryEnabled?'Disable commentary':'Enable commentary';
      }
      const heading=byId('radar-heading-title'),count=byId('radar-count');
    if(heading)heading.textContent=showing?'COMMENTARY':'WIDER AREA';
    if(count)count.hidden=showing;
      if(showing)scheduleCommentaryFit();
      if(lastDisplayStatus)renderAircraftPhoto(lastDisplayStatus,Boolean(lastDisplayStatus.aircraft_id));
    }
    function paintCommentary(text){
      const element=byId('radar-commentary');
      if(!element)return;
      text=String(text||'').trim();
      element.textContent=text;
      element.classList.toggle('visible',Boolean(text));
      syncCommentaryPresentation();
      if(text)scheduleCommentaryFit();
    }
    function formatCommentaryCountdown(seconds){
      const total=Math.max(0,Math.ceil(Number(seconds)||0));
      if(total<60)return total+'s';
      const minutes=Math.floor(total/60),remainder=total%60;
      return remainder?minutes+'m '+remainder+'s':minutes+'m';
    }
    function formatAltitudeFeet(value){
      const numeric=factNumber(value);
      return Number.isFinite(numeric)?Math.round(numeric).toLocaleString()+' ft':'';
    }
    function formatVerticalRate(value){
      const numeric=factNumber(value);
      return Number.isFinite(numeric)?Math.abs(Math.round(numeric)).toLocaleString()+' ft/min':'';
    }
    function nearbyPlaceText(facts){
      const name=String(facts.nearby_place_name||'').trim();
      const distance=factNumber(facts.nearby_place_distance_nm);
      if(!name||!Number.isFinite(distance))return '';
      const kind=String(facts.nearby_place_kind||'place').toLowerCase();
      const centre=({village:'village centre',town:'town centre',city:'city centre',hamlet:'hamlet centre',municipality:'centre',suburb:'centre',neighbourhood:'centre'})[kind]||'centre';
      if(distance<=0.13)return 'Almost directly over '+name+' '+centre;
      return 'Over '+name+', '+smartDistanceNm(distance,true)+' from the '+centre;
    }
    function destinationText(facts){
      const name=String(facts.destination_name||'').trim();
      const distance=factNumber(facts.destination_distance_nm);
      if(!name||!Number.isFinite(distance))return '';
      const aligned=Boolean(facts.destination_track_aligned);
      const minutes=factNumber(facts.destination_minutes_at_ground_speed);
      const range=smartDistanceNm(distance,distance<10);
      if(aligned&&Number.isFinite(minutes)&&minutes>0&&minutes<=360){
        const eta=minutes<1?'<1 min':Math.round(minutes)+' min';
        return name+' is '+range+' ahead \\u00b7 about '+eta+' at current ground speed';
      }
      return name+' is '+range+' away';
    }
    function performanceText(facts){
      const altitude=factNumber(facts.altitude_ft),rate=factNumber(facts.vertical_rate_fpm),selected=factNumber(facts.selected_altitude_ft);
      const alt=Number.isFinite(altitude)?formatAltitudeFeet(altitude):'';
      const vr=Number.isFinite(rate)?formatVerticalRate(rate):'';
      let primary='';
      if(Number.isFinite(rate)&&rate<=-300){
        primary='Descending'+(alt?' through '+alt:'')+(vr?' at '+vr:'');
        if(Number.isFinite(selected)&&Number.isFinite(altitude)&&selected<altitude-500)primary+=' towards selected '+formatAltitudeFeet(selected);
      }else if(Number.isFinite(rate)&&rate>=300){
        primary='Climbing'+(alt?' through '+alt:'')+(vr?' at '+vr:'');
        if(Number.isFinite(selected)&&Number.isFinite(altitude)&&selected>altitude+500)primary+=' towards selected '+formatAltitudeFeet(selected);
      }else if(Number.isFinite(rate)&&Math.abs(rate)<=100&&alt){
        primary='Level at '+alt;
      }
      const phaseStart=factNumber(facts.vertical_phase_window_start_seconds),phaseEnd=factNumber(facts.vertical_phase_window_end_seconds);
      if(primary&&Number.isFinite(phaseStart)&&Number.isFinite(phaseEnd)){
        if(phaseStart===0)primary+=' \\u00b7 '+(rate<0?'descent':'climb')+' began within the last '+Math.round(phaseEnd)+'s';
        else primary+=' \\u00b7 '+(rate<0?'descent':'climb')+' began '+Math.round(phaseStart)+'\\u2013'+Math.round(phaseEnd)+'s ago';
      }
      const extras=[];
      const trackRate=factNumber(facts.track_rate_deg_s);
      if(Number.isFinite(trackRate)&&Math.abs(trackRate)>=0.4)extras.push('Track changing '+Math.abs(trackRate).toFixed(1)+'\\u00b0/s');
      const mach=factNumber(facts.mach),tas=factNumber(facts.tas_kt),ias=factNumber(facts.ias_kt);
      if(Number.isFinite(mach)&&mach>0)extras.push('Mach '+mach.toFixed(2));
      else if(Number.isFinite(tas)&&tas>0)extras.push('TAS '+formatSpeedKnots(tas,0));
      else if(Number.isFinite(ias)&&ias>0)extras.push('IAS '+formatSpeedKnots(ias,0));
      const windSpeed=factNumber(facts.wind_speed_kt),windDir=factNumber(facts.wind_dir_deg);
      if(Number.isFinite(windSpeed)&&windSpeed>=5&&Number.isFinite(windDir))extras.push('Wind '+Math.round(windDir)+'\\u00b0 at '+formatSpeedKnots(windSpeed,0));
      const modes=Array.isArray(facts.nav_modes)?facts.nav_modes:[];
      const modeLabels=[];
      for(const mode of modes){
        const key=String(mode||'').toLowerCase();
        const label=key==='autopilot'?'autopilot':key==='althold'?'altitude hold':key==='lnav'?'LNAV':key==='vnav'?'VNAV':key==='approach'?'approach mode':'';
        if(label&&!modeLabels.includes(label))modeLabels.push(label);
      }
      if(modeLabels.length)extras.push(modeLabels.slice(0,3).join(' \\u00b7 '));
      return [primary,...extras].filter(Boolean).slice(0,2).join(' \\u00b7 ');
    }
    function richCommentaryText(facts,remainingSeconds=null){
      if(!facts||typeof facts!=='object')return '';
      const current=factNumber(facts.current_distance_nm),trend=String(facts.trend||'');
      const projected=factNumber(facts.projected_distance_nm),storedSeconds=factNumber(facts.projected_seconds);
      const remainingCandidate=factNumber(remainingSeconds);
      const remaining=Number.isFinite(remainingCandidate)?remainingCandidate:storedSeconds;
      const place=nearbyPlaceText(facts),destination=destinationText(facts),performance=performanceText(facts);
      const details=[];
      const add=value=>{ value=String(value||'').trim(); if(value&&!details.includes(value))details.push(value); };
      let headline='';
      const squawk=String(facts.squawk||'').trim(),emergency=String(facts.emergency||'').trim().toLowerCase();
      if(squawk==='7700')headline='Aircraft is broadcasting emergency squawk 7700.';
      else if(squawk==='7600')headline='Aircraft is broadcasting radio-failure squawk 7600.';
      else if(squawk==='7500')headline='Aircraft is broadcasting emergency squawk 7500.';
      else if(emergency&&emergency!=='none'&&emergency!=='no emergency')headline='Aircraft is broadcasting an emergency state: '+emergency+'.';
      if(!headline&&Number.isFinite(current)){
        if(current<=0.05)headline='Aircraft is directly overhead.';
        else if(current<=0.25)headline='Aircraft is almost directly overhead.';
        else if(current<(1/1.150779448))headline=trend==='receding'?'Aircraft is very close and moving away.':trend==='approaching'?'Aircraft is very close and still approaching.':'Aircraft is very close to your location.';
        else if(trend==='approaching')headline='Aircraft is approaching your location.';
        else if(trend==='receding')headline='Aircraft is moving away from your location.';
      }
      if(Number.isFinite(projected)&&Number.isFinite(remaining)&&remaining>=0&&remaining<=600){
        const when=remaining<=0?'now':'in '+formatCommentaryCountdown(remaining);
        const direction=String(facts.projected_direction||'').trim();
        if(projected<=0.01)add('Current track projects directly over your exact location '+when);
        else if(projected<=0.05)add('Current track projects within '+smartDistanceNm(.05,true)+' of your exact location '+when);
        else add('Projected closest pass '+smartDistanceNm(projected,projected<1)+(direction?' '+direction:'')+' '+when);
      }
      const closest=factNumber(facts.closest_distance_nm),closestAge=factNumber(facts.closest_age_seconds);
      if(trend==='receding'&&Number.isFinite(closest)&&Number.isFinite(closestAge)&&closestAge>=5&&closestAge<=300)add('Closest observed pass '+smartDistanceNm(closest,closest<1)+' \\u00b7 '+Math.round(closestAge)+'s ago');
      add(place);
      add(performance);
      add(destination);
      if(!headline){
        if(performance)headline=performance+'.';
        else if(destination)headline=Boolean(facts.destination_track_aligned)?'Aircraft is tracking towards '+String(facts.destination_name||'its destination')+'.':'Destination is '+String(facts.destination_name||'known')+'.';
        else if(place)headline='Aircraft position resolved over a nearby place.';
        else return '';
        if(details[0]===performance)details.shift();
      }
      const useful=details.filter(item=>item&&item+'.'!==headline).slice(0,3);
      return useful.length?headline+'\\n'+useful.join(' \\u00b7 ')+'.':headline;
    }
    function tickCommentaryCountdown(){
      if(commentaryFacts){
        const remaining=commentaryCountdownDeadline?(commentaryCountdownDeadline-Date.now())/1000:null;
        paintCommentary(richCommentaryText(commentaryFacts,remaining));
        return;
      }
      if(!commentaryCountdownTemplate)return;
      const remaining=(commentaryCountdownDeadline-Date.now())/1000;
      const phrase=remaining>0?'in '+formatCommentaryCountdown(remaining):'now';
      paintCommentary(commentaryCountdownTemplate.replace('{COUNTDOWN}',phrase));
    }
    function renderCommentary(d){
      commentaryFacts=d&&d.commentary_facts&&typeof d.commentary_facts==='object'?d.commentary_facts:null;
      const text=String(d.commentary||'').trim();
      const template=String(d.commentary_template||'').trim();
      const seconds=commentaryFacts?Number(commentaryFacts.projected_seconds):Number(d.commentary_countdown_seconds);
      commentaryFallbackText=text;
      commentaryCountdownDeadline=Number.isFinite(seconds)&&seconds>0?Date.now()+(seconds*1000):0;
      if(commentaryFacts){ tickCommentaryCountdown(); return; }
      if(template.includes('{COUNTDOWN}')&&Number.isFinite(seconds)&&seconds>0){
        commentaryCountdownTemplate=template;
        tickCommentaryCountdown();
        return;
      }
      commentaryCountdownTemplate=''; commentaryCountdownDeadline=0;
      paintCommentary(commentaryFallbackText);
    }
    const altitude=value=>typeof value==='number'?Math.round(value).toLocaleString()+' FT':String(value||'--').toUpperCase();
    function renderAltitude(value){
      const element=byId('altitude');
      element.textContent=altitude(value);
      element.classList.remove('altitude-low','altitude-mid','altitude-high');
      const numeric=Number(value);
      if(!Number.isFinite(numeric))return;
      element.classList.add(numeric<5000?'altitude-low':numeric<15000?'altitude-mid':'altitude-high');
    }
    function vertical(value){ if(typeof value!=='number')return 'LEVEL'; if(value>150)return '\\u2191 '+Math.abs(Math.round(value)).toLocaleString(); if(value<-150)return '\\u2193 '+Math.abs(Math.round(value)).toLocaleString(); return 'LEVEL'; }
    const metricHistoryAges=[30,60,90];
    function historicalMetricValue(metric,sample){
      if(!sample)return '--';
      if(metric==='altitude')return altitude(sample.altitude);
      if(metric==='speed')return sample.speed==null?'--':formatSpeedKnots(sample.speed,0);
      if(metric==='vertical')return vertical(sample.vertical_rate);
      if(metric==='distance')return sample.distance_nm==null?'--':formatDistanceNm(sample.distance_nm,1);
      return '--';
    }
        function renderMetricHistory(history,observations){
      const data=history&&typeof history==='object'?history:{};
            observations=Array.isArray(observations)?observations:null;
            for(let index=0;index<metricHistoryAges.length;index++){
                const age=metricHistoryAges[index];
                const sample=observations?observations[index]||null:data[String(age)]||null;
        for(const metric of ['altitude','speed','vertical','distance']){
          const target=byId(metric+'-history-'+age);
                    if(target){
                        target.textContent=historicalMetricValue(metric,sample);
                        const label=target.parentElement&&target.parentElement.querySelector('.metric-history-age');
                        if(label)label.textContent=String(age)+'s';
                    }
        }
      }
    }
    function fitSingleLine(element,minSize=18){
      element.style.fontSize='';
      const available=element.clientWidth;
      if(!available)return;
      let size=parseFloat(getComputedStyle(element).fontSize);
      while(element.scrollWidth>available&&size>minSize){
        size=Math.max(minSize,size-1);
        element.style.fontSize=size+'px';
      }
    }
    function normaliseAirportHeights(heading){
      const airports=[...heading.querySelectorAll('.route-airport')];
      if(airports.length!==2)return;
      const maskTexts=airports.map(svg=>svg.querySelector('.route-airport-text'));
      if(maskTexts.some(text=>!text))return;
      for(const text of maskTexts)text.removeAttribute('transform');
      const boxes=maskTexts.map(text=>text.getBBox());
      const targetHeight=Math.max(...boxes.map(box=>box.height));
      if(!Number.isFinite(targetHeight)||targetHeight<=0)return;
      airports.forEach((svg,index)=>{
        const box=boxes[index];
        if(!box.height)return;
        const scaleY=targetHeight/box.height;
        const centreY=box.y+box.height/2;
        const transform='translate(0 '+centreY+') scale(1 '+scaleY+') translate(0 '+(-centreY)+')';
        maskTexts[index].setAttribute('transform',transform);
        const outline=svg.querySelector('.route-airport-outline');
        if(outline)outline.setAttribute('transform',transform);
      });
    }
    function regionName(countryCode){
      const code=String(countryCode||'').trim().toUpperCase();
      if(!code)return '';
      try{ return new Intl.DisplayNames(['en'],{type:'region'}).of(code)||code; }catch(_){ return code; }
    }
    function renderDestinationSummary(d,hasAircraft){
      const target=byId('destination-summary');
      if(!target)return;
      const airport=String(d.destination_name||'').trim();
      if(!hasAircraft||!airport){ target.textContent=''; target.hidden=true; return; }
      const country=regionName(d.destination_country);
      target.textContent='Flying to '+airport+(country?', '+country:'')+'.';
      target.hidden=false;
    }
    function renderAircraftPhoto(d,hasAircraft){
            const panel=byId('route-photo-panel'),img=byId('aircraft-photo'),map=routeMapElement;
      if(!panel||!img)return;
            const aircraftId=hasAircraft?String(d.aircraft_id||''):'';
            if(aircraftId!==routePhotoAircraftId){
                clearInterval(routePhotoTimer); routePhotoTimer=null; routePhotoAircraftId=aircraftId; routePhotoRequest++;
                routePhotoRevision=''; routePhotoInPhotoView=false; panel.hidden=true; map.classList.remove('photo-active'); img.removeAttribute('src');
            }
      const available=Boolean(d&&d.aircraft_photo_available);
            if(!hasAircraft||!available){
                routePhotoRequest++;
                clearInterval(routePhotoTimer); routePhotoTimer=null; routePhotoRevision=''; routePhotoInPhotoView=false;
                panel.hidden=true; map.classList.remove('photo-active'); img.removeAttribute('src'); return;
            }
      const revision=String(d.aircraft_photo_revision||'');
      const url='/aircraft-photo?v='+encodeURIComponent(revision)+snapshotAssetSuffix;
            if(routePhotoRevision===revision&&img.getAttribute('src')===url){
                if(img.complete&&img.naturalWidth&&!routePhotoTimer)img.onload();
                return;
            }
            routePhotoRevision=revision;
            const request=++routePhotoRequest;
            panel.hidden=true; map.classList.remove('photo-active'); map.classList.add('visible');
      img.alt=(String(d.registration||'Aircraft').trim()||'Aircraft')+' aircraft photo';
            img.onload=()=>{
                if(request!==routePhotoRequest||aircraftId!==routePhotoAircraftId||img.getAttribute('src')!==url)return;
                clearInterval(routePhotoTimer); routePhotoInPhotoView=false; panel.hidden=true; map.classList.remove('photo-active');
                routePhotoTimer=setInterval(()=>{
                    if(aircraftId!==routePhotoAircraftId||!img.complete||!img.naturalWidth)return;
                    routePhotoInPhotoView=!routePhotoInPhotoView;
                    panel.hidden=!routePhotoInPhotoView;
                    map.classList.toggle('photo-active',routePhotoInPhotoView);
                },10000);
            };
            img.onerror=()=>{
                if(request!==routePhotoRequest||aircraftId!==routePhotoAircraftId)return;
                clearInterval(routePhotoTimer); routePhotoTimer=null; panel.hidden=true; map.classList.remove('photo-active');
            };
            if(img.getAttribute('src')!==url)img.src=url;
            else if(img.complete&&img.naturalWidth)img.onload();
    }
    function renderHeading(d,hasAircraft){
      const heading=byId('callsign'),lookup=byId('route-lookup-status');
      heading.replaceChildren();
      const routeKnown=Boolean(d.route),hasCallsign=Boolean(String(d.callsign||'').trim());
      const routePending=hasAircraft&&hasCallsign&&!routeKnown&&d.route_resolution_status==='pending';
      heading.classList.toggle('route-known',hasAircraft&&routeKnown);
      heading.classList.toggle('route-unknown',hasAircraft&&!routeKnown);
      if(lookup){ lookup.hidden=!routePending; lookup.textContent=routePending?'Looking up route\\u2026':''; }
      byId('wall').classList.toggle('route-unavailable',hasAircraft&&!routeKnown&&!routePending);
      renderDestinationSummary(d,hasAircraft);
      if(!hasAircraft){ heading.textContent=''; return; }
      if(routePending)return;
      if(!routeKnown){ heading.textContent='N/A to N/A'; return; }
      const airport=(side,code,country,available,revision)=>{
        const svg=document.createElementNS(svgNS,'svg'); svg.setAttribute('class','route-airport'); svg.setAttribute('viewBox','0 0 150 100'); svg.setAttribute('role','img'); svg.setAttribute('aria-label',String(code||''));
        const makeText=className=>{ const element=document.createElementNS(svgNS,'text'); element.setAttribute('class',className); element.setAttribute('x','75'); element.setAttribute('y','90'); element.setAttribute('text-anchor','middle'); element.setAttribute('font-family','Segoe UI,Arial,sans-serif'); element.setAttribute('font-size','112'); element.setAttribute('font-weight','800'); element.setAttribute('letter-spacing','-4'); element.setAttribute('textLength','144'); element.setAttribute('lengthAdjust','spacingAndGlyphs'); element.textContent=String(code||''); return element; };
        const text=makeText('route-airport-text');
        if(available){
          const maskId='route-mask-'+side+'-'+String(revision||'').replace(/[^A-Za-z0-9_-]/g,'');
          const defs=document.createElementNS(svgNS,'defs'),mask=document.createElementNS(svgNS,'mask'); mask.setAttribute('id',maskId); mask.setAttribute('maskUnits','userSpaceOnUse'); mask.setAttribute('x','0'); mask.setAttribute('y','0'); mask.setAttribute('width','150'); mask.setAttribute('height','100'); text.setAttribute('fill','white'); mask.appendChild(text); defs.appendChild(mask); svg.appendChild(defs);
          const href='/'+side+'-flag?v='+encodeURIComponent(revision||'')+snapshotAssetSuffix;
          const squareFlag=country==='CH'||country==='VA';
          if(squareFlag){
            const underlay=document.createElementNS(svgNS,'image'); underlay.setAttribute('href',href); underlay.setAttribute('x','0'); underlay.setAttribute('y','0'); underlay.setAttribute('width','150'); underlay.setAttribute('height','100'); underlay.setAttribute('preserveAspectRatio','none'); underlay.setAttribute('mask','url(#'+maskId+')'); svg.appendChild(underlay);
            const image=document.createElementNS(svgNS,'image'); image.setAttribute('href',href); image.setAttribute('x','25'); image.setAttribute('y','0'); image.setAttribute('width','100'); image.setAttribute('height','100'); image.setAttribute('preserveAspectRatio','xMidYMid meet'); image.setAttribute('mask','url(#'+maskId+')'); svg.appendChild(image);
          }else{
            const image=document.createElementNS(svgNS,'image'); image.setAttribute('href',href); image.setAttribute('x','0'); image.setAttribute('y','0'); image.setAttribute('width','150'); image.setAttribute('height','100'); image.setAttribute('preserveAspectRatio','none'); image.setAttribute('mask','url(#'+maskId+')'); svg.appendChild(image);
          }
          const outline=makeText('route-airport-outline'); svg.appendChild(outline);
        }else{
          text.setAttribute('fill','currentColor'); svg.appendChild(text);
          const outline=makeText('route-airport-outline'); svg.appendChild(outline);
        }
        heading.appendChild(svg);
      };
      airport('origin',d.origin,d.origin_country,d.origin_flag_available,d.origin_flag_revision);
      const join=document.createElement('span'); join.className='route-join'; join.textContent='to'; heading.appendChild(join);
      airport('destination',d.destination,d.destination_country,d.destination_flag_available,d.destination_flag_revision);
      normaliseAirportHeights(heading);
    }
    function pulseBrandLogo(){
      const wall=byId('wall');
      clearTimeout(brandPulseTimer);
      wall.classList.add('brand-pulse');
      brandPulseTimer=setTimeout(()=>wall.classList.remove('brand-pulse'),1500);
    }
    function updateSoundButton(){
      const button=byId('sound-toggle');
      button.classList.toggle('muted',!soundEnabled);
      button.classList.toggle('blocked',audioBlocked&&soundEnabled);
      button.setAttribute('aria-pressed',String(soundEnabled));
      button.setAttribute('aria-label',!soundEnabled?'Enable aircraft change sound':audioBlocked?'Enable aircraft sound':'Mute aircraft change sound');
    }
    function toggleSound(){
      if(audioBlocked&&soundEnabled){ playTone(); return; }
      soundEnabled=!soundEnabled;
      audioBlocked=false;
      localStorage.setItem('overhead-sound',soundEnabled?'on':'muted');
      updateSoundButton();
    }
    function updateCommentaryButton(){
      syncCommentaryPresentation();
    }
    function toggleCommentary(){
      const wall=byId('wall');
      if(!wall||!wall.classList.contains('radar-open')||wall.classList.contains('settings-open'))return;
      commentaryEnabled=!commentaryEnabled;
      localStorage.setItem('overhead-commentary',commentaryEnabled?'on':'off');
      syncCommentaryPresentation();
    }
    function refreshUnitSettings(){
      const speed=byId('settings-speed-unit'),distance=byId('settings-distance-unit');
      if(speed)speed.value=speedUnit;
      if(distance)distance.value=distanceUnit;
    }
    function renderUnitSensitiveValues(d,resetCommentary=true){
      if(!d)return;
      byId('speed').textContent=d.speed==null?'--':formatSpeedKnots(d.speed,0);
      byId('distance-main').textContent=formatDistanceValue(d.distance_nm,1);
      byId('distance-unit').textContent=distanceUnitSpec().label;
    renderMetricHistory(d.metric_history,d.metric_history_observations);
      if(resetCommentary)renderCommentary(d); else tickCommentaryCountdown();
      const prefix=d.tracking_active?'AUTO ':'';
      const radarRange=byId('radar-range');
      if(radarRange&&Number.isFinite(Number(d.radar_radius_nm)))radarRange.textContent=displayRadarRangeValue(!d.tracking_active&&radarRangePending!==null?radarRangePending:d.radar_radius_nm,Boolean(d.tracking_active));
    }
    function setSpeedUnit(value){
      if(!validSpeedUnits.has(value))return;
      speedUnit=value; localStorage.setItem('overhead-speed-unit',speedUnit); refreshUnitSettings(); renderUnitSensitiveValues(lastDisplayStatus,false);
    }
    function setDistanceUnit(value){
      if(!validDistanceUnits.has(value))return;
      distanceUnit=value; localStorage.setItem('overhead-distance-unit',distanceUnit); refreshUnitSettings();
      if(!flightTrackingActive){
        const saved=Number(localStorage.getItem(radarRangeKey()));
        const requested=radarRanges.includes(saved)?saved:nearestRadarRange(currentRadarRange*distanceUnitSpec().factor);
        currentRadarRange=radarRangeNm(requested);
        radarManualOverride=true;
        localStorage.setItem(radarRangeKey(),String(requested));
        requestRadarRange(currentRadarRange);
      }
      renderUnitSensitiveValues(lastDisplayStatus,false);
      updateRadarRangeControls();
    }
    function updateFullscreenButton(){
      const active=Boolean(document.fullscreenElement);
      const button=byId('fullscreen-toggle');
      button.classList.toggle('active',active);
      button.setAttribute('aria-pressed',String(active));
      button.setAttribute('aria-label',active?'Exit full screen':'Enter full screen');
    }
    async function toggleFullscreen(){
      try{
        if(document.fullscreenElement)await document.exitFullscreen();
        else await document.documentElement.requestFullscreen();
      }catch(_){}
      updateFullscreenButton();
    }

    async function playTone(){ if(!soundEnabled)return; const tone=byId('alert-tone'); try{ tone.currentTime=0; await tone.play(); audioBlocked=false; }catch(_){ audioBlocked=true; } updateSoundButton(); }
    function locationStatus(message,error=false){
      const element=byId('location-status');
      element.textContent=message;
      element.classList.toggle('error',error);
    }
    function formatCoordinate(value){
      return Number.isFinite(Number(value))?Number(value).toFixed(6):'--';
    }
    function setLocationCandidate(latitude,longitude,accuracy=null,centre=true){
      const lat=Number(latitude),lon=Number(longitude);
      if(!Number.isFinite(lat)||!Number.isFinite(lon)||lat<-90||lat>90||lon<-180||lon>180)return;
    byId('location-latitude').value=formatCoordinate(lat);
    byId('location-longitude').value=formatCoordinate(lon);
      if(locationMap){
        const point=L.latLng(lat,lon);
        if(!locationMarker){
          locationMarker=L.marker(point,{draggable:true}).addTo(locationMap);
          locationMarker.on('dragend',()=>{ const p=locationMarker.getLatLng(); setLocationCandidate(p.lat,p.lng,null,false); locationStatus('Marker moved. Save when you are happy with this home centre.'); });
        }else locationMarker.setLatLng(point);
        if(locationAccuracyCircle){ locationAccuracyCircle.remove(); locationAccuracyCircle=null; }
        if(Number.isFinite(Number(accuracy))&&Number(accuracy)>0){
          locationAccuracyCircle=L.circle(point,{radius:Number(accuracy),color:'#14ecff',weight:1,opacity:.7,fillColor:'#14ecff',fillOpacity:.07,interactive:false}).addTo(locationMap);
        }
        if(centre){
          const currentZoom=locationMap.getZoom();
          const zoom=Number.isFinite(currentZoom)?Math.max(currentZoom,13):13;
          locationMap.setView(point,zoom,{animate:false});
        }
      }
    }
    function initialiseLocationMap(latitude,longitude){
      const lat=Number.isFinite(Number(latitude))?Number(latitude):0;
      const lon=Number.isFinite(Number(longitude))?Number(longitude):0;
      if(locationMap){
        locationMap.setView([lat,lon],12,{animate:false});
        return;
      }
      locationMap=L.map('location-map',{zoomControl:true,attributionControl:true,minZoom:2,maxZoom:19}).setView([lat,lon],12);
      const tiles=L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'});
      tiles.on('tileerror',()=>locationStatus('Map tiles could not be loaded. Your selected coordinates can still be saved.',true));
      tiles.addTo(locationMap);
      locationMap.on('click',event=>{ setLocationCandidate(event.latlng.lat,event.latlng.lng,null,false); locationStatus('Location selected from the map. Save when ready.'); });
    }
    async function openLocationSelector(){
            const revision=++locationDialogRevision;
            modalReturnFocus=document.activeElement;
      byId('location-backdrop').classList.add('open');
            focusModalControl('location-close');
      let lat=Number(currentHomeLatitude),lon=Number(currentHomeLongitude);
      if(!Number.isFinite(lat)||!Number.isFinite(lon)){
        try{
          const response=await fetch('/status?active='+(document.visibilityState==='visible'?'1':'0')+'&t='+Date.now(),{cache:'no-store'});
          if(response.ok){
            const status=await response.json();
            lat=Number(status.home_latitude); lon=Number(status.home_longitude);
            if(Number.isFinite(lat)&&Number.isFinite(lon)){
              currentHomeLatitude=lat; currentHomeLongitude=lon;
              currentLocationRevision=Math.max(currentLocationRevision,Number(status.location_revision)||0);
            }
          }
        }catch(_){}
      }
            if(revision!==locationDialogRevision||!byId('location-backdrop').classList.contains('open'))return;
      if(!Number.isFinite(lat)||!Number.isFinite(lon)){
        closeLocationSelector();
        return;
      }
      initialiseLocationMap(lat,lon);
      requestAnimationFrame(()=>{
                if(revision!==locationDialogRevision||!byId('location-backdrop').classList.contains('open'))return;
        locationMap.invalidateSize(false);
        setLocationCandidate(lat,lon,null,true);
      });
      locationStatus('Choose a point on the map or use your device location.');
    }
    function closeLocationSelector(){
            locationDialogRevision++;
      setLocationButtonBusy(false);
      byId('location-backdrop').classList.remove('open');
      setRadar(true);
            const returnTarget=modalReturnFocus&&modalReturnFocus.isConnected&&modalReturnFocus.getClientRects().length
                ?modalReturnFocus:byId('settings-toggle');
            if(returnTarget)returnTarget.focus();
    modalReturnFocus=null;
    }
    function setLocationButtonBusy(busy){
      const button=byId('location-use-device');
      button.classList.toggle('locating',busy);
      button.disabled=busy;
      button.textContent=busy?'LOCATING...':'USE MY LOCATION';
      button.setAttribute('aria-busy',String(busy));
    }
        function applyManualLocation(){
            const latitude=Number(byId('location-latitude').value),longitude=Number(byId('location-longitude').value);
            if(!Number.isFinite(latitude)||!Number.isFinite(longitude)||latitude < -90||latitude > 90||longitude < -180||longitude > 180){
                locationStatus('Enter a latitude from -90 to 90 and a longitude from -180 to 180.',true);
                return;
            }
            setLocationCandidate(latitude,longitude,null,true);
            locationStatus('Coordinates selected. Save when ready.');
        }
    function useDeviceLocation(){
      if(!navigator.geolocation){
        setLocationButtonBusy(false);
        locationStatus('This browser does not provide device geolocation. Select the position on the map instead.',true);
        return;
      }
      setLocationButtonBusy(true);
      locationStatus('Getting a high-accuracy device location...');
      navigator.geolocation.getCurrentPosition(
        position=>{
          setLocationButtonBusy(false);
          const accuracy=Number(position.coords.accuracy);
          setLocationCandidate(position.coords.latitude,position.coords.longitude,accuracy,true);
          locationStatus(Number.isFinite(accuracy)?'Device location found. Reported accuracy is approximately +/-'+Math.round(accuracy)+' metres.':'Device location found. Review the marker and save.');
        },
        error=>{
          setLocationButtonBusy(false);
          locationStatus('Could not get your device location: '+error.message+'. You can still select the point on the map.',true);
        },
        {enableHighAccuracy:true,timeout:15000,maximumAge:30000}
      );
    }
    async function saveSelectedLocation(){
            const latitude=Number(byId('location-latitude').value),longitude=Number(byId('location-longitude').value);
            if(!Number.isFinite(latitude)||!Number.isFinite(longitude)||latitude < -90||latitude > 90||longitude < -180||longitude > 180){
                locationStatus('Enter a valid latitude and longitude before saving.',true);
        return;
      }
      const button=byId('location-save');
      button.disabled=true;
      locationStatus('Saving location and refreshing nearby aircraft...');
      try{
        const response=await fetch('/location',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude,longitude})});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not save location');
                const responseRevision=Number(data.location_revision)||0;
                if(responseRevision>=currentLocationRevision){
                    currentLocationRevision=responseRevision;
                    currentHomeLatitude=Number(data.latitude); currentHomeLongitude=Number(data.longitude);
                    byId('settings-location-value').textContent=formatCoordinate(currentHomeLatitude)+', '+formatCoordinate(currentHomeLongitude);
                }
        closeLocationSelector();
        update();
      }catch(error){
        locationStatus(error.message||'Could not save location',true);
      }finally{
        button.disabled=false;
      }
    }
    let snapshotToastTimer=null;
    function showSnapshotToast(message,error=false){
      const toast=byId('snapshot-toast');
      if(!toast)return;
      clearTimeout(snapshotToastTimer);
      toast.textContent=message;
      toast.classList.toggle('error',error);
      toast.classList.add('show');
      snapshotToastTimer=setTimeout(()=>toast.classList.remove('show'),2200);
    }
    function snapshotFlash(){
      const flash=byId('snapshot-flash');
      if(!flash)return;
      flash.classList.remove('active');
      void flash.offsetWidth;
      flash.classList.add('active');
      setTimeout(()=>flash.classList.remove('active'),460);
    }
    const snapshotDelay=milliseconds=>new Promise(resolve=>setTimeout(resolve,milliseconds));
    const snapshotWorkingMessages=["Flippin' heck!",'Easy, son!','Bosh!'];
    let snapshotWorkingRun=0;
    function stopSnapshotWorkingMessages(){
      snapshotWorkingRun+=1;
      const label=byId('snapshot-countdown-label');
      if(label)label.classList.remove('message-pulse');
    }
    async function runSnapshotWorkingMessages(){
      const label=byId('snapshot-countdown-label');
      if(!label)return;
      const run=++snapshotWorkingRun;
      for(const message of snapshotWorkingMessages){
        if(run!==snapshotWorkingRun)return;
        label.textContent=message;
        label.classList.remove('message-pulse');
        void label.offsetWidth;
        label.classList.add('message-pulse');
        await snapshotDelay(2000);
      }
      if(run===snapshotWorkingRun)label.classList.remove('message-pulse');
    }
    async function runSnapshotCountdown(){
      const overlay=byId('snapshot-countdown'),label=byId('snapshot-countdown-label'),number=byId('snapshot-countdown-number');
      if(!overlay||!label||!number)return;
      stopSnapshotWorkingMessages();
      overlay.classList.remove('working');
      overlay.classList.add('show');
      overlay.setAttribute('aria-hidden','false');
      label.textContent='SNAPSHOT IN';
      for(const value of ['3','2','1']){
        number.textContent=value;
        number.classList.remove('tick');
        void number.offsetWidth;
        number.classList.add('tick');
        await snapshotDelay(900);
      }
      number.textContent='';
      number.classList.remove('tick');
      overlay.classList.add('working');
    }
    function closeSnapshotCountdown(){
      const overlay=byId('snapshot-countdown'),label=byId('snapshot-countdown-label'),number=byId('snapshot-countdown-number');
      stopSnapshotWorkingMessages();
      if(!overlay)return;
      overlay.classList.remove('show','working');
      overlay.setAttribute('aria-hidden','true');
      if(label)label.textContent='SNAPSHOT IN';
      if(number){ number.textContent=''; number.classList.remove('tick'); }
    }
    function snapshotLabel(item){
      const flight=String(item.commercial_flight||item.callsign||item.registration||'AIRCRAFT').trim();
      const registration=String(item.registration||'').trim();
      const route=item.origin&&item.destination?String(item.origin).toUpperCase()+' to '+String(item.destination).toUpperCase():'';
      return [flight,registration,route].filter(Boolean).join('  \\u00b7  ');
    }
    function snapshotDescription(item){
      const flight=String(item.commercial_flight||item.callsign||item.registration||'AIRCRAFT').trim();
      const registration=String(item.registration||'').trim();
      const aircraft=String(item.aircraft_name||item.aircraft_type||'').trim();
      return [flight,registration,aircraft].filter((value,index,all)=>value&&all.indexOf(value)===index).join('  \\u00b7  ');
    }
    function snapshotRouteDetail(item){
      const route=item.origin&&item.destination?String(item.origin).toUpperCase()+' to '+String(item.destination).toUpperCase():'Route unavailable';
      return route+'  \\u00b7  '+(item.radar?'RADAR OPEN':'RADAR CLOSED');
    }
    function openSnapshotViewer(item,variant){
            viewerReturnFocus=document.activeElement;
      const viewer=byId('snapshot-viewer'),image=byId('snapshot-viewer-image');
      byId('snapshot-viewer-title').textContent=snapshotLabel(item)+'  \\u00b7  '+variant.toUpperCase();
      image.src='/snapshot-image?id='+encodeURIComponent(item.id)+'&variant='+encodeURIComponent(variant)+'&t='+Date.now();
      viewer.classList.add('open');
    focusModalControl('snapshot-viewer-close');
    }
    function closeSnapshotViewer(){
      byId('snapshot-viewer').classList.remove('open');
      byId('snapshot-viewer-image').removeAttribute('src');
    if(viewerReturnFocus&&viewerReturnFocus.isConnected)viewerReturnFocus.focus();
    viewerReturnFocus=null;
    }
    function renderSnapshotGallery(items){
      const grid=byId('snapshot-grid');
      grid.replaceChildren();
      if(!items.length){ const empty=document.createElement('div'); empty.className='snapshot-empty'; empty.textContent='No flight snapshots saved yet.'; grid.appendChild(empty); return; }
      for(const item of items){
        const card=document.createElement('article'); card.className='snapshot-card';
        const head=document.createElement('div'); head.className='snapshot-card-head';
        const title=document.createElement('div'); title.className='snapshot-card-title';
        const timestamp=document.createElement('strong'); timestamp.className='snapshot-card-time'; timestamp.textContent=String(item.captured_display||item.captured_at||'');
        const description=document.createElement('span'); description.className='snapshot-card-description'; description.textContent=snapshotDescription(item);
        const detail=document.createElement('span'); detail.className='snapshot-card-detail'; detail.textContent=snapshotRouteDetail(item);
        title.append(timestamp,description,detail);
        const remove=document.createElement('button'); remove.type='button'; remove.className='snapshot-delete'; remove.textContent='DELETE';
        remove.addEventListener('click',async()=>{ if(!confirm('Delete this desktop and mobile snapshot pair?'))return; const response=await fetch('/snapshots?id='+encodeURIComponent(item.id),{method:'DELETE'}); if(response.ok)await loadSnapshotGallery(); else showSnapshotToast('COULD NOT DELETE SNAPSHOT',true); });
        head.append(title,remove);
        const pair=document.createElement('div'); pair.className='snapshot-pair';
        for(const variant of ['desktop','mobile']){
          const variantBox=document.createElement('div'); variantBox.className='snapshot-variant';
          const button=document.createElement('button'); button.type='button'; button.className='snapshot-thumb'; button.setAttribute('aria-label','Open '+variant+' snapshot');
          const image=document.createElement('img'); image.loading='lazy'; image.alt=variant+' Over-Head snapshot'; image.src='/snapshot-image?id='+encodeURIComponent(item.id)+'&variant='+variant;
          const label=document.createElement('span'); label.textContent=variant.toUpperCase();
          button.append(image,label); button.addEventListener('click',()=>openSnapshotViewer(item,variant));
          const actions=document.createElement('div'); actions.className='snapshot-actions';
          const open=document.createElement('button'); open.type='button'; open.className='snapshot-action'; open.textContent='OPEN'; open.addEventListener('click',()=>openSnapshotViewer(item,variant));
          const download=document.createElement('a'); download.className='snapshot-action'; download.textContent='DOWNLOAD'; download.href='/snapshot-image?id='+encodeURIComponent(item.id)+'&variant='+variant+'&download=1'; download.setAttribute('download','');
          actions.append(open,download); variantBox.append(button,actions); pair.appendChild(variantBox);
        }
        card.append(head,pair); grid.appendChild(card);
      }
    }
    async function loadSnapshotGallery(open=false){
            if(open)modalReturnFocus=document.activeElement;
      try{
        const response=await fetch('/snapshots?t='+Date.now(),{cache:'no-store'});
        if(!response.ok)throw new Error('Could not load snapshots');
        const data=await response.json();
        const items=Array.isArray(data.snapshots)?data.snapshots:[];
        renderSnapshotGallery(items);
        if(open){ setSettings(false); closeSnapshotViewer(); byId('snapshot-backdrop').classList.add('open'); byId('snapshots-toggle').classList.add('active'); byId('snapshots-toggle').setAttribute('aria-pressed','true'); focusModalControl('snapshot-close'); }
      }catch(_){ if(open)showSnapshotToast('COULD NOT OPEN SNAPSHOTS',true); }
    }
    function closeSnapshotGallery(){ closeSnapshotViewer(); byId('snapshot-backdrop').classList.remove('open'); byId('snapshots-toggle').classList.remove('active'); byId('snapshots-toggle').setAttribute('aria-pressed','false'); if(modalReturnFocus&&modalReturnFocus.isConnected)modalReturnFocus.focus(); modalReturnFocus=null; }
    async function captureSnapshot(){
      if(snapshotId)return;
      const wall=byId('wall'),button=byId('camera-toggle');
      if(button.disabled||wall.classList.contains('settings-open')||byId('location-backdrop').classList.contains('open')||byId('snapshot-backdrop').classList.contains('open'))return;
      const radar=wall.classList.contains('radar-open');
      button.disabled=true; button.classList.remove('saved','failed'); button.classList.add('capturing'); button.setAttribute('aria-busy','true');
      try{
        await runSnapshotCountdown();
        const commentary=runSnapshotWorkingMessages();
        const responsePromise=fetch('/snapshots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({radar,sound_enabled:soundEnabled,commentary_enabled:commentaryEnabled,speed_unit:speedUnit,distance_unit:distanceUnit})});
        const [response]=await Promise.all([responsePromise,commentary]);
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not save snapshot');
        closeSnapshotCountdown();
        snapshotFlash(); button.classList.add('saved'); showSnapshotToast('DESKTOP + MOBILE SNAPSHOTS SAVED');
        await loadSnapshotGallery(false);
      }catch(error){ closeSnapshotCountdown(); button.classList.add('failed'); showSnapshotToast(String(error.message||'COULD NOT SAVE SNAPSHOT').toUpperCase(),true); }
      finally{
        closeSnapshotCountdown();
        button.classList.remove('capturing'); button.removeAttribute('aria-busy');
        setTimeout(()=>button.classList.remove('saved','failed'),1800);
        button.disabled=byId('wall').classList.contains('no-aircraft');
      }
    }
        let radarRenderKey='';
        function drawRadar(contacts,radius,trackingActive){
            const items=contacts||[];
            const visualKey=JSON.stringify([radius,Boolean(trackingActive),items.map(c=>[
                c.aircraft_hex,c.callsign,c.x,c.y,c.track,c.flight,c.selected,c.emergency,
            ])]);
            const layer=byId('radar-contacts');
            if(visualKey!==radarRenderKey){
                radarRenderKey=visualKey;
                layer.replaceChildren();
            for(const c of items){
        const canTrack=!trackingActive&&Boolean(c.aircraft_hex&&c.callsign);
        const group=document.createElementNS(svgNS,'g'); group.setAttribute('class','contact'+(canTrack?' trackable':'')+(c.selected?' selected':'')+(c.emergency?' emergency':'')); group.setAttribute('transform','translate('+c.x+' '+c.y+') rotate('+(Number(c.track)||0)+')');
        if(canTrack){
          const activate=()=>selectRadarContact(c);
          group.setAttribute('role','button'); group.setAttribute('tabindex','0'); group.setAttribute('aria-label','Select '+c.callsign+' for tracking');
          group.addEventListener('click',activate);
          group.addEventListener('keydown',event=>{ if(event.key==='Enter'||event.key===' '){ event.preventDefault(); activate(); } });
        }
        if(canTrack){ const hit=document.createElementNS(svgNS,'circle'); hit.setAttribute('r','4.2'); hit.setAttribute('fill','transparent'); group.appendChild(hit); }
        const plane=document.createElementNS(svgNS,'path'); plane.setAttribute('d','M0-2.3L.65-.25 2.3.7 2.3 1.2.6.7.45 2 1.15 2.5 1.15 2.8 0 2.5-1.15 2.8-1.15 2.5-.45 2-.6.7-2.3 1.2-2.3.7-.65-.25Z'); group.appendChild(plane);
        if((contacts||[]).length<=10&&c.flight&&!c.selected){ const label=document.createElementNS(svgNS,'text'); label.textContent=c.flight; label.setAttribute('x','2.8'); label.setAttribute('y','-1.5'); label.setAttribute('transform','rotate('+(0-(Number(c.track)||0))+' 2.8 -1.5)'); group.appendChild(label); }
        layer.appendChild(group);
      }
            }
      if(trackingActive){ currentRadarRange=Number(radius)||25; }
      else if(!radarManualOverride){ currentRadarRange=Number(radius)||25; }
      byId('radar-count').textContent=(contacts||[]).length+' CONTACTS';
      byId('radar-range').textContent=displayRadarRangeValue(trackingActive?radius:currentRadarRange,trackingActive);
      updateRadarRangeControls();
    }
    const isMobileViewport=()=>window.matchMedia('(max-width:700px), (max-width:950px) and (max-height:500px)').matches;
    const radarPreferenceKey=()=>isMobileViewport()?'overhead-mobile-radar-v1':'overhead-radar-v2';
    function refreshSidePanelButtons(){
      const wall=byId('wall'),settingsOpen=wall.classList.contains('settings-open'),radarOpen=wall.classList.contains('radar-open');
      const radarButton=byId('radar-toggle'),settingsButton=byId('settings-toggle');
      radarButton.setAttribute('aria-pressed',String(radarOpen&&!settingsOpen));
      radarButton.textContent=radarOpen&&!settingsOpen?(isMobileViewport()?'FLIGHT':'HIDE RADAR'):'RADAR';
      settingsButton.classList.toggle('active',settingsOpen);
      settingsButton.setAttribute('aria-pressed',String(settingsOpen));
      settingsButton.setAttribute('aria-label',settingsOpen?'Close settings':'Open settings');
    }
    function afterPanelChange(){
      requestAnimationFrame(()=>{ fitSingleLine(byId('registration'),18); const logo=byId('operator-logo'); if(logo&&logo.complete&&logo.naturalWidth)fitLogoArtwork(logo); if(routeMap)routeMap.invalidateSize(false); });
    }
    function setRadar(open){
      const wall=byId('wall');
      wall.classList.remove('settings-open');
      wall.classList.toggle('radar-open',open);
      radarWasOpenBeforeSettings=open;
      localStorage.setItem(radarPreferenceKey(),open?'open':'closed');
      refreshSidePanelButtons();
      updateCommentaryButton();
      afterPanelChange();
    }
    function setSettings(open){
      const wall=byId('wall');
      const currentlyOpen=wall.classList.contains('settings-open');
      if(open&&!currentlyOpen){
        radarWasOpenBeforeSettings=wall.classList.contains('radar-open');
        wall.classList.add('settings-open');
      }else if(!open&&currentlyOpen){
        wall.classList.remove('settings-open');
        wall.classList.toggle('radar-open',radarWasOpenBeforeSettings);
        localStorage.setItem(radarPreferenceKey(),radarWasOpenBeforeSettings?'open':'closed');
      }
      refreshSidePanelButtons();
      updateCommentaryButton();
      afterPanelChange();
    }
    const idleMessages=['Looking out...','Negative contact...','No reported traffic...'];
    let idleMessageIndex=0;
    function rotateIdleMessage(){
      const wall=byId('wall'),message=byId('idle-message');
      if(!wall||!message||flightTrackingActive||(!wall.classList.contains('no-aircraft')&&!wall.classList.contains('loading')))return;
      idleMessageIndex=(idleMessageIndex+1)%idleMessages.length;
      message.textContent=idleMessages[idleMessageIndex];
    }
    function normaliseTrackInput(value){ return String(value||'').toUpperCase().replace(/[^A-Z0-9]/g,''); }
    function syncApiHeartbeat(d){
      const heart=byId('api-heart');
      if(!heart)return;
      // Observe only real aircraft-provider requests, never probe upstream for a heartbeat.
      const providers=d.providers&&typeof d.providers==='object'?d.providers:{};
      const entries=Object.entries(providers);
      const attempted=entries.filter(([,p])=>Number(p&&p.requests||0)>0);
      const failed=attempted.filter(([,p])=>String(p.last_status||'')!=='OK').map(([name,p])=>name+' ('+String(p.last_status||'failed')+')');
      const healthy=failed.length===0;
      heart.classList.toggle('api-unhealthy',!healthy);
      heart.setAttribute('aria-label',healthy?'API heartbeat normal':'API heartbeat failure');
      heart.title=failed.length?'API request failed: '+failed.join(', '):attempted.length?'API heartbeat: last observed requests successful':'API heartbeat: awaiting first response';
      // A new successful upstream request pulses once. Repeated /status polls never pulse.
      const revision=entries.map(([name,p])=>name+':'+Number(p&&p.requests||0)+':'+String(p&&p.last_status||'')).join('|');
      if(snapshotId){ apiHealthRevision=revision; heart.classList.remove('api-pulse'); return; }
      if(revision!==apiHealthRevision){
        const previous=apiHealthRevision;
        apiHealthRevision=revision;
        clearTimeout(apiHeartPulseTimer);
        heart.classList.remove('api-pulse');
        if(previous&&attempted.some(([,p])=>String(p.last_status||'')==='OK')){
          requestAnimationFrame(()=>{
            heart.classList.add('api-pulse');
            apiHeartPulseTimer=setTimeout(()=>heart.classList.remove('api-pulse'),1550);
          });
        }
      }
    }
    function refreshTrackControls(){
      const input=byId('track-flight'),button=byId('track-button'),clear=byId('track-clear');
      const value=normaliseTrackInput(input.value),hasValue=Boolean(value);
      input.readOnly=flightTrackingActive;
      clear.hidden=!hasValue||flightTrackingActive;
      button.textContent=flightTrackingActive?'STOP':'TRACK';
      button.classList.toggle('ready',hasValue&&!flightTrackingActive);
      button.setAttribute('aria-pressed',String(flightTrackingActive));
      button.setAttribute('title',flightTrackingActive?'Stop tracking '+flightTrackingQuery:(hasValue?'Track '+value:'Track this flight'));
    }
    function syncFlightTracking(d){
      flightTrackingActive=Boolean(d.tracking_active);
      flightTrackingQuery=String(d.tracking_query||'');
      const input=byId('track-flight'),button=byId('track-button');
      if(flightTrackingActive)input.value=flightTrackingQuery;
      button.classList.toggle('tracking',flightTrackingActive);
      refreshTrackControls();
      const wall=byId('wall'),trackingMessage=byId('tracking-message');
      const waiting=flightTrackingActive&&!d.tracking_found;
      wall.classList.toggle('tracking-wait',waiting);
      if(trackingMessage)trackingMessage.textContent=waiting?'Tracking '+flightTrackingQuery+'...':'';
    }
    function selectRadarContact(contact){
      if(flightTrackingActive)return;
      const input=byId('track-flight');
      const value=normaliseTrackInput(contact&&contact.callsign);
      const aircraftHex=String(contact&&contact.aircraft_hex||'').toUpperCase();
      if(!value||!/^~?[0-9A-F]{6}$/.test(aircraftHex))return;
      pendingTrackHex=aircraftHex;
      pendingTrackFlight=value;
      input.value=value;
      input.setCustomValidity('');
      input.focus({preventScroll:true});
      refreshTrackControls();
    }
    async function submitFlightTracking(event){
      event.preventDefault();
      const input=byId('track-flight');
      const value=normaliseTrackInput(input.value);
      const startingTrack=!flightTrackingActive;
      try{
        let response;
        if(flightTrackingActive){
          suppressNextAircraftTone=false;
          response=await fetch('/track',{method:'DELETE',cache:'no-store'});
        }else{
          if(!value){ input.focus(); return; }
          suppressNextAircraftTone=true;
          const request={flight:value};
          if(pendingTrackHex&&pendingTrackFlight===value)request.aircraft_hex=pendingTrackHex;
          response=await fetch('/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(request),cache:'no-store'});
        }
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not change flight tracking');
        if(data.flight)input.value=data.flight;
        if(data.tracking){ pendingTrackHex=''; pendingTrackFlight=''; }
        if(isMobileViewport()&&data.tracking)setRadar(false);
        await update();
      }catch(error){
        if(startingTrack)suppressNextAircraftTone=false;
        input.setCustomValidity(error.message||'Could not track flight');
        input.reportValidity();
        setTimeout(()=>input.setCustomValidity(''),1800);
      }
    }
    async function clearTrackSelection(){
      if(flightTrackingActive)return;
      const input=byId('track-flight');
      pendingTrackHex=''; pendingTrackFlight='';
      input.value='';
      input.setCustomValidity('');
      refreshTrackControls();
      input.focus();
    }
    async function changeRadarRange(direction){
      if(flightTrackingActive)return;
      const current=nearestRadarRange(radarRangeUser());
      const index=radarRanges.indexOf(current);
      const next=radarRanges[Math.max(0,Math.min(radarRanges.length-1,index+direction))]; if(next===current)return;
      currentRadarRange=radarRangeNm(next); radarManualOverride=true; localStorage.setItem(radarRangeKey(),String(next));
      byId('radar-range').textContent=next+' '+distanceUnitSpec().label;
      updateRadarRangeControls();
      await requestRadarRange(currentRadarRange);
    }
    async function update(){
        const requestRevision=++statusRequestRevision;
        const rangeRevision=radarRangeRevision;
      try{
        const statusUrl=snapshotId?'/snapshot-state?id='+encodeURIComponent(snapshotId):'/status?active='+(document.visibilityState==='visible'?'1':'0')+'&t='+Date.now(); const response=await fetch(statusUrl,{cache:'no-store'}); if(!response.ok)throw new Error('status'); const d=await response.json();
            if(requestRevision!==statusRequestRevision)return;
        lastDisplayStatus=d;
        statusFailureCount=0;
        updateBrowserTitle(d);
        byId('wall').classList.remove('loading');
        const mode=String(d.mode||'live').toLowerCase(); byId('live').className='live '+mode; byId('mode').textContent=({rate_limited:'Rate Limited',provider_unavailable:'Provider unavailable',reconnecting:'Reconnecting',starting:'Starting',scanning:'Scanning',queued:'Waiting for aircraft data',last_known:'Last known position',position_unavailable:'Position unavailable',cached:'Cached observations',live:'Live',demo:'Demo',error:'Error'})[mode]||mode.replace(/_/g,' ');
        const hasAircraft=Boolean(d.aircraft_id); byId('wall').classList.toggle('no-aircraft',!hasAircraft); byId('wall').classList.toggle('aircraft-active',hasAircraft); if(!hasAircraft)byId('wall').classList.remove('brand-pulse'); renderHeading(d,hasAircraft); const camera=byId('camera-toggle'); if(camera)camera.disabled=!hasAircraft; if(snapshotId){
          if(typeof d.snapshot_sound_enabled==='boolean'){ soundEnabled=d.snapshot_sound_enabled; audioBlocked=false; updateSoundButton(); }
          if(typeof d.snapshot_commentary_enabled==='boolean')commentaryEnabled=d.snapshot_commentary_enabled;
          if(validSpeedUnits.has(String(d.snapshot_speed_unit||'')))speedUnit=String(d.snapshot_speed_unit);
          if(validDistanceUnits.has(String(d.snapshot_distance_unit||'')))distanceUnit=String(d.snapshot_distance_unit);
          refreshUnitSettings(); updateCommentaryButton();
        }
        if(d.aircraft_id&&d.aircraft_id!==trackedAircraftId){
          trackedAircraftId=d.aircraft_id;
          if(suppressNextAircraftTone)suppressNextAircraftTone=false;
          else if(!snapshotId){ if(soundEnabled)pulseBrandLogo(); playTone(); }
        } else if(!d.aircraft_id){ trackedAircraftId=''; }
        byId('registration').textContent=d.flight_details||d.registration||'REGISTRATION UNKNOWN'; byId('aircraft-name').textContent=d.aircraft_name+(d.aircraft_type?'  \\u00b7  '+d.aircraft_type:''); requestAnimationFrame(()=>fitSingleLine(byId('registration'),18));
        const logo=byId('operator-logo'),brandName=byId('route-brand-name'),brandBox=byId('route-brand');
        const airlineLabel=d.airline_name||'';
        brandName.textContent=airlineLabel;
        if(d.logo_available){
          brandBox.style.display='flex';
          logo.onload=()=>{ brandBox.style.display='flex'; logo.style.display='block'; brandName.style.display='none'; requestAnimationFrame(()=>fitLogoArtwork(logo)); };
          logo.onerror=()=>{ logo.style.display='none'; brandName.style.display=airlineLabel?'block':'none'; brandBox.style.display=airlineLabel?'flex':'none'; };
          logo.alt=(airlineLabel||'Airline')+' logo';
          logo.src='/operator-logo?v='+encodeURIComponent(d.logo_revision)+snapshotAssetSuffix;
        }else{
          logo.removeAttribute('src'); logo.style.display='none';
          brandName.style.display=airlineLabel?'block':'none';
          brandBox.style.display=airlineLabel?'flex':'none';
        }
        renderAltitude(d.altitude); byId('vertical').textContent=vertical(d.vertical_rate);
        renderUnitSensitiveValues(d);
        if(Number.isFinite(Number(d.home_latitude))&&Number.isFinite(Number(d.home_longitude))){
                    currentLocationRevision=Math.max(currentLocationRevision,Number(d.location_revision)||0);
          currentHomeLatitude=Number(d.home_latitude); currentHomeLongitude=Number(d.home_longitude);
          byId('settings-location-value').textContent=formatCoordinate(currentHomeLatitude)+', '+formatCoordinate(currentHomeLongitude);
        }
        renderRouteMap(d);
        renderAircraftPhoto(d,hasAircraft);
        syncFlightTracking(d);
        syncApiHeartbeat(d);
        if(rangeRevision===radarRangeRevision)drawRadar(d.radar_contacts,d.radar_radius_nm,d.tracking_active);
        byId('flightaware-credit').hidden=!Boolean(d.flightaware_configured);
        if(d.acquisition_status==='queued'&&d.position_state!=='reported'){
          const position=Number(d.acquisition_queue_position),length=Number(d.acquisition_queue_length);
          const queue=Number.isFinite(position)&&Number.isFinite(length)?' ('+position+' of '+length+')':'';
          const eta=Number.isFinite(Number(d.acquisition_retry_in_seconds))?' - earliest slot about '+formatObservationAge(d.acquisition_retry_in_seconds):'';
          const lastKnown=d.position_state==='last_known'?'Last known position '+formatObservationAge(d.position_age_seconds)+' ago; ':'Position unavailable; ';
          byId('footer-status').textContent=lastKnown+'waiting in shared aircraft queue'+queue+eta;
        }
        else if(d.tracking_acquisition_outcome==='rate_limited')byId('footer-status').textContent='Aircraft updates delayed; retrying when permitted';
        else if(d.tracking_acquisition_outcome==='provider_unavailable')byId('footer-status').textContent='Aircraft updates temporarily unavailable';
        else if(d.tracking_acquisition_outcome==='stale')byId('footer-status').textContent='Only older aircraft positions are available';
        else if(d.acquisition_outcome==='stale')byId('footer-status').textContent='Only older aircraft positions are available';
        else if(d.acquisition_outcome==='invalid_data')byId('footer-status').textContent='Aircraft position data could not be validated';
        else if(d.position_state==='reported')byId('footer-status').textContent='Position reported '+formatObservationAge(d.position_age_seconds)+' ago';
        else if(d.position_state==='last_known')byId('footer-status').textContent='Last known position '+formatObservationAge(d.position_age_seconds)+' ago';
        else if(d.aircraft_id)byId('footer-status').textContent='Aircraft position unavailable';
        else if(d.acquisition_status==='acquiring')byId('footer-status').textContent='Updating aircraft information';
        else if(d.tracking_active&&d.tracking_acquisition_outcome==='not_observed')byId('footer-status').textContent='Selected aircraft not observed in the latest update';
        else byId('footer-status').textContent='Updated '+d.updated;
        if(snapshotId){ requestAnimationFrame(()=>requestAnimationFrame(()=>{ window.__overheadSnapshotReady=true; })); }
      }catch(error){
                if(requestRevision!==statusRequestRevision)return;
        statusFailureCount+=1;
        if(snapshotId||statusFailureCount>=2){ byId('live').className='live reconnecting'; byId('mode').textContent='RECONNECTING'; }
        if(snapshotId)window.__overheadSnapshotError=String(error&&error.message||'snapshot render failed');
      }
    }
    if(snapshotId)document.documentElement.classList.add('snapshot-render');
    loadHeaderLogo();
    const initialRadarPreference=localStorage.getItem(radarPreferenceKey());
    setRadar(snapshotId?snapshotRadar:(isMobileViewport()?initialRadarPreference==='open':initialRadarPreference!=='closed'));
    byId('radar-toggle').addEventListener('click',()=>setRadar(!byId('wall').classList.contains('radar-open')||byId('wall').classList.contains('settings-open')));
    byId('settings-toggle').addEventListener('click',()=>setSettings(!byId('wall').classList.contains('settings-open')));
    document.addEventListener('keydown',event=>{if(event.key.toLowerCase()!=='r'||event.defaultPrevented||event.altKey||event.ctrlKey||event.metaKey||event.shiftKey)return;if(event.target.closest('input,textarea,select,[contenteditable="true"],[role="textbox"]'))return;setRadar(!byId('wall').classList.contains('radar-open')||byId('wall').classList.contains('settings-open'))});
    document.addEventListener('keydown',trapModalTab);
    updateSoundButton();
    updateCommentaryButton();
    refreshUnitSettings();
    byId('sound-toggle').addEventListener('click',toggleSound);
    byId('commentary-toggle').addEventListener('click',toggleCommentary);
    window.addEventListener('resize',()=>{ if(commentaryIsShowing())scheduleCommentaryFit(); },{passive:true});
    byId('settings-speed-unit').addEventListener('change',event=>setSpeedUnit(event.target.value));
    byId('settings-distance-unit').addEventListener('change',event=>setDistanceUnit(event.target.value));
    byId('camera-toggle').addEventListener('click',captureSnapshot);
    byId('snapshots-toggle').addEventListener('click',()=>loadSnapshotGallery(true));
    byId('snapshot-close').addEventListener('click',closeSnapshotGallery);
    byId('snapshot-viewer-close').addEventListener('click',closeSnapshotViewer);
    byId('snapshot-backdrop').addEventListener('click',event=>{ if(event.target===byId('snapshot-backdrop'))closeSnapshotGallery(); });
    byId('fullscreen-toggle').addEventListener('click',toggleFullscreen);
    document.addEventListener('fullscreenchange',updateFullscreenButton);
    updateFullscreenButton();
    byId('track-flight').addEventListener('input',()=>{ if(flightTrackingActive){ byId('track-flight').value=flightTrackingQuery; return; } pendingTrackHex=''; pendingTrackFlight=''; refreshTrackControls(); });
    byId('track-clear').addEventListener('click',clearTrackSelection);
    byId('radar-track-form').addEventListener('submit',submitFlightTracking);
    byId('zoom-in').addEventListener('click',()=>changeRadarRange(-1)); byId('zoom-out').addEventListener('click',()=>changeRadarRange(1));
    byId('settings-location').addEventListener('click',openLocationSelector);
    byId('location-latitude').addEventListener('change',applyManualLocation);
    byId('location-longitude').addEventListener('change',applyManualLocation);
    byId('location-use-device').addEventListener('click',useDeviceLocation);
    byId('location-save').addEventListener('click',saveSelectedLocation);
    byId('location-close').addEventListener('click',closeLocationSelector);
    byId('location-cancel').addEventListener('click',closeLocationSelector);
    byId('location-backdrop').addEventListener('click',event=>{ if(event.target===byId('location-backdrop'))closeLocationSelector(); });
    document.addEventListener('keydown',event=>{
      if(event.key!=='Escape')return;
      if(byId('snapshot-viewer').classList.contains('open'))closeSnapshotViewer();
      else if(byId('snapshot-backdrop').classList.contains('open'))closeSnapshotGallery();
      else if(byId('location-backdrop').classList.contains('open'))closeLocationSelector();
      else if(byId('wall').classList.contains('settings-open'))setSettings(false);
    });
    const savedRange=Number(localStorage.getItem(radarRangeKey())); if(radarRanges.includes(savedRange)){ currentRadarRange=radarRangeNm(savedRange); radarManualOverride=true; requestRadarRange(currentRadarRange); }
    let cursorIdleTimer=null;
    function resetCursorIdle(){
      document.documentElement.classList.remove('cursor-hidden');
      clearTimeout(cursorIdleTimer);
      cursorIdleTimer=setTimeout(()=>document.documentElement.classList.add('cursor-hidden'),10000);
    }
    if(!snapshotId){ document.addEventListener('mousemove',resetCursorIdle,{passive:true}); resetCursorIdle(); }
    window.addEventListener('resize',()=>requestAnimationFrame(()=>{ fitSingleLine(byId('registration'),18); const logo=byId('operator-logo'); if(logo&&logo.complete&&logo.naturalWidth)fitLogoArtwork(logo); }));
    if(snapshotId){ update(); }else{ setInterval(update,2000); setInterval(tickCommentaryCountdown,250); setInterval(rotateIdleMessage,4000); setInterval(advanceRotation,250); update(); loadSnapshotGallery(false); } document.addEventListener('dblclick',toggleFullscreen);
  </script>
</body>
</html>
"""


def circular_difference_degrees(first: float, second: float) -> float:
    """Return the smallest absolute angular difference between two bearings."""
    return abs((float(first) - float(second) + 180.0) % 360.0 - 180.0)


def initial_bearing_degrees(latitude1: float, longitude1: float, latitude2: float, longitude2: float) -> float:
    """Return the initial great-circle bearing from one coordinate to another."""
    lat1 = math.radians(float(latitude1))
    lat2 = math.radians(float(latitude2))
    delta_lon = math.radians(float(longitude2) - float(longitude1))
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_direction(bearing: float) -> str:
    """Return an eight-point lower-case compass direction."""
    directions = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")
    return directions[int((float(bearing) + 22.5) // 45.0) % 8]


def format_projection_eta(seconds: float) -> str:
    """Format a short closest-point ETA without implying schedule precision."""
    total = max(0, int(round(float(seconds))))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if remainder == 0:
        return f"{minutes}m"
    return f"{minutes}m {remainder}s"


def destination_point(latitude: float, longitude: float, bearing_degrees: float, distance_nm: float) -> tuple[float, float]:
    """Project a point along a great-circle path on a spherical Earth."""
    earth_radius_nm = 3440.065
    angular_distance = float(distance_nm) / earth_radius_nm
    bearing = math.radians(float(bearing_degrees))
    latitude1 = math.radians(float(latitude))
    longitude1 = math.radians(float(longitude))
    latitude2 = math.asin(
        math.sin(latitude1) * math.cos(angular_distance)
        + math.cos(latitude1) * math.sin(angular_distance) * math.cos(bearing)
    )
    longitude2 = longitude1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(latitude1),
        math.cos(angular_distance) - math.sin(latitude1) * math.sin(latitude2),
    )
    projected_longitude = (math.degrees(longitude2) + 540.0) % 360.0 - 180.0
    return math.degrees(latitude2), projected_longitude


def projected_closest_approach(
    plane: dict[str, Any] | None,
    home_latitude: float,
    home_longitude: float,
    samples: list[dict[str, Any]],
    now_monotonic: float | None = None,
) -> dict[str, Any]:
    """Project the closest future ground point to home from a stable observed vector.

    The projection is deliberately short-horizon. It treats the current ADS-B track
    as the initial bearing of a great-circle path and uses current ground speed only
    after recent observed movement agrees with both. Position age is subtracted from
    the ETA, so provider/network delay does not make the prediction late. Turning,
    stale, slow or distant aircraft produce no prediction rather than a confident but
    fragile one.
    """
    if not plane:
        return {}
    try:
        latitude = float(plane.get("lat"))
        longitude = float(plane.get("lon"))
        track = float(plane.get("track")) % 360.0
        speed = float(plane.get("gs"))
        home_latitude = float(home_latitude)
        home_longitude = float(home_longitude)
    except (TypeError, ValueError):
        return {}
    values = (latitude, longitude, track, speed, home_latitude, home_longitude)
    if not all(math.isfinite(value) for value in values):
        return {}
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and -90 <= home_latitude <= 90 and -180 <= home_longitude <= 180):
        return {}
    if speed < COMMENTARY_PROJECTION_MIN_SPEED_KT:
        return {}

    current_distance = haversine_nm(latitude, longitude, home_latitude, home_longitude)
    if not math.isfinite(current_distance) or current_distance > COMMENTARY_PROJECTION_MAX_DISTANCE_NM:
        return {}

    usable: list[dict[str, float]] = []
    for sample in samples:
        try:
            observed_at = float(sample.get("observed_at"))
            sample_latitude = float(sample.get("latitude"))
            sample_longitude = float(sample.get("longitude"))
            sample_track = float(sample.get("track")) % 360.0
            sample_speed = float(sample.get("speed"))
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in (observed_at, sample_latitude, sample_longitude, sample_track, sample_speed)):
            usable.append({
                "observed_at": observed_at,
                "latitude": sample_latitude,
                "longitude": sample_longitude,
                "track": sample_track,
                "speed": sample_speed,
            })
    usable.sort(key=lambda item: item["observed_at"])
    if len(usable) < 2:
        return {}
    latest = usable[-1]
    baseline_candidates = [
        sample for sample in usable[:-1]
        if COMMENTARY_MIN_TREND_SPAN_SECONDS
        <= latest["observed_at"] - sample["observed_at"]
        <= COMMENTARY_TREND_WINDOW_SECONDS
    ]
    if not baseline_candidates:
        return {}
    baseline = min(
        baseline_candidates,
        key=lambda sample: abs((latest["observed_at"] - sample["observed_at"]) - 20.0),
    )
    span_seconds = latest["observed_at"] - baseline["observed_at"]
    if circular_difference_degrees(latest["track"], baseline["track"]) > COMMENTARY_PROJECTION_TRACK_STABILITY_DEGREES:
        return {}
    if circular_difference_degrees(track, latest["track"]) > COMMENTARY_PROJECTION_TRACK_STABILITY_DEGREES:
        return {}

    movement_distance = haversine_nm(
        baseline["latitude"], baseline["longitude"], latest["latitude"], latest["longitude"]
    )
    if not math.isfinite(movement_distance) or movement_distance < 0.08:
        return {}
    movement_bearing = initial_bearing_degrees(
        baseline["latitude"], baseline["longitude"], latest["latitude"], latest["longitude"]
    )
    if circular_difference_degrees(movement_bearing, track) > COMMENTARY_PROJECTION_MOTION_TRACK_TOLERANCE_DEGREES:
        return {}
    observed_speed = movement_distance / span_seconds * 3600.0
    speed_tolerance = max(60.0, speed * 0.30)
    if not math.isfinite(observed_speed) or abs(observed_speed - speed) > speed_tolerance:
        return {}

    earth_radius_nm = 3440.065
    angular_distance = current_distance / earth_radius_nm
    bearing_to_home = math.radians(initial_bearing_degrees(latitude, longitude, home_latitude, home_longitude))
    track_radians = math.radians(track)
    bearing_difference = bearing_to_home - track_radians
    cross_track_angle = math.asin(max(-1.0, min(1.0, math.sin(angular_distance) * math.sin(bearing_difference))))
    along_track_angle = math.atan2(
        math.sin(angular_distance) * math.cos(bearing_difference),
        math.cos(angular_distance),
    )
    along_track_nm = along_track_angle * earth_radius_nm
    if along_track_nm <= 0:
        return {}
    seconds_from_observation = along_track_nm / speed * 3600.0
    reference_now = float(now_monotonic) if now_monotonic is not None else latest["observed_at"]
    position_age_seconds = max(0.0, reference_now - latest["observed_at"])
    seconds = seconds_from_observation - position_age_seconds
    if seconds < 3.0 or seconds > COMMENTARY_PROJECTION_MAX_SECONDS:
        return {}

    miss_distance = abs(cross_track_angle) * earth_radius_nm
    if not math.isfinite(miss_distance) or miss_distance > COMMENTARY_PROJECTION_MAX_MISS_NM:
        return {}
    closest_latitude, closest_longitude = destination_point(latitude, longitude, track, along_track_nm)
    miss_bearing = initial_bearing_degrees(home_latitude, home_longitude, closest_latitude, closest_longitude)
    radial_closing_speed = speed * max(0.0, math.cos(bearing_difference))
    return {
        "distance_nm": miss_distance,
        "seconds": seconds,
        "direction": compass_direction(miss_bearing),
        "bearing_degrees": miss_bearing,
        "closest_latitude": closest_latitude,
        "closest_longitude": closest_longitude,
        "closing_speed_kt": radial_closing_speed,
        "track_degrees": track,
        "speed_kt": speed,
        "observed_speed_kt": observed_speed,
        "position_age_seconds": position_age_seconds,
    }



class NearbyPlaceStore:
    """Resolve the selected aircraft to a nearby named settlement without blocking live refreshes."""

    SETTLEMENT_KEYS = ("village", "town", "city", "hamlet", "municipality", "suburb", "neighbourhood")

    def __init__(self, cache_path: Path = Path("cache/nearby-places.json")) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._inflight = False
        self._last_request_at = 0.0
        self._failures: dict[str, float] = {}
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            self._records = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            self._records: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _cell(latitude: float, longitude: float) -> str:
        step = NEARBY_PLACE_CELL_DEGREES
        lat = round(latitude / step) * step
        lon = round(longitude / step) * step
        return f"{lat:.2f},{lon:.2f}"

    @classmethod
    def _normalise(cls, payload: dict[str, Any]) -> dict[str, Any]:
        address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
        name = ""
        kind = ""
        for key in cls.SETTLEMENT_KEYS:
            candidate = str(address.get(key) or "").strip()
            if candidate:
                name = candidate
                kind = key
                break
        if not name:
            addresstype = str(payload.get("addresstype") or payload.get("type") or "").strip().lower()
            display_name = str(payload.get("name") or "").strip()
            if addresstype in cls.SETTLEMENT_KEYS and display_name:
                name = display_name
                kind = addresstype
        try:
            latitude = float(payload.get("lat"))
            longitude = float(payload.get("lon"))
        except (TypeError, ValueError):
            return {}
        if not name or not math.isfinite(latitude) or not math.isfinite(longitude):
            return {}
        return {"name": name, "kind": kind or "place", "latitude": latitude, "longitude": longitude}

    def _save(self) -> None:
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.cache_path)

    def _lookup(self, cell: str, latitude: float, longitude: float) -> None:
        try:
            query = urlencode({
                "format": "jsonv2",
                "lat": f"{latitude:.6f}",
                "lon": f"{longitude:.6f}",
                "zoom": "13",
                "addressdetails": "1",
                "accept-language": "en",
            })
            request = urllib.request.Request(
                f"{NOMINATIM_REVERSE_URL}?{query}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)
            record = self._normalise(payload if isinstance(payload, dict) else {})
            with self._lock:
                if record:
                    self._records[cell] = record
                    self._failures.pop(cell, None)
                    try:
                        self._save()
                    except OSError:
                        pass
                else:
                    self._failures[cell] = time.monotonic()
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            with self._lock:
                self._failures[cell] = time.monotonic()
        finally:
            with self._lock:
                self._inflight = False

    def get(self, latitude: Any, longitude: Any) -> dict[str, Any]:
        try:
            current_latitude = float(latitude)
            current_longitude = float(longitude)
        except (TypeError, ValueError):
            return {}
        if not math.isfinite(current_latitude) or not math.isfinite(current_longitude):
            return {}
        cell = self._cell(current_latitude, current_longitude)
        now = time.monotonic()
        with self._lock:
            cached = dict(self._records.get(cell) or {})
            failed_at = self._failures.get(cell)
            can_retry = failed_at is None or now - failed_at >= NEARBY_PLACE_RETRY_SECONDS
            if (
                not cached
                and can_retry
                and not self._inflight
                and now - self._last_request_at >= NEARBY_PLACE_REQUEST_INTERVAL_SECONDS
            ):
                self._inflight = True
                self._last_request_at = now
                threading.Thread(
                    target=self._lookup,
                    args=(cell, current_latitude, current_longitude),
                    daemon=True,
                ).start()
        if not cached:
            return {}
        try:
            distance_nm = haversine_nm(
                current_latitude, current_longitude,
                float(cached["latitude"]), float(cached["longitude"]),
            )
        except (KeyError, TypeError, ValueError):
            return {}
        distance_miles = distance_nm * 1.150779448
        if not math.isfinite(distance_miles) or distance_miles > NEARBY_PLACE_MAX_DISTANCE_MILES:
            return {}
        cached["distance_miles"] = distance_miles
        return cached


def nearby_place_commentary(place: dict[str, Any] | None) -> str:
    """Describe a named settlement only when its mapped centre is within one statute mile."""
    place = place or {}
    name = str(place.get("name") or "").strip()
    kind = str(place.get("kind") or "place").strip().lower()
    try:
        distance_miles = float(place.get("distance_miles"))
    except (TypeError, ValueError):
        return ""
    if not name or not math.isfinite(distance_miles) or distance_miles < 0 or distance_miles > NEARBY_PLACE_MAX_DISTANCE_MILES:
        return ""
    centre_label = {
        "village": "village centre",
        "town": "town centre",
        "city": "city centre",
        "hamlet": "hamlet centre",
        "municipality": "centre",
        "suburb": "centre",
        "neighbourhood": "centre",
    }.get(kind, "centre")
    if distance_miles <= 0.15:
        return f"Almost directly over {name} {centre_label}."
    return f"Over {name}, {distance_miles:.1f} miles from the {centre_label}."

def build_aircraft_commentary(
    samples: list[dict[str, Any]],
    current_distance_nm: float | None,
    vertical_rate: Any,
    now_monotonic: float,
    projection: dict[str, Any] | None = None,
    countdown_phrase: str | None = None,
    nearby_place: dict[str, Any] | None = None,
) -> str:
    """Build useful commentary from motion, closest-approach maths and nearby settlements."""
    try:
        distance = float(current_distance_nm) if current_distance_nm is not None else None
    except (TypeError, ValueError):
        distance = None
    if distance is None or not math.isfinite(distance) or distance < 0:
        return ""

    valid_samples = []
    for sample in samples:
        try:
            observed_at = float(sample.get("observed_at"))
            sample_distance = float(sample.get("distance_nm"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(observed_at) and math.isfinite(sample_distance) and sample_distance >= 0:
            valid_samples.append({"observed_at": observed_at, "distance_nm": sample_distance})
    valid_samples.sort(key=lambda item: item["observed_at"])

    trend = ""
    if len(valid_samples) >= 2:
        latest = valid_samples[-1]
        baseline_candidates = [
            sample for sample in valid_samples[:-1]
            if COMMENTARY_MIN_TREND_SPAN_SECONDS
            <= latest["observed_at"] - sample["observed_at"]
            <= COMMENTARY_TREND_WINDOW_SECONDS
        ]
        if baseline_candidates:
            baseline = min(
                baseline_candidates,
                key=lambda sample: abs((latest["observed_at"] - sample["observed_at"]) - 20.0),
            )
            change = latest["distance_nm"] - baseline["distance_nm"]
            if change <= -COMMENTARY_MIN_TREND_CHANGE_NM:
                trend = "approaching"
            elif change >= COMMENTARY_MIN_TREND_CHANGE_NM:
                trend = "receding"

    def distance_text(value: float, precise: bool = False) -> str:
        if precise or value < 1.0:
            return f"{value:.2f} NM"
        return f"{value:.1f} NM"

    place_detail = nearby_place_commentary(nearby_place)
    projection = projection or {}
    try:
        projected_distance = float(projection.get("distance_nm"))
        projected_seconds = float(projection.get("seconds"))
    except (TypeError, ValueError):
        projected_distance = float("nan")
        projected_seconds = float("nan")

    projection_detail = ""
    if (
        math.isfinite(projected_distance)
        and math.isfinite(projected_seconds)
        and 3 <= projected_seconds <= COMMENTARY_PROJECTION_MAX_SECONDS
    ):
        eta_phrase = countdown_phrase if countdown_phrase is not None else f"in {format_projection_eta(projected_seconds)}"
        direction = str(projection.get("direction") or "").strip()
        if projected_distance <= 0.01:
            projection_detail = f"Track projects directly over your exact location {eta_phrase}."
        elif projected_distance <= COMMENTARY_DIRECT_OVERHEAD_NM:
            projection_detail = f"Track projects within 0.05 NM of your exact location {eta_phrase}."
        else:
            miss = distance_text(projected_distance, precise=projected_distance < 1.0)
            side = f" {direction}" if direction else ""
            projection_detail = f"Projected closest pass: {miss}{side} {eta_phrase}."

    closest = min(valid_samples, key=lambda sample: sample["distance_nm"], default=None)
    closest_index = valid_samples.index(closest) if closest is not None else -1
    closest_age = max(0, int(round(now_monotonic - closest["observed_at"]))) if closest is not None else None
    closest_detail = ""
    if (
        trend == "receding"
        and closest is not None
        and closest_age is not None
        and 0 < closest_index < len(valid_samples) - 1
        and 5 <= closest_age <= 180
        and distance >= closest["distance_nm"] + COMMENTARY_MIN_TREND_CHANGE_NM
    ):
        closest_detail = f"Closest observed pass: {distance_text(closest['distance_nm'])}, {closest_age}s ago."

    def join_commentary(headline: str, *details: str) -> str:
        useful = [item.strip().rstrip(".") for item in details if item and item.strip()]
        if not useful:
            return headline
        return headline + "\n" + " Â· ".join(useful[:2]) + "."

    if distance <= COMMENTARY_DIRECT_OVERHEAD_NM:
        return join_commentary("Aircraft is directly overhead.", place_detail)
    if distance <= COMMENTARY_ALMOST_OVERHEAD_NM:
        return join_commentary("Aircraft is almost directly overhead.", place_detail, projection_detail)
    if distance < COMMENTARY_ONE_STATUTE_MILE_NM:
        if trend == "receding":
            return join_commentary("Aircraft is less than 1 mile away and moving away.", closest_detail, place_detail)
        if trend == "approaching":
            return join_commentary("Aircraft is less than 1 mile away and still approaching.", place_detail, projection_detail)
        return join_commentary("Aircraft is less than 1 mile from your location.", place_detail, projection_detail)

    if trend == "approaching":
        return join_commentary("Aircraft is approaching your location.", place_detail, projection_detail)
    if trend == "receding":
        return join_commentary("Aircraft is moving away from your location.", closest_detail, place_detail)

    if projection_detail:
        return join_commentary("Aircraft track is now stable enough to predict its closest approach.", place_detail, projection_detail)
    if place_detail:
        return join_commentary("Aircraft position has been resolved against a nearby settlement.", place_detail)

    try:
        rate = float(vertical_rate)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(rate):
        return ""
    if rate >= 300:
        return "Aircraft is climbing; horizontal approach trend is not yet established."
    if rate <= -300:
        return "Aircraft is descending; horizontal approach trend is not yet established."
    if abs(rate) <= 100:
        return "Aircraft is level; horizontal approach trend is not yet established."
    return ""


def build_commentary_facts(
    samples: list[dict[str, Any]],
    current_distance_nm: float | None,
    plane: dict[str, Any],
    route: dict[str, Any],
    projection: dict[str, Any] | None,
    nearby_place: dict[str, Any] | None,
    metric_history: dict[str, dict[str, Any] | None],
    now_monotonic: float,
) -> dict[str, Any]:
    """Return structured facts for unit-aware, conservative browser commentary."""
    def finite(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    distance = finite(current_distance_nm)
    facts: dict[str, Any] = {
        "current_distance_nm": distance,
        "altitude_ft": finite(plane.get("alt_baro")),
        "altitude_geom_ft": finite(plane.get("alt_geom")),
        "ground_speed_kt": finite(plane.get("gs")),
        "ias_kt": finite(plane.get("ias")),
        "tas_kt": finite(plane.get("tas")),
        "mach": finite(plane.get("mach")),
        "vertical_rate_fpm": finite(plane.get("baro_rate")),
        "geom_rate_fpm": finite(plane.get("geom_rate")),
        "track_deg": finite(plane.get("track")),
        "track_rate_deg_s": finite(plane.get("track_rate")),
        "roll_deg": finite(plane.get("roll")),
        "true_heading_deg": finite(plane.get("true_heading")),
        "mag_heading_deg": finite(plane.get("mag_heading")),
        "selected_altitude_ft": None,
        "selected_altitude_source": "",
        "selected_heading_deg": finite(plane.get("nav_heading")),
        "qnh_hpa": finite(plane.get("nav_qnh")),
        "wind_speed_kt": finite(plane.get("wind_speed") if plane.get("wind_speed") is not None else plane.get("ws")),
        "wind_dir_deg": finite(plane.get("wind_dir") if plane.get("wind_dir") is not None else plane.get("wd")),
        "oat_c": finite(plane.get("oat")),
        "tat_c": finite(plane.get("tat")),
        "squawk": str(plane.get("squawk") or "").strip(),
        "emergency": str(plane.get("emergency") or "").strip(),
        "position_age_seconds": finite(plane.get("seen_pos")),
        "destination_name": str(route.get("destination_name") or route.get("destination") or "").strip(),
        "destination_country": str(route.get("destination_country") or "").strip().upper(),
        "destination_distance_nm": None,
        "destination_bearing_deg": None,
        "destination_track_delta_deg": None,
        "destination_track_aligned": False,
        "destination_minutes_at_ground_speed": None,
        "route_source": str(route.get("route_source") or "").strip(),
        "nearby_place_name": str((nearby_place or {}).get("name") or "").strip(),
        "nearby_place_kind": str((nearby_place or {}).get("kind") or "").strip(),
        "nearby_place_distance_nm": None,
    }

    for key, label in (("nav_altitude_mcp", "MCP"), ("nav_altitude_fms", "FMS")):
        selected = finite(plane.get(key))
        if selected is not None and selected > 0:
            facts["selected_altitude_ft"] = selected
            facts["selected_altitude_source"] = label
            break

    raw_modes = plane.get("nav_modes")
    if isinstance(raw_modes, str):
        modes = [part.strip() for part in re.split(r"[,; ]+", raw_modes) if part.strip()]
    elif isinstance(raw_modes, (list, tuple, set)):
        modes = [str(part).strip() for part in raw_modes if str(part).strip()]
    else:
        modes = []
    facts["nav_modes"] = modes

    if nearby_place:
        place_miles = finite(nearby_place.get("distance_miles"))
        if place_miles is not None and place_miles >= 0:
            facts["nearby_place_distance_nm"] = place_miles / 1.150779448

    valid_samples: list[dict[str, float]] = []
    for sample in samples:
        observed_at = finite(sample.get("observed_at"))
        sample_distance = finite(sample.get("distance_nm"))
        if observed_at is not None and sample_distance is not None and sample_distance >= 0:
            valid_samples.append({"observed_at": observed_at, "distance_nm": sample_distance})
    valid_samples.sort(key=lambda item: item["observed_at"])

    trend = ""
    if len(valid_samples) >= 2:
        latest = valid_samples[-1]
        baselines = [
            sample for sample in valid_samples[:-1]
            if COMMENTARY_MIN_TREND_SPAN_SECONDS <= latest["observed_at"] - sample["observed_at"] <= COMMENTARY_TREND_WINDOW_SECONDS
        ]
        if baselines:
            baseline = min(baselines, key=lambda sample: abs((latest["observed_at"] - sample["observed_at"]) - 20.0))
            change = latest["distance_nm"] - baseline["distance_nm"]
            if change <= -COMMENTARY_MIN_TREND_CHANGE_NM:
                trend = "approaching"
            elif change >= COMMENTARY_MIN_TREND_CHANGE_NM:
                trend = "receding"
    facts["trend"] = trend

    closest = min(valid_samples, key=lambda sample: sample["distance_nm"], default=None)
    if closest is not None and trend == "receding":
        index = valid_samples.index(closest)
        age = max(0.0, now_monotonic - closest["observed_at"])
        if 0 < index < len(valid_samples) - 1 and 5 <= age <= 300 and distance is not None and distance >= closest["distance_nm"] + COMMENTARY_MIN_TREND_CHANGE_NM:
            facts["closest_distance_nm"] = closest["distance_nm"]
            facts["closest_age_seconds"] = age

    projection = projection or {}
    facts["projected_distance_nm"] = finite(projection.get("distance_nm"))
    facts["projected_seconds"] = finite(projection.get("seconds"))
    facts["projected_direction"] = str(projection.get("direction") or "").strip()
    facts["projected_closing_speed_kt"] = finite(projection.get("closing_speed_kt"))

    latitude = finite(plane.get("lat"))
    longitude = finite(plane.get("lon"))
    destination_latitude = finite(route.get("destination_latitude"))
    destination_longitude = finite(route.get("destination_longitude"))
    track = finite(plane.get("track"))
    ground_speed = finite(plane.get("gs"))
    if None not in (latitude, longitude, destination_latitude, destination_longitude):
        remaining = haversine_nm(latitude, longitude, destination_latitude, destination_longitude)
        bearing = initial_bearing_degrees(latitude, longitude, destination_latitude, destination_longitude)
        facts["destination_distance_nm"] = remaining
        facts["destination_bearing_deg"] = bearing
        if track is not None:
            delta = circular_difference_degrees(track % 360.0, bearing)
            facts["destination_track_delta_deg"] = delta
            facts["destination_track_aligned"] = delta <= 20.0
        if ground_speed is not None and ground_speed >= 30:
            facts["destination_minutes_at_ground_speed"] = remaining / ground_speed * 60.0

    current_rate = finite(plane.get("baro_rate"))
    phase = "descending" if current_rate is not None and current_rate <= -300 else "climbing" if current_rate is not None and current_rate >= 300 else ""
    if phase:
        previous_age = 0
        for age in METRIC_HISTORY_AGES_SECONDS:
            sample = metric_history.get(str(age)) or {}
            sample_rate = finite(sample.get("vertical_rate"))
            sample_phase = "descending" if sample_rate is not None and sample_rate <= -300 else "climbing" if sample_rate is not None and sample_rate >= 300 else ""
            if sample_rate is not None and sample_phase != phase:
                facts["vertical_phase_window_start_seconds"] = previous_age
                facts["vertical_phase_window_end_seconds"] = age
                break
            previous_age = age

    return facts

def commentary_observation_time(plane: dict[str, Any], now_monotonic: float) -> float | None:
    """Translate ADS-B seen_pos age into a monotonic observation timestamp."""
    if not isinstance(plane, dict):
        return None
    try:
        seen_pos = float(plane.get("seen_pos"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seen_pos) or seen_pos < 0 or seen_pos > 20:
        return None
    return now_monotonic - seen_pos


def empty_metric_history() -> dict[str, dict[str, Any] | None]:
    """Return empty immutable-age metric-history slots."""
    return {str(age): None for age in METRIC_HISTORY_AGES_SECONDS}


def previous_metric_observations(
    observations: list[dict[str, Any]], now_monotonic: float, max_age_seconds: float,
) -> list[dict[str, Any] | None]:
    """Return genuine prior samples nearest to the fixed 30/60/90-second slots."""
    slots: dict[str, dict[str, Any] | None] = {
        str(target): None for target in METRIC_HISTORY_AGES_SECONDS
    }
    tolerance = METRIC_HISTORY_STEP_SECONDS / 2
    maximum_age = min(float(max_age_seconds), METRIC_HISTORY_AGES_SECONDS[-1] + tolerance)
    for sample in observations[:-1]:
        try:
            age = float(now_monotonic) - float(sample["observed_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(age) or age <= 0 or age > maximum_age:
            continue
        target = min(METRIC_HISTORY_AGES_SECONDS, key=lambda value: (abs(age - value), value))
        if abs(age - target) > tolerance:
            continue
        key = str(target)
        current = slots[key]
        if current is None or abs(age - target) < abs(float(current["age_seconds"]) - target):
            slots[key] = {
                "age_seconds": age,
                "altitude": sample.get("altitude"),
                "speed": sample.get("speed"),
                "vertical_rate": sample.get("vertical_rate"),
                "distance_nm": sample.get("distance_nm"),
            }
    return [slots[str(target)] for target in METRIC_HISTORY_AGES_SECONDS]


def shift_metric_history(
    history: dict[str, dict[str, Any] | None],
    pending: dict[str, Any] | None,
    steps: int = 1,
) -> dict[str, dict[str, Any] | None]:
    """Age one locked sample through 30/60/90-second slots without reselecting it."""
    shifted = {
        str(age): (dict(history.get(str(age))) if history.get(str(age)) is not None else None)
        for age in METRIC_HISTORY_AGES_SECONDS
    }
    carry = dict(pending) if pending is not None else None
    for _ in range(max(0, int(steps))):
        shifted["90"] = shifted["60"]
        shifted["60"] = shifted["30"]
        shifted["30"] = carry
        carry = None
    return shifted


class FrameState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.route_failure_log: RouteFailureLog | None = None
        self.last_enrichment_future: concurrent.futures.Future[Any] | None = None
        self.location_lock = threading.Lock()
        self.location_revision = 0
        self.png = b""
        self.logo = b""
        self.logo_type = "image/svg+xml"
        self.aircraft_photo = b""
        self.aircraft_photo_type = "image/jpeg"
        self.origin_flag = b""
        self.destination_flag = b""
        self.last_live_aircraft: list[dict[str, Any]] = []
        self.last_live_aircraft_received_at: float | None = None
        self.last_live_aircraft_query_radius_nm: float | None = None
        self.has_live_success = False
        self.live_fetch_failures = 0
        self.last_acquisition_attempted_at: float | None = None
        self.last_acquisition_succeeded_at: float | None = None
        self.position_observed_at_monotonic: float | None = None
        self.position_aircraft_id = ""
        self.metric_history_aircraft_id = ""
        self.metric_history = empty_metric_history()
        self.metric_history_pending: dict[str, Any] | None = None
        self.metric_history_last_shift_at: float | None = None
        self.metric_observation_aircraft_id = ""
        self.metric_observations: list[dict[str, Any]] = []
        self.metric_observation_key: tuple[Any, ...] | None = None
        self.last_tracking_result: dict[str, Any] | None = None
        self.last_automatic_tracking_result: dict[str, Any] | None = None
        self.commentary_aircraft_id = ""
        self.commentary_samples: list[dict[str, Any]] = []
        self.payload: dict[str, Any] = {"mode": "starting", "updated": "never", "total": 0, "location_revision": 0}

    def update(self, image, mode: str, plane: dict[str, Any] | None, distance: float | None, aircraft: list[dict[str, Any]], settings, logo: tuple[bytes, str, str] | None, identity: dict[str, str] | None = None, route: dict[str, Any] | None = None, origin_flag: bytes | None = None, destination_flag: bytes | None = None, flightaware_configured: bool = False, tracking: dict[str, Any] | None = None, api_health: dict[str, Any] | None = None, position_observed_at: float | None = None, nearby_place: dict[str, Any] | None = None, aircraft_photo: tuple[bytes, str, str] | None = None, aircraft_photo_debug: dict[str, Any] | None = None, expected_location_revision: int | None = None, reported_position_observed_at: float | None = None, new_position_observation: bool = False, preserve_radar_radius: bool = False) -> None:
        data = BytesIO()
        image.save(data, format="PNG")
        plane = plane or {}
        identity = identity or {}
        route = route or {}
        tracking = tracking or {}
        api_health = api_health or {"ok": False, "checked_at": 0, "services": {}}
        nearby_place = nearby_place or {}
        aircraft_photo_debug = dict(aircraft_photo_debug or {})
        registration = identity.get("registration") or str(plane.get("r") or "").strip()
        code = identity.get("operator_code") or operator_code(plane)
        logo_data, logo_type, logo_source = logo or (b"", "", "")
        photo_data, photo_type, photo_revision = aircraft_photo or (b"", "", "")
        payload = {
            "mode": mode,
            "updated": time.strftime("%H:%M:%S"),
            "total": len(aircraft),
            "aircraft_id": aircraft_identity(plane),
            "callsign": str(plane.get("flight") or "").strip(),
            "route": route.get("route", ""),
            "route_source": route.get("route_source", ""),
            "commercial_flight": route.get("commercial_flight", ""),
            "flightaware_configured": bool(flightaware_configured),
            "api_health_ok": bool(api_health.get("ok")),
            "api_health_checked_at": int(api_health.get("checked_at") or 0),
            "api_health_services": dict(api_health.get("services") or {}),
            "tracking_active": bool(tracking.get("active")),
            "tracking_query": str(tracking.get("query") or ""),
            "tracking_resolved_callsign": str(tracking.get("resolved_callsign") or ""),
            "tracking_found": bool(tracking.get("found")),
            "tracking_acquisition_outcome": tracking.get("acquisition_outcome", "waiting"),
            "acquisition_outcome": mode if mode in {"stale", "invalid_data"} else tracking.get("acquisition_outcome", mode),
            "acquisition_interval_seconds": TRACKING_ACQUISITION_INTERVAL_SECONDS if tracking.get("active") or plane else NORMAL_ACQUISITION_INTERVAL_SECONDS,
            "observation_source": plane.get("source"),
            "observation_timestamp": plane.get("observed_at"),
            "origin": route.get("origin", ""), "destination": route.get("destination", ""),
            "origin_name": route.get("origin_name", ""), "destination_name": route.get("destination_name", ""),
            "origin_country": route.get("origin_country", ""), "destination_country": route.get("destination_country", ""),
            "origin_latitude": route.get("origin_latitude"), "origin_longitude": route.get("origin_longitude"),
            "destination_latitude": route.get("destination_latitude"), "destination_longitude": route.get("destination_longitude"),
            "origin_flag_available": bool(origin_flag), "destination_flag_available": bool(destination_flag),
            "origin_flag_revision": hashlib.sha256(origin_flag).hexdigest()[:12] if origin_flag else "",
            "destination_flag_revision": hashlib.sha256(destination_flag).hexdigest()[:12] if destination_flag else "",
            "registration": registration,
            "flight_details": flight_details(plane, registration),
            "aircraft_type": str(plane.get("t") or "").strip() or identity.get("aircraft_type", ""),
            "aircraft_name": identity.get("aircraft_name") or aircraft_name(plane.get("t"), plane.get("desc")),
            "altitude": plane.get("alt_baro"), "speed": plane.get("gs"), "vertical_rate": plane.get("baro_rate"),
            "track": plane.get("track") or 0, "distance_nm": distance, "squawk": plane.get("squawk"), "emergency": plane.get("emergency"),
            "aircraft_latitude": plane.get("lat"), "aircraft_longitude": plane.get("lon"),
            "telemetry": {
                "alt_geom": plane.get("alt_geom"), "ias": plane.get("ias"), "tas": plane.get("tas"), "mach": plane.get("mach"),
                "track_rate": plane.get("track_rate"), "roll": plane.get("roll"),
                "true_heading": plane.get("true_heading"), "mag_heading": plane.get("mag_heading"),
                "geom_rate": plane.get("geom_rate"), "nav_qnh": plane.get("nav_qnh"),
                "nav_altitude_mcp": plane.get("nav_altitude_mcp"), "nav_altitude_fms": plane.get("nav_altitude_fms"),
                "nav_heading": plane.get("nav_heading"), "nav_modes": plane.get("nav_modes"),
                "wind_speed": plane.get("wind_speed") if plane.get("wind_speed") is not None else plane.get("ws"),
                "wind_dir": plane.get("wind_dir") if plane.get("wind_dir") is not None else plane.get("wd"),
                "oat": plane.get("oat"), "tat": plane.get("tat"), "category": plane.get("category"),
                "nic": plane.get("nic"), "nac_p": plane.get("nac_p"), "nac_v": plane.get("nac_v"), "sil": plane.get("sil"),
                "messages": plane.get("messages"), "rssi": plane.get("rssi"), "seen": plane.get("seen"), "seen_pos": plane.get("seen_pos"),
            },
            "nearby_place_name": nearby_place.get("name", ""),
            "nearby_place_kind": nearby_place.get("kind", ""),
            "nearby_place_distance_miles": nearby_place.get("distance_miles"),
            "operator_code": code, "airline_name": identity.get("airline_name", ""), "logo_available": bool(logo_data),
            "aircraft_photo_url": identity.get("photo_url", ""),
            "aircraft_photo_thumbnail_url": identity.get("photo_thumbnail_url", ""),
            "aircraft_photo_available": bool(photo_data),
            "aircraft_photo_revision": photo_revision,
            "aircraft_photo_source": "Local cache from ADSBDB / airport-data.com" if photo_data else "",
            "aircraft_photo_debug": aircraft_photo_debug,
            "logo_source": logo_source,
            "logo_revision": f"{code}-{hashlib.sha256(logo_data).hexdigest()[:12]}" if logo_data else code,
            "home_latitude": settings.latitude,
            "home_longitude": settings.longitude,
            "radar_radius_nm": settings.radius_nm,
            "radar_contacts": radar_contacts(aircraft, settings, plane),
        }
        history_now = time.monotonic()
        current_aircraft_id = str(payload.get("aircraft_id") or "")
        current_metric_sample = {
            "altitude": payload.get("altitude"),
            "speed": payload.get("speed"),
            "vertical_rate": payload.get("vertical_rate"),
            "distance_nm": payload.get("distance_nm"),
        }
        with self.lock:
            if expected_location_revision is not None and expected_location_revision != self.location_revision:
                return
            if preserve_radar_radius and not tracking.get("active"):
                try:
                    current_radius = float(self.payload.get("radar_radius_nm"))
                except (TypeError, ValueError):
                    current_radius = float("nan")
                if math.isfinite(current_radius) and 1 <= current_radius <= 250:
                    display_settings = copy.copy(settings)
                    display_settings.radius_nm = current_radius
                    payload["radar_radius_nm"] = current_radius
                    payload["radar_contacts"] = radar_contacts(aircraft, display_settings, plane)
            payload["location_revision"] = self.location_revision
            payload["acquisition_status"] = self.payload.get("acquisition_status", "ready")
            current_observation = reported_position_observed_at
            if current_observation is None and current_aircraft_id == self.position_aircraft_id:
                current_observation = self.position_observed_at_monotonic
            self.position_aircraft_id = current_aircraft_id
            self.position_observed_at_monotonic = current_observation if current_aircraft_id else None
            if current_aircraft_id != self.metric_observation_aircraft_id:
                self.metric_observation_aircraft_id = current_aircraft_id
                self.metric_observations = []
                self.metric_observation_key = None
            if new_position_observation and current_aircraft_id and current_observation is not None and plane:
                try:
                    observed_latitude, observed_longitude = validate_location(plane.get("lat"), plane.get("lon"))
                except ValueError:
                    observed_latitude = observed_longitude = None
                if observed_latitude is not None and observed_longitude is not None:
                    observation_key = (current_aircraft_id, current_observation)
                    if not self.metric_observations or current_observation > self.metric_observations[-1]["observed_at"] + 0.5:
                        self.metric_observations.append({
                            "observed_at": current_observation,
                            "altitude": current_metric_sample["altitude"],
                            "speed": current_metric_sample["speed"],
                            "vertical_rate": current_metric_sample["vertical_rate"],
                            "distance_nm": current_metric_sample["distance_nm"],
                        })
                        self.metric_observations = self.metric_observations[-4:]
                        self.metric_observation_key = observation_key
            payload["_position_observed_at_monotonic"] = self.position_observed_at_monotonic
            payload["_state_updated_monotonic"] = history_now
            payload["last_acquisition_attempted_at"] = self.last_acquisition_attempted_at
            payload["last_acquisition_succeeded_at"] = self.last_acquisition_succeeded_at
            self.metric_history_aircraft_id = current_aircraft_id
            metric_history_observations = previous_metric_observations(
                self.metric_observations,
                history_now,
                metric_history_retention_seconds(settings.refresh_seconds),
            )
            self.metric_history = {
                str(age): sample
                for age, sample in zip(METRIC_HISTORY_AGES_SECONDS, metric_history_observations)
            }

            if current_aircraft_id != self.commentary_aircraft_id:
                self.commentary_aircraft_id = current_aircraft_id
                self.commentary_samples = []
            observation_at = None
            if current_aircraft_id and mode == "live" and position_observed_at is not None:
                try:
                    candidate_observation_at = float(position_observed_at)
                except (TypeError, ValueError):
                    candidate_observation_at = float("nan")
                if math.isfinite(candidate_observation_at) and candidate_observation_at <= history_now + 1.0:
                    observation_at = candidate_observation_at
            try:
                commentary_distance = float(distance) if distance is not None else None
            except (TypeError, ValueError):
                commentary_distance = None
            if observation_at is not None and commentary_distance is not None and math.isfinite(commentary_distance):
                last_sample = self.commentary_samples[-1] if self.commentary_samples else None
                if last_sample is None or observation_at > last_sample["observed_at"] + 0.5:
                    self.commentary_samples.append({
                        "observed_at": observation_at,
                        "distance_nm": commentary_distance,
                        "latitude": plane.get("lat"),
                        "longitude": plane.get("lon"),
                        "track": plane.get("track"),
                        "speed": plane.get("gs"),
                    })
                cutoff = history_now - COMMENTARY_SAMPLE_WINDOW_SECONDS
                self.commentary_samples = [
                    sample for sample in self.commentary_samples if sample["observed_at"] >= cutoff
                ]
            elif not current_aircraft_id:
                self.commentary_samples = []
            projection = (
                projected_closest_approach(plane, settings.latitude, settings.longitude, self.commentary_samples, history_now)
                if current_aircraft_id and mode == "live" and observation_at is not None
                else {}
            )
            payload["projected_closest_distance_nm"] = projection.get("distance_nm")
            payload["projected_closest_seconds"] = projection.get("seconds")
            payload["projected_closest_direction"] = projection.get("direction", "")
            payload["metric_history"] = {
                age: (dict(sample) if sample is not None else None)
                for age, sample in self.metric_history.items()
            }
            payload["metric_history_observations"] = metric_history_observations
            payload["commentary_facts"] = (
                build_commentary_facts(
                    self.commentary_samples, commentary_distance, plane, route, projection, nearby_place,
                    self.metric_history, history_now,
                )
                if current_aircraft_id and mode == "live" and observation_at is not None
                else {}
            )
            if current_aircraft_id and mode == "live" and observation_at is not None:
                commentary_template = build_aircraft_commentary(
                    self.commentary_samples, commentary_distance, payload.get("vertical_rate"), history_now,
                    projection, "{COUNTDOWN}", nearby_place
                )
                projected_seconds = projection.get("seconds")
                has_countdown = "{COUNTDOWN}" in commentary_template and isinstance(projected_seconds, (int, float)) and math.isfinite(float(projected_seconds))
                payload["commentary_template"] = commentary_template if has_countdown else ""
                payload["commentary_countdown_seconds"] = float(projected_seconds) if has_countdown else None
                payload["commentary"] = (
                    commentary_template.replace("{COUNTDOWN}", f"in {format_projection_eta(float(projected_seconds))}")
                    if has_countdown else commentary_template
                )
            else:
                payload["commentary_template"] = ""
                payload["commentary_countdown_seconds"] = None
                payload["commentary"] = ""
            self.png = data.getvalue()
            self.logo = logo_data
            self.logo_type = logo_type or "application/octet-stream"
            self.aircraft_photo = photo_data
            self.aircraft_photo_type = photo_type or "application/octet-stream"
            self.origin_flag = origin_flag or b""
            self.destination_flag = destination_flag or b""
            self.payload = payload


class SnapshotStore:
    """Persist paired desktop/mobile screenshots rendered from one frozen flight state."""

    DESKTOP_VIEWPORT = (1920, 1080)
    MOBILE_VIEWPORT = (390, 844)

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().with_name("screenshots")
        self.index_path = self.root / "index.json"
        self.lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _safe_component(value: Any, fallback: str = "") -> str:
        cleaned = re.sub(r"[^A-Za-z0-9-]+", "", str(value or "").upper().replace("_", "-"))
        return cleaned[:32] or fallback

    def _load_index_unlocked(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [item for item in raw if isinstance(item, dict) and item.get("id")] if isinstance(raw, list) else []

    def _write_index_unlocked(self, records: list[dict[str, Any]]) -> None:
        temp = self.index_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(self.index_path)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._load_index_unlocked()

    def pending_state(self, snapshot_id: str) -> dict[str, Any] | None:
        with self.lock:
            item = self.pending.get(snapshot_id)
            return json.loads(json.dumps(item["payload"])) if item else None

    def pending_asset(self, snapshot_id: str, kind: str) -> tuple[bytes, str] | None:
        with self.lock:
            item = self.pending.get(snapshot_id)
            if not item:
                return None
            if kind == "operator-logo":
                body = item.get("logo", b"")
                return (body, item.get("logo_type") or "application/octet-stream") if body else None
            if kind == "aircraft-photo":
                body = item.get("aircraft_photo", b"")
                return (body, item.get("aircraft_photo_type") or "application/octet-stream") if body else None
            if kind == "origin-flag":
                body = item.get("origin_flag", b"")
                return (body, "image/svg+xml") if body else None
            if kind == "destination-flag":
                body = item.get("destination_flag", b"")
                return (body, "image/svg+xml") if body else None
        return None

    def filename(self, snapshot_id: str, variant: str) -> str | None:
        if variant not in {"desktop", "mobile"}:
            return None
        with self.lock:
            record = next((item for item in self._load_index_unlocked() if item.get("id") == snapshot_id), None)
            filename = str(record.get(variant) or "") if record else ""
        return filename if filename and Path(filename).name == filename else None

    def image(self, snapshot_id: str, variant: str) -> bytes | None:
        filename = self.filename(snapshot_id, variant)
        if not filename:
            return None
        try:
            return (self.root / filename).read_bytes()
        except OSError:
            return None

    def delete(self, snapshot_id: str) -> bool:
        with self.lock:
            records = self._load_index_unlocked()
            record = next((item for item in records if item.get("id") == snapshot_id), None)
            if not record:
                return False
            records = [item for item in records if item.get("id") != snapshot_id]
            self._write_index_unlocked(records)
        for variant in ("desktop", "mobile"):
            filename = str(record.get(variant) or "")
            if filename and Path(filename).name == filename:
                try:
                    (self.root / filename).unlink()
                except FileNotFoundError:
                    pass
        return True

    @staticmethod
    def _chromium_executable() -> str | None:
        configured = os.environ.get("OVERHEAD_CHROMIUM", "").strip()
        return configured or None

    def _render_pair(self, snapshot_id: str, radar: bool, base_url: str, desktop_path: Path, mobile_path: Path, visitor_session_id: str | None = None) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Flight snapshots require Playwright. Install requirements.txt first.") from exc
        executable = self._chromium_executable()
        try:
            with sync_playwright() as playwright:
                browser_args = ["--disable-dev-shm-usage"]
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    browser_args.append("--no-sandbox")
                launch_args: dict[str, Any] = {"headless": True, "args": browser_args}
                if executable:
                    launch_args["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_args)
                try:
                    for variant, viewport, output in (
                        ("desktop", self.DESKTOP_VIEWPORT, desktop_path),
                        ("mobile", self.MOBILE_VIEWPORT, mobile_path),
                    ):
                        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]}, device_scale_factor=1)
                        try:
                            if visitor_session_id:
                                page.context.add_cookies([{
                                    "name": "overhead_session",
                                    "value": visitor_session_id,
                                    "url": base_url,
                                }])
                            url = f"{base_url}?snapshot={quote(snapshot_id)}&radar={1 if radar else 0}&variant={variant}"
                            page.goto(url, wait_until="networkidle", timeout=30000)
                            page.wait_for_function("window.__overheadSnapshotReady===true || Boolean(window.__overheadSnapshotError)", timeout=15000)
                            error = page.evaluate("window.__overheadSnapshotError")
                            if error:
                                raise RuntimeError(str(error))
                            page.wait_for_timeout(650)
                            page.screenshot(path=str(output), full_page=False)
                        finally:
                            page.close()
                finally:
                    browser.close()
        except Exception as exc:
            if executable:
                raise RuntimeError(f"Chromium could not render the flight snapshots: {exc}") from exc
            raise RuntimeError(
                "Chromium is not available for flight snapshots. Run 'python -m playwright install chromium' or set OVERHEAD_CHROMIUM."
            ) from exc

    def capture(
        self, state: FrameState, radar: bool, base_url: str, sound_enabled: bool = True,
        commentary_enabled: bool = True, speed_unit: str = "kt", distance_unit: str = "nm",
        visitor_session_id: str | None = None,
    ) -> dict[str, Any]:
        with self.capture_lock:
            with state.lock:
                payload = json.loads(json.dumps(state.payload))
                payload['snapshot_sound_enabled'] = bool(sound_enabled)
                payload['snapshot_commentary_enabled'] = bool(commentary_enabled)
                payload['snapshot_speed_unit'] = speed_unit if speed_unit in {"kt", "mph", "kmh"} else "kt"
                payload['snapshot_distance_unit'] = distance_unit if distance_unit in {"nm", "mi", "km"} else "nm"
                logo = bytes(state.logo)
                logo_type = state.logo_type
                aircraft_photo = bytes(state.aircraft_photo)
                aircraft_photo_type = state.aircraft_photo_type
                origin_flag = bytes(state.origin_flag)
                destination_flag = bytes(state.destination_flag)
            if not payload.get("aircraft_id"):
                raise ValueError("No current aircraft is available to capture")

            now = datetime.now().astimezone()
            callsign = self._safe_component(payload.get("callsign"), "AIRCRAFT")
            registration = self._safe_component(payload.get("registration"))
            route = ""
            if payload.get("origin") and payload.get("destination"):
                route = self._safe_component(f"{payload.get('origin')}-{payload.get('destination')}")
            stamp = now.strftime("%Y-%m-%d_%H%M%S")
            unique = now.strftime("%Y%m%dT%H%M%S%f")
            snapshot_id = f"{unique}_{callsign}"
            parts = [stamp, callsign]
            if registration and registration != callsign:
                parts.append(registration)
            if route:
                parts.append(route)
            basename = "_".join(parts)
            desktop_name = f"{basename}_desktop.png"
            mobile_name = f"{basename}_mobile.png"
            desktop_path = self.root / desktop_name
            mobile_path = self.root / mobile_name

            with self.lock:
                self.pending[snapshot_id] = {
                    "payload": payload,
                    "logo": logo,
                    "logo_type": logo_type,
                    "aircraft_photo": aircraft_photo,
                    "aircraft_photo_type": aircraft_photo_type,
                    "origin_flag": origin_flag,
                    "destination_flag": destination_flag,
                }
            try:
                self._render_pair(snapshot_id, bool(radar), base_url, desktop_path, mobile_path, visitor_session_id)
                record = {
                    "id": snapshot_id,
                    "captured_at": now.isoformat(timespec="seconds"),
                    "captured_display": now.strftime("%d %b %Y · %H:%M:%S"),
                    "callsign": str(payload.get("callsign") or ""),
                    "commercial_flight": str(payload.get("commercial_flight") or ""),
                    "registration": str(payload.get("registration") or ""),
                    "aircraft_name": str(payload.get("aircraft_name") or ""),
                    "aircraft_type": str(payload.get("aircraft_type") or ""),
                    "origin": str(payload.get("origin") or ""),
                    "destination": str(payload.get("destination") or ""),
                    "radar": bool(radar),
                    "mode": str(payload.get("mode") or ""),
                    "desktop": desktop_name,
                    "mobile": mobile_name,
                    "desktop_size": list(self.DESKTOP_VIEWPORT),
                    "mobile_size": list(self.MOBILE_VIEWPORT),
                }
                with self.lock:
                    records = [item for item in self._load_index_unlocked() if item.get("id") != snapshot_id]
                    records.insert(0, record)
                    self._write_index_unlocked(records)
                return record
            except Exception:
                for path in (desktop_path, mobile_path):
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            finally:
                with self.lock:
                    self.pending.pop(snapshot_id, None)


def update_state(state: FrameState, settings, demo: bool, logos: LogoStore, identities: AircraftIdentityStore, routes: FlightRouteStore | None = None, flags: FlagStore | None = None, tracker: FlightTrackingState | None = None, health: APIHealthMonitor | None = None, places: NearbyPlaceStore | None = None, persist_frame: bool = True, cached_acquisition: dict[str, Any] | None = None, tracking_only: bool = False, cached_tracking_result: dict[str, Any] | None = None, perform_tracking_lookup: bool = True, automatic_tracking: tuple[str, tuple[str, ...]] | None = None, cached_automatic_tracking_result: dict[str, Any] | None = None, perform_automatic_tracking: bool = False) -> None:
    with state.lock:
        sample_settings = copy.copy(settings)
        location_revision = state.location_revision
        if cached_acquisition is not None and cached_acquisition.get("target_location_revision") != location_revision:
            cached_acquisition = None
        cached_aircraft = list(state.last_live_aircraft)
        cached_aircraft_received_at = state.last_live_aircraft_received_at
        if not demo and cached_acquisition is None and (not tracking_only or perform_tracking_lookup or perform_automatic_tracking):
            state.last_acquisition_attempted_at = time.time()
    tracking = tracker.snapshot() if tracker else {"active": False, "query": "", "candidates": (), "resolved_callsign": "", "last_plane": None, "revision": 0}
    tracking_revision = int(tracking.get("revision") or 0)
    tracking_query_radius = None
    if tracking.get("active") and tracking.get("last_plane"):
        previous_observed_at = tracking.get("last_position_observed_at")
        previous_age = time.monotonic() - previous_observed_at if previous_observed_at is not None else float("inf")
        if previous_age <= retained_position_seconds(sample_settings.refresh_seconds):
            previous_distance = tracking_distance_nm(tracking["last_plane"], sample_settings)
        else:
            previous_distance = None
        if previous_distance is not None:
            tracking_query_radius = automatic_tracking_radius(previous_distance)
    local_position_received_at: float | None = None
    selection_position_age_limit = POSITION_FRESHNESS_SECONDS
    acquisition_query_radius = (
        tracking_query_radius
        if tracking_query_radius is not None
        else min(250.0, max(1.0, float(sample_settings.query_radius_nm)))
    )
    if tracking_only and not demo:
        age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
        aircraft = age_aircraft_positions(cached_aircraft, age)
        selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
        mode = "cached"
    elif cached_acquisition is not None and not demo:
        local_position_received_at = float(cached_acquisition["fetched_at"])
        cache_age = max(0.0, time.monotonic() - local_position_received_at)
        cached_aircraft = list(cached_acquisition["aircraft"])
        cached_aircraft_received_at = local_position_received_at
        aircraft = aircraft_within_radius(
            age_aircraft_positions(cached_aircraft, cache_age),
            sample_settings,
            float(sample_settings.query_radius_nm),
        )
        selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
        mode = "cached"
        with state.lock:
            state.last_live_aircraft = list(cached_aircraft)
            state.last_live_aircraft_received_at = local_position_received_at
            state.last_live_aircraft_query_radius_nm = float(cached_acquisition["radius_nm"])
            state.has_live_success = True
            state.last_acquisition_succeeded_at = float(cached_acquisition["succeeded_at"])
    elif demo:
        aircraft, mode = DEMO["ac"], "demo"
    else:
        try:
            if tracking_query_radius is None:
                aircraft = fetch_aircraft(sample_settings)
            else:
                aircraft = fetch_aircraft(sample_settings, radius_nm=tracking_query_radius)
            mode = "live"
            local_position_received_at = time.monotonic()
        except AircraftPositionInvalid:
            age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
            aircraft = age_aircraft_positions(cached_aircraft, age)
            selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
            mode = "invalid_data"
        except AircraftPositionStale:
            age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
            aircraft = age_aircraft_positions(cached_aircraft, age)
            selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
            mode = "stale"
        except AircraftFeedRateLimited:
            with state.lock:
                if state.location_revision != location_revision:
                    return
                state.live_fetch_failures += 1
                cached_aircraft = list(state.last_live_aircraft)
                cached_aircraft_received_at = state.last_live_aircraft_received_at
            age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
            aircraft = age_aircraft_positions(cached_aircraft, age)
            selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
            mode = "rate_limited"
        except AircraftFeedUnavailable:
            with state.lock:
                if state.location_revision != location_revision:
                    return
                state.live_fetch_failures += 1
                cached_aircraft = list(state.last_live_aircraft)
                cached_aircraft_received_at = state.last_live_aircraft_received_at
            age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
            aircraft = age_aircraft_positions(cached_aircraft, age)
            selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
            mode = "provider_unavailable"
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            with state.lock:
                if state.location_revision != location_revision:
                    return
                state.live_fetch_failures += 1
                cached_aircraft = list(state.last_live_aircraft)
                cached_aircraft_received_at = state.last_live_aircraft_received_at
            age = time.monotonic() - cached_aircraft_received_at if cached_aircraft_received_at is not None else float("inf")
            aircraft = age_aircraft_positions(cached_aircraft, age)
            selection_position_age_limit = retained_position_seconds(sample_settings.refresh_seconds)
            mode = "reconnecting"
        else:
            with state.lock:
                if state.location_revision != location_revision:
                    return
                state.last_live_aircraft = list(aircraft)
                state.last_live_aircraft_received_at = local_position_received_at
                state.last_live_aircraft_query_radius_nm = acquisition_query_radius
                state.has_live_success = True
                state.live_fetch_failures = 0
                state.last_acquisition_succeeded_at = time.time()

    plane: dict[str, Any] | None
    distance: float | None
    tracking_found = False
    tracked_position_received_at: float | None = None
    reported_position_observed_at: float | None = None
    new_position_observation = False
    automatic_result_to_remember = None

    if tracking.get("active") and not demo:
        resolved_callsign = str(tracking.get("resolved_callsign") or "")
        tracked_plane = None
        tracked_position_received_at: float | None = None
        tracking_result_from_cache = cached_tracking_result is not None
        if tracking_result_from_cache:
            tracked_plane = cached_tracking_result.get("plane")
            resolved_callsign = str(cached_tracking_result.get("resolved_callsign") or resolved_callsign)
            tracked_position_received_at = cached_tracking_result.get("received_at")
            tracked_position_observed_at = cached_tracking_result.get("observed_at")
            tracking_outcome = cached_tracking_result.get("outcome", "live" if tracked_plane else "not_observed")
            if tracked_plane is not None and tracked_position_received_at is not None:
                tracked_plane = age_aircraft_positions([tracked_plane], time.monotonic() - tracked_position_received_at)[0]
        elif perform_tracking_lookup:
            tracking_outcome = "not_observed"
            tracking_attempted_at = time.monotonic()
            try:
                pinned_hex = normalise_adsb_hex((tracking.get("last_plane") or {}).get("hex"))
                lookup_candidates = ("hex:" + pinned_hex,) if pinned_hex else tracking.get("candidates") or ()
                tracked_plane, resolved = fetch_tracked_aircraft(lookup_candidates)
                if tracked_plane:
                    tracked_position_received_at = time.monotonic()
                    resolved_callsign = resolved
                    tracking_outcome = "live"
                    with state.lock:
                        if state.location_revision == location_revision:
                            state.last_acquisition_succeeded_at = time.time()
            except AircraftPositionStale:
                tracking_outcome = "stale"
                tracked_plane = None
            except AircraftFeedRateLimited:
                tracking_outcome = "rate_limited"
                tracked_plane = None
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
                tracking_outcome = "provider_unavailable"
                tracked_plane = None
            tracked_position_observed_at = (
                aircraft_position_observed_at(tracked_plane, tracked_position_received_at)
                if tracked_plane is not None and tracked_position_received_at is not None else None
            )
        else:
            tracked_position_observed_at = None
            tracking_outcome = "waiting"
        if perform_tracking_lookup and not tracking_result_from_cache:
            with state.lock:
                state.last_tracking_result = {
                    "plane": dict(tracked_plane) if tracked_plane else None,
                    "resolved_callsign": resolved_callsign,
                    "received_at": time.monotonic(),
                    "attempted_at": tracking_attempted_at,
                    "observed_at": tracked_position_observed_at,
                    "outcome": tracking_outcome,
                    "revision": tracking_revision,
                }
        if tracked_plane is None:
            tracked_plane = tracking.get("last_plane")
            tracked_position_received_at = None
            tracked_position_observed_at = tracking.get("last_position_observed_at")
        elif tracking.get("last_position_observed_at") is not None and (
            tracked_position_observed_at is None or tracked_position_observed_at <= tracking["last_position_observed_at"]
        ):
            tracked_plane = tracking.get("last_plane")
            tracked_position_received_at = None
            tracked_position_observed_at = tracking["last_position_observed_at"]

        if tracker and int(tracker.snapshot().get("revision") or 0) != tracking_revision:
            return

        plane = tracked_plane
        position_age = (
            max(0.0, time.monotonic() - tracked_position_observed_at)
            if tracked_position_observed_at is not None else float("inf")
        )
        position_limit = retained_position_seconds(sample_settings.refresh_seconds)
        if plane is not None and position_age > position_limit:
            plane = remove_position_data(plane)
            position_state = "unavailable"
        elif plane is not None and position_age > POSITION_FRESHNESS_SECONDS:
            position_state = "last_known"
        elif plane is not None and tracked_position_observed_at is not None:
            position_state = "reported"
        else:
            position_state = "unavailable"
        if plane is not None and tracked_position_received_at is None and position_state != "unavailable":
            plane = dict(plane, seen_pos=position_age)
        distance = tracking_distance_nm(plane, sample_settings) if position_state != "unavailable" else None
        if plane is not None:
            tracking_found = position_state == "reported"
            if distance is not None:
                sample_settings.radius_nm = automatic_tracking_radius(distance)
            aircraft = merge_tracked_aircraft(aircraft, plane)
        tracking = {
            **tracking,
            "resolved_callsign": resolved_callsign,
            "found": tracking_found,
            "position_state": position_state,
            "acquisition_outcome": tracking_outcome,
        }
        if tracking_only and tracked_position_received_at is not None:
            mode = "live"
        elif tracking_only and plane is None:
            mode = {"rate_limited": "rate_limited", "provider_unavailable": "provider_unavailable", "stale": "position_unavailable"}.get(tracking_outcome, "scanning")
    else:
        plane, distance = select_nearest_within_radius(
            aircraft, sample_settings, selection_position_age_limit,
        )
        tracking = {**tracking, "found": False}
        reported_position_observed_at = (
            aircraft_position_observed_at(plane, local_position_received_at)
            if plane is not None and local_position_received_at is not None else None
        )
        acquisition_position_received_at = local_position_received_at
        if automatic_tracking and (
            perform_automatic_tracking or cached_automatic_tracking_result is not None
        ):
            automatic_result = cached_automatic_tracking_result
            automatic_result_is_new = False
            if automatic_result is None and perform_automatic_tracking:
                attempted_at = time.monotonic()
                automatic_result = {
                    "aircraft_id": automatic_tracking[0],
                    "plane": None,
                    "observed_at": None,
                    "received_at": attempted_at,
                    "attempted_at": attempted_at,
                    "outcome": "not_observed",
                }
                automatic_result_is_new = True
                try:
                    tracked_plane, _ = fetch_tracked_aircraft(automatic_tracking[1])
                    if tracked_plane and aircraft_identity(tracked_plane) == automatic_tracking[0]:
                        received_at = time.monotonic()
                        automatic_result = {
                            "aircraft_id": automatic_tracking[0],
                            "plane": dict(tracked_plane),
                            "observed_at": aircraft_position_observed_at(tracked_plane, received_at),
                            "received_at": received_at,
                            "attempted_at": attempted_at,
                            "outcome": "live",
                        }
                except AircraftPositionStale:
                    automatic_result["outcome"] = "stale"
                except AircraftFeedRateLimited:
                    automatic_result["outcome"] = "rate_limited"
                except (AircraftFeedUnavailable, OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
                    automatic_result["outcome"] = "provider_unavailable"
            tracked_plane = automatic_result.get("plane") if automatic_result else None
            observed_at = automatic_result.get("observed_at") if automatic_result else None
            received_at = automatic_result.get("received_at") if automatic_result else None
            if automatic_result_is_new:
                automatic_result_to_remember = automatic_result
            if tracked_plane and observed_at is not None and received_at is not None:
                tracked_plane = age_aircraft_positions(
                    [tracked_plane], max(0.0, time.monotonic() - float(received_at)),
                )[0]
                aircraft = merge_tracked_aircraft(aircraft, tracked_plane)
                plane, distance = select_nearest_within_radius(
                    aircraft, sample_settings, selection_position_age_limit,
                )
                if plane is not None and aircraft_identity(plane) == automatic_tracking[0]:
                    reported_position_observed_at = float(observed_at)
                    local_position_received_at = float(received_at)
                else:
                    reported_position_observed_at = (
                        aircraft_position_observed_at(plane, acquisition_position_received_at)
                        if plane is not None and acquisition_position_received_at is not None else None
                    )
                    local_position_received_at = acquisition_position_received_at
                mode = "live" if plane is not None else "scanning"

    if mode in {"live", "cached"} and plane is None and not tracking.get("active"):
        mode = "scanning"

    if tracker and int(tracker.snapshot().get("revision") or 0) != tracking_revision:
        return

    if tracking.get("active") and not demo:
        position_observed_at = tracked_position_observed_at if tracked_position_received_at is not None and position_age <= POSITION_FRESHNESS_SECONDS else None
        if tracked_position_observed_at is not None and plane is not None and plane.get("lat") is not None:
            reported_position_observed_at = tracked_position_observed_at
            new_position_observation = tracked_position_received_at is not None
    else:
        position_observed_at = commentary_observation_time(plane, local_position_received_at) if plane is not None and local_position_received_at is not None and cached_acquisition is None else None
        new_position_observation = bool(
            mode == "live" and local_position_received_at is not None
            and reported_position_observed_at is not None
        )
    cached_route = (
        routes.cached_route(plane)
        if routes is not None and plane is not None and hasattr(routes, "cached_route") else {}
    )
    image = render(plane, distance, mode)
    with state.location_lock:
        with state.lock:
            if state.location_revision != location_revision:
                return
            if tracker and int(tracker.snapshot().get("revision") or 0) != tracking_revision:
                return
            if tracker and tracked_position_received_at is not None:
                tracker.remember(plane, str(tracking.get("resolved_callsign") or ""), tracked_position_observed_at)
        if persist_frame:
            save_frame(image, sample_settings)
        state.update(
            image, mode, plane, distance, aircraft, sample_settings,
            None, {}, cached_route, None, None,
            bool(routes and routes.api_key), tracking, health.snapshot() if health else None, position_observed_at, {},
            expected_location_revision=location_revision,
            reported_position_observed_at=reported_position_observed_at,
            new_position_observation=new_position_observation,
            preserve_radar_radius=not bool(tracking.get("active")),
        )
        with state.lock:
            route_lookup_available = bool(
                routes is not None
                and plane is not None
                and (
                    not hasattr(routes, "callsign")
                    or routes.callsign(plane)
                )
            )
            state.payload["route_resolution_status"] = (
                "resolved" if cached_route else "pending" if route_lookup_available else "unavailable"
            )
        if automatic_result_to_remember is not None:
            with state.lock:
                state.last_automatic_tracking_result = automatic_result_to_remember
    if demo or plane is None or not _enrichment_slots.acquire(blocking=False):
        return

    def enrich_and_publish() -> None:
        try:
            route_started_at = time.monotonic()
            route = cached_route or ((routes.get(plane) or {}) if routes is not None else {})
            route_elapsed = max(0.0, time.monotonic() - route_started_at)
            identity = identities.get(plane) or {} if identities is not None else {}
            aircraft_photo = identities.photo_asset(identity, plane) if identities is not None and hasattr(identities, "photo_asset") else None
            aircraft_photo_debug = identities.photo_debug(identity, plane) if identities is not None and hasattr(identities, "photo_debug") else {}
            origin_flag = flags.get(route.get("origin_country", "")) if flags else None
            destination_flag = flags.get(route.get("destination_country", "")) if flags else None
            code = identity.get("operator_code") or operator_code(plane)
            nearby_place = (
                places.get(plane.get("lat"), plane.get("lon"))
                if places and plane.get("lat") is not None and plane.get("lon") is not None else {}
            )
            logo = logos.get(code, identity.get("airline_name", ""))
            with state.location_lock:
                with state.lock:
                    if state.location_revision != location_revision:
                        return
                    if str(state.payload.get("aircraft_id") or "") != aircraft_identity(plane):
                        return
                    latest_observation = state.position_observed_at_monotonic
                    if not tracking.get("active"):
                        if (
                            latest_observation is None
                            or time.monotonic() - latest_observation > POSITION_FRESHNESS_SECONDS
                        ):
                            return
                    if (
                        reported_position_observed_at is not None
                        and latest_observation is not None
                        and latest_observation > reported_position_observed_at + 0.001
                    ):
                        return
                    if tracker and int(tracker.snapshot().get("revision") or 0) != tracking_revision:
                        return
                state.update(
                    image, mode, plane, distance, aircraft, sample_settings,
                    logo, identity, route, origin_flag, destination_flag,
                    bool(routes and routes.api_key), tracking, health.snapshot() if health else None,
                    position_observed_at, nearby_place,
                    aircraft_photo=aircraft_photo,
                    aircraft_photo_debug=aircraft_photo_debug,
                    expected_location_revision=location_revision,
                    reported_position_observed_at=reported_position_observed_at,
                    new_position_observation=False,
                    preserve_radar_radius=not bool(tracking.get("active")),
                )
                with state.lock:
                    state.payload["route_resolution_status"] = "resolved" if route else "unresolved"
                if state.route_failure_log is not None:
                    debug = routes._current_lookup_debug() if isinstance(routes, FlightRouteStore) else {}
                    debug.setdefault("elapsed_seconds", round(route_elapsed, 3))
                    state.route_failure_log.observe(plane, route, debug)
        except Exception:
            with state.lock:
                if state.location_revision == location_revision and state.payload.get("aircraft_id") == aircraft_identity(plane):
                    state.payload["enrichment_status"] = "unavailable"
                    state.payload["route_resolution_status"] = "unresolved"

    try:
        future = _enrichment_executor.submit(enrich_and_publish)
    except RuntimeError:
        _enrichment_slots.release()
        return
    with state.lock:
        state.last_enrichment_future = future

    def finish_enrichment(completed: concurrent.futures.Future[Any]) -> None:
        _enrichment_slots.release()

    future.add_done_callback(finish_enrichment)


def restore_nearest_cached_aircraft(
    state: FrameState,
    settings: Settings,
    routes: FlightRouteStore,
    aircraft_id_before_stop: str,
) -> None:
    """Resume nearest fresh cached traffic after TRACK stops, without a provider request."""
    with state.lock:
        sample_settings = copy.copy(settings)
        location_revision = state.location_revision
        cached_aircraft = list(state.last_live_aircraft)
        received_at = state.last_live_aircraft_received_at
        previous = dict(state.payload)
        logo_data, logo_type = bytes(state.logo), state.logo_type
        photo_data, photo_type = bytes(state.aircraft_photo), state.aircraft_photo_type
        origin_flag, destination_flag = bytes(state.origin_flag), bytes(state.destination_flag)
    elapsed = time.monotonic() - received_at if received_at is not None else float("inf")
    aircraft = age_aircraft_positions(cached_aircraft, elapsed)
    plane, distance = select_nearest(aircraft, sample_settings, POSITION_FRESHNESS_SECONDS)
    current_aircraft_id = aircraft_identity(plane)
    preserve_enrichment = bool(plane and current_aircraft_id == aircraft_id_before_stop)
    identity = {
        "registration": previous.get("registration", ""),
        "operator_code": previous.get("operator_code", ""),
        "aircraft_type": previous.get("aircraft_type", ""),
        "aircraft_name": previous.get("aircraft_name", ""),
        "airline_name": previous.get("airline_name", ""),
    } if preserve_enrichment else {}
    route = {
        key: previous.get(key)
        for key in (
            "route", "route_source", "commercial_flight", "origin", "destination",
            "origin_name", "destination_name", "origin_country", "destination_country",
            "origin_latitude", "origin_longitude", "destination_latitude", "destination_longitude",
        )
    } if preserve_enrichment else {}
    logo = (logo_data, logo_type, str(previous.get("logo_source") or "")) if preserve_enrichment and logo_data else None
    photo = (
        photo_data, photo_type, str(previous.get("aircraft_photo_revision") or "")
    ) if preserve_enrichment and photo_data else None
    flags = (origin_flag, destination_flag) if preserve_enrichment else (None, None)
    tracking = {"active": False, "query": "", "resolved_callsign": "", "found": False}
    mode = "cached" if plane else "scanning"
    image = render(plane, distance, mode)
    state.update(
        image, mode, plane, distance, aircraft, sample_settings,
        logo, identity, route, flags[0], flags[1], bool(routes.api_key), tracking,
        {
            "ok": bool(previous.get("api_health_ok")),
            "checked_at": int(previous.get("api_health_checked_at") or 0),
            "services": dict(previous.get("api_health_services") or {}),
        },
        None, None,
        aircraft_photo=photo,
        expected_location_revision=location_revision,
        reported_position_observed_at=(
            aircraft_position_observed_at(plane, received_at)
            if plane is not None and received_at is not None else None
        ),
        new_position_observation=False,
    )


class ActiveMonitorLease:
    """Browser visibility-based lease. No upstream polling without an active viewer."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last_seen = 0.0
        self.activation_revision = 0

    def touch(self):
        with self.lock:
            now = time.monotonic()
            if now - self.last_seen >= 12.0:
                self.activation_revision += 1
            self.last_seen = now

    def active(self):
        with self.lock:
            return time.monotonic() - self.last_seen < 12.0

    def revision(self):
        with self.lock:
            return self.activation_revision


VISITOR_SESSION_TTL_SECONDS = 30 * 60
VISITOR_SNAPSHOT_RETENTION_SECONDS = VISITOR_SESSION_TTL_SECONDS
SNAPSHOT_RECONCILE_INTERVAL_SECONDS = 60.0
MAX_VISITOR_SESSIONS = 64
GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS = NORMAL_ACQUISITION_INTERVAL_SECONDS
TRACKING_ACQUISITION_INTERVAL_SECONDS = 30.0
MAX_VISITOR_SNAPSHOTS = 50
MAX_ENRICHMENT_WORKERS = 4
MAX_QUEUED_ENRICHMENTS = 64
_enrichment_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_ENRICHMENT_WORKERS, thread_name_prefix="aircraft-enrichment",
)
_enrichment_slots = threading.BoundedSemaphore(MAX_ENRICHMENT_WORKERS + MAX_QUEUED_ENRICHMENTS)


class VisitorSession:
    """Mutable display state owned by one opaque browser session."""

    def __init__(self, session_id: str, defaults: Settings, snapshot_root: Path) -> None:
        self.session_id = session_id
        self.settings = copy.copy(defaults)
        self.state = FrameState()
        self.tracker = FlightTrackingState(self.settings.radius_nm)
        self.lease = ActiveMonitorLease()
        self.active_lease_revision = 0
        self.last_access_at = time.monotonic()
        self.cookie_issued_at = self.last_access_at
        self.last_fetch_at = 0.0
        self.last_tracking_at = 0.0
        self.last_auto_tracking_at = 0.0
        self.auto_tracking_key: tuple[str, ...] | None = None
        self.last_tracking_success_at = 0.0
        self.last_scheduled_at = 0.0
        self.refresh_requested_at: float | None = None
        self.refresh_reasons: set[str] = set()
        self.cached_acquisition: dict[str, Any] | None = None
        self.cached_tracking_result: dict[str, Any] | None = None
        self.cached_auto_tracking_result: dict[str, Any] | None = None
        self.acquisition_kind = "normal"
        self.last_track_action_at = 0.0
        self.last_snapshot_at = 0.0
        self.last_archive_touch_at = 0.0
        folder = hashlib.sha256(session_id.encode("ascii")).hexdigest()[:32]
        self.snapshots = SnapshotStore(snapshot_root / folder)
        self.state.payload = {
            "mode": "starting",
            "updated": "never",
            "total": 0,
            "home_latitude": self.settings.latitude,
            "home_longitude": self.settings.longitude,
            "radar_radius_nm": self.settings.radius_nm,
            "radar_contacts": [],
            "location_revision": 0,
            "acquisition_status": "waiting",
        }


class VisitorSessionStore:
    """Owns expiring visitor state and globally paces acquisition cycles."""

    def __init__(
        self,
        defaults: Settings,
        snapshot_root: Path | None = None,
        session_ttl_seconds: float = VISITOR_SESSION_TTL_SECONDS,
        max_sessions: int = MAX_VISITOR_SESSIONS,
        global_min_interval_seconds: float | None = None,
        route_failure_log: RouteFailureLog | None = None,
    ) -> None:
        self.defaults = copy.copy(defaults)
        self.route_failure_log = route_failure_log
        self.snapshot_root = snapshot_root or Path(__file__).resolve().with_name("screenshots") / "visitors"
        self.session_ttl_seconds = max(1.0, float(session_ttl_seconds))
        self.max_sessions = max(1, int(max_sessions))
        configured_interval = GLOBAL_ACQUISITION_MIN_INTERVAL_SECONDS if global_min_interval_seconds is None else global_min_interval_seconds
        self.global_min_interval_seconds = max(0.0, float(configured_interval))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.snapshot_lock = threading.Lock()
        self.shared_aircraft_results: list[dict[str, Any]] = []
        self.shared_tracking_results: dict[tuple[str, ...], dict[str, Any]] = {}
        self.shared_auto_tracking_results: dict[tuple[str, ...], dict[str, Any]] = {}
        self.sessions: dict[str, VisitorSession] = {}
        self.next_acquisition_at = 0.0
        self.next_tracking_at = 0.0
        self.tracking_next_due: dict[tuple[str, ...], float] = {}
        self.last_snapshot_reconcile_at = 0.0
        self._reconcile_snapshot_root()

    def _reconcile_snapshot_root(self, now: float | None = None) -> None:
        self.snapshot_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.snapshot_root.is_symlink() or not self.snapshot_root.is_dir():
            raise ValueError("Visitor snapshot root must be a real directory")
        current_time = time.time() if now is None else float(now)
        for entry in os.scandir(self.snapshot_root):
            if not re.fullmatch(r"[0-9a-f]{32}", entry.name):
                continue
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    path.unlink()
                elif entry.is_dir(follow_symlinks=False):
                    modified_at = entry.stat(follow_symlinks=False).st_mtime
                    if current_time - modified_at >= VISITOR_SNAPSHOT_RETENTION_SECONDS:
                        shutil.rmtree(path)
            except FileNotFoundError:
                continue
        self.last_snapshot_reconcile_at = time.monotonic()

    @staticmethod
    def valid_session_id(value: str | None) -> bool:
        return bool(value and 32 <= len(value) <= 64 and all(char.isalnum() or char in "_-" for char in value))

    def _expire_unlocked(self, now: float) -> list[Path]:
        expired = [
            session_id
            for session_id, session in self.sessions.items()
            if now - session.last_access_at >= self.session_ttl_seconds
        ]
        return [self.sessions.pop(session_id).snapshots.root for session_id in expired]

    def _remove_snapshot_folders(self, paths: list[Path]) -> None:
        for path in paths:
            candidate = Path(path)
            try:
                if candidate.parent.resolve() != self.snapshot_root.resolve():
                    continue
                if not re.fullmatch(r"[0-9a-f]{32}", candidate.name) or candidate.is_symlink():
                    continue
                if candidate.is_dir():
                    shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                continue

    def retained_snapshot_count(self) -> int:
        total = 0
        try:
            entries = os.scandir(self.snapshot_root)
        except OSError:
            return total
        with entries:
            for entry in entries:
                if not re.fullmatch(r"[0-9a-f]{32}", entry.name) or entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                index_path = Path(entry.path) / "index.json"
                if index_path.is_symlink():
                    continue
                try:
                    records = json.loads(index_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(records, list):
                    for record in records:
                        if not isinstance(record, dict) or not record.get("id"):
                            continue
                        filenames = [str(record.get(key) or "") for key in ("desktop", "mobile")]
                        if any(not name or Path(name).name != name for name in filenames):
                            continue
                        paths = [Path(entry.path) / name for name in filenames]
                        if all(path.is_file() and not path.is_symlink() for path in paths):
                            total += 1
        return total

    def get_or_create(
        self, session_id: str | None = None, touch_existing: bool = True, create: bool = True,
    ) -> tuple[VisitorSession | None, bool]:
        now = time.monotonic()
        with self.lock:
            if now - self.last_snapshot_reconcile_at >= SNAPSHOT_RECONCILE_INTERVAL_SECONDS:
                self._reconcile_snapshot_root()
            expired_paths = self._expire_unlocked(now)
            session = self.sessions.get(session_id) if self.valid_session_id(session_id) else None
            created = False
            if session is None and create and self.valid_session_id(session_id) and len(self.sessions) < self.max_sessions:
                archive_name = hashlib.sha256(session_id.encode("ascii")).hexdigest()[:32]
                archive = self.snapshot_root / archive_name
                try:
                    recent_archive = (
                        archive.is_dir()
                        and not archive.is_symlink()
                        and time.time() - archive.stat(follow_symlinks=False).st_mtime < VISITOR_SNAPSHOT_RETENTION_SECONDS
                    )
                except OSError:
                    recent_archive = False
                if recent_archive:
                    session = VisitorSession(session_id, self.defaults, self.snapshot_root)
                    self.sessions[session_id] = session
            if session is not None:
                session.state.route_failure_log = self.route_failure_log
                if touch_existing:
                    session.last_access_at = now
            elif create and len(self.sessions) < self.max_sessions:
                new_id = secrets.token_urlsafe(32)
                while new_id in self.sessions:
                    new_id = secrets.token_urlsafe(32)
                session = VisitorSession(new_id, self.defaults, self.snapshot_root)
                session.state.route_failure_log = self.route_failure_log
                session.last_access_at = now
                self.sessions[new_id] = session
                created = True
            else:
                session = None
        self._remove_snapshot_folders(expired_paths)
        return session, created

    def touch(self, session: VisitorSession, active: bool = False) -> None:
        now = time.monotonic()
        with self.lock:
            if self.sessions.get(session.session_id) is not session:
                return
            if active:
                session.last_access_at = now
                session.lease.touch()
                if now - session.last_archive_touch_at >= SNAPSHOT_RECONCILE_INTERVAL_SECONDS:
                    archive = session.snapshots.root
                    if archive.is_dir() and not archive.is_symlink():
                        try:
                            os.utime(archive, None, follow_symlinks=False)
                            session.last_archive_touch_at = now
                        except OSError:
                            pass
                lease_revision = session.lease.revision()
                if lease_revision > session.active_lease_revision:
                    session.active_lease_revision = lease_revision
                    session.refresh_reasons.add("activation")
                    if session.refresh_requested_at is None:
                        session.refresh_requested_at = now
                    self.wake.set()

    def request_refresh(self, session: VisitorSession, reason: str = "manual") -> None:
        with self.lock:
            if self.sessions.get(session.session_id) is not session:
                return
            session.refresh_reasons.add(reason)
            if session.refresh_requested_at is None:
                session.refresh_requested_at = time.monotonic()
            self.wake.set()

    def request_tracking_refresh(self, session: VisitorSession) -> None:
        with self.lock:
            if self.sessions.get(session.session_id) is not session or not session.tracker.snapshot()["active"]:
                return
            now = time.monotonic()
            session.refresh_reasons.add("tracking")
            if session.refresh_requested_at is None:
                session.refresh_requested_at = now
            key = self.tracking_share_key(session)
            if key is not None:
                self.tracking_next_due[key] = now
            self.wake.set()

    def cancel_refresh(self, session: VisitorSession, reason: str) -> None:
        with self.lock:
            if self.sessions.get(session.session_id) is not session:
                return
            session.refresh_reasons.discard(reason)
            if not session.refresh_reasons:
                session.refresh_requested_at = None

    def refresh_cookie_if_needed(self, session: VisitorSession) -> bool:
        now = time.monotonic()
        with self.lock:
            if self.sessions.get(session.session_id) is not session:
                return False
            if now - session.cookie_issued_at < max(60.0, self.session_ttl_seconds / 2.0):
                return False
            session.cookie_issued_at = now
            return True

    def _cached_aircraft_unlocked(self, session: VisitorSession, now: float) -> dict[str, Any] | None:
        if session.tracker.snapshot()["active"]:
            return None
        with session.state.lock:
            target_latitude = session.settings.latitude
            target_longitude = session.settings.longitude
            target_location_revision = session.state.location_revision
            try:
                required_radius = min(250.0, max(1.0, float(session.settings.query_radius_nm)))
            except (TypeError, ValueError):
                return None
        cache_ttl = max(NORMAL_ACQUISITION_INTERVAL_SECONDS, self.global_min_interval_seconds)
        for result in sorted(self.shared_aircraft_results, key=lambda item: item["fetched_at"], reverse=True):
            age = now - result["fetched_at"]
            if age < 0 or age >= cache_ttl:
                continue
            if result["source_session_id"] == session.session_id:
                continue
            try:
                distance = haversine_nm(
                    result["latitude"], result["longitude"],
                    target_latitude, target_longitude,
                )
            except (TypeError, ValueError):
                continue
            if distance + required_radius > result["radius_nm"]:
                continue
            return {
                "aircraft": list(result["aircraft"]),
                "fetched_at": result["fetched_at"],
                "succeeded_at": result["succeeded_at"],
                "radius_nm": result["radius_nm"],
                "source_session_id": result["source_session_id"],
                "target_location_revision": target_location_revision,
            }
        return None

    def remember_shared_result(self, session: VisitorSession) -> None:
        with session.state.lock:
            fetched_at = session.state.last_live_aircraft_received_at
            query_radius = session.state.last_live_aircraft_query_radius_nm
            succeeded_at = session.state.last_acquisition_succeeded_at
            aircraft = list(session.state.last_live_aircraft)
            latitude = session.settings.latitude
            longitude = session.settings.longitude
        if fetched_at is None or query_radius is None or succeeded_at is None:
            return
        result = {
            "source_session_id": session.session_id,
            "latitude": latitude,
            "longitude": longitude,
            "radius_nm": float(query_radius),
            "fetched_at": fetched_at,
            "succeeded_at": succeeded_at,
            "aircraft": aircraft,
        }
        with self.lock:
            self.shared_aircraft_results = [
                item for item in self.shared_aircraft_results
                if not (
                    item["latitude"] == latitude
                    and item["longitude"] == longitude
                    and item["radius_nm"] == float(query_radius)
                )
            ]
            self.shared_aircraft_results.append(result)
            self.shared_aircraft_results.sort(key=lambda item: item["fetched_at"], reverse=True)
            del self.shared_aircraft_results[64:]
            self.wake.set()

    @staticmethod
    def tracking_share_key(session: VisitorSession) -> tuple[str, ...] | None:
        tracking = session.tracker.snapshot()
        pinned_hex = normalise_adsb_hex((tracking.get("last_plane") or {}).get("hex"))
        if pinned_hex:
            return ("hex:" + pinned_hex,)
        candidates = tracking.get("candidates") or ()
        hex_candidates = tuple(
            "hex:" + normalise_adsb_hex(candidate[4:])
            for candidate in candidates
            if str(candidate).lower().startswith("hex:") and normalise_adsb_hex(str(candidate)[4:])
        )
        return tuple(sorted(hex_candidates)) or tuple(sorted(str(candidate).strip().upper() for candidate in candidates)) or None

    @staticmethod
    def automatic_tracking_target(session: VisitorSession) -> tuple[str, tuple[str, ...]] | None:
        if session.tracker.snapshot()["active"]:
            return None
        with session.state.lock:
            selected_id = str(session.state.payload.get("aircraft_id") or "")
            aircraft = list(session.state.last_live_aircraft)
            received_at = session.state.last_live_aircraft_received_at
            position_observed_at = session.state.position_observed_at_monotonic
            settings = copy.copy(session.settings)
        if (
            not selected_id or received_at is None or position_observed_at is None
            or time.monotonic() - position_observed_at > POSITION_FRESHNESS_SECONDS
        ):
            return None
        aged_aircraft = age_aircraft_positions(
            aircraft, max(0.0, time.monotonic() - received_at),
        )
        plane, _ = select_nearest_within_radius(
            aged_aircraft, settings, POSITION_FRESHNESS_SECONDS,
        )
        if not plane or aircraft_identity(plane) != selected_id:
            return None
        hex_code = normalise_adsb_hex(plane.get("hex"))
        candidate = "hex:" + hex_code if hex_code else normalise_flight_query(plane.get("flight"))
        return (selected_id, (candidate,)) if candidate else None

    def _cached_automatic_tracking_unlocked(
        self, key: tuple[str, ...], aircraft_id: str, now: float,
    ) -> dict[str, Any] | None:
        result = self.shared_auto_tracking_results.get(key)
        if result is None:
            result = self.shared_tracking_results.get(key)
        if (
            result is None
            or (
                result.get("aircraft_id") != aircraft_id
                and aircraft_identity(result.get("plane") or {}) != aircraft_id
            )
            or result.get("received_at") is None
            or now < float(result["received_at"])
            or now - float(result.get("attempted_at", result["received_at"])) >= TRACKING_ACQUISITION_INTERVAL_SECONDS
        ):
            return None
        return dict(result)

    def remember_automatic_tracking_result(self, session: VisitorSession) -> None:
        target = self.automatic_tracking_target(session)
        if target is None:
            return
        with session.state.lock:
            result = copy.deepcopy(session.state.last_automatic_tracking_result)
        if result is None or result.get("aircraft_id") != target[0]:
            return
        with self.lock:
            self.shared_auto_tracking_results[target[1]] = result
            self.shared_tracking_results[target[1]] = result
            self.tracking_next_due[target[1]] = float(result.get("attempted_at", result["received_at"])) + TRACKING_ACQUISITION_INTERVAL_SECONDS
            while len(self.shared_auto_tracking_results) > 128:
                del self.shared_auto_tracking_results[next(iter(self.shared_auto_tracking_results))]
            self.wake.set()

    def _cached_tracking_unlocked(self, session: VisitorSession, now: float) -> dict[str, Any] | None:
        key = self.tracking_share_key(session)
        result = self.shared_tracking_results.get(key) if key else None
        if (
            result is None
            or result.get("received_at") is None
            or now < float(result["received_at"])
            or now - float(result.get("attempted_at", result["received_at"])) >= TRACKING_ACQUISITION_INTERVAL_SECONDS
        ):
            return None
        return dict(result)

    def remember_tracking_result(self, session: VisitorSession) -> None:
        key = self.tracking_share_key(session)
        if key is None:
            return
        tracker = session.tracker.snapshot()
        with session.state.lock:
            result = copy.deepcopy(session.state.last_tracking_result)
        if result is not None and result.get("revision") != tracker["revision"]:
            return
        found = bool(session.state.payload.get("tracking_found"))
        if result is None and (not found or not tracker.get("last_plane") or tracker.get("last_position_observed_at") is None):
            return
        result = result or {
            "plane": dict(tracker["last_plane"]),
            "resolved_callsign": str(tracker.get("resolved_callsign") or ""),
            "observed_at": float(tracker["last_position_observed_at"]),
            "received_at": float(tracker["last_position_observed_at"])
            + max(0.0, float(tracker["last_plane"].get("seen_pos") or 0.0)),
        }
        with self.lock:
            self.shared_tracking_results[key] = result
            if result.get("plane") is not None:
                session.last_tracking_success_at = float(result["received_at"])
            original_candidates = tracker.get("candidates") or ()
            original_key = tuple(sorted(str(candidate).strip().upper() for candidate in original_candidates))
            if original_key:
                self.shared_tracking_results[original_key] = result
            hex_code = normalise_adsb_hex((result.get("plane") or {}).get("hex"))
            if hex_code:
                hex_key = ("hex:" + hex_code,)
                self.shared_tracking_results[hex_key] = result
                self.tracking_next_due[hex_key] = float(result.get("attempted_at", result["received_at"])) + TRACKING_ACQUISITION_INTERVAL_SECONDS
            while len(self.shared_tracking_results) > 128:
                del self.shared_tracking_results[next(iter(self.shared_tracking_results))]
            self.wake.set()

    def claim_due(self, now: float | None = None) -> tuple[VisitorSession | None, float | None]:
        current = time.monotonic() if now is None else float(now)
        with self.lock:
            expired_paths = self._expire_unlocked(current)
            active = [
                session
                for session in self.sessions.values()
                if session.lease.active()
            ]
            normal_interval = max(NORMAL_ACQUISITION_INTERVAL_SECONDS, self.global_min_interval_seconds)
            due = [
                session for session in active
                if bool(session.refresh_reasons - {"tracking"})
                or current - session.last_fetch_at >= normal_interval
            ]
            tracking_due = [
                session for session in active
                if session.tracker.snapshot()["active"]
                and (
                    "tracking" in session.refresh_reasons
                    or current - session.last_tracking_at >= TRACKING_ACQUISITION_INTERVAL_SECONDS
                )
            ]
            automatic_due = []
            for session in active:
                target = self.automatic_tracking_target(session)
                target_key = target[1] if target else None
                if target_key != session.auto_tracking_key:
                    session.auto_tracking_key = target_key
                    session.last_auto_tracking_at = current
                if target and current - session.last_auto_tracking_at >= TRACKING_ACQUISITION_INTERVAL_SECONDS:
                    automatic_due.append(session)
            due.sort(key=lambda item: (
                item.refresh_requested_at
                if item.refresh_requested_at is not None
                else item.last_fetch_at + normal_interval,
                item.last_scheduled_at,
            ))
            tracking_due.sort(key=lambda item: (
                item.last_tracking_success_at,
                item.refresh_requested_at
                if "tracking" in item.refresh_reasons and item.refresh_requested_at is not None
                else item.last_tracking_at + TRACKING_ACQUISITION_INTERVAL_SECONDS,
                item.last_scheduled_at,
            ))
            active_keys = {self.tracking_share_key(item) for item in active if item.tracker.snapshot()["active"]}
            self.tracking_next_due = {key: deadline for key, deadline in self.tracking_next_due.items() if key in active_keys or deadline > current}
            normal_wait = max(0.0, self.next_acquisition_at - current)
            tracking_wait = min((max(0.0, self.tracking_next_due.get(self.tracking_share_key(item), 0.0) - current) for item in tracking_due), default=0.0)
            automatic_wait = min((max(0.0, self.tracking_next_due.get(item.auto_tracking_key, 0.0) - current) for item in automatic_due), default=0.0)
            if normal_wait < 0.001:
                normal_wait = 0.0
            if tracking_wait < 0.001:
                tracking_wait = 0.0
            if automatic_wait < 0.001:
                automatic_wait = 0.0
            session = None
            if tracking_due:
                for candidate in tracking_due:
                    cached_result = self._cached_tracking_unlocked(candidate, current)
                    if cached_result is not None:
                        session = candidate
                        session.cached_tracking_result = cached_result
                        session.acquisition_kind = "tracking_cache"
                        session.last_tracking_at = float(cached_result.get("attempted_at", cached_result["received_at"]))
                        session.refresh_reasons.discard("tracking")
                        break
            if session is None and automatic_due:
                for candidate in automatic_due:
                    target = self.automatic_tracking_target(candidate)
                    if target is None:
                        continue
                    cached_result = self._cached_automatic_tracking_unlocked(target[1], target[0], current)
                    if cached_result is not None:
                        session = candidate
                        session.cached_auto_tracking_result = cached_result
                        session.acquisition_kind = "automatic_tracking_cache"
                        session.last_auto_tracking_at = float(cached_result.get("attempted_at", cached_result["received_at"]))
                        automatic_due.remove(candidate)
                        break
                eligible_automatic = [
                    item for item in automatic_due
                    if self.tracking_next_due.get(item.auto_tracking_key, 0.0) <= current
                ]
                if session is None and eligible_automatic:
                    session = eligible_automatic[0]
                    automatic_due.remove(session)
                    session.cached_auto_tracking_result = None
                    session.acquisition_kind = "automatic_tracking"
                    session.last_auto_tracking_at = current
                    session.last_scheduled_at = current
                    key = session.auto_tracking_key
                    if key is not None:
                        self.tracking_next_due[key] = current + TRACKING_ACQUISITION_INTERVAL_SECONDS
            eligible = [item for item in tracking_due if self.tracking_next_due.get(self.tracking_share_key(item), 0.0) <= current]
            if session is None and eligible:
                session = eligible[0]
                tracking_due.remove(session)
                combine_normal = False
                session.acquisition_kind = "combined" if combine_normal else "tracking"
                session.cached_tracking_result = None
                session.last_tracking_at = current
                session.last_scheduled_at = current
                session.refresh_reasons.discard("tracking")
                if combine_normal:
                    due.remove(session)
                    session.last_fetch_at = current
                    self.next_acquisition_at = current + normal_interval
                    session.refresh_reasons.clear()
                self.next_tracking_at = current + TRACKING_ACQUISITION_INTERVAL_SECONDS
                key = self.tracking_share_key(session)
                if key is not None:
                    self.tracking_next_due[key] = self.next_tracking_at
            if session is not None:
                if session in due:
                    due.remove(session)
                if session in tracking_due:
                    tracking_due.remove(session)
            if session is None and due and normal_wait <= 0.0:
                for candidate in due:
                    cached_result = self._cached_aircraft_unlocked(candidate, current)
                    if cached_result is not None:
                        session = candidate
                        session.cached_acquisition = cached_result
                        session.acquisition_kind = "normal_cache"
                        due.remove(candidate)
                        session.last_fetch_at = current
                        session.last_scheduled_at = current
                        session.refresh_requested_at = None
                        session.refresh_reasons.clear()
                        break
                if session is None:
                    session = due.pop(0)
                    session.cached_acquisition = None
                    session.cached_tracking_result = None
                    session.acquisition_kind = "normal"
                    session.last_fetch_at = current
                    session.last_scheduled_at = current
                    session.refresh_requested_at = None
                    session.refresh_reasons.clear()
                    self.next_acquisition_at = current + normal_interval
            if session is not None:
                with session.state.lock:
                    session.state.payload["acquisition_status"] = "acquiring"
                    for field in ("acquisition_queue_position", "acquisition_queue_length", "acquisition_retry_in_seconds"):
                        session.state.payload.pop(field, None)
            if session is not None and session.acquisition_kind in {"tracking", "tracking_cache"}:
                queue = tracking_due
                queue_wait = tracking_wait
                interval = 0.0
            elif session is not None and session.acquisition_kind in {"automatic_tracking", "automatic_tracking_cache"}:
                queue = automatic_due
                queue_wait = automatic_wait
                interval = TRACKING_ACQUISITION_INTERVAL_SECONDS
            elif session is not None:
                queue = due
                queue_wait = max(0.0, self.next_acquisition_at - current)
                interval = normal_interval
            elif tracking_due and tracking_wait > 0.0:
                queue = tracking_due
                queue_wait = tracking_wait
                interval = TRACKING_ACQUISITION_INTERVAL_SECONDS
            elif automatic_due and automatic_wait > 0.0:
                queue = automatic_due
                queue_wait = automatic_wait
                interval = TRACKING_ACQUISITION_INTERVAL_SECONDS
            elif due and session is None:
                queue = due
                queue_wait = normal_wait
                interval = normal_interval
            else:
                queue = []
                queue_wait = 0.0
                interval = 0.0
            for position, queued in enumerate(queue, start=1):
                wait_seconds = queue_wait + (position - 1) * interval
                with queued.state.lock:
                    queued.state.payload["acquisition_status"] = "queued"
                    queued.state.payload["acquisition_queue_position"] = position
                    queued.state.payload["acquisition_queue_length"] = len(queue)
                    queued.state.payload["acquisition_retry_in_seconds"] = int(wait_seconds + 0.999)
        self._remove_snapshot_folders(expired_paths)
        wait_until_next = min(
            tracking_wait if tracking_due else float("inf"),
            automatic_wait if automatic_due else float("inf"),
            normal_wait if due else float("inf"),
        )
        if session is not None:
            return session, 0.0 if session.cached_acquisition is not None or session.cached_tracking_result is not None else wait_until_next
        return None, wait_until_next if math.isfinite(wait_until_next) else None


def refresh_loop(state: FrameState, settings, demo: bool, logos: LogoStore, identities: AircraftIdentityStore, routes: FlightRouteStore, flags: FlagStore, refresh_event: threading.Event, tracker: FlightTrackingState | None = None, health: APIHealthMonitor | None = None, places: NearbyPlaceStore | None = None, lease: ActiveMonitorLease | None = None) -> None:
    last_fetch = 0.0
    last_lease_revision = lease.revision() if lease else 0
    with state.lock:
        last_location_revision = state.location_revision
    while True:
        if lease is not None and not lease.active():
            time.sleep(1.0)
            continue
        with state.lock:
            location = (settings.latitude, settings.longitude)
            location_revision = state.location_revision
        lease_revision = lease.revision() if lease else last_lease_revision
        lease_activated = lease_revision != last_lease_revision
        location_changed = location_revision != last_location_revision
        # A home-location change needs a fresh geographic query immediately;
        # ordinary radar zoom and UI refreshes must still honour the request budget.
        if not location_changed and not lease_activated and time.monotonic() - last_fetch < NORMAL_ACQUISITION_INTERVAL_SECONDS:
            time.sleep(1.0)
            continue
        last_location_revision = location_revision
        last_lease_revision = lease_revision
        last_fetch = time.monotonic()
        try:
            update_state(state, settings, demo, logos, identities, routes, flags, tracker, health, places)
        except Exception as exc:
            with state.lock:
                if state.location_revision == location_revision:
                    state.payload["mode"] = "error"
                    state.payload["updated"] = time.strftime("%H:%M:%S")
                    state.payload["error"] = type(exc).__name__
        # Manual UI events cannot bypass the upstream refresh budget.
        time.sleep(1.0)


def visitor_refresh_loop(
    sessions: VisitorSessionStore,
    demo: bool,
    logos: LogoStore,
    identities: AircraftIdentityStore,
    routes: FlightRouteStore,
    flags: FlagStore,
    health: APIHealthMonitor | None = None,
    places: NearbyPlaceStore | None = None,
) -> None:
    """Refresh active visitor contexts through one globally paced worker."""
    while True:
        session, retry_in = sessions.claim_due()
        if session is None:
            timeout = 1.0 if retry_in is None else min(1.0, max(0.05, retry_in))
            sessions.wake.wait(timeout)
            sessions.wake.clear()
            continue
        with sessions.lock:
            cached_acquisition = session.cached_acquisition
            session.cached_acquisition = None
            cached_tracking_result = session.cached_tracking_result
            session.cached_tracking_result = None
            cached_auto_tracking_result = session.cached_auto_tracking_result
            session.cached_auto_tracking_result = None
            acquisition_kind = session.acquisition_kind
        automatic_target = sessions.automatic_tracking_target(session) if acquisition_kind in {"automatic_tracking", "automatic_tracking_cache"} else None
        try:
            update_state(
                session.state, session.settings, demo, logos, identities,
                routes, flags, session.tracker, health, places,
                persist_frame=False, cached_acquisition=cached_acquisition,
                tracking_only=acquisition_kind in {"tracking", "tracking_cache", "automatic_tracking", "automatic_tracking_cache"},
                cached_tracking_result=cached_tracking_result,
                perform_tracking_lookup=acquisition_kind in {"tracking", "combined"},
                automatic_tracking=automatic_target,
                cached_automatic_tracking_result=cached_auto_tracking_result,
                perform_automatic_tracking=acquisition_kind == "automatic_tracking",
            )
        except Exception as exc:
            with session.state.lock:
                session.state.payload["mode"] = "error"
                session.state.payload["updated"] = time.strftime("%H:%M:%S")
                session.state.payload["error"] = type(exc).__name__
        if acquisition_kind in {"tracking", "combined"} and cached_tracking_result is None:
            sessions.remember_tracking_result(session)
        if acquisition_kind in {"automatic_tracking", "automatic_tracking_cache"}:
            sessions.remember_automatic_tracking_result(session)
        if acquisition_kind in {"normal", "combined"} and cached_acquisition is None:
            sessions.remember_shared_result(session)
        with session.state.lock:
            session.state.payload["acquisition_status"] = "ready"
            session.state.payload.pop("acquisition_retry_in_seconds", None)


def save_location_config(config_path: Path, latitude: Any, longitude: Any) -> tuple[float, float]:
    """Validate and atomically save the first-run home location."""
    latitude_value, longitude_value = validate_location(latitude, longitude)
    settings = Settings(latitude=latitude_value, longitude=longitude_value, demo_on_failure=False)
    payload = {
        "latitude": settings.latitude,
        "longitude": settings.longitude,
        "radius_nm": settings.radius_nm,
        "refresh_seconds": settings.refresh_seconds,
        "output_rgb": settings.output_rgb,
        "output_png": settings.output_png,
        "demo_on_failure": False,
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    return latitude_value, longitude_value


def update_live_location_config(config_path: Path, settings: Settings, latitude: Any, longitude: Any) -> tuple[float, float]:
    """Persist a new live home location while preserving all other config keys."""
    latitude_value, longitude_value = validate_location(latitude, longitude)
    payload: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    payload["latitude"] = latitude_value
    payload["longitude"] = longitude_value
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(config_path)
    settings.latitude = latitude_value
    settings.longitude = longitude_value
    return latitude_value, longitude_value


def apply_live_location_change(
    state: FrameState, config_path: Path, settings: Settings,
    latitude: Any, longitude: Any, tracker: FlightTrackingState | None = None,
) -> tuple[float, float, int]:
    """Persist and publish a location change as one serialized state transition."""
    with state.location_lock:
        with state.lock:
            latitude_value, longitude_value = update_live_location_config(
                config_path, settings, latitude, longitude
            )
            state.location_revision += 1
            location_revision = state.location_revision
            state.last_live_aircraft = []
            state.last_live_aircraft_received_at = None
            state.last_live_aircraft_query_radius_nm = None
            state.has_live_success = False
            state.live_fetch_failures = 0
            state.last_acquisition_attempted_at = None
            state.last_acquisition_succeeded_at = None
            state.position_observed_at_monotonic = None
            state.position_aircraft_id = ""
            state.metric_history_aircraft_id = ""
            state.metric_history = empty_metric_history()
            state.metric_history_pending = None
            state.metric_history_last_shift_at = None
            state.metric_observation_aircraft_id = ""
            state.metric_observations = []
            state.metric_observation_key = None
            state.commentary_aircraft_id = ""
            state.commentary_samples = []
            state.png = b""
            state.logo = b""
            state.aircraft_photo = b""
            state.origin_flag = b""
            state.destination_flag = b""
            state.payload = {
                "mode": "starting", "updated": "never", "total": 0,
                "home_latitude": latitude_value, "home_longitude": longitude_value,
                "radar_radius_nm": settings.radius_nm, "radar_contacts": [],
                "location_revision": state.location_revision,
            }
            if tracker:
                tracker.invalidate_position()
    return latitude_value, longitude_value, location_revision


def apply_visitor_location_change(
    session: VisitorSession, latitude: Any, longitude: Any,
) -> tuple[float, float, int]:
    """Change only one visitor's home location without persisting it globally."""
    state, settings, tracker = session.state, session.settings, session.tracker
    latitude_value, longitude_value = validate_location(latitude, longitude)
    with state.location_lock:
        with state.lock:
            settings.latitude = latitude_value
            settings.longitude = longitude_value
            state.location_revision += 1
            location_revision = state.location_revision
            state.last_live_aircraft = []
            state.last_live_aircraft_received_at = None
            state.last_live_aircraft_query_radius_nm = None
            state.has_live_success = False
            state.live_fetch_failures = 0
            state.last_acquisition_attempted_at = None
            state.last_acquisition_succeeded_at = None
            state.position_observed_at_monotonic = None
            state.position_aircraft_id = ""
            state.metric_history_aircraft_id = ""
            state.metric_history = empty_metric_history()
            state.metric_history_pending = None
            state.metric_history_last_shift_at = None
            state.metric_observation_aircraft_id = ""
            state.metric_observations = []
            state.metric_observation_key = None
            state.commentary_aircraft_id = ""
            state.commentary_samples = []
            state.png = b""
            state.logo = b""
            state.aircraft_photo = b""
            state.origin_flag = b""
            state.destination_flag = b""
            state.payload = {
                "mode": "starting",
                "updated": "never",
                "total": 0,
                "home_latitude": latitude_value,
                "home_longitude": longitude_value,
                "radar_radius_nm": settings.radius_nm,
                "radar_contacts": [],
                "location_revision": location_revision,
                "acquisition_status": "queued",
            }
            tracker.invalidate_position()
    return latitude_value, longitude_value, location_revision


def configuration_required_handler(config_path: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Over-Head"
        sys_version = ""

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path in {"/", "/setup"}:
                self.send(HTTPStatus.OK, "text/html; charset=utf-8", CONFIG_REQUIRED_PAGE)
            else:
                self.send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not found")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/setup":
                self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Not found"}')
                return
            try:
                peer_is_local = ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:
                peer_is_local = False
            host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
            try:
                host_is_local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                host_is_local = host.casefold() == "localhost"
            if any(self.headers.get(name) for name in (
                "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP",
            )):
                peer_is_local = False
            if not peer_is_local or not host_is_local:
                self.send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"Only the local operator can configure the server location"}')
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length <= 0 or content_length > 4096:
                self.send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"Invalid request"}')
                return
            try:
                request = json.loads(self.rfile.read(content_length))
                latitude, longitude = save_location_config(config_path, request.get("latitude"), request.get("longitude"))
            except (json.JSONDecodeError, AttributeError, OSError, ValueError) as exc:
                body = json.dumps({"error": str(exc) or "Could not save location"}).encode()
                self.send(HTTPStatus.BAD_REQUEST, "application/json", body)
                return
            body = json.dumps({"saved": True, "latitude": latitude, "longitude": longitude}).encode()
            self.send(HTTPStatus.OK, "application/json", body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def send(self, status, content_type, body) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in SECURITY_RESPONSE_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args) -> None:
            return

    return Handler


def overhead_logo_asset(base_dir: Path) -> tuple[bytes, str] | None:
    candidates = [
        base_dir / "Over-Head_Logo.svg",
        base_dir / "Over-Head_Logo.png",
        base_dir / "Over-Head_Logo.webp",
        base_dir / "Over-Head_Logo.jpg",
        base_dir / "Over-Head_Logo.jpeg",
        base_dir / "Over-Head_Logo",
    ]
    content_types = {
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    for path in candidates:
        if not path.is_file():
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        suffix = path.suffix.lower()
        if suffix in content_types:
            return body, content_types[suffix]
        stripped = body.lstrip()
        if stripped.startswith(b"<svg") or b"<svg" in stripped[:500]:
            return body, "image/svg+xml"
        if body.startswith(b"\x89PNG\r\n\x1a\n"):
            return body, "image/png"
        if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
            return body, "image/webp"
        if body.startswith(b"\xff\xd8\xff"):
            return body, "image/jpeg"
    return None


def handler_factory(state: FrameState, settings, refresh_event: threading.Event, tone_path: Path, config_path: Path, routes: FlightRouteStore, logos: LogoStore, tracker: FlightTrackingState, snapshots: SnapshotStore, demo: bool = False, lease: ActiveMonitorLease | None = None, sessions: VisitorSessionStore | None = None, identities: AircraftIdentityStore | None = None, flags: FlagStore | None = None, health: APIHealthMonitor | None = None, places: NearbyPlaceStore | None = None):
    asset_dir = Path(__file__).resolve().parent
    visitor_get_paths = {
        "/frame.png", "/status", "/snapshots", "/snapshot-state", "/snapshot-image",
        "/route-debug", "/photo-debug", "/photo-test", "/operator-logo", "/aircraft-photo",
        "/origin-flag", "/destination-flag", "/radar-range",
    }
    visitor_mutation_paths = {"/snapshots", "/track", "/location"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Over-Head"
        sys_version = ""

        def visitor_context(self, touch_activity: bool = True, create: bool = False):
            if sessions is None:
                return state, settings, tracker, snapshots, lease, None
            if hasattr(self, "_visitor_context"):
                return self._visitor_context
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
            except Exception:
                cookie = SimpleCookie()
            morsel = cookie.get("overhead_session")
            session_id = morsel.value if morsel else None
            session, created = sessions.get_or_create(
                session_id, touch_existing=touch_activity, create=create,
            )
            if session is None:
                status = HTTPStatus.SERVICE_UNAVAILABLE if create else HTTPStatus.UNAUTHORIZED
                message = b'{"error":"Visitor capacity reached"}' if create else b'{"error":"Active visitor session required"}'
                self.send(status, "application/json", message, cache=False)
                self._visitor_context = None
                return None
            if created:
                self._new_visitor_cookie = session.session_id
            self._visitor_context = (
                session.state, session.settings, session.tracker, session.snapshots,
                session.lease, session,
            )
            return self._visitor_context

        def request_refresh(self, session, reason: str = "manual") -> None:
            if sessions is not None and session is not None:
                sessions.request_refresh(session, reason)
            else:
                refresh_event.set()

        def cancel_refresh(self, session, reason: str) -> None:
            if sessions is not None and session is not None:
                sessions.cancel_refresh(session, reason)

        def same_origin_request(self) -> bool:
            fetch_site = self.headers.get("Sec-Fetch-Site", "").casefold()
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            request_host = self.headers.get("Host", "")
            return bool(request_host and urlsplit(origin).netloc.casefold() == request_host.casefold())

        def allow_session_action(self, session, field: str, interval: float) -> bool:
            if sessions is None or session is None:
                return True
            now = time.monotonic()
            with sessions.lock:
                previous = float(getattr(session, field))
                if now - previous < interval:
                    return False
                setattr(session, field, now)
                return True

        def do_GET(self) -> None:
            request_url = urlsplit(self.path)
            path = request_url.path
            if sessions is not None and path == "/radar-range" and not self.same_origin_request():
                self.send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"Cross-origin request rejected"}', cache=False)
                return
            context = None
            if sessions is not None and path in visitor_get_paths:
                active_status = parse_qs(request_url.query).get("active", ["0"])[0] == "1"
                context = self.visitor_context(
                    touch_activity=path != "/status" or active_status,
                    create=path == "/status" and active_status,
                )
                if context is None:
                    return
                state, settings, tracker, snapshots, lease, session = context
            if path == "/": self.send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE, cache=False)
            elif path == "/frame.png":
                with state.lock: body = state.png
                self.send(HTTPStatus.OK, "image/png", body, cache=False)
            elif path == "/status":
                active = parse_qs(request_url.query).get("active", ["0"])[0] == "1"
                if sessions is not None and session is not None:
                    sessions.touch(session, active=active)
                    if active and sessions.refresh_cookie_if_needed(session):
                        self._new_visitor_cookie = session.session_id
                elif lease is not None and active:
                    lease.touch()
                with state.lock:
                    snapshot = dict(state.payload)
                    position_observed_at = state.position_observed_at_monotonic
                    state_updated_at = snapshot.pop("_state_updated_monotonic", None)
                    snapshot.pop("_position_observed_at_monotonic", None)
                    last_attempted_at = state.last_acquisition_attempted_at
                    last_succeeded_at = state.last_acquisition_succeeded_at
                    metric_observations = [dict(sample) for sample in state.metric_observations]
                    metric_observation_aircraft_id = state.metric_observation_aircraft_id
                if sessions is not None and session is not None and session.refresh_requested_at is not None:
                    snapshot["acquisition_status"] = "queued"
                now_monotonic = time.monotonic()
                position_age = (
                    max(0.0, now_monotonic - position_observed_at)
                    if position_observed_at is not None else None
                )
                position_limit = retained_position_seconds(settings.refresh_seconds)
                if position_age is None:
                    position_state = "unavailable"
                elif position_age <= POSITION_FRESHNESS_SECONDS:
                    position_state = "reported"
                elif position_age <= position_limit:
                    position_state = "last_known"
                else:
                    position_state = "unavailable"
                snapshot["position_age_seconds"] = position_age
                snapshot["position_state"] = position_state
                snapshot["metric_history_observations"] = (
                    previous_metric_observations(
                        metric_observations, now_monotonic, position_limit,
                    )
                    if position_state != "unavailable"
                    and metric_observation_aircraft_id == str(snapshot.get("aircraft_id") or "")
                    else []
                )
                snapshot["last_acquisition_attempted_at"] = last_attempted_at
                snapshot["last_acquisition_succeeded_at"] = last_succeeded_at
                if snapshot.get("mode") == "live":
                    if position_state == "last_known":
                        snapshot["mode"] = "last_known"
                    elif position_state == "unavailable" and snapshot.get("aircraft_id"):
                        snapshot["mode"] = "position_unavailable"
                if (
                    not snapshot.get("tracking_active")
                    and snapshot.get("aircraft_id")
                    and position_state != "reported"
                ):
                    snapshot.update({
                        "mode": "scanning",
                        "aircraft_id": "",
                        "callsign": "",
                        "registration": "",
                        "flight_details": "",
                        "aircraft_type": "",
                        "aircraft_name": "",
                        "route": "",
                        "route_source": "",
                        "commercial_flight": "",
                        "origin": "",
                        "destination": "",
                        "origin_name": "",
                        "destination_name": "",
                        "route_resolution_status": "unavailable",
                        "aircraft_latitude": None,
                        "aircraft_longitude": None,
                        "distance_nm": None,
                        "altitude": None,
                        "speed": None,
                        "vertical_rate": None,
                        "track": 0,
                        "telemetry": {},
                        "metric_history": empty_metric_history(),
                        "metric_history_observations": [],
                        "radar_contacts": [],
                        "position_age_seconds": None,
                        "position_state": "unavailable",
                    })
                if snapshot.get("acquisition_status") == "queued" and position_state != "reported":
                    if snapshot.get("tracking_active"):
                        snapshot["mode"] = "queued"
                snapshot["browser_status_checked_at"] = time.time()
                elapsed_since_update = max(0.0, now_monotonic - state_updated_at) if state_updated_at is not None else 0.0
                contacts = []
                for contact in snapshot.get("radar_contacts", []):
                    try:
                        contact_age = float(contact.get("position_age_seconds")) + elapsed_since_update
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if math.isfinite(contact_age) and 0 <= contact_age <= POSITION_FRESHNESS_SECONDS:
                        contacts.append({**contact, "position_age_seconds": contact_age})
                snapshot["radar_contacts"] = contacts
                if position_state == "last_known":
                    snapshot["tracking_found"] = False if snapshot.get("tracking_active") else snapshot.get("tracking_found", False)
                    snapshot["commentary"] = ""
                    snapshot["commentary_template"] = ""
                    snapshot["commentary_countdown_seconds"] = None
                elif position_state == "unavailable" and snapshot.get("aircraft_id"):
                    for field in ("aircraft_latitude", "aircraft_longitude", "distance_nm", "altitude", "speed", "vertical_rate"):
                        snapshot[field] = None
                    snapshot["track"] = 0
                    snapshot["telemetry"] = {}
                    snapshot["tracking_found"] = False
                    snapshot["commentary"] = ""
                    snapshot["commentary_template"] = ""
                    snapshot["commentary_countdown_seconds"] = None
                    snapshot["radar_contacts"] = []
                snapshot["providers"] = aircraft_provider_status()
                body = json.dumps(snapshot).encode()
                self.send(HTTPStatus.OK, "application/json", body, cache=False)
            elif path == "/provider-status":
                self.send(HTTPStatus.OK, "application/json", json.dumps(aircraft_provider_status()).encode(), cache=False)
            elif path == "/snapshots":
                body = json.dumps({"snapshots": snapshots.list()}).encode()
                self.send(HTTPStatus.OK, "application/json", body, cache=False)
            elif path == "/snapshot-state":
                snapshot_id = parse_qs(request_url.query).get("id", [""])[0]
                payload = snapshots.pending_state(snapshot_id)
                if payload is None:
                    self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Snapshot render state not found"}', cache=False)
                else:
                    self.send(HTTPStatus.OK, "application/json", json.dumps(payload).encode(), cache=False)
            elif path == "/snapshot-image":
                query = parse_qs(request_url.query)
                snapshot_id = query.get("id", [""])[0]
                variant = query.get("variant", [""])[0]
                body = snapshots.image(snapshot_id, variant)
                if body is None:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"Snapshot not found", cache=False)
                else:
                    headers = None
                    if query.get("download", [""])[0] == "1":
                        filename = snapshots.filename(snapshot_id, variant)
                        if filename:
                            headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
                    self.send(HTTPStatus.OK, "image/png", body, cache=False, headers=headers)
            elif path == "/route-debug":
                with state.lock:
                    current = {
                        key: state.payload.get(key)
                        for key in (
                            "callsign", "registration", "route", "route_source",
                            "commercial_flight", "aircraft_latitude", "aircraft_longitude",
                            "updated",
                        )
                    }
                body = json.dumps({"current": current}, indent=2).encode()
                self.send(HTTPStatus.OK, "application/json", body, cache=False)
            elif path == "/photo-debug":
                with state.lock:
                    debug = {
                        "callsign": state.payload.get("callsign", ""),
                        "registration": state.payload.get("registration", ""),
                        "aircraft_id": state.payload.get("aircraft_id", ""),
                        "photo_available": bool(state.payload.get("aircraft_photo_available")),
                        "photo_revision": state.payload.get("aircraft_photo_revision", ""),
                        "photo_source": state.payload.get("aircraft_photo_source", ""),
                        "photo": dict(state.payload.get("aircraft_photo_debug") or {}),
                        "test_image_url": "/aircraft-photo",
                    }
                self.send(HTTPStatus.OK, "application/json", json.dumps(debug, indent=2).encode(), cache=False)
            elif path == "/photo-test":
                with state.lock:
                    available = bool(state.payload.get("aircraft_photo_available"))
                    debug = {
                        "callsign": state.payload.get("callsign", ""),
                        "registration": state.payload.get("registration", ""),
                        "photo_available": available,
                        "photo_revision": state.payload.get("aircraft_photo_revision", ""),
                        "photo": dict(state.payload.get("aircraft_photo_debug") or {}),
                    }
                diagnostic = json.dumps(debug, indent=2).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                image = (
                    f'<img src="/aircraft-photo?t={int(time.time())}" alt="Aircraft photo" style="display:block;max-width:92vw;max-height:70vh;margin:20px auto;border:1px solid #14ecff;border-radius:12px">'
                    if available else '<p style="font-size:22px">No cached aircraft photo is currently available.</p>'
                )
                body = (
                    '<!doctype html><meta charset="utf-8"><title>Over-Head Photo Test</title>'
                    '<body style="margin:24px;background:#02050a;color:#e8f6ff;font-family:Segoe UI,Arial,sans-serif">'
                    '<h1 style="color:#14ecff">Over-Head aircraft photo test</h1>'+image+
                    '<p><a href="/aircraft-photo" style="color:#14ecff">Open the cached image directly</a> &nbsp; '
                    '<a href="/photo-debug" style="color:#14ecff">View JSON diagnostics</a></p>'
                    '<pre style="white-space:pre-wrap;color:#9bc5d5">'+diagnostic+'</pre></body>'
                ).encode()
                self.send(HTTPStatus.OK, "text/html; charset=utf-8", body, cache=False)
            elif path == "/over-head-logo":
                asset = overhead_logo_asset(asset_dir)
                if asset:
                    body, content_type = asset
                    self.send(HTTPStatus.OK, content_type, body, cache=True)
                else:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"Over-Head_Logo not found", cache=False)
            elif path == "/operator-logo":
                snapshot_id = parse_qs(request_url.query).get("snapshot", [""])[0]
                asset = snapshots.pending_asset(snapshot_id, "operator-logo") if snapshot_id else None
                if asset:
                    body, content_type = asset
                else:
                    with state.lock: body, content_type = state.logo, state.logo_type
                if body: self.send(HTTPStatus.OK, content_type, body, cache=True)
                else: self.send(HTTPStatus.NOT_FOUND, "text/plain", b"No logo", cache=False)
            elif path == "/aircraft-photo":
                snapshot_id = parse_qs(request_url.query).get("snapshot", [""])[0]
                asset = snapshots.pending_asset(snapshot_id, "aircraft-photo") if snapshot_id else None
                if asset:
                    body, content_type = asset
                else:
                    with state.lock: body, content_type = state.aircraft_photo, state.aircraft_photo_type
                if body: self.send(HTTPStatus.OK, content_type, body, cache=True)
                else: self.send(HTTPStatus.NOT_FOUND, "text/plain", b"No cached aircraft photo", cache=False)
            elif path in {"/origin-flag", "/destination-flag"}:
                snapshot_id = parse_qs(request_url.query).get("snapshot", [""])[0]
                kind = "origin-flag" if path == "/origin-flag" else "destination-flag"
                asset = snapshots.pending_asset(snapshot_id, kind) if snapshot_id else None
                if asset:
                    body, _ = asset
                else:
                    with state.lock:
                        body = state.origin_flag if path == "/origin-flag" else state.destination_flag
                if body: self.send(HTTPStatus.OK, "image/svg+xml", body, cache=True)
                else: self.send(HTTPStatus.NOT_FOUND, "text/plain", b"No flag", cache=False)
            elif path == "/beep-tone.mp3":
                try:
                    body = tone_path.read_bytes()
                except OSError:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"beep-tone.mp3 not found", cache=False)
                else:
                    self.send(HTTPStatus.OK, "audio/mpeg", body, cache=True)
            elif path == "/radar-range":
                try:
                    radius = float(parse_qs(request_url.query).get("radius", [""])[0])
                except (TypeError, ValueError):
                    radius = 0.0
                if not math.isfinite(radius) or not 2.5 <= radius <= 100.0:
                    self.send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"invalid range"}', cache=False)
                else:
                    if tracker.snapshot()["active"]:
                        self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Radar range is automatic while flight tracking is active"}', cache=False)
                        return
                    with state.lock:
                        previous_radius = settings.radius_nm
                        observation_radius = state.last_live_aircraft_query_radius_nm
                        settings.radius_nm = float(radius)
                        settings.query_radius_nm = max(
                            float(settings.query_radius_nm or 0.0), float(radius),
                        )
                        tracker.set_manual_radius(float(radius))
                        state.payload["radar_radius_nm"] = radius
                    update_state(
                        state, settings, demo, logos, identities, routes, flags, tracker,
                        health, places, persist_frame=False, tracking_only=True,
                    )
                    if radius > previous_radius and (
                        observation_radius is None or radius > observation_radius
                    ):
                        self.request_refresh(session, "radar_range")
                    with state.lock:
                        contacts = list(state.payload.get("radar_contacts") or [])
                    self.send(HTTPStatus.OK, "application/json", json.dumps({"radius_nm": radius, "radar_contacts": contacts}).encode(), cache=False)
            else: self.send(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if content_length <= 0 or content_length > 4096:
                self.send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"Invalid request"}', cache=False)
                return
            try:
                request = json.loads(self.rfile.read(content_length))
            except (json.JSONDecodeError, AttributeError):
                self.send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"Invalid JSON"}', cache=False)
                return
            if not isinstance(request, dict):
                self.send(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"JSON body must be an object"}', cache=False)
                return

            session = None
            if sessions is not None and path in visitor_mutation_paths:
                if not self.same_origin_request():
                    self.send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"Cross-origin request rejected"}', cache=False)
                    return
                context = self.visitor_context(create=False)
                if context is None:
                    return
                state, settings, tracker, snapshots, lease, session = context

            if path == "/snapshots":
                if not self.allow_session_action(session, "last_snapshot_at", 30.0):
                    self.send(HTTPStatus.TOO_MANY_REQUESTS, "application/json", b'{"error":"Please wait before capturing another snapshot"}', cache=False)
                    return
                snapshot_lock_acquired = False
                if sessions is not None and session is not None:
                    snapshot_lock_acquired = sessions.snapshot_lock.acquire(blocking=False)
                    if not snapshot_lock_acquired:
                        self.send(HTTPStatus.TOO_MANY_REQUESTS, "application/json", b'{"error":"Another visitor is saving a snapshot"}', cache=False)
                        return
                try:
                    if sessions is not None and session is not None:
                        total_snapshots = sessions.retained_snapshot_count()
                        if total_snapshots >= MAX_VISITOR_SNAPSHOTS:
                            self.send(HTTPStatus.TOO_MANY_REQUESTS, "application/json", b'{"error":"The shared snapshot limit has been reached"}', cache=False)
                            return
                    if len(snapshots.list()) >= 20:
                        self.send(HTTPStatus.TOO_MANY_REQUESTS, "application/json", b'{"error":"Delete a saved snapshot before capturing another"}', cache=False)
                        return
                    try:
                        base_url = f"http://127.0.0.1:{self.server.server_port}/"
                        record = snapshots.capture(
                            state, bool(request.get("radar")), base_url, bool(request.get("sound_enabled", True)),
                            bool(request.get("commentary_enabled", True)), str(request.get("speed_unit") or "kt"),
                            str(request.get("distance_unit") or "nm"),
                            visitor_session_id=session.session_id if session is not None else None,
                        )
                    except ValueError as exc:
                        self.send(HTTPStatus.CONFLICT, "application/json", json.dumps({"error": str(exc)}).encode(), cache=False)
                        return
                    except (OSError, RuntimeError) as exc:
                        self.send(HTTPStatus.SERVICE_UNAVAILABLE, "application/json", json.dumps({"error": str(exc)}).encode(), cache=False)
                        return
                finally:
                    if snapshot_lock_acquired:
                        sessions.snapshot_lock.release()
                self.send(HTTPStatus.CREATED, "application/json", json.dumps({"saved": True, "snapshot": record}).encode(), cache=False)
                return

            if path == "/track":
                if demo:
                    self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Flight tracking is unavailable in demo mode"}', cache=False)
                    return
                if not self.allow_session_action(session, "last_track_action_at", 1.0):
                    self.send(HTTPStatus.TOO_MANY_REQUESTS, "application/json", b'{"error":"Please wait before changing the tracked aircraft"}', cache=False)
                    return
                try:
                    aircraft_hex = normalise_adsb_hex(request.get("aircraft_hex"))
                    if request.get("aircraft_hex") and not aircraft_hex:
                        raise ValueError("Invalid aircraft hex identifier")
                    seed_plane = None
                    live_aircraft: list[dict[str, Any]] = []
                    if aircraft_hex:
                        query = re.sub(r"[^A-Z0-9]", "", str(request.get("flight") or "").upper()) or aircraft_hex
                        if not 3 <= len(query) <= 10:
                            query = aircraft_hex
                        candidates = ("hex:" + aircraft_hex,)
                        with state.lock:
                            live_aircraft = list(state.last_live_aircraft)
                            location_revision = state.location_revision
                        seed_plane = next((plane for plane in live_aircraft if normalise_adsb_hex(plane.get("hex")) == aircraft_hex), None)
                    else:
                        query = normalise_flight_query(request.get("flight"))
                        candidates = logos.tracking_callsigns(query)
                        with state.lock:
                            location_revision = state.location_revision
                except ValueError as exc:
                    self.send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": str(exc)}).encode(), cache=False)
                    return
                tracker.start(query, candidates, seed_plane)
                immediate = seed_plane is not None
                if immediate:
                    distance = tracking_distance_nm(seed_plane, settings)
                    live_aircraft = merge_tracked_aircraft(live_aircraft, seed_plane)
                    with state.lock:
                        settings.radius_nm = automatic_tracking_radius(distance)
                        mode = str(state.payload.get("mode") or "live")
                        api_health = {
                            "ok": bool(state.payload.get("api_health_ok")),
                            "checked_at": int(state.payload.get("api_health_checked_at") or 0),
                            "services": dict(state.payload.get("api_health_services") or {}),
                        }
                    image = render(seed_plane, distance, mode)
                    if session is None:
                        save_frame(image, settings)
                    tracking = tracker.snapshot()
                    tracking["found"] = True
                    state.update(
                        image, mode, seed_plane, distance, live_aircraft, settings, None, {}, {}, None, None,
                        bool(routes.api_key), tracking, api_health, None,
                        expected_location_revision=location_revision,
                        reported_position_observed_at=aircraft_position_observed_at(seed_plane, time.monotonic()),
                    )
                else:
                    with state.lock:
                        state.position_observed_at_monotonic = None
                        state.position_aircraft_id = ""
                        state.payload.update({
                            "mode": "starting",
                            "aircraft_id": "",
                            "callsign": "",
                            "registration": "",
                            "flight_details": "",
                            "aircraft_type": "",
                            "aircraft_name": "",
                            "altitude": None,
                            "speed": None,
                            "vertical_rate": None,
                            "distance_nm": None,
                            "aircraft_latitude": None,
                            "aircraft_longitude": None,
                            "track": 0,
                            "telemetry": {},
                            "route": "",
                            "origin": "",
                            "destination": "",
                            "commentary": "",
                            "commentary_template": "",
                            "tracking_active": True,
                            "tracking_query": query,
                            "tracking_resolved_callsign": "",
                            "tracking_found": False,
                            "position_state": "unavailable",
                            "radar_contacts": radar_contacts(state.last_live_aircraft, settings, None),
                            "acquisition_status": "queued",
                        })
                self.request_refresh(session, "tracking")
                self.send(HTTPStatus.OK, "application/json", json.dumps({"tracking": True, "flight": query, "candidates": candidates, "immediate": immediate}).encode(), cache=False)
                return

            if path != "/location":
                self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Not found"}', cache=False)
                return
            if demo:
                self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Location cannot be changed in demo mode"}', cache=False)
                return
            try:
                if session is not None:
                    latitude, longitude, location_revision = apply_visitor_location_change(
                        session, request.get("latitude"), request.get("longitude")
                    )
                else:
                    latitude, longitude, location_revision = apply_live_location_change(
                        state, config_path, settings, request.get("latitude"), request.get("longitude"), tracker
                    )
            except (OSError, ValueError) as exc:
                body = json.dumps({"error": str(exc) or "Could not save location"}).encode()
                self.send(HTTPStatus.BAD_REQUEST, "application/json", body, cache=False)
                return
            self.request_refresh(session, "location")
            if session is not None:
                sessions.request_tracking_refresh(session)
            body = json.dumps({"saved": True, "latitude": latitude, "longitude": longitude, "location_revision": location_revision}).encode()
            self.send(HTTPStatus.OK, "application/json", body, cache=False)

        def do_DELETE(self) -> None:
            request_url = urlsplit(self.path)
            path = request_url.path
            session = None
            if sessions is not None and path in {"/snapshots", "/track"}:
                if not self.same_origin_request():
                    self.send(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"Cross-origin request rejected"}', cache=False)
                    return
                context = self.visitor_context()
                if context is None:
                    return
                state, settings, tracker, snapshots, lease, session = context
            if path == "/snapshots":
                snapshot_id = parse_qs(request_url.query).get("id", [""])[0]
                if snapshots.delete(snapshot_id):
                    self.send(HTTPStatus.OK, "application/json", b'{"deleted":true}', cache=False)
                else:
                    self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Snapshot not found"}', cache=False)
                return
            if request_url.path != "/track":
                self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Not found"}', cache=False)
                return
            with state.lock:
                aircraft_id_before_stop = str(state.payload.get("aircraft_id") or "")
            restored_radius = tracker.stop()
            self.cancel_refresh(session, "tracking")
            with state.lock:
                settings.radius_nm = restored_radius
            restore_nearest_cached_aircraft(state, settings, routes, aircraft_id_before_stop)
            self.send(HTTPStatus.OK, "application/json", json.dumps({"tracking": False, "radius_nm": restored_radius}).encode(), cache=False)

        def send(self, status, content_type, body, cache=True, headers=None) -> None:
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "public, max-age=30" if cache else "no-store")
            for name, value in SECURITY_RESPONSE_HEADERS.items(): self.send_header(name, value)
            visitor_cookie = getattr(self, "_new_visitor_cookie", None)
            if visitor_cookie:
                cookie = f"overhead_session={visitor_cookie}; Path=/; Max-Age={int(sessions.session_ttl_seconds)}; HttpOnly; SameSite=Lax"
                if self.headers.get("X-Forwarded-Proto", "").casefold() == "https":
                    cookie += "; Secure"
                self.send_header("Set-Cookie", cookie)
            for name, value in (headers or {}).items(): self.send_header(name, value)
            self.end_headers(); self.wfile.write(body)

        def log_message(self, fmt, *args) -> None: return

    return Handler



def flightaware_api_key(config_path: Path) -> str:
    """Load the AeroAPI key without exposing it to browser payloads."""
    environment = os.environ.get("FLIGHTAWARE_AEROAPI_KEY", "").strip()
    if environment:
        return environment
    if not config_path.is_file():
        return ""
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(raw.get("flightaware_api_key") or "").strip() if isinstance(raw, dict) else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json"); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8765); parser.add_argument("--demo", action="store_true"); parser.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args()
    config_path = Path(args.config)
    url = f"http://{args.host}:{args.port}/"

    browser_opened = False
    if not args.demo and not config_path.exists():
        server = ThreadingHTTPServer((args.host, args.port), configuration_required_handler(config_path))
        print(f"Over-Head setup running at {url}")
        print(f"Location not configured: {config_path}")
        if args.open_browser:
            webbrowser.open(f"{url}?v={int(time.time())}")
            browser_opened = True
        interrupted = False
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            server.server_close()
        if interrupted or not config_path.exists():
            return 0

    settings = load_settings(config_path, allow_missing=args.demo)
    state = FrameState()
    logos = LogoStore()
    identities = AircraftIdentityStore()
    route_api_key = flightaware_api_key(config_path)
    routes = FlightRouteStore(api_key=route_api_key)
    flags = FlagStore()
    tracker = FlightTrackingState(settings.radius_nm)
    health = APIHealthMonitor(settings)
    places = NearbyPlaceStore()
    snapshots = SnapshotStore()
    refresh_event = threading.Event()
    lease = ActiveMonitorLease()
    try:
        failure_log = configured_log(config_path)
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger(__name__).error("Route failure logging configuration invalid (%s)", type(exc).__name__)
        failure_log = None
    sessions = VisitorSessionStore(settings, snapshots.root / "visitors", route_failure_log=failure_log)
    # API health polling disabled
    threading.Thread(target=visitor_refresh_loop, args=(sessions, args.demo, logos, identities, routes, flags, health, places), daemon=True).start()
    tone_path = Path(__file__).resolve().with_name("beep-tone.mp3")
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(
        state, settings, refresh_event, tone_path, config_path, routes, logos, tracker,
        snapshots, args.demo, lease, sessions, identities, flags, health, places,
    ))
    print(f"Over-Head monitor running at {url}"); print("UI revision: commentary-hide-radar-v91")
    if route_api_key:
        print("Route data: FlightAware AeroAPI primary / ADSB.lol live-validated fallbacks")
    else:
        print("Route data: ADSB.lol live-validated fallbacks (FlightAware key not configured)")
        print("Set FLIGHTAWARE_AEROAPI_KEY or flightaware_api_key in config.json for schedule-aware primary resolution.")
    print("Double-click the display to enter browser full-screen mode.")
    if args.open_browser and not browser_opened:
        webbrowser.open(f"{url}?v={int(time.time())}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
