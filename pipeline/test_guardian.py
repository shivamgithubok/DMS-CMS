"""Guardian gate and MQTT publish. No camera and no VLM weights."""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request

import guardian
import risk


def _vlm(event: str, confidence: float) -> dict:
    return {"event": event, "confidence": confidence, "subjects": ["passenger"], "description": event, "severity": "low"}


class RouteTest(unittest.TestCase):
    def test_dummy_drive_covers_about_three_minutes(self) -> None:
        self.assertEqual(len(guardian.ROUTE), guardian.DEMO_SECONDS)
        self.assertEqual(guardian.ROUTE[0][:2], (28.6139, 77.209))
        self.assertEqual(guardian.ROUTE[-1][:2], (28.6205, 77.231))
        self.assertNotEqual(guardian.ROUTE[0][:2], guardian.ROUTE[30][:2])


class GateTest(unittest.TestCase):
    def test_button_sends_without_a_cabin_event(self) -> None:
        assessment = risk.assess([], None, True)
        self.assertTrue(guardian.should_notify(True, assessment, None))

    def test_high_score_without_vlm_stays_on_the_car(self) -> None:
        assessment = risk.assess(
            ["drowsiness:Drowsy@0.90", "distraction:Distracted@0.80"],
            _vlm("none", 0.90),
            False,
        )
        self.assertEqual(assessment["severity"], "high")
        self.assertFalse(guardian.should_notify(False, assessment, _vlm("none", 0.90)))
        self.assertFalse(guardian.vlm_confirms(_vlm("phone", 0.40)))

    def test_high_score_with_a_closed_event_sends(self) -> None:
        answer = _vlm("phone", 0.99)
        assessment = risk.assess(["activity:Phone Use@0.70"], answer, False)
        self.assertEqual(assessment["severity"], "high")
        self.assertTrue(guardian.should_notify(False, assessment, answer))

    def test_why_text_separates_the_button_the_wait_and_a_quiet_ride(self) -> None:
        self.assertEqual(guardian.explain(False, None, None, False), "No cabin look yet.")
        self.assertIn("SOS button", guardian.explain(True, {"severity": "high"}, None, True))
        waiting = risk.assess(
            ["drowsiness:Drowsy@0.90", "distraction:Distracted@0.80"],
            _vlm("none", 0.90),
            False,
        )
        self.assertIn("Waiting", guardian.explain(False, waiting, _vlm("none", 0.90), False))
        answer = _vlm("phone", 0.99)
        confirmed = risk.assess(["activity:Phone Use@0.70"], answer, False)
        self.assertIn("0.99", guardian.explain(False, confirmed, answer, True))


class PublishTest(unittest.TestCase):
    def test_ride_tracks_before_the_alert(self) -> None:
        bus = guardian.MemoryBus()
        trip = guardian.TripPublisher(bus, interval_s=0.05)
        link = trip.begin(0, 1)
        time.sleep(0.2)
        topics = [item["topic"] for item in bus.messages]
        self.assertTrue(link.endswith(trip.token or ""))
        self.assertFalse(any(topic.endswith("/sos") for topic in topics))
        self.assertTrue(any(topic.endswith("/state") for topic in topics))
        self.assertTrue(any(topic.endswith("/track") for topic in topics))
        state = next(item["payload"] for item in bus.messages if item["topic"].endswith("/state"))
        self.assertEqual(state["mode"], "ride")
        self.assertEqual(state["rear"], 1)
        self.assertNotIn("button", state)
        self.assertEqual(state["alert_reason"], "No cabin look yet.")
        self.assertIsNone(state["vlm_record"])
        token = trip.token
        assessment = risk.assess([], _vlm("altercation", 0.80), False)
        trip.alert(assessment, _vlm("altercation", 0.80), 0, 1, "No", "No", False)
        time.sleep(0.2)
        trip.stop()
        self.assertEqual(trip.token, token)
        topics = [item["topic"] for item in bus.messages]
        self.assertTrue(any(topic.endswith("/sos") for topic in topics))
        alerted = [item["payload"] for item in bus.messages if item["topic"].endswith("/state") and item["payload"]["mode"] == "incident"]
        self.assertTrue(alerted)
        self.assertEqual(alerted[-1]["vlm_record"]["event"], "altercation")
        self.assertIn("0.80", alerted[-1]["alert_reason"])
        self.assertEqual(alerted[-1]["driver"]["eyes"], "--")
        track = next(item["payload"] for item in bus.messages if item["topic"].endswith("/track"))
        self.assertEqual(track["fix"], "dummy")


