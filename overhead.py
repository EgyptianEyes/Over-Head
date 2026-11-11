#!/usr/bin/env python3
"""Render nearby ADS-B traffic to a 128x64 RGB888 framebuffer."""

from __future__ import annotations

import argparse
import json
import math
import threading
import datetime
import email.utils
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont


WIDTH = 128
HEIGHT = 64
NORMAL_ACQUISITION_INTERVAL_SECONDS = 60.0
BG = (2, 4, 9)
CYAN = (20, 236, 255)
WHITE = (232, 246, 255)
MUTED = (88, 132, 150)
AMBER = (255, 176, 32)
RED = (255, 62, 82)


@dataclass
class Settings:
    latitude: float = 51.5074
    longitude: float = -0.1278
    radius_nm: float = 25.0
    refresh_seconds: float = NORMAL_ACQUISITION_INTERVAL_SECONDS
    output_rgb: str = "output/frame.rgb"
    output_png: str = "output/frame.png"
    demo_on_failure: bool = False
    query_radius_nm: float | None = None

    def __post_init__(self) -> None:
        if self.query_radius_nm is None:
            self.query_radius_nm = float(self.radius_nm)


class AircraftFeedRateLimited(OSError):
    """Neither aircraft provider is currently eligible for a request."""

    def __init__(self, retry_seconds: float):
        self.retry_seconds = max(0.0, float(retry_seconds))
        super().__init__(f"Aircraft providers unavailable; retry in {self.retry_seconds:.0f}s")


class AircraftFeedUnavailable(OSError):
    """Aircraft providers are restricted or unavailable for a non-quota reason."""

    def __init__(self, retry_seconds: float):
        self.retry_seconds = max(0.0, float(retry_seconds))
        super().__init__(f"Aircraft providers unavailable; retry in {self.retry_seconds:.0f}s")


class AircraftPositionStale(OSError):
    pass


class AircraftPositionInvalid(OSError):
    pass


AIRCRAFT_PROVIDERS = (
    ("ADSB.lol", "https://api.adsb.lol"),
    ("ADSB.fi", "https://opendata.adsb.fi"),
)
_aircraft_request_lock = threading.RLock()
_provider_retry_until: dict[str, float] = {}
_provider_backoff: dict[str, float] = {}
_provider_request_counts: dict[str, int] = {}
_provider_last_status: dict[str, str] = {}
_provider_last_attempt_at: dict[str, float] = {}
_provider_last_success_at: dict[str, float] = {}
_last_successful_aircraft_provider = ""
_last_provider_switch_reason = ""
_provider_request_windows: dict[str, list[float]] = {}
PROVIDER_REQUESTS_PER_MINUTE = 30
AGGREGATE_REQUESTS_PER_MINUTE = 30
EMPTY_PRIMARY_VERIFY_EVERY = 3
_active_aircraft_provider = ""
_last_aircraft_observation: float | None = None
_provider_empty_counts: dict[str, int] = {}


def normalise_aircraft(record: dict[str, Any], source: str, received_at: float, payload_time: Any = None) -> dict[str, Any]:
    result = dict(record)
    result["hex"] = str(record.get("hex") or "").strip().lower()
    if "flight" in result:
        result["flight"] = str(result["flight"] or "").strip().upper()
    try:
        age = float(record.get("seen_pos"))
        generated_at = float(payload_time) if payload_time is not None else received_at
        if generated_at > 100_000_000_000:
            generated_at /= 1000.0
        if not math.isfinite(age) or age < 0 or not math.isfinite(generated_at):
            raise ValueError("Invalid position age")
        validate_location(record.get("lat"), record.get("lon"))
        observed_at = min(received_at, generated_at) - age
        result.update(source=source, observed_at=observed_at, received_at=received_at,
                      reported_position_age=age, seen_pos=max(0.0, received_at - observed_at))
    except (TypeError, ValueError, OverflowError):
        result.update(source=source, observed_at=None, received_at=received_at, seen_pos=999.0)
    return result


