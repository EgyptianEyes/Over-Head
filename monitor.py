#!/usr/bin/env python3
"""Serve Over-Head as a sharp, full-screen monitor display."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from xml.etree import ElementTree

from overhead import DEMO, Settings, fetch_aircraft, haversine_nm, load_settings, render, save_frame, select_nearest, validate_location


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
MAX_LOGO_BYTES = 1_000_000
HOUSE_GREY = "#6c9aac"
RADAR_RANGES = (5, 10, 15, 25, 40, 60, 100)
TRACK_RADAR_RANGES = (5, 10, 15, 25, 40, 60, 100, 150, 250, 400, 600, 1000, 1500, 2500, 4000, 6000, 10000)
FLIGHTAWARE_AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi"
ADSBIM_ROUTESET_URL = "https://adsb.im/api/0/routeset"
ADSLOL_ROUTESET_URL = "https://api.adsb.lol/api/0/routeset"
ADSLOL_ROUTE_URL = "https://api.adsb.lol/api/0/route/{callsign}/{latitude}/{longitude}"
ADSLOL_CALLSIGN_URL = "https://api.adsb.lol/v2/callsign/{callsign}"
OURAIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
ROUTE_CACHE_TTL_SECONDS = 900
ROUTE_FAILURE_RETRY_SECONDS = 30
OURAIRPORTS_TTL_SECONDS = 7 * 24 * 60 * 60

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


def tracking_distance_nm(plane: dict[str, Any] | None, settings) -> float | None:
    """Return a tracked aircraft's distance from home."""
    if not plane:
        return None
    lat, lon = plane.get("lat"), plane.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return haversine_nm(settings.latitude, settings.longitude, float(lat), float(lon))


def automatic_tracking_radius(distance_nm: float | None) -> float:
    """Choose a stable home-centred radar range with useful headroom."""
    if distance_nm is None or not math.isfinite(float(distance_nm)):
        return 25.0
    required = max(5.0, float(distance_nm) * 1.18)
    for radius in TRACK_RADAR_RANGES:
        if radius >= required:
            return float(radius)
    return float(math.ceil(required / 1000.0) * 1000.0)


