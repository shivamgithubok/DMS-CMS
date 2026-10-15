"""Guardian ride path.

The ride starts when a passenger is in the cabin. The phone page follows that
trip. The SOS button, or a high score confirmed by a closed VLM event at
confidence 0.60 or higher, raises a red alert on the same page. The position
is a dummy route of about three minutes.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import journey

CONFIRM_EVENTS = {"phone", "reaching", "lying_down", "altercation", "unusual_object"}
MIN_CONFIDENCE = 0.60
VEHICLE_ID = "CAB-18"
DRIVER_ID = "DRV-1042"
PASSENGER_ID = "PAX-220"

# Dummy drive through central Delhi. One point is one second, so the
# published track lasts about three minutes, then the car stays at the end.
DEMO_SECONDS = 180
WAYPOINTS = [
    (28.6139, 77.2090, 22),
    (28.6160, 77.2120, 28),
    (28.6185, 77.2165, 32),
    (28.6200, 77.2210, 36),
    (28.6210, 77.2260, 30),
    (28.6205, 77.2310, 24),
]


def _meters(start: tuple, end: tuple) -> float:
    lat1 = math.radians(start[0])
    lat2 = math.radians(end[0])
    east = math.radians(end[1] - start[1]) * math.cos((lat1 + lat2) / 2)
    north = lat2 - lat1
    return 6371000 * math.hypot(east, north)


def _bearing(start: tuple, end: tuple) -> float:
    lat1, lon1 = math.radians(start[0]), math.radians(start[1])
    lat2, lon2 = math.radians(end[0]), math.radians(end[1])
    dlon = lon2 - lon1
    east = math.sin(dlon) * math.cos(lat2)
    north = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(east, north)) + 360) % 360


def build_route(seconds: int = DEMO_SECONDS) -> list[tuple]:
    """Lat, lon, speed, heading. fix stays dummy on every published point."""
    steps = max(2, seconds)
    lengths = [_meters(WAYPOINTS[i], WAYPOINTS[i + 1]) for i in range(len(WAYPOINTS) - 1)]
    total = sum(lengths) or 1.0
    points = []
    for index in range(steps):
        target = total * (index / (steps - 1))
        walked = 0.0
        for seg, length in enumerate(lengths):
            if walked + length < target and seg < len(lengths) - 1:
                walked += length
                continue
            span = length or 1.0
            blend = min(1.0, max(0.0, (target - walked) / span))
            start = WAYPOINTS[seg]
            end = WAYPOINTS[seg + 1]
            lat = start[0] + (end[0] - start[0]) * blend
            lon = start[1] + (end[1] - start[1]) * blend
            speed = start[2] + (end[2] - start[2]) * blend
            points.append((round(lat, 6), round(lon, 6), int(round(speed)), int(round(_bearing(start, end)))))
            break
    return points


ROUTE = build_route()


def vlm_confirms(vlm: dict | None) -> bool:
    if not isinstance(vlm, dict):
        return False
    event = str(vlm.get("event") or "")
    if event not in CONFIRM_EVENTS:
        return False
    try:
        confidence = float(vlm.get("confidence"))
    except (TypeError, ValueError):
        return False
    if confidence != confidence:
        return False
    return confidence >= MIN_CONFIDENCE


def should_notify(button: bool, assessment: dict, vlm: dict | None) -> bool:
    """True when the open ride should show a red alert."""
    if button:
        return True
    if assessment.get("severity") != "high":
        return False
    return vlm_confirms(vlm)


def explain(button: bool, assessment: dict | None, vlm: dict | None, alerted: bool) -> str:
    """One sentence for the guardian Why card."""
    if alerted and button:
        return "SOS button. The page turns red immediately."
    if alerted and vlm_confirms(vlm):
        event = str(vlm.get("event") or "event")
        confidence = _confidence(vlm.get("confidence"))
        if confidence is None:
            return f"Cabin event {event}. High score confirmed."
        return f"Cabin event {event} at {confidence:.2f}. High score confirmed."
    if alerted:
        return "Alert is on for this ride."
    if isinstance(assessment, dict) and (
        assessment.get("severity") == "high" or assessment.get("action") == "incident"
    ):
        return "High score on the car. Waiting for a closed cabin event at 0.60 or higher."
    if not isinstance(vlm, dict):
        return "No cabin look yet."
    if str(vlm.get("event") or "none") == "none":
        return "Ride live. The last cabin look did not name an event."
    return "Ride live. The cabin look is not confirmed as an alert."


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class MemoryBus:
    """Records publishes for tests."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def publish(self, topic: str, payload: dict, qos: int, retain: bool) -> bool:
        self.messages.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return True


