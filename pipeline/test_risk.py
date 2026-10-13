"""Risk score checks. No camera and no model."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import deque
from pathlib import Path

import numpy as np

import risk


def _vlm(event: str, confidence: float, severity: str = "high") -> dict:
    return {
        "event": event,
        "confidence": confidence,
        "subjects": ["driver"],
        "description": event,
        "severity": severity,
    }


class AssessTest(unittest.TestCase):
    def test_fatigue_heads_share_one_bucket(self) -> None:
        result = risk.assess(
            [
                "drowsiness:Drowsy@0.90",
                "eye_state:Closed@0.90",
                "yawn:Yawning@0.80",
            ],
            None,
            False,
        )
        self.assertEqual(result["score"], 3)
        self.assertEqual(result["parts"], ["fatigue:3"])
        self.assertEqual(result["severity"], "medium")
        self.assertEqual(result["action"], "warning")

    def test_gaze_alone_stays_low(self) -> None:
        result = risk.assess(["gaze:Top-Left@0.70"], None, False)
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["action"], "none")

    def test_phone_activity_plus_vlm_is_high(self) -> None:
        alone = risk.assess([], _vlm("phone", 0.80), False)
        self.assertEqual(alone["parts"], ["vlm:phone:2"])
        result = risk.assess(
            ["activity:Phone Use@0.70"],
            _vlm("phone", 0.80, severity="low"),
            False,
        )
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["parts"], ["activity:3", "vlm:phone:3"])
        self.assertEqual(result["action"], "incident")

    def test_weak_or_empty_vlm_adds_nothing(self) -> None:
        drowsy = ["drowsiness:Drowsy@0.80"]
        quiet = risk.assess(drowsy, _vlm("none", 0.90), False)
        weak = risk.assess(drowsy, _vlm("phone", 0.40), False)
        self.assertEqual(quiet["score"], 3)
        self.assertEqual(quiet["parts"], ["fatigue:3"])
        self.assertEqual(weak["score"], 3)
        self.assertEqual(weak["parts"], ["fatigue:3"])

    def test_altercation_without_dms_is_high(self) -> None:
        result = risk.assess([], _vlm("altercation", 0.80), False)
        self.assertEqual(result["score"], 5)
        self.assertEqual(result["parts"], ["vlm:altercation:5"])
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["action"], "incident")

    def test_emotion_and_age_add_nothing(self) -> None:
        result = risk.assess(
            ["emotion:Fear@0.99", "age:Minor@0.99"],
            None,
            False,
        )
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["action"], "none")
        self.assertEqual(result["parts"], [])

    def test_sos_is_critical_when_models_are_quiet(self) -> None:
        result = risk.assess([], None, True)
        self.assertEqual(result["score"], 11)
        self.assertEqual(result["severity"], "critical")
        self.assertEqual(result["action"], "incident")
        self.assertEqual(result["parts"], ["sos"])
        self.assertEqual(risk.message(result), "critical 11  sos")


class SaveOnceTest(unittest.TestCase):
    def test_second_answer_rewrites_the_same_episode(self) -> None:
        import run_lvgl_demo as demo

        with tempfile.TemporaryDirectory() as tmp:
            demo.RESULTS = Path(tmp)
            app = demo.CabinDemo.__new__(demo.CabinDemo)
            app.episode = 0
            app.buffer = deque([(1.0, np.zeros((8, 8, 3), dtype=np.uint8))])
            app.incident_dir = None
            app.incident_video = None
            app.clip = "no"
            app.vlm_result = _vlm("altercation", 0.80)
            assessment = risk.assess([], app.vlm_result, False)
            app.commit_incident([], assessment, "fused")
            folder = app.incident_dir
            app.vlm_result = _vlm("altercation", 0.91)
            app.commit_incident([], risk.assess([], app.vlm_result, False), "fused")

            episodes = list(Path(tmp).joinpath("incidents").iterdir())
            self.assertEqual(len(episodes), 1)
            self.assertEqual(app.incident_dir, folder)
            payload = json.loads((folder / "events.json").read_text())
            self.assertEqual(payload["episode"], 1)
            self.assertEqual(payload["video"], "clip.mp4")
            self.assertGreater((folder / "clip.mp4").stat().st_size, 0)
            self.assertEqual(payload["vlm"]["confidence"], 0.91)
            self.assertEqual(payload["parts"], ["vlm:altercation:5"])
            self.assertNotIn("planned_not_loaded", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