def merge_tracked_aircraft(aircraft: list[dict[str, Any]], tracked: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Ensure an explicitly tracked aircraft is present exactly once."""
    if not tracked:
        return list(aircraft)
    tracked_id = aircraft_identity(tracked)
    merged = [plane for plane in aircraft if not tracked_id or aircraft_identity(plane) != tracked_id]
    merged.append(tracked)
    return merged


def fetch_tracked_aircraft(callsigns: tuple[str, ...] | list[str]) -> tuple[dict[str, Any] | None, str]:
    """Resolve an explicit tracking target by exact live ADS-B callsign."""
    for callsign in callsigns:
        candidate = normalise_flight_query(callsign)
        url = ADSLOL_CALLSIGN_URL.format(callsign=quote(candidate, safe=""))
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.load(response)
        planes = payload.get("ac", []) if isinstance(payload, dict) else []
        fresh: list[tuple[float, dict[str, Any]]] = []
        for plane in planes if isinstance(planes, list) else []:
            if not isinstance(plane, dict):
                continue
            flight = re.sub(r"[^A-Z0-9]", "", str(plane.get("flight") or "").upper())
            if flight and flight != candidate:
                continue
            lat, lon = plane.get("lat"), plane.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                continue
            try:
                age = float(plane.get("seen_pos", plane.get("seen", 999)))
            except (TypeError, ValueError):
                age = 999.0
            if age <= 60:
                fresh.append((age, plane))
        if fresh:
            _, plane = min(fresh, key=lambda item: item[0])
            return plane, candidate
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
        self.manual_radius = float(manual_radius)

    def start(self, query: str, candidates: tuple[str, ...] | list[str]) -> None:
        with self.lock:
            self.active = True
            self.query = query
            self.candidates = tuple(candidates)
            self.resolved_callsign = ""
            self.last_plane = None

    def stop(self) -> float:
        with self.lock:
            self.active = False
            self.query = ""
            self.candidates = ()
            self.resolved_callsign = ""
            self.last_plane = None
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
                "manual_radius": self.manual_radius,
            }

    def remember(self, plane: dict[str, Any], resolved_callsign: str) -> None:
        with self.lock:
            if not self.active:
                return
            self.last_plane = dict(plane)
            self.resolved_callsign = resolved_callsign


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
    for plane in aircraft:
        lat, lon = plane.get("lat"), plane.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        try:
            if float(plane.get("seen_pos", 999)) > 20:
                continue
        except (TypeError, ValueError):
            continue
        east = (float(lon) - settings.longitude) * longitude_scale
        north = (float(lat) - settings.latitude) * latitude_scale
        distance = math.hypot(east, north)
        if distance > settings.radius_nm:
            continue
        callsign = str(plane.get("flight") or plane.get("r") or plane.get("hex") or "").strip()
        contacts.append({
            "x": round(50 + east / settings.radius_nm * 46, 2),
            "y": round(50 - north / settings.radius_nm * 46, 2),
            "callsign": callsign,
            "track": plane.get("track") or 0,
            "altitude": plane.get("alt_baro"),
            "selected": bool(selected_hex and str(plane.get("hex") or "") == selected_hex),
            "emergency": str(plane.get("squawk") or "") in {"7500", "7600", "7700"},
        })
    return contacts


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
        return tuple(dict.fromkeys(candidates))

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

    def __init__(self, cache_path: Path = Path("cache/aircraft-identities.json")) -> None:
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._unavailable: set[str] = set()
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
    def _normalise(record: dict[str, Any]) -> dict[str, str]:
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
            for key in keys:
                cached = self._records.get(key)
                if isinstance(cached, dict):
                    return cached
            lookup_key = keys[0] if keys else ""
            if not lookup_key or lookup_key in self._unavailable:
                return {}
            identifier = lookup_key.split(":", 1)[1]
            try:
                result = self._normalise(self._fetch(identifier))
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
                self._unavailable.add(lookup_key)
                return {}
            if not any(result.values()):
                self._unavailable.add(lookup_key)
                return {}
            cache_keys = set(keys)
            if result["registration"]:
                cache_keys.add(f"registration:{re.sub(r'[^A-Z0-9]', '', result['registration'])}")
            if result["mode_s"]:
                cache_keys.add(f"mode_s:{re.sub(r'[^A-F0-9]', '', result['mode_s'])}")
            for key in cache_keys:
                self._records[key] = result
            self._save()
            return result



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
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
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
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.load(response)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return payload if isinstance(payload, dict) else {}

    def _adsbim_routeset(self, callsign: str, plane: dict[str, Any] | None) -> dict[str, Any]:
        return self._routeset(ADSBIM_ROUTESET_URL, callsign, plane)

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
        with urllib.request.urlopen(request, timeout=6) as response:
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
        return {
            "route": f"{origin_code} to {destination_code}",
            "origin": origin_code,
            "destination": destination_code,
            "origin_country": str(origin.get("countryiso2") or "").strip().upper(),
            "destination_country": str(destination.get("countryiso2") or "").strip().upper(),
            "origin_latitude": self._coordinate(origin.get("lat"), -90, 90),
            "origin_longitude": self._coordinate(origin.get("lon"), -180, 180),
            "destination_latitude": self._coordinate(destination.get("lat"), -90, 90),
            "destination_longitude": self._coordinate(destination.get("lon"), -180, 180),
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
        self._debug = json.loads(json.dumps(debug, default=str))

    def debug_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._debug, default=str))

    def get(self, plane: dict[str, Any] | None) -> dict[str, Any]:
        callsign = self.callsign(plane)
        registration = self.registration(plane)
        position = self._plane_position(plane)
        debug: dict[str, Any] = {
            "callsign": callsign,
            "registration": registration,
            "position": {"latitude": position[0], "longitude": position[1]} if position else None,
            "flightaware_configured": bool(self.api_key),
            "attempts": [],
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
                    "route": cached.get("route"),
                    "route_source": cached.get("route_source"),
                    "age_seconds": round(age, 1),
                    "accepted": age < self.ttl_seconds and plausible,
                    "validation": detail,
                })
                if age < self.ttl_seconds and plausible:
                    cached["route"] = f"{cached['origin']} to {cached['destination']}"
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
                    debug["attempts"].append({"source": "FlightAware AeroAPI", "error": type(exc).__name__})
                route = self._normalise_flightaware(flight) if flight else {}
                if route:
                    plausible, detail = self._plausibility(route, plane)
                    debug["attempts"].append({
                        "source": "FlightAware AeroAPI",
                        "route": route.get("route"),
                        "commercial_flight": route.get("commercial_flight"),
                        "accepted": plausible,
                        "validation": detail,
                    })
                    if plausible:
                        self._records[key] = route
                        self._save()
                        debug["decision"] = "Accepted FlightAware AeroAPI route"
                        self._set_debug(debug)
                        return route
                elif flight:
                    debug["attempts"].append({"source": "FlightAware AeroAPI", "accepted": False, "reason": "flight had no usable airport pair"})
            else:
                debug["attempts"].append({"source": "FlightAware AeroAPI", "skipped": True, "reason": "no API key configured"})

            free_route_attempts = (
                ("adsb.im routeset", self._adsbim_routeset),
                ("ADSB.lol routeset", self._adsblol_routeset),
                ("ADSB.lol route", self._adsblol),
            )
            for source_name, lookup in free_route_attempts:
                try:
                    raw = lookup(callsign, plane)
                except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError) as exc:
                    raw = {}
                    debug["attempts"].append({"source": source_name, "error": type(exc).__name__})
                if not raw:
                    continue

                route = self._normalise_routeset(raw, source_name)
                raw_summary = {
                    "airport_codes": raw.get("airport_codes"),
                    "airport_codes_iata": raw.get("_airport_codes_iata"),
                    "provider_plausible": raw.get("plausible"),
                }
                if route:
                    plausible, detail = self._plausibility(route, plane)
                    debug["attempts"].append({
                        "source": source_name,
                        "raw": raw_summary,
                        "route": route.get("route"),
                        "accepted": plausible,
                        "validation": detail,
                    })
                    if plausible:
                        self._records[key] = route
                        self._save()
                        debug["decision"] = f"Accepted {source_name} after independent live-position validation"
                        self._set_debug(debug)
                        return route
                else:
                    debug["attempts"].append({
                        "source": source_name,
                        "raw": raw_summary,
                        "accepted": False,
                        "reason": f"{source_name} returned no usable airport pair",
                    })

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
    async function waitForLive(){ for(let attempt=0;attempt<120;attempt++){ await new Promise(resolve=>setTimeout(resolve,500)); try{ const response=await fetch('/status?t='+Date.now(),{cache:'no-store'}); if(response.ok){ location.replace('/'); return; } }catch(_){} } setStatus('Location saved, but the live display did not start. Restart Over-Head.',true); }
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
  <meta name="overhead-ui-revision" content="persistent-flight-tracking-v46">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <style>
    :root { color-scheme: dark; --bg:#02050a; --line:#123846; --cyan:#14ecff; --white:#e8f6ff; --muted:#6c9aac; --amber:#ffb020; --green:#54d889; --red:#ff3e52; }
    * { box-sizing: border-box; }
    html,body { width:100%; height:100%; margin:0; overflow:hidden; background:var(--bg); }
    body { color:var(--white); font-family:"Segoe UI",Arial,sans-serif; cursor:none; -webkit-font-smoothing:antialiased; text-rendering:geometricPrecision; }
    .wall { width:100vw; height:100vh; padding:clamp(18px,2.4vw,52px); display:grid; grid-template-rows:auto 1fr auto; gap:clamp(14px,2vh,28px); background:radial-gradient(circle at 76% 48%,rgba(20,236,255,.075),transparent 28%),linear-gradient(145deg,#06121a 0%,var(--bg) 52%); }
    header,footer { display:flex; align-items:center; justify-content:space-between; }
    .header-tools { display:flex; align-items:center; gap:clamp(14px,1.5vw,28px); transition:transform 220ms ease; }
    .wall:not(.radar-open):not(.settings-open) .header-tools { transform:translateY(clamp(-38px,-3.55vh,-27px)); }
    .radar-toggle { cursor:pointer; border:1px solid var(--line); border-radius:999px; padding:.62em 1.05em; background:rgba(20,236,255,.055); color:var(--muted); font:750 clamp(11px,1vw,18px)/1 "Segoe UI",sans-serif; letter-spacing:.14em; }
    .radar-toggle:hover,.radar-toggle:focus-visible,.wall.radar-open .radar-toggle { color:var(--cyan); border-color:rgba(20,236,255,.48); outline:none; }
    .sound-toggle { width:clamp(38px,2.7vw,50px); height:clamp(38px,2.7vw,50px); padding:9px; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); }
    .sound-toggle:hover,.sound-toggle:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .sound-toggle svg { width:100%; height:100%; }
    .sound-toggle .mute-slash { display:none; }
    .sound-toggle.muted { color:var(--muted); }
    .sound-toggle.muted .mute-slash { display:block; }
    .sound-toggle.blocked { color:var(--amber); border-color:rgba(255,176,32,.65); }
    .fullscreen-toggle,.settings-toggle { width:clamp(38px,2.7vw,50px); height:clamp(38px,2.7vw,50px); padding:9px; display:grid; place-items:center; cursor:pointer; border:1px solid var(--line); border-radius:50%; background:rgba(20,236,255,.055); color:var(--cyan); }
    .fullscreen-toggle:hover,.fullscreen-toggle:focus-visible,.settings-toggle:hover,.settings-toggle:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .fullscreen-toggle svg,.settings-toggle svg { width:100%; height:100%; }
    .fullscreen-toggle .fullscreen-exit { display:none; }
    .fullscreen-toggle.active .fullscreen-enter { display:none; }
    .fullscreen-toggle.active .fullscreen-exit { display:block; }
    .settings-toggle.active { color:var(--amber); border-color:rgba(255,176,32,.65); background:rgba(255,176,32,.08); }

    .brand { width:clamp(220px,24vw,440px); height:clamp(52px,4.4vw,82px); display:flex; align-items:center; justify-content:flex-start; }
    .brand-logo { display:block; visibility:hidden; max-width:100%; max-height:100%; width:auto; height:auto; object-fit:contain; object-position:left center; filter:grayscale(0) saturate(1) brightness(1); opacity:1; transition:filter 900ms ease,opacity 900ms ease; }
    .brand-logo.ready { visibility:visible; }
    .wall.aircraft-active .brand-logo { filter:grayscale(1) saturate(0) brightness(.82) contrast(1.62); opacity:.82; }
    .wall.aircraft-active.brand-pulse .brand-logo { filter:grayscale(0) saturate(1.08) brightness(1.04) contrast(1.18); opacity:1; }
    .live { display:flex; align-items:center; gap:.7em; color:var(--muted); font-size:clamp(14px,1.45vw,26px); font-weight:700; letter-spacing:.16em; }
    .live-dot { width:.62em; height:.62em; border-radius:50%; background:var(--cyan); box-shadow:0 0 1em var(--cyan); }
    .live.demo .live-dot { background:var(--amber); box-shadow:0 0 1em var(--amber); }
    .live.error .live-dot { background:var(--red); box-shadow:0 0 1em var(--red); }
    .live.reconnecting .live-dot { background:var(--amber); box-shadow:0 0 1em var(--amber); }
    .content { min-height:0; display:grid; grid-template-columns:minmax(0,1fr); gap:clamp(14px,1.3vw,24px); }
    .wall.radar-open .content,.wall.settings-open .content { grid-template-columns:minmax(0,1fr) clamp(320px,29vw,560px); }
    main { position:relative; min-height:0; border:1px solid var(--line); border-radius:clamp(16px,2vw,32px); background:rgba(0,2,7,.72); display:grid; grid-template-columns:1fr; overflow:visible; box-shadow:inset 0 0 60px rgba(20,236,255,.025),0 28px 80px rgba(0,0,0,.34); }
    .information { min-width:0; padding:clamp(26px,4vw,76px); display:flex; flex-direction:column; justify-content:space-between; }
    .flight-heading { position:relative; min-width:0; display:grid; grid-template-columns:minmax(0,1fr) auto; align-items:center; gap:clamp(28px,3.4vw,64px); }
    .flight-copy { position:relative; z-index:2; min-width:0; }
    .callsign { overflow:visible; color:var(--white); font-size:clamp(64px,10.5vw,198px); font-weight:800; letter-spacing:-.065em; line-height:.88; white-space:nowrap; display:flex; align-items:center; gap:.12em; }
    .callsign.route-known { font-size:clamp(64px,7.2vw,138px); }
    .callsign.route-unknown { color:var(--muted); font-weight:400; letter-spacing:0; }
    .route-airport { width:1.32em; height:.88em; flex:0 0 1.32em; min-width:1.32em; min-height:.88em; max-width:1.32em; max-height:.88em; overflow:visible; filter:drop-shadow(0 0 8px rgba(108,154,172,.16)); }
    .route-airport-outline { fill:none; stroke:#6c9aac; stroke-width:2.1; stroke-linejoin:round; paint-order:stroke; vector-effect:non-scaling-stroke; }
    .route-join { color:var(--muted); font-size:.45em; font-weight:650; letter-spacing:0; }
    .identity { margin-top:clamp(18px,2.2vh,34px); min-width:0; width:100%; display:flex; flex-direction:column; gap:.24em; }
    .registration { display:block; width:100%; min-width:0; overflow:hidden; white-space:nowrap; color:var(--muted); font-size:clamp(22px,2.5vw,44px); font-weight:700; letter-spacing:.1em; }
    .aircraft-name { color:var(--white); font-size:clamp(17px,1.55vw,28px); font-weight:650; letter-spacing:.065em; }
    .route-visual { position:absolute; top:clamp(-126px,-13.5vh,-112px); right:0; width:clamp(530px,29.7vw,575px); height:clamp(430px,51vh,460px); overflow:visible; z-index:0; pointer-events:none; }
    .route-brand { position:absolute; top:0; left:50%; transform:translateX(-50%); width:100%; height:clamp(92px,10.5vh,108px); display:flex; align-items:center; justify-content:center; overflow:hidden; pointer-events:none; z-index:20; background:linear-gradient(90deg,rgba(2,5,10,0) 0%,#02050a 5%,#02050a 95%,rgba(2,5,10,0) 100%); }
    .route-brand-name { display:none; max-width:92%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--muted); font-size:clamp(34px,3.4vw,58px); font-weight:750; letter-spacing:.015em; line-height:1; text-align:center; }
    .route-brand-logo { display:none; width:100%; height:100%; object-fit:contain; transform-origin:center; filter:brightness(1.35) saturate(1.22) contrast(1.06) drop-shadow(0 0 14px rgba(108,154,172,.20)); }
    .route-map { display:none; position:absolute; top:clamp(90.882px,10.3275vh,99.144px); left:calc(50% + clamp(18px,1.2vw,24px)); transform:translateX(-50%); width:clamp(567.6px,38.28vw,739.2px); height:clamp(277.2px,33vh,376.2px); overflow:hidden; border:0; border-radius:clamp(14px,1.5vw,22px); background:transparent; box-shadow:none; pointer-events:auto; z-index:0; }
    .route-map.visible { display:block; }
    .route-map::before { content:""; position:absolute; inset:0; z-index:250; pointer-events:none; border-radius:inherit; background:radial-gradient(ellipse at center,rgba(232,246,255,.28) 0%,rgba(108,154,172,.16) 28%,rgba(108,154,172,.075) 48%,rgba(108,154,172,0) 70%); mix-blend-mode:screen; }
    .route-map::after { content:""; position:absolute; inset:-1px; z-index:500; pointer-events:none; border-radius:inherit; background:
      linear-gradient(to right,#000207 0%,rgba(0,2,7,.96) 3%,rgba(0,2,7,.72) 12%,rgba(0,2,7,.30) 24%,rgba(0,2,7,0) 38%,rgba(2,5,10,0) 69%,rgba(2,5,10,.28) 81%,rgba(2,5,10,.68) 90%,rgba(2,5,10,.94) 97%,#02050a 100%),
      linear-gradient(to bottom,#02050a 0%,rgba(2,5,10,.90) 3%,rgba(2,5,10,.56) 11%,rgba(2,5,10,.22) 20%,rgba(2,5,10,0) 31%,rgba(2,5,10,0) 69%,rgba(2,5,10,.22) 80%,rgba(2,5,10,.56) 89%,rgba(2,5,10,.90) 97%,#02050a 100%); }
    .route-map.leaflet-container { background:transparent; outline:0; border:0; box-shadow:none; }

    .route-map .leaflet-tile-pane { opacity:.78; filter:grayscale(1) sepia(.14) hue-rotate(132deg) saturate(.58) brightness(.62) contrast(1.62); }
    .route-map .leaflet-tooltip.route-label { padding:2px 5px; border:1px solid rgba(108,154,172,.5); border-radius:5px; background:rgba(2,5,10,.82); box-shadow:none; color:#e8f6ff; font:750 10px/1 "Segoe UI",sans-serif; letter-spacing:.06em; }
    .route-map .leaflet-tooltip.route-label::before { display:none; }
    .route-direction-flow { pointer-events:none; animation:route-flow 10s linear infinite; filter:drop-shadow(0 0 4px rgba(232,246,255,.95)) drop-shadow(0 0 8px rgba(20,236,255,.7)); }
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
    .settings-location-value { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--white); font-size:clamp(13px,1vw,18px); letter-spacing:.02em; }
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
    .location-coordinate strong { color:var(--white); font:700 clamp(15px,1.2vw,20px)/1.15 "Segoe UI",sans-serif; }
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
    .radar-stage { min-height:0; display:grid; place-items:center; }
    #radar { width:min(100%,52vh); aspect-ratio:1; overflow:visible; }
    .radar-grid { fill:rgba(20,236,255,.018); stroke:rgba(20,236,255,.18); stroke-width:.35; vector-effect:non-scaling-stroke; }
    .radar-axis { stroke:rgba(20,236,255,.09); stroke-width:.3; vector-effect:non-scaling-stroke; }
    .radar-home { fill:var(--white); filter:drop-shadow(0 0 2px var(--cyan)); }
    .contact { color:var(--muted); transition:transform 800ms ease; }
    .contact.selected { color:var(--cyan); filter:drop-shadow(0 0 1.6px var(--cyan)); }
    .contact.emergency { color:var(--red); }
    .contact path { fill:currentColor; }
    .contact text { fill:currentColor; font:700 3.2px "Segoe UI",sans-serif; letter-spacing:.02em; }
    .radar-foot { display:flex; align-items:center; justify-content:space-between; gap:clamp(10px,1vw,16px); }
    .radar-track { min-width:0; flex:1 1 auto; display:flex; align-items:center; gap:clamp(7px,.6vw,10px); }
    .track-input { min-width:0; width:clamp(92px,8.8vw,150px); height:2.15em; padding:0 .7em; border:1px solid var(--line); border-radius:999px; outline:none; background:rgba(20,236,255,.035); color:var(--white); font:700 clamp(10px,.82vw,14px)/1 "Segoe UI",sans-serif; letter-spacing:.08em; text-transform:uppercase; }
    .track-input::placeholder { color:#46636f; }
    .track-input:focus { border-color:rgba(20,236,255,.72); box-shadow:0 0 10px rgba(20,236,255,.10); }
    .track-button { height:2.15em; padding:0 .9em; cursor:pointer; border:1px solid var(--line); border-radius:999px; background:rgba(20,236,255,.055); color:var(--cyan); font:750 clamp(10px,.82vw,14px)/1 "Segoe UI",sans-serif; letter-spacing:.12em; }
    .track-button:hover,.track-button:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .track-button.tracking { animation:track-button-pulse 2.6s ease-in-out infinite; }
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
    .zoom-button:hover,.zoom-button:focus-visible { border-color:var(--cyan); background:rgba(20,236,255,.12); outline:none; }
    .zoom-button:disabled { color:#34525e; border-color:#18303a; cursor:default; }
    .wall.no-aircraft .information { justify-content:center; }
    .wall.no-aircraft .flight-heading,.wall.no-aircraft .identity,.wall.no-aircraft .route-visual,.wall.no-aircraft .metrics { display:none; }
    .scan-message { display:none; position:absolute; inset:0; align-items:center; justify-content:center; color:var(--muted); font:700 clamp(20px,2vw,36px)/1.2 "Segoe UI",sans-serif; letter-spacing:.06em; text-align:center; pointer-events:none; }
    .wall.loading .scan-message,.wall.no-aircraft .scan-message { display:flex; }
    .wall.loading .information { visibility:hidden; }
    footer { gap:clamp(12px,1.2vw,22px); color:#52717e; font:600 clamp(9px,.72vw,13px)/1.25 "Segoe UI",sans-serif; letter-spacing:.075em; }
    .footer-credits { min-width:0; flex:1 1 auto; }
    .footer-attribution { color:inherit; text-decoration:none; white-space:nowrap; }
    .footer-attribution:hover { color:var(--muted); }
    #footer-status { flex:0 0 auto; white-space:nowrap; }
    @media (max-width:1150px) { .wall.radar-open .content,.wall.settings-open .content { grid-template-columns:minmax(0,1fr) minmax(280px,36vw); } .wall.radar-open .metrics,.wall.settings-open .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } }
    @media (max-aspect-ratio:4/3) { .callsign { font-size:clamp(56px,15vw,130px); } }
  </style>
</head>
<body>
  <section class="wall no-aircraft loading" id="wall">
    <header><div class="brand"><img class="brand-logo" id="brand-logo" alt="Over-Head"></div><div class="header-tools"><button class="sound-toggle" id="sound-toggle" type="button" aria-label="Mute aircraft change sound" aria-pressed="true"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18h6m-5 2h4M6.5 16.5h11c-1.6-1.7-2.2-3.5-2.2-6.2A3.3 3.3 0 0 0 12 7a3.3 3.3 0 0 0-3.3 3.3c0 2.7-.6 4.5-2.2 6.2Z" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path class="mute-slash" d="M4 4l16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg></button><button class="fullscreen-toggle" id="fullscreen-toggle" type="button" aria-label="Enter full screen" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><g class="fullscreen-enter"><path d="M4 9V4h5M15 4h5v5M20 15v5h-5M9 20H4v-5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g><g class="fullscreen-exit"><path d="M9 4v5H4M20 9h-5V4M15 20v-5h5M4 15h5v5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></g></svg></button><button class="settings-toggle" id="settings-toggle" type="button" aria-label="Open settings" aria-pressed="false"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9.7 3.4h4.6l.6 2a7.4 7.4 0 0 1 1.6.9l2-.5 2.3 4-1.5 1.5a7.9 7.9 0 0 1 0 1.8l1.5 1.5-2.3 4-2-.5a7.4 7.4 0 0 1-1.6.9l-.6 2H9.7l-.6-2a7.4 7.4 0 0 1-1.6-.9l-2 .5-2.3-4 1.5-1.5a7.9 7.9 0 0 1 0-1.8L3.2 9.8l2.3-4 2 .5a7.4 7.4 0 0 1 1.6-.9l.6-2Z" fill="none" stroke="currentColor" stroke-width="1.55" stroke-linejoin="round"/><circle cx="12" cy="12" r="2.6" fill="none" stroke="currentColor" stroke-width="1.55"/></svg></button><button class="radar-toggle" id="radar-toggle" type="button" aria-pressed="false">RADAR</button><div class="live" id="live"><span class="live-dot"></span><span id="mode">STARTING</span></div></div></header>
    <div class="content">
    <main>
      <div class="scan-message" id="scan-message">Scanning the skies&hellip;</div>
      <section class="information">
        <div class="flight-heading"><div class="flight-copy"><div class="callsign" id="callsign">-</div><div class="identity"><span class="registration" id="registration"></span><span class="aircraft-name" id="aircraft-name"></span></div></div><div class="route-visual"><div class="route-brand" id="route-brand"><span class="route-brand-name" id="route-brand-name"></span><img class="route-brand-logo" id="operator-logo" alt=""></div><div class="route-map" id="route-map" aria-label="Route map centred on the tracked aircraft"></div></div></div>
        <div class="metrics">
          <div class="metric"><div class="metric-label">ALTITUDE</div><div class="metric-value" id="altitude">--</div></div>
          <div class="metric"><div class="metric-label">GROUND SPEED</div><div class="metric-value" id="speed">--</div></div>
          <div class="metric"><div class="metric-label">VERTICAL</div><div class="metric-value" id="vertical">--</div></div>
          <div class="metric metric-distance"><div class="metric-label">DISTANCE</div><div class="metric-value"><span id="distance-main">--</span> NM</div></div>
        </div>
      </section>
    </main>
    <aside class="radar-panel" aria-label="Nearby aircraft radar">
      <div class="radar-heading"><strong>WIDER AREA</strong><span id="radar-count">0 CONTACTS</span></div>
      <div class="radar-stage"><svg id="radar" viewBox="0 0 100 100" role="img" aria-label="North-up radar of nearby aircraft">
        <circle class="radar-grid" cx="50" cy="50" r="46"/><circle class="radar-grid" cx="50" cy="50" r="30.7"/><circle class="radar-grid" cx="50" cy="50" r="15.3"/>
        <path class="radar-axis" d="M50 4V96M4 50H96"/><text x="50" y="2.8" text-anchor="middle" fill="#6c9aac" font-size="3">N</text>
        <g id="radar-contacts"></g><circle class="radar-home" cx="50" cy="50" r="1.25"/>
      </svg></div>
      <div class="radar-foot"><form class="radar-track" id="radar-track-form"><input class="track-input" id="track-flight" type="text" maxlength="10" autocomplete="off" spellcheck="false" aria-label="Flight number or callsign to track" placeholder="BA703"><button class="track-button" id="track-button" type="submit" aria-pressed="false">TRACK</button></form><div class="radar-zoom"><button class="zoom-button" id="zoom-in" type="button" aria-label="Zoom in and reduce tracking range">&minus;</button><span id="radar-range">25 NM</span><button class="zoom-button" id="zoom-out" type="button" aria-label="Zoom out and increase tracking range">+</button></div></div>
    </aside>
    <aside class="settings-panel" aria-label="Display settings">
      <div class="settings-heading"><strong>SETTINGS</strong><span>DISPLAY</span></div>
      <div class="settings-list">
        <div class="setting-row"><span class="setting-label">SOUND ALERTS</span><div class="setting-control"><span id="settings-sound-state">ON</span><button class="setting-action" id="settings-sound" type="button">MUTE</button></div></div>
        <div class="setting-row"><span class="setting-label">CURRENT LOCATION</span><div class="setting-control"><span class="settings-location-value" id="settings-location-value">LOADING</span><button class="setting-action" id="settings-location" type="button">SELECT LOCATION</button></div></div>
        <div class="setting-row"><span class="setting-label">FULL SCREEN</span><div class="setting-control"><span id="settings-fullscreen-state">WINDOWED</span><button class="setting-action" id="settings-fullscreen" type="button">ENTER</button></div></div>
      </div>
      <div class="settings-foot">SETTINGS ARE SAVED ON THIS DISPLAY</div>
    </aside>
    </div>
    <footer><span class="footer-credits">For Sam &#x1F9E1;&nbsp;&nbsp;&nbsp;&nbsp;Data: <a class="footer-attribution" href="https://adsb.lol/" target="_blank" rel="noopener">ADSB.lol</a><span id="flightaware-credit" hidden> + <a class="footer-attribution" href="https://www.flightaware.com/commercial/aeroapi/" target="_blank" rel="noopener">FlightAware</a></span> + <a class="footer-attribution" href="https://www.adsbdb.com/" target="_blank" rel="noopener">ADSBDB</a> + <a class="footer-attribution" href="https://ourairports.com/data/" target="_blank" rel="noopener">OurAirports</a>&nbsp;&nbsp;/&nbsp;&nbsp;Map: <a class="footer-attribution" href="https://leafletjs.com/" target="_blank" rel="noopener">Leaflet</a> + &copy; <a class="footer-attribution" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>&nbsp;&nbsp;/&nbsp;&nbsp;Logos: <a class="footer-attribution" href="https://github.com/soaring-symbols/soaring-symbols" target="_blank" rel="noopener">Soaring Symbols</a>&nbsp;&nbsp;/&nbsp;&nbsp;Flags: <a class="footer-attribution" href="https://github.com/catamphetamine/country-flag-icons" target="_blank" rel="noopener">country-flag-icons</a>&nbsp;&nbsp;/&nbsp;&nbsp;Sound: arunangshubanerjee via <a class="footer-attribution" href="https://pixabay.com/" target="_blank" rel="noopener">Pixabay</a>&nbsp;&nbsp;/&nbsp;&nbsp;Inspired by: <a class="footer-attribution" href="https://github.com/AxisNimble/TheFlightWall_OSS" target="_blank" rel="noopener">TheFlightWall</a></span><span id="footer-status">Connecting</span></footer>
  </section>
  <div class="location-backdrop" id="location-backdrop" role="dialog" aria-modal="true" aria-labelledby="location-title">
    <div class="location-dialog">
      <div class="location-dialog-head"><div><strong id="location-title">SELECT CURRENT LOCATION</strong><p>Use your device's high-accuracy browser location, or click and drag the marker to set the exact home centre used for aircraft tracking.</p></div><button class="location-close" id="location-close" type="button" aria-label="Close location selector">&times;</button></div>
      <div id="location-map" aria-label="Map for selecting the Over-Head home location"></div>
      <div class="location-details"><div class="location-coordinate"><span>LATITUDE</span><strong id="location-latitude">--</strong></div><div class="location-coordinate"><span>LONGITUDE</span><strong id="location-longitude">--</strong></div></div>
      <div class="location-status" id="location-status">Choose a point on the map or use your device location.</div>
      <div class="location-actions"><button class="setting-action" id="location-use-device" type="button">USE MY LOCATION</button><button class="setting-action" id="location-cancel" type="button">CANCEL</button><button class="setting-action location-primary" id="location-save" type="button">SAVE LOCATION</button></div>
    </div>
  </div>
  <audio id="alert-tone" preload="auto" src="/beep-tone.mp3"></audio>
  <script>
    const byId=id=>document.getElementById(id);
    function updateBrowserTitle(d){
      const prefix='Over-Head '+String.fromCodePoint(0x2708,0xFE0F);
      const hasAircraft=Boolean(d&&d.aircraft_id);
      if(!hasAircraft){ document.title=prefix; return; }
      const origin=String(d.origin||'N/A').trim().toUpperCase()||'N/A';
      const destination=String(d.destination||'N/A').trim().toUpperCase()||'N/A';
      document.title=prefix+' '+origin+' to '+destination;
    }
    const svgNS='http://www.w3.org/2000/svg';
    const radarRanges=[5,10,15,25,40,60,100]; let currentRadarRange=25;
    let flightTrackingActive=false,flightTrackingQuery='';
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
    let radarWasOpenBeforeSettings=false;
    const number=(value,digits=0)=>value==null?'--':Number(value).toFixed(digits);
    const routeMapElement=byId('route-map'); let routeMap=null,routeMapLayers=[];
    let routeFlowKey='',routeFlowStartedAt=Date.now();
    let currentHomeLatitude=null,currentHomeLongitude=null;
    let locationMap=null,locationMarker=null,locationAccuracyCircle=null;
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
    function clearRouteMap(){ for(const layer of routeMapLayers)layer.remove(); routeMapLayers=[]; routeMapElement.classList.remove('visible'); }
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
    function renderHeading(d,hasAircraft){
      const heading=byId('callsign'); heading.replaceChildren(); const routeKnown=Boolean(d.route); heading.classList.toggle('route-known',hasAircraft&&routeKnown); heading.classList.toggle('route-unknown',hasAircraft&&!routeKnown);
      if(!hasAircraft){ heading.textContent=''; return; }
      if(!routeKnown){ heading.textContent='N/A to N/A'; return; }
      const airport=(side,code,country,available,revision)=>{
        const svg=document.createElementNS(svgNS,'svg'); svg.setAttribute('class','route-airport'); svg.setAttribute('viewBox','0 0 150 100'); svg.setAttribute('role','img'); svg.setAttribute('aria-label',String(code||''));
        const makeText=className=>{ const element=document.createElementNS(svgNS,'text'); element.setAttribute('class',className); element.setAttribute('x','75'); element.setAttribute('y','90'); element.setAttribute('text-anchor','middle'); element.setAttribute('font-family','Segoe UI,Arial,sans-serif'); element.setAttribute('font-size','112'); element.setAttribute('font-weight','800'); element.setAttribute('letter-spacing','-4'); element.setAttribute('textLength','144'); element.setAttribute('lengthAdjust','spacingAndGlyphs'); element.textContent=String(code||''); return element; };
        const text=makeText('route-airport-text');
        if(available){
          const maskId='route-mask-'+side+'-'+String(revision||'').replace(/[^A-Za-z0-9_-]/g,'');
          const defs=document.createElementNS(svgNS,'defs'),mask=document.createElementNS(svgNS,'mask'); mask.setAttribute('id',maskId); mask.setAttribute('maskUnits','userSpaceOnUse'); mask.setAttribute('x','0'); mask.setAttribute('y','0'); mask.setAttribute('width','150'); mask.setAttribute('height','100'); text.setAttribute('fill','white'); mask.appendChild(text); defs.appendChild(mask); svg.appendChild(defs);
          const href='/'+side+'-flag?v='+encodeURIComponent(revision||'');
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
      const state=byId('settings-sound-state'),action=byId('settings-sound');
      if(state)state.textContent=soundEnabled?'ON':'MUTED';
      if(action)action.textContent=soundEnabled?'MUTE':'ENABLE';
    }
    function toggleSound(){
      if(audioBlocked&&soundEnabled){ playTone(); return; }
      soundEnabled=!soundEnabled;
      audioBlocked=false;
      localStorage.setItem('overhead-sound',soundEnabled?'on':'muted');
      updateSoundButton();
    }
    function updateFullscreenButton(){
      const active=Boolean(document.fullscreenElement);
      const button=byId('fullscreen-toggle');
      button.classList.toggle('active',active);
      button.setAttribute('aria-pressed',String(active));
      button.setAttribute('aria-label',active?'Exit full screen':'Enter full screen');
      const state=byId('settings-fullscreen-state'),action=byId('settings-fullscreen');
      if(state)state.textContent=active?'FULL SCREEN':'WINDOWED';
      if(action)action.textContent=active?'EXIT':'ENTER';
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
      byId('location-latitude').textContent=formatCoordinate(lat);
      byId('location-longitude').textContent=formatCoordinate(lon);
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
      byId('location-backdrop').classList.add('open');
      let lat=Number(currentHomeLatitude),lon=Number(currentHomeLongitude);
      if(!Number.isFinite(lat)||!Number.isFinite(lon)){
        try{
          const response=await fetch('/status?t='+Date.now(),{cache:'no-store'});
          if(response.ok){
            const status=await response.json();
            lat=Number(status.home_latitude); lon=Number(status.home_longitude);
            if(Number.isFinite(lat)&&Number.isFinite(lon)){
              currentHomeLatitude=lat; currentHomeLongitude=lon;
            }
          }
        }catch(_){}
      }
      if(!Number.isFinite(lat)||!Number.isFinite(lon)){
        closeLocationSelector();
        return;
      }
      initialiseLocationMap(lat,lon);
      requestAnimationFrame(()=>{
        locationMap.invalidateSize(false);
        setLocationCandidate(lat,lon,null,true);
      });
      locationStatus('Choose a point on the map or use your device location.');
    }
    function closeLocationSelector(){
      setLocationButtonBusy(false);
      byId('location-backdrop').classList.remove('open');
      setRadar(true);
    }
    function setLocationButtonBusy(busy){
      const button=byId('location-use-device');
      button.classList.toggle('locating',busy);
      button.disabled=busy;
      button.textContent=busy?'LOCATING...':'USE MY LOCATION';
      button.setAttribute('aria-busy',String(busy));
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
      const latitude=Number(byId('location-latitude').textContent),longitude=Number(byId('location-longitude').textContent);
      if(!Number.isFinite(latitude)||!Number.isFinite(longitude)){
        locationStatus('Select a valid location first.',true);
        return;
      }
      const button=byId('location-save');
      button.disabled=true;
      locationStatus('Saving location and refreshing nearby aircraft...');
      try{
        const response=await fetch('/location',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({latitude,longitude})});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not save location');
        currentHomeLatitude=Number(data.latitude); currentHomeLongitude=Number(data.longitude);
        byId('settings-location-value').textContent=formatCoordinate(currentHomeLatitude)+', '+formatCoordinate(currentHomeLongitude);
        closeLocationSelector();
        update();
      }catch(error){
        locationStatus(error.message||'Could not save location',true);
      }finally{
        button.disabled=false;
      }
    }
    function drawRadar(contacts,radius,trackingActive){
      const layer=byId('radar-contacts'); layer.replaceChildren();
      for(const c of contacts||[]){
        const group=document.createElementNS(svgNS,'g'); group.setAttribute('class','contact'+(c.selected?' selected':'')+(c.emergency?' emergency':'')); group.setAttribute('transform','translate('+c.x+' '+c.y+') rotate('+(Number(c.track)||0)+')');
        const plane=document.createElementNS(svgNS,'path'); plane.setAttribute('d','M0-2.3L.65-.25 2.3.7 2.3 1.2.6.7.45 2 1.15 2.5 1.15 2.8 0 2.5-1.15 2.8-1.15 2.5-.45 2-.6.7-2.3 1.2-2.3.7-.65-.25Z'); group.appendChild(plane);
        if(!c.selected&&(contacts||[]).length<=10){ const label=document.createElementNS(svgNS,'text'); label.textContent=c.callsign; label.setAttribute('x','2.8'); label.setAttribute('y','-1.5'); label.setAttribute('transform','rotate('+(0-(Number(c.track)||0))+' 2.8 -1.5)'); group.appendChild(label); }
        layer.appendChild(group);
      }
      currentRadarRange=Number(radius)||25; byId('radar-count').textContent=(contacts||[]).length+' CONTACTS'; byId('radar-range').textContent=(trackingActive?'AUTO ':'')+number(radius,0)+' NM';
      const rangeIndex=radarRanges.indexOf(currentRadarRange);
      byId('zoom-in').disabled=Boolean(trackingActive)||rangeIndex===0;
      byId('zoom-out').disabled=Boolean(trackingActive)||rangeIndex===radarRanges.length-1;
    }
    function refreshSidePanelButtons(){
      const wall=byId('wall'),settingsOpen=wall.classList.contains('settings-open'),radarOpen=wall.classList.contains('radar-open');
      const radarButton=byId('radar-toggle'),settingsButton=byId('settings-toggle');
      radarButton.setAttribute('aria-pressed',String(radarOpen&&!settingsOpen));
      radarButton.textContent=radarOpen&&!settingsOpen?'HIDE RADAR':'RADAR';
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
      localStorage.setItem('overhead-radar-v2',open?'open':'closed');
      refreshSidePanelButtons();
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
        localStorage.setItem('overhead-radar-v2',radarWasOpenBeforeSettings?'open':'closed');
      }
      refreshSidePanelButtons();
      afterPanelChange();
    }
    function normaliseTrackInput(value){ return String(value||'').toUpperCase().replace(/[^A-Z0-9]/g,''); }
    function syncFlightTracking(d){
      flightTrackingActive=Boolean(d.tracking_active);
      flightTrackingQuery=String(d.tracking_query||'');
      const input=byId('track-flight'),button=byId('track-button');
      if(flightTrackingActive&&document.activeElement!==input)input.value=flightTrackingQuery;
      button.classList.toggle('tracking',flightTrackingActive);
      button.setAttribute('aria-pressed',String(flightTrackingActive));
      button.setAttribute('title',flightTrackingActive?'Tracking '+flightTrackingQuery+'. Click TRACK again to stop.':'Track this flight');
      const scan=byId('scan-message');
      if(scan)scan.textContent=flightTrackingActive&&!d.tracking_found?'Tracking '+flightTrackingQuery+'...':'Scanning the skies...';
    }
    async function submitFlightTracking(event){
      event.preventDefault();
      const input=byId('track-flight');
      const value=normaliseTrackInput(input.value);
      if(!value){ input.focus(); return; }
      try{
        let response;
        if(flightTrackingActive&&value===normaliseTrackInput(flightTrackingQuery)){
          response=await fetch('/track',{method:'DELETE',cache:'no-store'});
        }else{
          response=await fetch('/track',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({flight:value}),cache:'no-store'});
        }
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'Could not change flight tracking');
        if(data.flight)input.value=data.flight;
        await update();
      }catch(error){
        input.setCustomValidity(error.message||'Could not track flight');
        input.reportValidity();
        setTimeout(()=>input.setCustomValidity(''),1800);
      }
    }
    async function changeRadarRange(direction){
      if(flightTrackingActive)return;
      let index=radarRanges.indexOf(currentRadarRange); if(index<0)index=radarRanges.reduce((best,value,i)=>Math.abs(value-currentRadarRange)<Math.abs(radarRanges[best]-currentRadarRange)?i:best,0);
      const next=radarRanges[Math.max(0,Math.min(radarRanges.length-1,index+direction))]; if(next===currentRadarRange)return;
      currentRadarRange=next; localStorage.setItem('overhead-radar-range',String(next)); byId('radar-range').textContent=next+' NM';
      const rangeIndex=radarRanges.indexOf(next);
      byId('zoom-in').disabled=rangeIndex===0; byId('zoom-out').disabled=rangeIndex===radarRanges.length-1;
      await fetch('/radar-range?radius='+next,{cache:'no-store'});
    }
    async function update(){
      try{
        const response=await fetch('/status?t='+Date.now(),{cache:'no-store'}); if(!response.ok)throw new Error('status'); const d=await response.json();
        updateBrowserTitle(d);
        byId('wall').classList.remove('loading');
        const mode=String(d.mode||'live').toLowerCase(); byId('live').className='live '+mode; byId('mode').textContent=mode.toUpperCase();
        const hasAircraft=Boolean(d.aircraft_id); byId('wall').classList.toggle('no-aircraft',!hasAircraft); byId('wall').classList.toggle('aircraft-active',hasAircraft); if(!hasAircraft)byId('wall').classList.remove('brand-pulse'); renderHeading(d,hasAircraft);
        if(d.aircraft_id&&d.aircraft_id!==trackedAircraftId){ trackedAircraftId=d.aircraft_id; if(soundEnabled)pulseBrandLogo(); playTone(); } else if(!d.aircraft_id){ trackedAircraftId=''; }
        byId('registration').textContent=d.flight_details||d.registration||'REGISTRATION UNKNOWN'; byId('aircraft-name').textContent=d.aircraft_name+(d.aircraft_type?'  \\u00b7  '+d.aircraft_type:''); requestAnimationFrame(()=>fitSingleLine(byId('registration'),18));
        const logo=byId('operator-logo'),brandName=byId('route-brand-name'),brandBox=byId('route-brand');
        const airlineLabel=d.airline_name||'';
        brandName.textContent=airlineLabel;
        if(d.logo_available){
          brandBox.style.display='flex';
          logo.onload=()=>{ brandBox.style.display='flex'; logo.style.display='block'; brandName.style.display='none'; requestAnimationFrame(()=>fitLogoArtwork(logo)); };
          logo.onerror=()=>{ logo.style.display='none'; brandName.style.display=airlineLabel?'block':'none'; brandBox.style.display=airlineLabel?'flex':'none'; };
          logo.alt=(airlineLabel||'Airline')+' logo';
          logo.src='/operator-logo?v='+encodeURIComponent(d.logo_revision);
        }else{
          logo.removeAttribute('src'); logo.style.display='none';
          brandName.style.display=airlineLabel?'block':'none';
          brandBox.style.display=airlineLabel?'flex':'none';
        }
        renderAltitude(d.altitude); byId('speed').textContent=d.speed==null?'--':Math.round(d.speed)+' KT'; byId('vertical').textContent=vertical(d.vertical_rate);
        byId('distance-main').textContent=number(d.distance_nm,1);
        if(Number.isFinite(Number(d.home_latitude))&&Number.isFinite(Number(d.home_longitude))){
          currentHomeLatitude=Number(d.home_latitude); currentHomeLongitude=Number(d.home_longitude);
          byId('settings-location-value').textContent=formatCoordinate(currentHomeLatitude)+', '+formatCoordinate(currentHomeLongitude);
        }
        renderRouteMap(d);
        syncFlightTracking(d);
        drawRadar(d.radar_contacts,d.radar_radius_nm,d.tracking_active);
        byId('flightaware-credit').hidden=!Boolean(d.flightaware_configured);
        byId('footer-status').textContent='Updated '+d.updated;
      }catch(_){ byId('live').className='live error'; byId('mode').textContent='RECONNECTING'; }
    }
    loadHeaderLogo();
    setRadar(localStorage.getItem('overhead-radar-v2')!=='closed');
    byId('radar-toggle').addEventListener('click',()=>setRadar(!byId('wall').classList.contains('radar-open')||byId('wall').classList.contains('settings-open')));
    byId('settings-toggle').addEventListener('click',()=>setSettings(!byId('wall').classList.contains('settings-open')));
    document.addEventListener('keydown',event=>{if(event.key.toLowerCase()==='r')setRadar(!byId('wall').classList.contains('radar-open')||byId('wall').classList.contains('settings-open'))});
    updateSoundButton();
    byId('sound-toggle').addEventListener('click',toggleSound);
    byId('settings-sound').addEventListener('click',toggleSound);
    byId('fullscreen-toggle').addEventListener('click',toggleFullscreen);
    byId('settings-fullscreen').addEventListener('click',toggleFullscreen);
    document.addEventListener('fullscreenchange',updateFullscreenButton);
    updateFullscreenButton();
    byId('radar-track-form').addEventListener('submit',submitFlightTracking);
    byId('zoom-in').addEventListener('click',()=>changeRadarRange(-1)); byId('zoom-out').addEventListener('click',()=>changeRadarRange(1));
    byId('settings-location').addEventListener('click',openLocationSelector);
    byId('location-use-device').addEventListener('click',useDeviceLocation);
    byId('location-save').addEventListener('click',saveSelectedLocation);
    byId('location-close').addEventListener('click',closeLocationSelector);
    byId('location-cancel').addEventListener('click',closeLocationSelector);
    byId('location-backdrop').addEventListener('click',event=>{ if(event.target===byId('location-backdrop'))closeLocationSelector(); });
    document.addEventListener('keydown',event=>{
      if(event.key!=='Escape')return;
      if(byId('location-backdrop').classList.contains('open'))closeLocationSelector();
      else if(byId('wall').classList.contains('settings-open'))setSettings(false);
    });
    const savedRange=Number(localStorage.getItem('overhead-radar-range')); if(radarRanges.includes(savedRange)&&savedRange!==25){ currentRadarRange=savedRange; fetch('/radar-range?radius='+savedRange,{cache:'no-store'}); }
    window.addEventListener('resize',()=>requestAnimationFrame(()=>{ fitSingleLine(byId('registration'),18); const logo=byId('operator-logo'); if(logo&&logo.complete&&logo.naturalWidth)fitLogoArtwork(logo); }));
    setInterval(update,2000); update(); document.addEventListener('dblclick',toggleFullscreen);
  </script>
</body>
</html>
"""


class FrameState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.png = b""
        self.logo = b""
        self.logo_type = "image/svg+xml"
        self.origin_flag = b""
        self.destination_flag = b""
        self.last_live_aircraft: list[dict[str, Any]] = []
        self.payload: dict[str, Any] = {"mode": "starting", "updated": "never", "total": 0}

    def update(self, image, mode: str, plane: dict[str, Any] | None, distance: float | None, aircraft: list[dict[str, Any]], settings, logo: tuple[bytes, str, str] | None, identity: dict[str, str] | None = None, route: dict[str, Any] | None = None, origin_flag: bytes | None = None, destination_flag: bytes | None = None, flightaware_configured: bool = False, tracking: dict[str, Any] | None = None) -> None:
        data = BytesIO()
        image.save(data, format="PNG")
        plane = plane or {}
        identity = identity or {}
        route = route or {}
        tracking = tracking or {}
        registration = identity.get("registration") or str(plane.get("r") or "").strip()
        code = identity.get("operator_code") or operator_code(plane)
        logo_data, logo_type, logo_source = logo or (b"", "", "")
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
            "tracking_active": bool(tracking.get("active")),
            "tracking_query": str(tracking.get("query") or ""),
            "tracking_resolved_callsign": str(tracking.get("resolved_callsign") or ""),
            "tracking_found": bool(tracking.get("found")),
            "origin": route.get("origin", ""), "destination": route.get("destination", ""),
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
            "operator_code": code, "airline_name": identity.get("airline_name", ""), "logo_available": bool(logo_data),
            "logo_source": logo_source,
            "logo_revision": f"{code}-{hashlib.sha256(logo_data).hexdigest()[:12]}" if logo_data else code,
            "home_latitude": settings.latitude,
            "home_longitude": settings.longitude,
            "radar_radius_nm": settings.radius_nm,
            "radar_contacts": radar_contacts(aircraft, settings, plane),
        }
        with self.lock:
            self.png = data.getvalue()
            self.logo = logo_data
            self.logo_type = logo_type or "application/octet-stream"
            self.origin_flag = origin_flag or b""
            self.destination_flag = destination_flag or b""
            self.payload = payload