def _request_budget_wait(name: str, now: float) -> float:
    windows = {key: [stamp for stamp in _provider_request_windows.get(key, []) if now - stamp < 60]
               for key in (name, "_aggregate")}
    waits = [0.0]
    for key, limit in ((name, PROVIDER_REQUESTS_PER_MINUTE), ("_aggregate", AGGREGATE_REQUESTS_PER_MINUTE)):
        window = windows[key]
        if len(window) >= limit:
            waits.append(max(0.0, 60 - (now - window[0])))
    return max(waits)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            return max(0.0, (date - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _pause_provider(name: str, error: Exception) -> None:
    status = getattr(error, "code", None)
    headers = getattr(error, "headers", None)
    retry = _retry_after_seconds(headers.get("Retry-After") if headers else None)
    if retry is None and headers and headers.get("x-ratelimit-remaining") == "0":
        retry = _retry_after_seconds(headers.get("x-ratelimit-reset"))
    minimum = 300.0 if status == 403 else 30.0
    delay = max(minimum, retry if retry is not None else _provider_backoff.get(name, minimum))
    _provider_retry_until[name] = time.monotonic() + delay
    _provider_backoff[name] = min(3600.0, max(minimum * 2.0, delay * 2.0))
    _provider_last_status[name] = "429" if status == 429 else f"HTTP {status}" if status is not None else type(error).__name__


def aircraft_provider_status() -> dict[str, dict[str, object]]:
    """Local-only diagnostics; never contacts an upstream API."""
    now = time.monotonic()
    providers = {
        name: {
            "requests": _provider_request_counts.get(name, 0),
            "retry_in_seconds": round(max(0.0, _provider_retry_until.get(name, 0) - now)),
            "last_status": _provider_last_status.get(name, "not-tested"),
            "last_attempt_at": _provider_last_attempt_at.get(name),
            "last_success_at": _provider_last_success_at.get(name),
            "last_successful_provider": name == _last_successful_aircraft_provider,
            "budget_retry_in_seconds": round(_request_budget_wait(name, now), 2),
        }
        for name, _ in AIRCRAFT_PROVIDERS
    }
    providers["_meta"] = {
        "last_successful_provider": _last_successful_aircraft_provider or None,
        "last_provider_switch_reason": _last_provider_switch_reason or None,
        "active_provider": _active_aircraft_provider or None,
        "last_observation_at": _last_aircraft_observation,
        "position_age_seconds": max(0.0, time.time() - _last_aircraft_observation) if _last_aircraft_observation is not None else None,
        "aggregate_requests_per_minute": AGGREGATE_REQUESTS_PER_MINUTE,
    }
    return providers


def _provider_url(name: str, base: str, path: str) -> str:
    """Translate ADSB.lol v2 paths to ADSB.fi's v3 API where necessary."""
    if name != "ADSB.fi":
        return base + path
    parts = path.strip("/").split("/")
    if len(parts) == 5 and parts[:2] == ["v2", "point"]:
        return base + f"/api/v3/lat/{parts[2]}/lon/{parts[3]}/dist/{parts[4]}"
    if len(parts) == 3 and parts[:2] == ["v2", "hex"]:
        return base + f"/api/v3/hex/{parts[2]}"
    if len(parts) == 3 and parts[:2] == ["v2", "callsign"]:
        return base + f"/api/v3/callsign/{parts[2]}"
    raise ValueError("Unsupported ADSB.fi aircraft path")


def fetch_provider_aircraft(path: str) -> list[dict[str, Any]]:
    """Try primary, then independent fallback; one request per eligible provider."""
    global _last_successful_aircraft_provider, _last_provider_switch_reason, _active_aircraft_provider, _last_aircraft_observation
    with _aircraft_request_lock:
        wait_times = []
        last_error: Exception | None = None
        best_records: list[dict[str, Any]] = []
        best_observation = float("-inf")
        usable_response = False
        valid_empty_response = False
        invalid_response = False
        stale_response = False
        target_kind, _, target = path.removeprefix("/v2/").partition("/")
        targeted = target_kind in {"hex", "callsign"}
        for name, base in AIRCRAFT_PROVIDERS:
            now = time.monotonic()
            remaining = max(_provider_retry_until.get(name, 0.0) - now, _request_budget_wait(name, now))
            if remaining > 0:
                wait_times.append(remaining)
                continue
            request = urllib.request.Request(_provider_url(name, base, path), headers={"User-Agent": "Over-Head/0.2 (+personal wall display)"})
            _active_aircraft_provider = name
            for key in (name, "_aggregate"):
                _provider_request_windows[key] = [stamp for stamp in _provider_request_windows.get(key, []) if now - stamp < 60] + [now]
            _provider_request_counts[name] = _provider_request_counts.get(name, 0) + 1
            _provider_last_attempt_at[name] = time.time()
            try:
                with urllib.request.urlopen(request, timeout=8) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("ac"), list):
                    raise ValueError(f"Unexpected {name} aircraft response")
                if any(not isinstance(record, dict) for record in payload["ac"]):
                    raise ValueError(f"Invalid {name} aircraft records")
                received_at = time.time()
                records = [normalise_aircraft(record, name, received_at, payload.get("now"))
                           if "lat" in record or "lon" in record else dict(record) for record in payload["ac"]]
                if targeted:
                    field = "hex" if target_kind == "hex" else "flight"
                    records = [record for record in records if str(record.get(field) or "").strip().upper() == target.upper()]
                invalid_positions = [record for record in records if record.get("observed_at") is None]
                fresh = [record for record in records if record.get("observed_at") is not None
                         and record.get("seen_pos", 999) <= (60 if targeted else 20)]
                stale = [record for record in records if record.get("observed_at") is not None
                         and record.get("seen_pos", 999) > (60 if targeted else 20)]
                if records and not fresh and (invalid_positions or stale):
                    invalid_response = bool(invalid_positions)
                    stale_response = bool(stale) and not invalid_positions
                    _provider_last_status[name] = "invalid positions" if invalid_response else "stale positions"
                    _last_provider_switch_reason = f"{name} returned {'invalid' if invalid_response else 'stale'} positions"
                    if invalid_response:
                        _pause_provider(name, ValueError(f"Invalid {name} aircraft positions"))
                    continue
                if targeted:
                    if target_kind == "callsign" and len({record.get("hex") for record in records}) > 1:
                        _provider_last_status[name] = "ambiguous identity"
                        _last_provider_switch_reason = f"{name} returned conflicting aircraft identities"
                        usable_response = True
                        continue
                if targeted and records and not fresh:
                    invalid_response = True
                    _provider_last_status[name] = "invalid or stale target position"
                    continue
                if not targeted and not records:
                    _provider_empty_counts[name] = _provider_empty_counts.get(name, 0) + 1
                    if name == AIRCRAFT_PROVIDERS[0][0] and _provider_empty_counts[name] % EMPTY_PRIMARY_VERIFY_EVERY != 0:
                        _provider_last_status[name] = "empty"
                        _provider_last_success_at[name] = time.time()
                        _last_successful_aircraft_provider = name
                        return []
                    if name == AIRCRAFT_PROVIDERS[0][0]:
                        _provider_last_status[name] = "empty; verifying fallback"
                        valid_empty_response = True
                        usable_response = True
                        continue
                    valid_empty_response = True
                elif records:
                    _provider_empty_counts[name] = 0
                usable_response = True
                _provider_retry_until.pop(name, None)
                _provider_backoff.pop(name, None)
                _provider_last_status[name] = "OK"
                previous_provider = _last_successful_aircraft_provider
                _provider_last_success_at[name] = time.time()
                _last_successful_aircraft_provider = name
                if previous_provider and previous_provider != name and not _last_provider_switch_reason:
                    _last_provider_switch_reason = f"{previous_provider} unavailable; used {name}"
                if fresh:
                    _last_aircraft_observation = max(record["observed_at"] for record in fresh)
                    return fresh
                if valid_empty_response:
                    return records
                observation = max((record.get("observed_at") or float("-inf") for record in records), default=float("-inf"))
                if observation > best_observation:
                    best_records, best_observation = records, observation
                _provider_last_status[name] = "stale" if records else "missing"
                _last_provider_switch_reason = f"{name} returned {'stale positions' if records else 'no matching aircraft'}"
            except urllib.error.HTTPError as exc:
                last_error = exc
                _last_provider_switch_reason = f"{name} returned HTTP {exc.code}"
                if exc.code in {403, 429}:
                    _pause_provider(name, exc)
                elif exc.code >= 500:
                    _pause_provider(name, exc)
                else:
                    _pause_provider(name, exc)
                exc.close()
            except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                _last_provider_switch_reason = f"{name} failed: {type(exc).__name__}"
                _pause_provider(name, exc)
        if usable_response:
            return best_records
        if valid_empty_response:
            return []
        if invalid_response:
            raise AircraftPositionInvalid("Aircraft providers returned invalid positions") from last_error
        if stale_response:
            raise AircraftPositionStale("Aircraft providers returned only stale positions") from last_error
        if invalid_response:
            raise AircraftPositionInvalid("Providers returned no valid aircraft positions") from last_error
        if stale_response:
            raise AircraftPositionStale("Providers returned only stale aircraft positions") from last_error
        if wait_times or any(_provider_retry_until.get(n, 0) > time.monotonic() for n, _ in AIRCRAFT_PROVIDERS):
            remaining = [max(0.0, _provider_retry_until.get(n, 0) - time.monotonic(), _request_budget_wait(n, time.monotonic())) for n, _ in AIRCRAFT_PROVIDERS]
            statuses = [_provider_last_status.get(name, "") for name, _ in AIRCRAFT_PROVIDERS]
            retry_seconds = max(1.0, min(remaining))
            if all(_request_budget_wait(name, time.monotonic()) > 0 for name, _ in AIRCRAFT_PROVIDERS):
                raise AircraftFeedRateLimited(retry_seconds) from last_error
            if any(status == "HTTP 403" for status in statuses):
                raise AircraftFeedUnavailable(retry_seconds) from last_error
            if all(status == "429" for status in statuses):
                raise AircraftFeedRateLimited(retry_seconds) from last_error
            raise OSError("All aircraft providers are cooling down") from last_error
        raise OSError("All aircraft providers failed") from last_error


def fetch_aircraft(settings: Settings, radius_nm: float | None = None) -> list[dict[str, Any]]:
    """Request positions only within the configured monitoring area."""
    configured_radius = settings.query_radius_nm if radius_nm is None else radius_nm
    query_radius = min(250.0, max(1.0, float(configured_radius)))
    records = fetch_provider_aircraft(f"/v2/point/{settings.latitude}/{settings.longitude}/{query_radius:g}")
    observed = [record for record in records if record.get("observed_at") is not None]
    if observed and not any(record.get("seen_pos", 999) <= 20 for record in observed):
        raise AircraftPositionStale("Only stale aircraft positions were returned")
    return records


DEMO = {
    "ac": [
        {
            "hex": "406b82",
            "flight": "BAW283 ",
            "r": "G-STBH",
            "t": "B77W",
            "lat": 51.533,
            "lon": -0.094,
            "alt_baro": 11875,
            "gs": 287.4,
            "track": 276.0,
            "squawk": "6352",
            "seen": 0.2,
            "seen_pos": 0.4,
            "category": "A5",
        }
    ]
}


def validate_location(latitude: Any, longitude: Any) -> tuple[float, float]:
    """Return validated geographic coordinates or raise ValueError."""
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        raise ValueError("Latitude and longitude must be numeric coordinates")
    try:
        latitude_value = float(latitude)
        longitude_value = float(longitude)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Latitude and longitude must be numeric coordinates") from exc
    if not math.isfinite(latitude_value) or not -90 <= latitude_value <= 90:
        raise ValueError("Latitude must be between -90 and 90")
    if not math.isfinite(longitude_value) or not -180 <= longitude_value <= 180:
        raise ValueError("Longitude must be between -180 and 180")
    return latitude_value, longitude_value


def load_settings(path: Path, allow_missing: bool = False) -> Settings:
    """Load settings, refusing to guess a live location when config is missing."""
    if not path.exists():
        if allow_missing:
            return Settings()
        raise FileNotFoundError(
            f"Configuration file not found: {path}. "
            "Live mode requires an explicit config.json with the display location."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be a JSON object")
    if not allow_missing and ("latitude" not in raw or "longitude" not in raw):
        raise ValueError("Live configuration requires explicit latitude and longitude values")
    allowed = Settings.__dataclass_fields__.keys()
    settings = Settings(**{k: raw[k] for k in allowed if k in raw})
    settings.latitude, settings.longitude = validate_location(settings.latitude, settings.longitude)
    return settings



def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_nm = 3440.065
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def select_nearest(
    aircraft: list[dict[str, Any]], settings: Settings, max_position_age_seconds: float = 20.0
) -> tuple[dict[str, Any] | None, float | None]:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for plane in aircraft:
        if not isinstance(plane, dict):
            continue
        lat, lon = plane.get("lat"), plane.get("lon")
        try:
            latitude, longitude = validate_location(lat, lon)
        except ValueError:
            continue
        try:
            seen_position = float(plane.get("seen_pos", 999))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(seen_position) or seen_position < 0 or seen_position > max_position_age_seconds:
            continue
        distance = haversine_nm(settings.latitude, settings.longitude, latitude, longitude)
        candidates.append((distance, plane))
    if not candidates:
        return None, None
    distance, plane = min(candidates, key=lambda item: item[0])
    return plane, distance


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    paths = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    )
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def text_fit(draw: ImageDraw.ImageDraw, value: str, max_width: int, size: int) -> ImageFont.ImageFont:
    while size > 5:
        candidate = font(size)
        if draw.textbbox((0, 0), value, font=candidate)[2] <= max_width:
            return candidate
        size -= 1
    return font(5)


def format_altitude(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{round(float(value) / 1000, 1):g}K FT"
    return "ALT N/A"


def draw_plane(draw: ImageDraw.ImageDraw, cx: int, cy: int, heading: float, color: tuple[int, int, int]) -> None:
    shape = [(0, -9), (2, -2), (9, 3), (9, 5), (2, 3), (2, 8), (5, 10), (5, 11), (0, 9), (-5, 11), (-5, 10), (-2, 8), (-2, 3), (-9, 5), (-9, 3), (-2, -2)]
    angle = math.radians(heading)
    points = []
    for x, y in shape:
        rx = x * math.cos(angle) - y * math.sin(angle)
        ry = x * math.sin(angle) + y * math.cos(angle)
        points.append((round(cx + rx), round(cy + ry)))
    draw.polygon(points, fill=color)


def render(plane: dict[str, Any] | None, distance_nm: float | None, status: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=(18, 53, 67))
    draw.line((0, 14, WIDTH, 14), fill=(18, 53, 67))
    draw.text((4, 3), "OVERHEAD", font=font(8), fill=CYAN)
    draw.text((89, 3), status.upper()[:8], font=font(7), fill=MUTED)

    if plane is None:
        draw.text((14, 25), "NO AIRCRAFT", font=font(12), fill=WHITE)
        draw.text((28, 44), "IN RANGE", font=font(8), fill=MUTED)
        return image

    callsign = str(plane.get("flight") or plane.get("r") or plane.get("hex") or "UNKNOWN").strip()
    registration = str(plane.get("r") or "").strip()
    aircraft_type = str(plane.get("t") or "TYPE N/A").strip()
    squawk = str(plane.get("squawk") or "")
    alert = squawk in {"7500", "7600", "7700"}
    accent = RED if alert else CYAN

    draw.text((4, 17), callsign, font=text_fit(draw, callsign, 83, 15), fill=WHITE)
    detail = "  ".join(part for part in (registration, aircraft_type) if part)
    draw.text((4, 34), detail[:22], font=text_fit(draw, detail[:22], 82, 8), fill=MUTED)
    draw.text((4, 47), format_altitude(plane.get("alt_baro")), font=font(8), fill=AMBER)
    speed = plane.get("gs")
    speed_text = f"{round(float(speed))} KT" if isinstance(speed, (int, float)) else "SPD N/A"
    distance_text = f"{distance_nm:.1f} NM" if distance_nm is not None else "DIST N/A"
    draw.text((48, 47), speed_text, font=font(8), fill=WHITE)
    draw.text((91, 54), distance_text, font=text_fit(draw, distance_text, 34, 7), fill=MUTED)
    draw_plane(draw, 105, 32, float(plane.get("track") or 0), accent)
    return image


def save_frame(image: Image.Image, settings: Settings) -> None:
    rgb_path = Path(settings.output_rgb)
    png_path = Path(settings.output_png)
    rgb_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(png_path)
    rgb_path.write_bytes(image.tobytes("raw", "RGB"))


def fetch_snapshot(
    settings: Settings, demo: bool = False
) -> tuple[dict[str, Any] | None, float | None, str, int]:
    aircraft, status = fetch_traffic_snapshot(settings, demo=demo)
    plane, distance = select_nearest(aircraft, settings)
    return plane, distance, status, len(aircraft)


def fetch_traffic_snapshot(
    settings: Settings, demo: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """Return live traffic, or bundled sample traffic only when demo is explicit."""
    if demo:
        return DEMO["ac"], "demo"
    return fetch_aircraft(settings), "live"


def produce_frame(settings: Settings, demo: bool = False) -> tuple[Image.Image, str]:
    plane, distance, status, _ = fetch_snapshot(settings, demo=demo)
    image = render(plane, distance, status)
    save_frame(image, settings)
    return image, status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--demo", action="store_true", help="render bundled sample aircraft")
    parser.add_argument("--watch", action="store_true", help="refresh continuously")
    args = parser.parse_args()
    settings = load_settings(Path(args.config), allow_missing=args.demo)
    while True:
        _, status = produce_frame(settings, demo=args.demo)
        print(f"Rendered {status} frame to {settings.output_rgb} and {settings.output_png}")
        if not args.watch:
            return 0
        time.sleep(NORMAL_ACQUISITION_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
