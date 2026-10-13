"""Journey trail stored on the device.

One row is one moment: position, severity, and the cabin card. The guardian
page draws the path from these rows and MQTT only moves the newest point.
Photos stay files. The path is what gets stored.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "results" / "journey.sqlite"


def trail_color(severity: str, mode: str = "") -> str:
    if mode in {"sos", "incident"}:
        return "red"
    if severity in {"medium", "high", "critical"}:
        return "amber"
    return "green"


class JourneyStore:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS points (
                    id INTEGER PRIMARY KEY,
                    trip_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    lat REAL,
                    lon REAL,
                    speed_kph REAL,
                    heading_deg REAL,
                    fix TEXT,
                    severity TEXT,
                    mode TEXT,
                    front INTEGER,
                    rear INTEGER,
                    phone TEXT,
                    reaching TEXT,
                    photo_path TEXT
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        return db

    def add(self, row: dict) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO points (
                    trip_id, ts, lat, lon, speed_kph, heading_deg, fix,
                    severity, mode, front, rear, phone, reaching, photo_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["trip_id"],
                    row["ts"],
                    row.get("lat"),
                    row.get("lon"),
                    row.get("speed_kph"),
                    row.get("heading_deg"),
                    row.get("fix"),
                    row.get("severity"),
                    row.get("mode"),
                    row.get("front"),
                    row.get("rear"),
                    row.get("phone"),
                    row.get("reaching"),
                    row.get("photo_path"),
                ),
            )
            return int(cursor.lastrowid)

    def history(self, trip_id: str) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM points WHERE trip_id = ? ORDER BY id",
                (trip_id,),
            ).fetchall()
        points = []
        for row in rows:
            item = dict(row)
            item["color"] = trail_color(str(item.get("severity") or ""), str(item.get("mode") or ""))
            points.append(item)
        return points

    def get(self, trip_id: str, point_id: int) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM points WHERE trip_id = ? AND id = ?",
                (trip_id, point_id),
            ).fetchone()
        return dict(row) if row else None
