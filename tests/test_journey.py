"""Journey trail colors and SQLite history. No camera."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from guardian import journey


class ColorTest(unittest.TestCase):
    def test_safe_warning_and_sos_use_three_colors(self) -> None:
        self.assertEqual(journey.trail_color("none"), "green")
        self.assertEqual(journey.trail_color("low"), "green")
        self.assertEqual(journey.trail_color("medium"), "amber")
        self.assertEqual(journey.trail_color("high"), "amber")
        self.assertEqual(journey.trail_color("low", "sos"), "red")
        self.assertEqual(journey.trail_color("low", "incident"), "red")


class StoreTest(unittest.TestCase):
    def test_history_keeps_each_moment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = journey.JourneyStore(Path(tmp) / "journey.sqlite")
            store.add(_point("t1", "low", 28.614, 77.210))
            store.add(_point("t1", "medium", 28.617, 77.216))
            store.add(_point("t1", "high", 28.620, 77.224, "sos"))
            history = store.history("t1")
            self.assertEqual([item["color"] for item in history], ["green", "amber", "red"])
            self.assertEqual(history[1]["phone"], "No")
            self.assertNotIn("emotion", history[0])
            self.assertEqual(store.get("t1", history[2]["id"])["severity"], "high")


def _point(trip: str, severity: str, lat: float, lon: float, mode: str = "ride") -> dict:
    return {
        "trip_id": trip,
        "ts": "2026-10-12T11:22:00+05:30",
        "lat": lat,
        "lon": lon,
        "speed_kph": 20,
        "heading_deg": 40,
        "fix": "dummy",
        "severity": severity,
        "mode": mode,
        "front": 0,
        "rear": 1,
        "phone": "No",
        "reaching": "--",
        "photo_path": None,
    }


if __name__ == "__main__":
    unittest.main()