def update_state(state: FrameState, settings, demo: bool, logos: LogoStore, identities: AircraftIdentityStore, routes: FlightRouteStore | None = None, flags: FlagStore | None = None, tracker: FlightTrackingState | None = None) -> None:
    tracking = tracker.snapshot() if tracker else {"active": False, "query": "", "candidates": (), "resolved_callsign": "", "last_plane": None}
    if demo:
        aircraft, mode = DEMO["ac"], "demo"
    else:
        try:
            aircraft, mode = fetch_aircraft(settings), "live"
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            with state.lock:
                aircraft = list(state.last_live_aircraft)
            mode = "reconnecting"
        else:
            with state.lock:
                state.last_live_aircraft = list(aircraft)

    plane: dict[str, Any] | None
    distance: float | None
    tracking_found = False

    if tracking.get("active") and not demo:
        resolved_callsign = str(tracking.get("resolved_callsign") or "")
        tracked_plane = None
        try:
            tracked_plane, resolved = fetch_tracked_aircraft(tracking.get("candidates") or ())
            if tracked_plane:
                resolved_callsign = resolved
                if tracker:
                    tracker.remember(tracked_plane, resolved_callsign)
        except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, TimeoutError):
            tracked_plane = None

        if tracked_plane is None:
            tracked_plane = tracking.get("last_plane")

        plane = tracked_plane
        distance = tracking_distance_nm(plane, settings)
        if plane is not None:
            tracking_found = True
            settings.radius_nm = automatic_tracking_radius(distance)
            aircraft = merge_tracked_aircraft(aircraft, plane)
        tracking = {
            **tracking,
            "resolved_callsign": resolved_callsign,
            "found": tracking_found,
        }
    else:
        plane, distance = select_nearest(aircraft, settings)
        tracking = {**tracking, "found": False}

    identity = {} if demo else (identities.get(plane) or {})
    route = {} if demo or routes is None else (routes.get(plane) or {})
    origin_flag = flags.get(route.get("origin_country", "")) if flags else None
    destination_flag = flags.get(route.get("destination_country", "")) if flags else None
    image = render(plane, distance, mode)
    save_frame(image, settings)
    code = identity.get("operator_code") or operator_code(plane)
    state.update(image, mode, plane, distance, aircraft, settings, logos.get(code, identity.get("airline_name", "")), identity, route, origin_flag, destination_flag, bool(routes and routes.api_key), tracking)


