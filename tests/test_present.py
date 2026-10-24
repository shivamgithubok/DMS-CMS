"""Screen-state checks. No camera and no model."""

from __future__ import annotations

import unittest

from pipeline.present import STATE_FIELDS, Display, state_line


FACE = {"bbox": [0, 0, 10, 10]}


def monitoring(**over) -> dict:
    fields = {
        "mode": "monitoring",
        "camera": "ok",
        "mic": "down",
        "gps": "no_fix",
        "dms": "ok",
        "occupancy": 2,
        "severity": "none",
        "alert": "",
        "vlm": "idle",
        "clip": "no",
        "message": "Alert    eyes Open",
        "fps": 24.0,
        "cms_front": "1",
        "cms_rear": "1",
        "cms_phone": "--",
        "cms_reach": "--",
        "dms_eyes": "Open",
        "dms_gaze": "Middle-Right",
        "dms_yawn": "No Yawn",
        "vlm_text": "",
    }
    fields.update(over)
    return fields


def calm_preds() -> dict:
    return {
        "drowsiness": {"label": "Alert"},
        "gaze": {"label": "Middle-Right"},
        "yawn": {"label": "No Yawn"},
        "eye_state": {"label": "Open"},
        "distraction": {"label": "Safe"},
        "activity": {"label": "None"},
    }


def show(display: Display, fields: dict, preds=None, face=FACE, link="idle", face_known=True) -> dict:
    display.apply(fields, preds if preds is not None else calm_preds(), face, link, "12", face_known=face_known)
    return fields


