"""Private, bounded route-failure history and offline reporting."""

import argparse
import csv
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
DEFAULT_PATH = Path("cache/route-failures.sqlite3")


class RouteFailureLog:
    def __init__(self, path: Path = DEFAULT_PATH, retention_days: int = 30,
                 max_records: int = 10000, history_limit: int = 50) -> None:
        if min(retention_days, max_records, history_limit) < 1:
            raise ValueError("Route failure retention and limits must be positive")
        self.path = Path(path)
        self.retention_days = retention_days
        self.max_records = max_records
        self.history_limit = history_limit
        self._lock = threading.Lock()
        self._last_error_at = -math.inf

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            self.path.chmod(0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA auto_vacuum = FULL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS flights (
                    flight_key TEXT PRIMARY KEY, operating_date TEXT NOT NULL,
                    callsign TEXT, registration TEXT, icao_hex TEXT,
                    first_failure_utc TEXT, last_seen_utc TEXT, last_seen_epoch REAL,
                    classification TEXT, status TEXT, resolved_utc TEXT,
                    origin TEXT, destination TEXT, provider TEXT,
                    failed_attempts INTEGER DEFAULT 0, provider_requests INTEGER DEFAULT 0,
                    fallback_attempts INTEGER DEFAULT 0, fallback_successes INTEGER DEFAULT 0
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS attempts (
                    flight_key TEXT NOT NULL, lookup_id TEXT NOT NULL,
                    timestamp_utc TEXT, failed INTEGER, details TEXT,
                    PRIMARY KEY (flight_key, lookup_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS diagnostics (
                    flight_key TEXT NOT NULL, event_id TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL, event TEXT NOT NULL,
                    details TEXT NOT NULL,
                    PRIMARY KEY (flight_key, event_id)
                )
            """)
            return connection
        except (OSError, sqlite3.Error):
            connection.close()
            raise

    @staticmethod
    def _identifier(value: Any) -> str:
        return "".join(character for character in str(value or "").upper()
                       if character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")[:32]

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "")[:256]

    @classmethod
    def _airport(cls, value: Any) -> str:
        code = cls._identifier(value)
        return "" if code in ("NA", "UNKNOWN", "NONE") else code

    @classmethod
    def _attempts(cls, debug: dict[str, Any]) -> list[dict[str, Any]]:
        attempts = []
        for candidate in debug.get("attempts", [])[:16]:
            if not isinstance(candidate, dict):
                continue
            attempt = {
                key: candidate[key] for key in (
                    "requested", "accepted", "skipped", "airport_information_returned",
                    "geographical_validation_passed",
                ) if key in candidate and isinstance(candidate[key], (bool, type(None)))
            }
            for key in ("source", "reason", "error", "route"):
                if key in candidate:
                    attempt[key] = cls._text(candidate[key])
            for key in ("retry_in_seconds", "retry_delay_seconds", "cooldown_seconds"):
                value = candidate.get(key)
                if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
                    attempt[key] = float(value) if isinstance(value, float) else int(value)
            if "geographical_validation_passed" in candidate and isinstance(
                candidate["geographical_validation_passed"], bool,
            ):
                attempt["geographical_validation_passed"] = candidate["geographical_validation_passed"]
            if isinstance(candidate.get("validation"), dict):
                attempt["validation"] = {
                    key: cls._text(candidate["validation"].get(key))
                    for key in ("reason", "basis")
                    if key in candidate["validation"]
                }
                for key in ("direct_nm", "from_origin_nm", "to_destination_nm", "nearest_endpoint_nm", "route_excess_nm", "allowed_excess_nm"):
                    value = candidate["validation"].get(key)
                    if isinstance(value, (int, float)) and math.isfinite(float(value)):
                        attempt["validation"][key] = round(float(value), 1)
            attempt["callsign"] = cls._identifier(candidate.get("callsign"))
            status = candidate.get("http_status")
            if isinstance(status, int):
                attempt["http_status"] = status
            count = candidate.get("request_count")
            if isinstance(count, int) and count >= 0:
                attempt["request_count"] = count
            attempt["requests"] = [
                {"callsign": cls._identifier(request.get("callsign")),
                 "http_status": request.get("http_status") if isinstance(request.get("http_status"), int) else None,
                 **({"error": cls._text(request["error"])} if request.get("error") else {})}
                for request in candidate.get("requests", [])[:16] if isinstance(request, dict)
            ] if isinstance(candidate.get("requests"), list) else []
            attempt["requested"] = bool(candidate.get("requested"))
            attempt["skipped"] = bool(candidate.get("skipped"))
            raw = candidate.get("raw")
            if isinstance(raw, dict):
                attempt["airports"] = {
                    key: cls._text(raw[key]) for key in ("airport_codes", "airport_codes_iata")
                    if key in raw
                }
                attempt["airports"]["returned"] = [
                    {field: cls._airport(airport.get(field)) for field in ("iata", "icao")}
                    for airport in raw.get("airports", [])[:16] if isinstance(airport, dict)
                ] if isinstance(raw.get("airports"), list) else []
                provider_plausible = raw.get("provider_plausible")
                if isinstance(provider_plausible, bool):
                    attempt["provider_plausible"] = provider_plausible
            validation = candidate.get("validation")
            if isinstance(validation, dict):
                attempt["validation_reason"] = cls._text(validation.get("reason"))
            attempts.append(attempt)
        return attempts

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM flights WHERE last_seen_epoch < ?",
                           (now - self.retention_days * 86400,))
        connection.execute("""
            DELETE FROM flights WHERE flight_key IN (
                SELECT flight_key FROM flights ORDER BY last_seen_epoch DESC, flight_key
                LIMIT -1 OFFSET ?
            )
        """, (self.max_records,))
        connection.execute("DELETE FROM attempts WHERE flight_key NOT IN (SELECT flight_key FROM flights)")
        connection.execute("DELETE FROM diagnostics WHERE flight_key NOT IN (SELECT flight_key FROM flights)")

    def observe(self, plane: dict[str, Any], route: dict[str, Any],
                debug: dict[str, Any], now: float | None = None) -> None:
        """Observe only a published visitor aircraft; never initiate a lookup."""
        with self._lock:
            try:
                self._observe(plane, route, debug, time.time() if now is None else now)
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                # Do not include paths, payloads, session data or configuration.
                current = time.monotonic()
                if current - self._last_error_at >= 60:
                    LOGGER.error("Route failure history unavailable (%s)", type(exc).__name__)
                    self._last_error_at = current

    def record_event(self, plane: dict[str, Any], event: str, details: dict[str, Any] | None = None,
                     now: float | None = None) -> None:
        """Persist a bounded retry/decision diagnostic without counting a provider request."""
        with self._lock:
            try:
                self._record_event(plane, event, details or {}, time.time() if now is None else now)
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                current = time.monotonic()
                if current - self._last_error_at >= 60:
                    LOGGER.error("Route failure history unavailable (%s)", type(exc).__name__)
                    self._last_error_at = current

    def _record_event(self, plane: dict[str, Any], event: str,
                      details: dict[str, Any], now: float) -> None:
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        operating_date = stamp[:10]
        callsign = self._identifier(plane.get("flight"))
        registration = "".join(character for character in str(plane.get("r") or "").upper()
                               if character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")[:32]
        icao_hex = self._identifier(plane.get("hex"))
        if not (callsign or registration or icao_hex):
            return
        key = f"{operating_date}|{callsign}|{icao_hex or registration}"
        safe_event = self._text(event) or "decision"
        safe_details = self._safe_details(details)
        encoded = json.dumps(safe_details, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 8192:
            safe_details = {"truncated": True, "summary": self._text(details.get("decision") or event)}
            encoded = json.dumps(safe_details, separators=(",", ":"), sort_keys=True)
        event_id = uuid.uuid4().hex
        connection = self._connect()
        try:
            with connection:
                self._prune(connection, now)
                existing = connection.execute("SELECT 1 FROM flights WHERE flight_key=?", (key,)).fetchone()
                resolved = bool(details.get("resolved"))
                if existing is None and resolved:
                    return
                recent = connection.execute(
                    "SELECT timestamp_utc FROM diagnostics WHERE flight_key=? AND event=? "
                    "ORDER BY timestamp_utc DESC,rowid DESC LIMIT 1",
                    (key, safe_event),
                ).fetchone()
                if recent is not None:
                    last_event_at = datetime.fromisoformat(recent["timestamp_utc"]).timestamp()
                    if 0 <= now - last_event_at < 30:
                        return
                if existing is None:
                    connection.execute("""
                        INSERT INTO flights (flight_key, operating_date, callsign, registration,
                            icao_hex, first_failure_utc, last_seen_utc, last_seen_epoch,
                            classification, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'missing_both', 'unresolved')
                    """, (key, operating_date, callsign, registration, icao_hex, stamp, stamp, now))
                else:
                    connection.execute("UPDATE flights SET last_seen_utc=?,last_seen_epoch=? WHERE flight_key=?",
                                       (stamp, now, key))
                connection.execute("INSERT OR IGNORE INTO diagnostics VALUES (?, ?, ?, ?, ?)",
                                   (key, event_id, stamp, safe_event, encoded))
                connection.execute("""
                    DELETE FROM diagnostics WHERE flight_key=? AND event_id NOT IN (
                        SELECT event_id FROM diagnostics WHERE flight_key=?
                        ORDER BY timestamp_utc DESC, rowid DESC LIMIT ?
                    )
                """, (key, key, self.history_limit * 4))
                self._prune(connection, now)
        finally:
            connection.close()

    @classmethod
    def _safe_details(cls, value: Any, depth: int = 0) -> Any:
        """Keep diagnostic JSON bounded and omit coordinates/session-like fields."""
        if depth >= 5:
            return "[truncated]"
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:12]:
                name = str(key)[:64]
                if any(token in name.casefold() for token in (
                    "latitude", "longitude", "coordinates", "session", "visitor", "location",
                    "aircraft_position", "plane_position", "position", "private_ip",
                )):
                    continue
                result[name] = cls._safe_details(item, depth + 1)
            return result
        if isinstance(value, list):
            return [cls._safe_details(item, depth + 1) for item in value[:12]]
        if isinstance(value, str):
            return cls._text(value)
        if isinstance(value, (bool, int)) or value is None:
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return cls._text(value)

    def _observe(self, plane: dict[str, Any], route: dict[str, Any],
                 debug: dict[str, Any], now: float) -> None:
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        operating_date = stamp[:10]
        callsign = self._identifier(plane.get("flight"))
        registration = "".join(character for character in str(plane.get("r") or "").upper()
                               if character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")[:32]
        icao_hex = self._identifier(plane.get("hex"))
        if not (callsign or registration or icao_hex):
            return
        key = f"{operating_date}|{callsign}|{icao_hex or registration}"
        origin, destination = self._airport(route.get("origin")), self._airport(route.get("destination"))
        classification = ("missing_both" if not origin and not destination else
                          "missing_origin" if not origin else "missing_destination" if not destination else "")
        attempts = self._attempts(debug)
        diagnostics = self._attempts({"attempts": debug.get("diagnostic_attempts", [])})
        requested = [attempt for attempt in attempts if attempt.get("requested")]
        alternative = self._identifier(debug.get("alternative_callsign"))
        fallback = bool(alternative and any(attempt["callsign"] == alternative for attempt in requested))
        fallback_success = fallback and any(
            attempt["callsign"] == alternative and attempt.get("accepted") for attempt in requested
        )
        connection = self._connect()
        try:
            with connection:
                self._prune(connection, now)
                existing = connection.execute("SELECT * FROM flights WHERE flight_key = ?", (key,)).fetchone()
                if existing is None:
                    if not classification:
                        return
                    connection.execute("""
                        INSERT INTO flights (
                            flight_key, operating_date, callsign, registration, icao_hex,
                            first_failure_utc, last_seen_utc, last_seen_epoch, classification, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved')
                    """, (key, operating_date, callsign, registration, icao_hex,
                          stamp, stamp, now, classification))
                connection.execute("""
                    UPDATE flights SET registration = ?, icao_hex = ?, last_seen_utc = ?,
                        last_seen_epoch = ?, origin = ?, destination = ?, provider = ?,
                        status = ?, resolved_utc = ?, classification = CASE WHEN ? = ''
                            THEN classification ELSE ? END
                    WHERE flight_key = ?
                """, (registration, icao_hex, stamp, now, origin, destination,
                      self._text(route.get("route_source")), "unresolved" if classification else "resolved",
                      None if classification else (existing["resolved_utc"] or stamp),
                      classification, classification, key))
                lookup_id = self._identifier(debug.get("lookup_id"))
                if lookup_id and requested:
                    cursor = connection.execute("""
                        INSERT OR IGNORE INTO attempts VALUES (?, ?, ?, ?, ?)
                    """, (key, lookup_id, stamp, bool(classification), json.dumps({
                        "original_callsign": callsign, "alternative_callsign": alternative,
                        "fallback_attempted": fallback, "fallback_succeeded": fallback_success,
                        "resolution_elapsed_seconds": (
                            max(0.0, float(debug["elapsed_seconds"]))
                            if isinstance(debug.get("elapsed_seconds"), (int, float))
                            and math.isfinite(float(debug["elapsed_seconds"])) else None
                        ),
                        "origin": origin, "destination": destination,
                        "classification": classification, "decision": self._text(debug.get("decision")),
                        "retry_in_seconds": (
                            max(0, int(debug["retry_in_seconds"]))
                            if isinstance(debug.get("retry_in_seconds"), (int, float))
                            and math.isfinite(float(debug["retry_in_seconds"])) else None
                        ),
                        "retry_delay_seconds": (
                            max(0, int(debug["retry_delay_seconds"]))
                            if isinstance(debug.get("retry_delay_seconds"), (int, float))
                            and math.isfinite(float(debug["retry_delay_seconds"])) else None
                        ),
                        "attempts": attempts,
                        "diagnostic_attempts": diagnostics,
                    })))
                    if cursor.rowcount:
                        connection.execute("""
                            UPDATE flights SET failed_attempts = failed_attempts + ?,
                                provider_requests = provider_requests + ?,
                                fallback_attempts = fallback_attempts + ?,
                                fallback_successes = fallback_successes + ? WHERE flight_key = ?
                        """, (bool(classification), sum(attempt.get("request_count", 1) for attempt in requested),
                              fallback, fallback_success, key))
                        connection.execute("""
                            DELETE FROM attempts WHERE flight_key = ? AND lookup_id NOT IN (
                                SELECT lookup_id FROM attempts WHERE flight_key = ?
                                ORDER BY timestamp_utc DESC, rowid DESC LIMIT ?
                            )
                        """, (key, key, self.history_limit))
                self._prune(connection, now)
        finally:
            connection.close()

    def records(self, now: float | None = None) -> list[dict[str, Any]]:
        with self._lock:
            connection = self._connect()
            try:
                with connection:
                    self._prune(connection, time.time() if now is None else now)
                records = []
                for row in connection.execute("SELECT * FROM flights ORDER BY operating_date, callsign"):
                    record = dict(row)
                    record["history"] = [
                        {"timestamp_utc": attempt["timestamp_utc"], "failed": bool(attempt["failed"]),
                         **json.loads(attempt["details"]),
                         "diagnostic_attempts": json.loads(attempt["details"]).get("diagnostic_attempts", [])}
                        for attempt in connection.execute(
                            "SELECT * FROM attempts WHERE flight_key = ? ORDER BY timestamp_utc, rowid",
                            (row["flight_key"],),
                        )
                    ]
                    record["diagnostics"] = [
                        {"timestamp_utc": event["timestamp_utc"], "event": event["event"],
                         **json.loads(event["details"])}
                        for event in connection.execute(
                            "SELECT * FROM diagnostics WHERE flight_key=? ORDER BY timestamp_utc,rowid",
                            (row["flight_key"],),
                        )
                    ]
                    records.append(record)
                return records
            finally:
                connection.close()


def configured_log(config_path: Path) -> RouteFailureLog:
    raw = json.loads(config_path.read_text()) if config_path.is_file() else {}
    options = raw.get("route_failure_logging", {})
    if not isinstance(options, dict):
        raise ValueError("route_failure_logging must be an object")
    return RouteFailureLog(
        Path(options.get("path", str(DEFAULT_PATH))),
        int(options.get("retention_days", 30)), int(options.get("max_records", 10000)),
        int(options.get("history_limit", 50)),
    )


def report(records: list[dict[str, Any]]) -> str:
    unresolved = [record for record in records if record["status"] == "unresolved"]
    recovered = [record for record in records if record["status"] == "resolved"]
    fallback_attempts = sum(record["fallback_attempts"] for record in records)
    successes = sum(record["fallback_successes"] for record in records)
    lines = [
        f"Unresolved flights: {len(unresolved)}",
        f"Recovered flights: {len(recovered)}",
        f"Missing origin: {sum(not record['origin'] for record in unresolved)}",
        f"Missing destination: {sum(not record['destination'] for record in unresolved)}",
        f"Unresolved with provider attempts: {sum(record['failed_attempts'] > 0 for record in unresolved)}",
        f"Leading-zero fallback successes: {successes}/{fallback_attempts}"
        + (f" ({successes / fallback_attempts:.1%})" if fallback_attempts else " (no attempts)"),
    ]
    for record in records:
        lines.append(f"{record['operating_date']} {record['callsign']} {record['registration']} "
                     f"{record['icao_hex']} {record['status']} "
                     f"{record['origin'] or 'N/A'} -> {record['destination'] or 'N/A'} "
                     f"failures={record['failed_attempts']} provider={record['provider']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--csv", type=Path, help="Export records including bounded attempt history")
    args = parser.parse_args()
    try:
        records = configured_log(args.config).records()
        print(report(records))
        if args.csv:
            fields = ["operating_date", "callsign", "registration", "icao_hex", "first_failure_utc",
                      "last_seen_utc", "classification", "status", "resolved_utc", "origin",
                      "destination", "provider", "failed_attempts", "provider_requests",
                      "fallback_attempts", "fallback_successes", "history"]
            with args.csv.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fields, extrasaction="ignore")
                writer.writeheader()
                for record in records:
                    writer.writerow({**record, "history": json.dumps(record["history"])})
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        LOGGER.error("Route failure report unavailable (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
