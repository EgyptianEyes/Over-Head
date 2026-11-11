"""Private, bounded route-failure history and offline reporting."""

import argparse
import csv
import json
import logging
import math
import sqlite3
import threading
import time
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
            attempt["callsign"] = cls._identifier(candidate.get("callsign"))
            status = candidate.get("http_status")
            if isinstance(status, int):
                attempt["http_status"] = status
            count = candidate.get("request_count")
            if isinstance(count, int) and count >= 0:
                attempt["request_count"] = count
            attempt["requests"] = [
                {"callsign": cls._identifier(request.get("callsign")),
                 "http_status": request.get("http_status") if isinstance(request.get("http_status"), int) else None}
                for request in candidate.get("requests", [])[:16] if isinstance(request, dict)
            ] if isinstance(candidate.get("requests"), list) else []
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
                        "attempts": attempts,
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
                         **json.loads(attempt["details"])}
                        for attempt in connection.execute(
                            "SELECT * FROM attempts WHERE flight_key = ? ORDER BY timestamp_utc, rowid",
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