def refresh_loop(state: FrameState, settings, demo: bool, logos: LogoStore, identities: AircraftIdentityStore, routes: FlightRouteStore, flags: FlagStore, refresh_event: threading.Event, tracker: FlightTrackingState | None = None) -> None:
    while True:
        try:
            update_state(state, settings, demo, logos, identities, routes, flags, tracker)
        except Exception as exc:
            with state.lock:
                state.payload["mode"] = "error"
                state.payload["updated"] = time.strftime("%H:%M:%S")
                state.payload["error"] = type(exc).__name__
        refresh_event.wait(timeout=max(1.0, settings.refresh_seconds))
        refresh_event.clear()


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


def configuration_required_handler(config_path: Path):
    class Handler(BaseHTTPRequestHandler):
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


def handler_factory(state: FrameState, settings, refresh_event: threading.Event, tone_path: Path, config_path: Path, routes: FlightRouteStore, logos: LogoStore, tracker: FlightTrackingState, demo: bool = False):
    asset_dir = Path(__file__).resolve().parent

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            request_url = urlsplit(self.path)
            path = request_url.path
            if path == "/": self.send(HTTPStatus.OK, "text/html; charset=utf-8", PAGE, cache=False)
            elif path == "/frame.png":
                with state.lock: body = state.png
                self.send(HTTPStatus.OK, "image/png", body, cache=False)
            elif path == "/status":
                with state.lock: body = json.dumps(state.payload).encode()
                self.send(HTTPStatus.OK, "application/json", body, cache=False)
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
                body = json.dumps({"current": current, "resolver": routes.debug_snapshot()}, indent=2).encode()
                self.send(HTTPStatus.OK, "application/json", body, cache=False)
            elif path == "/over-head-logo":
                asset = overhead_logo_asset(asset_dir)
                if asset:
                    body, content_type = asset
                    self.send(HTTPStatus.OK, content_type, body, cache=True)
                else:
                    self.send(HTTPStatus.NOT_FOUND, "text/plain", b"Over-Head_Logo not found", cache=False)
            elif path == "/operator-logo":
                with state.lock: body, content_type = state.logo, state.logo_type
                if body: self.send(HTTPStatus.OK, content_type, body, cache=True)
                else: self.send(HTTPStatus.NOT_FOUND, "text/plain", b"No logo", cache=False)
            elif path in {"/origin-flag", "/destination-flag"}:
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
                    radius = int(parse_qs(request_url.query).get("radius", [""])[0])
                except (TypeError, ValueError):
                    radius = 0
                if radius not in RADAR_RANGES:
                    self.send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": "invalid range", "allowed": RADAR_RANGES}).encode(), cache=False)
                else:
                    if tracker.snapshot()["active"]:
                        self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Radar range is automatic while flight tracking is active"}', cache=False)
                        return
                    settings.radius_nm = float(radius)
                    tracker.set_manual_radius(float(radius))
                    with state.lock: state.payload["radar_radius_nm"] = radius
                    refresh_event.set()
                    self.send(HTTPStatus.OK, "application/json", json.dumps({"radius_nm": radius}).encode(), cache=False)
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

            if path == "/track":
                if demo:
                    self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Flight tracking is unavailable in demo mode"}', cache=False)
                    return
                try:
                    query = normalise_flight_query(request.get("flight"))
                    candidates = logos.tracking_callsigns(query)
                except ValueError as exc:
                    self.send(HTTPStatus.BAD_REQUEST, "application/json", json.dumps({"error": str(exc)}).encode(), cache=False)
                    return
                tracker.start(query, candidates)
                with state.lock:
                    state.payload["tracking_active"] = True
                    state.payload["tracking_query"] = query
                    state.payload["tracking_resolved_callsign"] = ""
                    state.payload["tracking_found"] = False
                refresh_event.set()
                self.send(HTTPStatus.OK, "application/json", json.dumps({"tracking": True, "flight": query, "candidates": candidates}).encode(), cache=False)
                return

            if path != "/location":
                self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Not found"}', cache=False)
                return
            if demo:
                self.send(HTTPStatus.CONFLICT, "application/json", b'{"error":"Location cannot be changed in demo mode"}', cache=False)
                return
            try:
                latitude, longitude = update_live_location_config(
                    config_path, settings, request.get("latitude"), request.get("longitude")
                )
            except (OSError, ValueError) as exc:
                body = json.dumps({"error": str(exc) or "Could not save location"}).encode()
                self.send(HTTPStatus.BAD_REQUEST, "application/json", body, cache=False)
                return
            with state.lock:
                state.payload["home_latitude"] = latitude
                state.payload["home_longitude"] = longitude
            refresh_event.set()
            body = json.dumps({"saved": True, "latitude": latitude, "longitude": longitude}).encode()
            self.send(HTTPStatus.OK, "application/json", body, cache=False)

        def do_DELETE(self) -> None:
            if urlsplit(self.path).path != "/track":
                self.send(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"Not found"}', cache=False)
                return
            restored_radius = tracker.stop()
            settings.radius_nm = restored_radius
            with state.lock:
                state.payload["tracking_active"] = False
                state.payload["tracking_query"] = ""
                state.payload["tracking_resolved_callsign"] = ""
                state.payload["tracking_found"] = False
                state.payload["radar_radius_nm"] = restored_radius
            refresh_event.set()
            self.send(HTTPStatus.OK, "application/json", json.dumps({"tracking": False, "radius_nm": restored_radius}).encode(), cache=False)

        def send(self, status, content_type, body, cache=True) -> None:
            self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "public, max-age=30" if cache else "no-store"); self.end_headers(); self.wfile.write(body)

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
    refresh_event = threading.Event()
    threading.Thread(target=refresh_loop, args=(state, settings, args.demo, logos, identities, routes, flags, refresh_event, tracker), daemon=True).start()
    tone_path = Path(__file__).resolve().with_name("beep-tone.mp3")
    server = ThreadingHTTPServer((args.host, args.port), handler_factory(state, settings, refresh_event, tone_path, config_path, routes, logos, tracker, args.demo))
    print(f"Over-Head monitor running at {url}"); print("UI revision: persistent-flight-tracking-v46")
    if route_api_key:
        print("Route data: FlightAware AeroAPI primary / adsb.im + ADSB.lol live-validated fallbacks")
    else:
        print("Route data: adsb.im + ADSB.lol live-validated fallbacks (FlightAware key not configured)")
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