class BrokerTest(unittest.TestCase):
    def test_local_broker_delivers_a_track_point(self) -> None:
        import asyncio

        from amqtt.broker import Broker
        import paho.mqtt.client as mqtt

        loop = asyncio.new_event_loop()
        broker = Broker(
            {
                "listeners": {"default": {"type": "tcp", "bind": "127.0.0.1:1884"}},
                "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
            },
            loop=loop,
        )
        loop.run_until_complete(broker.start())
        threading.Thread(target=loop.run_forever, daemon=True).start()
        got: list[str] = []

        def on_message(_client, _userdata, message) -> None:
            got.append(message.payload.decode())

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_message = on_message
        client.connect("127.0.0.1", 1884, keepalive=20)
        client.subscribe("cms/CAB-18/trips/+/track")
        client.loop_start()
        time.sleep(0.2)
        bus = guardian.MqttBus("127.0.0.1", 1884)
        trip = guardian.TripPublisher(bus, interval_s=0.05)
        trip.begin(1, 0)
        deadline = time.time() + 3
        while time.time() < deadline and not got:
            time.sleep(0.05)
        trip.stop()
        client.loop_stop()
        client.disconnect()
        loop.call_soon_threadsafe(loop.stop)
        self.assertTrue(got)
        payload = json.loads(got[0])
        self.assertEqual(payload["fix"], "dummy")


class PageTest(unittest.TestCase):
    def test_page_shows_the_latest_mqtt_state(self) -> None:
        import tempfile
        from pathlib import Path

        import guardian_page
        import journey

        tmp = tempfile.TemporaryDirectory()
        store = journey.JourneyStore(Path(tmp.name) / "journey.sqlite")
        page = guardian_page.serve_in_thread(port=8091, mqtt_port=1885, store=store)
        try:
            bus = guardian.MqttBus("127.0.0.1", 1885)
            trip = guardian.TripPublisher(bus, interval_s=0.05, store=store)
            trip.begin(0, 1)
            deadline = time.time() + 4
            body = {}
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:8091/t/{trip.token}/live", timeout=1) as response:
                        body = json.loads(response.read().decode())
                except Exception:
                    body = {}
                if body.get("state") and body.get("track") and not body.get("sos"):
                    break
                time.sleep(0.1)
            self.assertEqual(body["state"]["mode"], "ride")
            self.assertIsNone(body.get("sos"))
            self.assertEqual(body["state"]["rear"], 1)
            self.assertEqual(body["track"]["fix"], "dummy")
            with urllib.request.urlopen(f"http://127.0.0.1:8091/t/{trip.token}/history", timeout=1) as response:
                history = json.loads(response.read().decode())
            self.assertGreaterEqual(len(history), 1)
            self.assertEqual(history[0]["color"], "green")
            self.assertEqual(history[0]["fix"], "dummy")
            trip.alert(
                risk.assess(["activity:Phone Use@0.70"], _vlm("phone", 0.99), False),
                _vlm("phone", 0.99),
                0,
                1,
                "Yes",
                "--",
                False,
            )
            deadline = time.time() + 4
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:8091/t/{trip.token}/live", timeout=1) as response:
                        body = json.loads(response.read().decode())
                except Exception:
                    body = {}
                if body.get("sos") and body.get("state", {}).get("phone") == "Yes":
                    break
                time.sleep(0.1)
            trip.stop()
            self.assertEqual(body["state"]["phone"], "Yes")
            self.assertEqual(body["state"]["mode"], "incident")
            self.assertEqual(body["sos"]["reason"], "vlm_confirm")
            self.assertEqual(body["state"]["vlm_record"]["event"], "phone")
            self.assertEqual(body["state"]["vlm_record"]["description"], "phone")
            self.assertIn("0.99", body["state"]["alert_reason"])
            with urllib.request.urlopen(f"http://127.0.0.1:8091/t/{trip.token}/history", timeout=1) as response:
                history = json.loads(response.read().decode())
            self.assertIn("red", [item["color"] for item in history])
            with urllib.request.urlopen(f"http://127.0.0.1:8091/t/{trip.token}", timeout=1) as response:
                html = response.read().decode()
                self.assertIn("DUMMY DATA", html)
            self.assertIn("Ride live", html)
            self.assertIn("Safety", html)
            self.assertIn("leaflet.js", html)
            self.assertIn("Passenger", html)
            self.assertIn("Show passenger", html)
            self.assertIn(">Why<", html)
            self.assertIn("Diagnosis", html)
            self.assertIn("No cabin look yet.", html)
            self.assertIn("/static/passenger/", html)
            self.assertIn("/static/driver_face.jpg", html)
            self.assertIn('class="driver"', html)
            with urllib.request.urlopen("http://127.0.0.1:8091/static/leaflet/leaflet.js", timeout=1) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(b"Leaflet", response.read())
            with urllib.request.urlopen("http://127.0.0.1:8091/static/passenger/00.jpg", timeout=1) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().startswith(b"\xff\xd8"))
            with urllib.request.urlopen("http://127.0.0.1:8091/static/driver_face.jpg", timeout=1) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue(response.read().startswith(b"\xff\xd8"))
        finally:
            page.stop()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