class DisplayTest(unittest.TestCase):
    def test_monitoring_is_attentive_and_ignores_optional_sensors(self) -> None:
        fields = show(Display(), monitoring())
        self.assertEqual(fields["safety"], "SAFE")
        self.assertEqual(fields["headline"], "ATTENTIVE")
        self.assertEqual(fields["driver"], "In frame")
        self.assertEqual(fields["dms_eyes"], "OPEN")
        self.assertEqual(fields["dms_gaze"], "SIDE")
        self.assertEqual(fields["drowsy"], "LOW")
        self.assertEqual(fields["dms_yawn"], "NONE")
        self.assertEqual(fields["activity"], "NONE")
        self.assertEqual(fields["system"], "MONITORING")
        self.assertEqual(fields["alert_title"], "")
        self.assertEqual(fields["link"], "STANDBY")

    def test_warning_is_immediate_and_states_the_reason(self) -> None:
        display = Display()
        show(display, monitoring())
        fields = show(
            display,
            monitoring(mode="warning", severity="medium", alert="eye_state:Closed@0.91"),
        )
        self.assertEqual(fields["safety"], "WARNING")
        self.assertEqual(fields["headline"], "DROWSY")
        self.assertEqual(fields["alert_title"], "DRIVER DROWSINESS")
        self.assertEqual(fields["alert_detail"], "Prolonged eye closure detected")
        self.assertEqual(fields["alert_action"], "Take a break when safe")
        self.assertEqual(fields["dms_eyes"], "CLOSED")

    def test_a_single_blink_does_not_change_the_reading(self) -> None:
        display = Display()
        show(display, monitoring())
        preds = calm_preds()
        preds["eye_state"]["label"] = "Closed"
        fields = show(display, monitoring(), preds)
        self.assertEqual(fields["dms_eyes"], "OPEN")
        self.assertEqual(fields["safety"], "SAFE")
        self.assertEqual(fields["headline"], "ATTENTIVE")

    def test_missing_face_waits_then_asks_the_driver_to_sit_in_frame(self) -> None:
        display = Display()
        last = None
        for index in range(6):
            last = show(display, monitoring(), face=None)
            if index < 5:
                self.assertEqual(last["driver"], "Looking for the driver")
                self.assertEqual(last["safety"], "SAFE")
                self.assertEqual(last["dms_eyes"], "--")
        self.assertIsNotNone(last)
        self.assertEqual(last["driver"], "Not in frame")
        self.assertEqual(last["safety"], "ATTENTION")
        self.assertEqual(last["headline"], "NOT DETECTED")
        self.assertEqual(last["alert_title"], "DRIVER NOT DETECTED")

    def test_face_returning_clears_the_missing_driver_notice(self) -> None:
        display = Display()
        for _ in range(6):
            show(display, monitoring(), face=None)
        fields = show(display, monitoring())
        self.assertEqual(fields["safety"], "SAFE")
        self.assertEqual(fields["headline"], "ATTENTIVE")
        self.assertEqual(fields["driver"], "In frame")

    def test_recovery_from_a_warning_waits(self) -> None:
        display = Display()
        show(display, monitoring())
        show(display, monitoring(mode="warning", severity="medium", alert="drowsiness:Drowsy@0.90"))
        for _ in range(5):
            fields = show(display, monitoring())
            self.assertEqual(fields["safety"], "WARNING")
        fields = show(display, monitoring())
        self.assertEqual(fields["safety"], "SAFE")

    def test_sos_dominates_a_dead_camera(self) -> None:
        fields = show(
            Display(),
            monitoring(mode="sos", severity="critical", alert="sos", camera="down"),
        )
        self.assertEqual(fields["safety"], "CRITICAL")
        self.assertEqual(fields["headline"], "EMERGENCY")
        self.assertEqual(fields["alert_title"], "EMERGENCY")
        self.assertEqual(fields["system"], "FAULT")

    def test_camera_loss_is_a_fault_not_a_safe_cabin(self) -> None:
        fields = show(Display(), monitoring(camera="down"))
        self.assertEqual(fields["safety"], "FAULT")
        self.assertEqual(fields["system"], "FAULT")
        self.assertEqual(fields["headline"], "NO CAMERA")
        self.assertEqual(fields["cabin_status"], "OFFLINE")
        self.assertEqual(fields["alert_action"], "Monitoring is paused")
        self.assertEqual(fields["dms_eyes"], "--")
        self.assertEqual(fields["occupancy"], "--")
        self.assertEqual(fields["cms_phone"], "--")

    def test_driver_monitor_fault_keeps_the_last_cabin_count(self) -> None:
        fields = show(Display(), monitoring(dms="down"))
        self.assertEqual(fields["safety"], "FAULT")
        self.assertEqual(fields["headline"], "MONITOR FAULT")
        self.assertEqual(fields["dms_eyes"], "--")
        self.assertEqual(fields["occupancy"], 2)
        self.assertEqual(fields["cms_front"], "1")

    def test_startup_is_informational(self) -> None:
        fields = show(
            Display(),
            monitoring(mode="self_test", vlm="idle"),
            preds={},
            face=None,
            face_known=False,
        )
        self.assertEqual(fields["safety"], "INFO")
        self.assertEqual(fields["headline"], "STARTING")
        self.assertEqual(fields["system"], "READY")
        self.assertEqual(fields["model"], "LOADING")
        self.assertEqual(fields["cabin_status"], "STANDBY")

    def test_phone_event_is_a_cabin_status(self) -> None:
        fields = show(
            Display(),
            monitoring(cms_phone="Yes", vlm="phone 0.80 high"),
        )
        self.assertEqual(fields["cabin_status"], "PHONE")
        self.assertEqual(fields["cms_phone"], "IN USE")

    def test_failed_recording_degrades_the_system_without_raising_a_false_alarm(self) -> None:
        fields = show(Display(), monitoring(clip="fail"))
        self.assertEqual(fields["safety"], "SAFE")
        self.assertEqual(fields["system"], "DEGRADED")
        self.assertEqual(fields["record"], "FAILED")

    def test_packet_keeps_one_column_per_field(self) -> None:
        fields = show(Display(), monitoring(message="a|b\nc", fps=12.5))
        line = state_line(fields)
        parts = line.split("|")
        self.assertEqual(len(parts), len(STATE_FIELDS))
        self.assertEqual(parts[STATE_FIELDS.index("safety")], "SAFE")
        self.assertEqual(parts[STATE_FIELDS.index("fps")], "12.5")
        self.assertEqual(parts[STATE_FIELDS.index("message")], "a/b c")
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