class MqttBus:
    """Publish to a local broker. A failed connection is reported and skipped."""

    def __init__(self, host: str = "127.0.0.1", port: int = 1883) -> None:
        self.host = host
        self.port = port
        self._client = None
        self._lock = threading.Lock()

    def publish(self, topic: str, payload: dict, qos: int, retain: bool) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            return False
        with self._lock:
            if self._client is None:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
                client.connect(self.host, self.port, keepalive=20)
                client.loop_start()
                self._client = client
            result = self._client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        return result.rc == mqtt.MQTT_ERR_SUCCESS


class TripPublisher:
    def __init__(self, bus=None, interval_s: float = 1.0, store: journey.JourneyStore | None = None) -> None:
        self.bus = bus if bus is not None else MqttBus()
        self.interval_s = interval_s
        self.store = store
        self.status = "idle"
        self.link: str | None = None
        self.token: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._state: dict | None = None
        self._version = 0
        self._sent_version = -1
        self._sos: dict | None = None
        self._sos_version = 0
        self._sent_sos = -1
        self._point = 0
        self._front = 0
        self._rear = 0
        self._phone = "--"
        self._reaching = "--"
        self._assessment: dict | None = None
        self._vlm: dict | None = None
        self._driver = _driver_view(None)
        self._button = False
        self._mode = "ride"

    def hold(self) -> None:
        if self.status in {"idle", "live"}:
            self.status = "held"

    def begin(self, front: int = 0, rear: int = 0) -> str | None:
        """Passenger boarded. One random token covers the whole ride."""
        minted = False
        with self._lock:
            if self.token is None:
                self.token = secrets.token_hex(4)
                self.link = f"http://127.0.0.1:8080/t/{self.token}"
                self.status = "live"
                self._front = front
                self._rear = rear
                self._mode = "ride"
                self._button = False
                self._rebuild()
                minted = True
        if minted:
            print(f"[GUARDIAN] ride {self.link}", flush=True)
        self._ensure_thread()
        return self.link

    def update(
        self,
        front: int,
        rear: int,
        phone: str = "--",
        reaching: str = "--",
        assessment: dict | None = None,
        vlm: dict | None = None,
    ) -> None:
        """Refresh the cabin card. An open alert stays red."""
        if self.token is None:
            return
        with self._lock:
            self._front = front
            self._rear = rear
            self._phone = phone
            self._reaching = reaching
            self._assessment = assessment
            self._vlm = vlm if isinstance(vlm, dict) else None
            self._rebuild()

    def note_driver(self, front: int, rear: int, driver: dict) -> None:
        """Latest driver reading. Keeps the cabin answer already stored."""
        if self.token is None:
            return
        with self._lock:
            self._front = front
            self._rear = rear
            self._driver = _driver_view(driver)
            self._rebuild()

    def alert(
        self,
        assessment: dict,
        vlm: dict | None,
        front: int,
        rear: int,
        phone: str,
        reaching: str,
        button: bool,
    ) -> str | None:
        """Raise SOS on the current ride. Starts the ride first if needed."""
        if self.token is None:
            self.begin(front, rear)
        mode = "sos" if button else "incident"
        with self._lock:
            self.status = "sent"
            self._front = front
            self._rear = rear
            self._phone = phone
            self._reaching = reaching
            self._assessment = assessment
            self._vlm = vlm if isinstance(vlm, dict) else None
            self._button = button
            self._mode = mode
            self._rebuild()
            self._sos = {
                "trip_id": self.token,
                "vehicle_id": VEHICLE_ID,
                "opened_at": _stamp(),
                "reason": "manual_sos" if button else "vlm_confirm",
                "severity": self._state.get("severity", "high"),
                "link": self.link,
            }
            self._sos_version += 1
        return self.link

    def _rebuild(self) -> None:
        payload = _state_payload(
            self.token,
            self._assessment,
            self._vlm,
            self._front,
            self._rear,
            self._phone,
            self._reaching,
            self._button,
            self._mode,
            self._driver,
        )
        if _card(payload) != _card(self._state):
            self._state = payload
            self._version += 1

    def end(self) -> None:
        """Passenger left. Stop publishing. The page keeps the stored trail."""
        self.stop()

    def _ensure_thread(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="guardian", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        try:
            self._publish_loop()
        except Exception:
            if self.status in {"sent", "live"} and self._sent_version < 0:
                self.status = "held"
                self.link = None

    def _publish_loop(self) -> None:
        token = self.token or ""
        while not self._stop.is_set():
            with self._lock:
                state = dict(self._state) if self._state else None
                send_state = state is not None and self._version != self._sent_version
                if send_state:
                    self._sent_version = self._version
                sos = dict(self._sos) if self._sos and self._sos_version != self._sent_sos else None
                if sos is not None:
                    self._sent_sos = self._sos_version
                point = ROUTE[min(self._point, len(ROUTE) - 1)]
                store_point = self._point < len(ROUTE)
                if store_point:
                    self._point += 1
            if sos is not None:
                self.bus.publish(f"cms/{VEHICLE_ID}/trips/{token}/sos", sos, 1, True)
            if send_state and state is not None:
                state.pop("button", None)
                self.bus.publish(f"cms/{VEHICLE_ID}/trips/{token}/state", state, 1, True)
            lat, lon, speed, heading = point
            track = {
                "trip_id": token,
                "ts": _stamp(),
                "lat": lat,
                "lon": lon,
                "speed_kph": speed,
                "heading_deg": heading,
                "fix": "dummy",
            }
            self.bus.publish(f"cms/{VEHICLE_ID}/trips/{token}/track", track, 0, False)
            if self.store is not None and state is not None and store_point:
                self.store.add(
                    {
                        "trip_id": token,
                        "ts": track["ts"],
                        "lat": lat,
                        "lon": lon,
                        "speed_kph": speed,
                        "heading_deg": heading,
                        "fix": "dummy",
                        "severity": state.get("severity"),
                        "mode": state.get("mode"),
                        "front": state.get("front"),
                        "rear": state.get("rear"),
                        "phone": state.get("phone"),
                        "reaching": state.get("reaching"),
                        "photo_path": state.get("photo_path"),
                    }
                )
            self._stop.wait(self.interval_s)


def _card(payload: dict | None) -> dict:
    if not payload:
        return {}
    return {key: value for key, value in payload.items() if key != "ts"}


def _confidence(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number:
        return None
    return number


def _driver_view(driver: dict | None) -> dict:
    src = driver or {}
    return {
        "eyes": str(src.get("eyes") or "--"),
        "gaze": str(src.get("gaze") or "--"),
        "yawn": str(src.get("yawn") or "--"),
        "focus": str(src.get("focus") or "--"),
    }


def _vlm_view(vlm: dict | None) -> dict | None:
    if not isinstance(vlm, dict):
        return None
    subjects = vlm.get("subjects")
    if not isinstance(subjects, list):
        subjects = []
    return {
        "event": str(vlm.get("event") or "none"),
        "confidence": _confidence(vlm.get("confidence")),
        "subjects": [str(item) for item in subjects],
        "description": str(vlm.get("description") or ""),
        "severity": str(vlm.get("severity") or ""),
    }


def _state_payload(token, assessment, vlm, front, rear, phone, reaching, button, mode: str, driver: dict | None) -> dict:
    record = _vlm_view(vlm)
    event = record["event"] if record else ""
    confidence = record["confidence"] if record else None
    vlm_line = event or "--"
    if event == "none":
        vlm_line = "--"
    elif isinstance(confidence, float):
        vlm_line = f"{event} {confidence:.2f}"
    alerted = mode in {"sos", "incident"}
    return {
        "trip_id": token,
        "ts": _stamp(),
        "mode": mode,
        "severity": (assessment or {}).get("severity", "none"),
        "front": front,
        "rear": rear,
        "phone": phone,
        "reaching": reaching,
        "vlm": vlm_line,
        "vlm_record": record,
        "driver": _driver_view(driver),
        "alert_reason": explain(button, assessment, vlm, alerted),
        "driver_id": DRIVER_ID,
        "passenger_id": PASSENGER_ID,
        "vehicle_id": VEHICLE_ID,
        "button": button,
    }


def write_outbox(path: Path, topic: str, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"topic": topic, "payload": payload}) + "\n")
