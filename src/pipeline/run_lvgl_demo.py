"""Drive the LVGL cabin screen from the DMS TensorRT engine.

Camera frames come from a video file. Front and rear seat counts come from
the cabin frame split into two bands.
Voice and GPS are reported as unavailable. With --vlm, a gate hands one cabin
still to Qwen and the driver loop keeps running. The answer lands on a later
frame. The risk score uses the DMS hold and that closed event. A high score
or SOS writes the rolling buffer out as one short cabin video. The guardian
ride starts when a passenger is seated. The SOS button, or a high score
confirmed by a closed VLM event, paints a red alert on that same page.
Demo files rewind at the end so the ride keeps moving. The map is one
dummy point per second for three minutes, then the car holds.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import cv2
import numpy as np

import dms
import guardian
from cms import cabin, cabin_record, occupancy, vlm_client
from guardian import journey
from pipeline import risk

RESULTS = ROOT / "results"
PORT = 9750
PREVIEW = (640, 360)
CLIP_NAME = "clip.mp4"
# Empty cabin frames in a row before the ride publisher stops.
EMPTY_RIDE_FRAMES = 8
# About four seconds when the sampled loop runs near 12 fps.
CLIP_FRAMES = 48


def write_clip(path: Path, frames: list[np.ndarray], fps: float) -> bool:
    """Encode cabin frames as one mp4. Returns False when the writer cannot open."""
    if not frames:
        return False
    first = np.ascontiguousarray(frames[0])
    height, width = first.shape[:2]
    width -= width % 2
    height -= height % 2
    if width < 2 or height < 2:
        return False
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        return False
    for frame in frames:
        picture = np.ascontiguousarray(frame)
        if picture.shape[1] != width or picture.shape[0] != height:
            picture = cv2.resize(picture, (width, height))
        writer.write(picture)
    writer.release()
    return path.is_file() and path.stat().st_size > 0


def clip_rate(stamps: list[float]) -> float:
    if len(stamps) < 2:
        return 8.0
    span = stamps[-1] - stamps[0]
    if span <= 0.05:
        return 8.0
    return min(15.0, max(4.0, (len(stamps) - 1) / span))


def screen_mode(assessment: dict, sos: bool) -> str:
    if sos or assessment["severity"] == "critical":
        return "sos"
    if assessment["action"] == "incident":
        return "incident"
    if assessment["action"] == "warning":
        return "warning"
    return "monitoring"


def alert_name(reasons: list[str], assessment: dict) -> str:
    if assessment["severity"] == "critical":
        return "sos"
    if reasons:
        return reasons[0].split("@", 1)[0]
    for part in assessment["parts"]:
        if part.startswith("vlm:"):
            return part.split(":")[1]
    return ""


def record_kind(assessment: dict) -> str:
    if "sos" in assessment["parts"]:
        return "sos"
    if any(part.startswith("vlm:") for part in assessment["parts"]):
        return "fused"
    return "dms"


class StateServer:
    def __init__(self) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", PORT))
        self.server.listen(1)
        self.server.setblocking(False)
        self.client = None

    def send(self, line: str) -> None:
        if self.client is None:
            try:
                self.client, _ = self.server.accept()
                self.client.setblocking(False)
            except BlockingIOError:
                return
        try:
            self.client.sendall((line + "\n").encode())
        except (BrokenPipeError, ConnectionResetError, BlockingIOError, OSError):
            self.close_client()

    def close_client(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def close(self) -> None:
        self.close_client()
        self.server.close()


def open_dashboard() -> None:
    already = subprocess.run(["pgrep", "-x", "cms_ui"], capture_output=True)
    if already.returncode == 0:
        print("[PIPE] dashboard already open")
        return
    env = os.environ.copy()
    env.setdefault("DISPLAY", ":1")
    binary = ROOT / "src" / "ui" / "cms_ui"
    print("[PIPE] opening dashboard")
    subprocess.Popen(
        [str(binary)],
        cwd=str(ROOT),
        env=env,
        start_new_session=True,
    )


def pop_command() -> str:
    path = RESULTS / "ui_cmd.txt"
    if not path.exists():
        return ""
    text = path.read_text().strip()
    path.write_text("")
    return text


PASSPORT = (96, 132)


def _short_gaze(label: str) -> str:
    if label in {"Middle-Left", "Middle-Right"}:
        return "Side"
    if label:
        return "Away"
    return "--"


def attach_view(fields: dict, preds: dict | None) -> dict:
    if preds:
        fields["dms_eyes"] = preds["eye_state"]["label"]
        fields["dms_gaze"] = _short_gaze(preds["gaze"]["label"])
        fields["dms_yawn"] = "No" if preds["yawn"]["label"] == "No Yawn" else "Yes"
        fields["dms_focus"] = "Safe" if preds["distraction"]["label"] == "Safe" else "Distracted"
    return fields


def dms_shown() -> bool:
    path = RESULTS / "dms_view.txt"
    try:
        return path.read_text().strip() == "1"
    except OSError:
        return False


def write_preview(cabin_frame, driver_frame, face, fields: dict) -> None:
    view = cv2.resize(cabin_frame, PREVIEW)
    cv2.rectangle(view, (0, 0), (PREVIEW[0], 36), (16, 16, 16), -1)
    cv2.putText(
        view,
        "CMS  cabin",
        (12, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    fps_label = f"{fields['fps']:.1f} FPS"
    fps_size = cv2.getTextSize(fps_label, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 2)[0]
    cv2.putText(
        view,
        fps_label,
        (PREVIEW[0] - fps_size[0] - 12, 26),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (80, 220, 255),
        2,
        cv2.LINE_AA,
    )
    if dms_shown():
        px, py = 72, 48
        pw, ph = PASSPORT
        if face and face.get("bbox"):
            x1, y1, x2, y2 = [int(v) for v in face["bbox"]]
            crop = driver_frame[max(0, y1) : max(0, y2), max(0, x1) : max(0, x2)]
            if crop.size == 0:
                crop = None
        else:
            crop = None
        if crop is None:
            passport = np.zeros((ph, pw, 3), dtype=np.uint8)
        else:
            passport = cv2.resize(crop, PASSPORT)
        view[py : py + ph, px : px + pw] = passport
        border = (80, 80, 220) if fields["mode"] in {"incident", "sos", "warning"} else (80, 220, 80)
        cv2.rectangle(view, (px - 2, py - 2), (px + pw + 2, py + ph + 2), border, 2)
        cv2.putText(view, "DMS", (px, py + ph + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
        lines = [
            f"Eyes {fields.get('dms_eyes', '--')}",
            f"Gaze {fields.get('dms_gaze', '--')}",
            f"Yawn {fields.get('dms_yawn', '--')}",
            fields.get("dms_focus", "--"),
        ]
        tx, ty = px + pw + 14, py + 28
        for line in lines:
            cv2.putText(view, line, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
            ty += 30
    payload = np.ascontiguousarray(view).tobytes()
    temporary = RESULTS / "live.bgr.tmp"
    target = RESULTS / "live.bgr"
    temporary.write_bytes(payload)
    temporary.replace(target)


def state_line(fields: dict) -> str:
    ordered = [
        fields["mode"],
        fields["camera"],
        fields["mic"],
        fields["gps"],
        fields["dms"],
        str(fields["occupancy"]),
        fields["severity"],
        fields["alert"],
        fields["vlm"],
        fields["clip"],
        fields["message"].replace("|", "/"),
        f"{fields['fps']:.1f}",
        fields["cms_front"],
        fields["cms_rear"],
        fields["cms_phone"],
        fields["cms_reach"],
    ]
    return "|".join(ordered)


class CabinDemo:
    def __init__(self, session, detector, vlm: vlm_client.VlmClient | None = None) -> None:
        self.session = session
        self.detector = detector
        self.history = {name: deque(maxlen=dms.SMOOTH_WINDOW) for name in dms.OUTPUT_ORDER}
        self.buffer = deque(maxlen=CLIP_FRAMES)
        self.hold = 0
        self.cooldown = 0
        self.latch = 0
        self.latched_mode = "monitoring"
        self.latched_message = ""
        self.latched_severity = "none"
        self.latched_alert = ""
        self.episode = 0
        self.clip = "no"
        self.vlm = "idle"
        self.samples = 0
        self.gate = cabin.CabinGate()
        self.gate_log: list[dict] = []
        self.vlm_result: dict | None = None
        self.vlm_client = vlm
        self.vlm_waiting = False
        self.wait_max_s = 0.0
        self.open_reasons: list[str] = []
        self.incident_dir: Path | None = None
        self.incident_video: str | None = None
        self.journey = journey.JourneyStore()
        self.guardian = guardian.TripPublisher(store=self.journey)
        self.empty_frames = 0

    def commit_incident(self, reasons: list[str], assessment: dict, kind: str) -> None:
        """Write one episode video. A later answer rewrites events.json in place."""
        if self.incident_dir is None:
            self.episode += 1
            folder = RESULTS / "incidents" / f"episode_{self.episode:02d}"
            folder.mkdir(parents=True, exist_ok=True)
            held = list(self.buffer)
            frames = [frame for _stamp, frame in held]
            stamps = [float(stamp) for stamp, _frame in held]
            saved = write_clip(folder / CLIP_NAME, frames, clip_rate(stamps))
            self.incident_dir = folder
            self.incident_video = CLIP_NAME if saved else None
            self.clip = "saved" if saved else "no"
        payload = {
            "episode": self.episode,
            "kind": kind,
            "reasons": list(reasons),
            "score": assessment["score"],
            "severity": assessment["severity"],
            "action": assessment["action"],
            "parts": list(assessment["parts"]),
            "vlm": self.vlm_result if isinstance(self.vlm_result, dict) else None,
            "gps": None,
            "video": self.incident_video,
            "guardian": getattr(self, "guardian", None).status if getattr(self, "guardian", None) else "idle",
            "guardian_link": getattr(self, "guardian", None).link if getattr(self, "guardian", None) else None,
        }
        (self.incident_dir / "events.json").write_text(json.dumps(payload, indent=2))

    def latch_assessment(self, assessment: dict, sos: bool, reasons: list[str]) -> None:
        self.latched_mode = screen_mode(assessment, sos)
        self.latched_severity = assessment["severity"]
        self.latched_alert = alert_name(reasons, assessment)
        self.latched_message = risk.message(assessment)

    def close_incident(self) -> None:
        self.incident_dir = None
        self.incident_video = None

    def follow_passenger(self, front: int, rear: int) -> None:
        """Start the guardian ride when someone is seated. End it after they leave."""
        notice = getattr(self, "guardian", None)
        if notice is None:
            return
        if front + rear > 0:
            self.empty_frames = 0
            if notice.token is None:
                notice.begin(front, rear)
            return
        if notice.token is None or notice.status == "sent":
            return
        self.empty_frames += 1
        if self.empty_frames < EMPTY_RIDE_FRAMES:
            return
        notice.end()
        self.guardian = guardian.TripPublisher(store=getattr(self, "journey", None))
        self.empty_frames = 0

    def offer_guardian(self, button: bool, assessment: dict, front: int, rear: int) -> None:
        """Start the clip before this. A confirmed event paints the red alert."""
        notice = getattr(self, "guardian", None)
        if notice is None:
            return
        phone = "--"
        reaching = "--"
        if isinstance(self.vlm_result, dict):
            event = self.vlm_result.get("event")
            if event == "phone":
                phone = "Yes"
            elif event == "reaching":
                reaching = "Yes"
            elif event in guardian.CONFIRM_EVENTS:
                phone = "No"
                reaching = "No"
        vlm = self.vlm_result if isinstance(self.vlm_result, dict) else None
        if notice.token is not None:
            notice.update(front, rear, phone, reaching, assessment, vlm)
        if guardian.should_notify(button, assessment, vlm):
            notice.alert(assessment, vlm, front, rear, phone, reaching, button)
        elif assessment.get("severity") == "high" or assessment.get("action") == "incident":
            notice.hold()

    def sync_guardian(self, front: int, rear: int, preds: dict | None) -> None:
        """Push the latest driver reading without clearing the cabin answer."""
        notice = getattr(self, "guardian", None)
        if notice is None or notice.token is None or not preds:
            return
        yawn = preds.get("yawn") or {}
        focus = preds.get("distraction") or {}
        eyes = preds.get("eye_state") or {}
        gaze = preds.get("gaze") or {}
        notice.note_driver(
            front,
            rear,
            {
                "eyes": eyes.get("label") or "--",
                "gaze": _short_gaze(gaze.get("label") or ""),
                "yawn": "No" if yawn.get("label") == "No Yawn" else "Yes",
                "focus": "Safe" if focus.get("label") == "Safe" else "Distracted",
            },
        )

    def fields(self, mode: str, front: int, rear: int, severity: str, alert: str, message: str) -> dict:
        return {
            "mode": mode,
            "camera": "ok",
            "mic": "down",
            "gps": "no_fix",
            "dms": "ok",
            "occupancy": front + rear,
            "severity": severity,
            "alert": alert,
            "vlm": self.vlm,
            "clip": self.clip,
            "message": message,
            "fps": 0.0,
            "dms_eyes": "--",
            "dms_gaze": "--",
            "dms_yawn": "--",
            "dms_focus": "--",
            "cms_front": str(front),
            "cms_rear": str(rear),
            "cms_phone": "--",
            "cms_reach": "--",
        }

    def step(self, driver_frame, cabin_frame, index: int, fps: float) -> dict:
        command = pop_command()
        face_img, face = dms.crop_face(driver_frame, self.detector)
        outputs = self.session.run(None, {"image": dms.preprocess(face_img)})
        preds = dms.smooth(self.history, dms.decode(outputs))
        seats = occupancy.count(cabin_frame, self.detector)
        front = seats["front"]
        rear = seats["rear"]
        self.last_front = front
        self.last_rear = rear
        self.follow_passenger(front, rear)
        reasons = dms.trigger_reasons(preds, face is not None)
        shot = cabin_frame
        if shot.shape[1] != PREVIEW[0] or shot.shape[0] != PREVIEW[1]:
            shot = cv2.resize(shot, PREVIEW)
        self.buffer.append((time.perf_counter(), shot))
        self.samples += 1
        self._take_vlm_answer(index)

        if command == "sos":
            sos_reasons = reasons or ["manual_sos"]
            assessment = risk.assess(sos_reasons, self.vlm_result, True)
            self.commit_incident(sos_reasons, assessment, "sos")
            self.offer_guardian(True, assessment, front, rear)
            self.commit_incident(sos_reasons, assessment, "sos")
            self.latch_assessment(assessment, True, sos_reasons)
            self.latch = 30
            self.open_reasons = list(sos_reasons)
        elif command == "dismiss":
            self.latch = 0
            self.hold = 0
            self.cooldown = 15
            self.vlm = "idle"
            self.close_incident()

        now = time.perf_counter()
        dms_ready = bool(reasons) and self.hold + (1 if reasons else 0) >= dms.TRIGGER_HOLD and self.cooldown == 0
        blocked = self.latch > 0 and command != "sos"
        gate_reason = None if blocked else self.gate.decide(now, front, rear, dms_ready, command == "sos")
        if gate_reason:
            self.vlm = f"gate:{gate_reason}"
            self.gate_log.append(
                {
                    "index": index,
                    "reason": gate_reason,
                    "occupancy": front + rear,
                    "front": front,
                    "rear": rear,
                    "dms": [item.split("@", 1)[0] for item in reasons],
                    "dms_reasons": list(reasons),
                    "vlm_engine": None,
                    "sent_at": now,
                    "answer_index": None,
                    "wait_s": None,
                }
            )
            shot = RESULTS / "cabin_gate_frame.jpg"
            cv2.imwrite(str(shot), cabin_frame)
            if self.vlm_client is not None:
                # The engine accepts 512 input tokens. A full cabin frame plus
                # the instruction ran past that, so the look uses a 640x360 still.
                stills = RESULTS / "vlm_stills"
                stills.mkdir(parents=True, exist_ok=True)
                vlm_shot = stills / f"frame_{index:04d}.jpg"
                cv2.imwrite(str(vlm_shot), cv2.resize(cabin_frame, PREVIEW))
                self.vlm_waiting = self.vlm_client.submit(vlm_shot)
                self.gate_log[-1]["submitted"] = self.vlm_waiting

        if self.latch > 0 and command != "dismiss":
            self.latch -= 1
            fields = self.fields(
                self.latched_mode,
                front,
                rear,
                self.latched_severity,
                self.latched_alert,
                self.latched_message,
            )
            fields["fps"] = fps
            attach_view(fields, preds)
            cabin.apply_vlm(fields, self.vlm_result)
            write_preview(cabin_frame, driver_frame, face, fields)
            self.sync_guardian(front, rear, preds)
            return fields

        if reasons:
            self.hold += 1
        else:
            self.hold = 0
        if self.cooldown > 0:
            self.cooldown -= 1

        mode = "monitoring"
        severity = "none"
        alert = ""
        message = (
            f"{preds['drowsiness']['label']}    eyes {preds['eye_state']['label']}"
        )
        if self.hold >= dms.TRIGGER_HOLD and self.cooldown == 0:
            if not self.vlm_waiting:
                self.close_incident()
            self.open_reasons = list(reasons)
            assessment = risk.assess(reasons, self.vlm_result, False)
            severity = assessment["severity"]
            mode = screen_mode(assessment, False)
            alert = alert_name(reasons, assessment)
            message = risk.message(assessment)
            answered = (
                isinstance(self.vlm_result, dict)
                and self.vlm_result.get("event") in cabin.CLOSED_EVENTS
            )
            if not answered and not str(self.vlm).startswith("gate:"):
                self.vlm = "planned"
            if assessment["action"] == "incident":
                self.commit_incident(reasons, assessment, record_kind(assessment))
                self.offer_guardian(False, assessment, front, rear)
                self.commit_incident(reasons, assessment, record_kind(assessment))
            self.latch_assessment(assessment, False, reasons)
            self.latch = 18
            self.cooldown = dms.VLM_COOLDOWN
            self.hold = 0
        elif (
            not reasons
            and not self.gate.cooling(time.perf_counter())
            and not (
                isinstance(self.vlm_result, dict)
                and self.vlm_result.get("event") in cabin.CLOSED_EVENTS
            )
        ):
            self.vlm = "idle"

        fields = self.fields(mode, front, rear, severity, alert, message)
        fields["fps"] = fps
        attach_view(fields, preds)
        cabin.apply_vlm(fields, self.vlm_result)
        write_preview(cabin_frame, driver_frame, face, fields)
        self.sync_guardian(front, rear, preds)
        return fields

    def note_step(self, seconds: float) -> None:
        if self.vlm_waiting:
            self.wait_max_s = max(self.wait_max_s, seconds)

    def _take_vlm_answer(self, index: int) -> None:
        if self.vlm_client is None or not self.vlm_waiting:
            return
        answer = self.vlm_client.poll()
        if not answer:
            return
        self.vlm_waiting = False
        self.vlm_result = answer
        if str(answer.get("event") or "") in cabin.CLOSED_EVENTS:
            self.vlm = cabin_record.label(answer)
        entry = self.gate_log[-1] if self.gate_log else {}
        if "dms_reasons" in entry:
            reasons = list(entry["dms_reasons"])
        else:
            reasons = list(self.open_reasons)
        sos = self.latched_mode == "sos"
        assessment = risk.assess(reasons, answer, sos)
        front = getattr(self, "last_front", 0)
        rear = getattr(self, "last_rear", 0)
        clip = assessment["action"] == "incident" or self.incident_dir is not None
        if clip:
            self.commit_incident(reasons, assessment, record_kind(assessment))
            self.offer_guardian(sos, assessment, front, rear)
            self.commit_incident(reasons, assessment, record_kind(assessment))
        elif str(answer.get("event") or "") in cabin.CLOSED_EVENTS:
            self.offer_guardian(sos, assessment, front, rear)
        promote = assessment["action"] in {"warning", "incident"} or sos
        if promote:
            self.latch_assessment(assessment, sos, reasons)
            if self.latch == 0:
                self.latch = 18
                self.cooldown = dms.VLM_COOLDOWN
        if not entry:
            return
        entry["answer_index"] = index
        entry["wait_s"] = round(time.perf_counter() - float(entry.get("sent_at") or 0), 2)
        entry["vlm_engine"] = "qwen3.5-0.8b-fp16"
        entry["vlm"] = answer
        entry["risk"] = assessment


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cabin", type=Path, nargs="?", default=ROOT / "videos" / "cabin_passenger.mp4")
    parser.add_argument("--driver", type=Path, default=ROOT / "videos" / "driver_face.mp4")
    parser.add_argument("--camera", type=int, default=None, help="V4L2 camera index, for example 0")
    parser.add_argument("--sample-every", type=int, default=2)
    parser.add_argument("--pace", type=float, default=0.02)
    parser.add_argument("--vlm", action="store_true", help="Ask Qwen3.5-0.8B when the cabin gate opens")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "ui_shots").mkdir(exist_ok=True)
    (RESULTS / "ui_cmd.txt").write_text("")

    look = None
    if args.vlm:
        print("[PIPE] loading Qwen3.5-0.8B")
        look = vlm_client.open_qwen(ROOT, RESULTS)
        print("[PIPE] vlm ready")
    session, _contract = dms.load_session(dms.MODEL_PATH)
    detector = dms.load_face_detector()
    demo = CabinDemo(session, detector, look)
    server = StateServer()
    print(f"[PIPE] listening on 127.0.0.1:{PORT}")
    open_dashboard()

    boot = demo.fields("self_test", 0, 0, "none", "", "Camera ok    mic down    GPS no fix")
    deadline = time.time() + 1.2
    while time.time() < deadline:
        server.send(state_line(boot))
        time.sleep(0.1)

    if args.camera is not None:
        driver_cap = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
        cabin_cap = driver_cap
        source = f"camera {args.camera}"
        args.sample_every = 1
        args.pace = 0.0
    else:
        driver_cap = cv2.VideoCapture(str(args.driver))
        cabin_cap = cv2.VideoCapture(str(args.cabin))
        source = f"driver {args.driver.name}    cabin {args.cabin.name}"
    if not driver_cap.isOpened() or not cabin_cap.isOpened():
        raise RuntimeError(f"Could not open {source}")
    driver_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    if cabin_cap is not driver_cap:
        cabin_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    print(f"[PIPE] source {source}")

    log_path = RESULTS / "ui_demo_log.jsonl"
    log = log_path.open("w")
    index = 0
    shown = 0
    misses = 0
    fps_value = 0.0
    last_tick = time.perf_counter()
    try:
        while True:
            ok_driver, driver_frame = driver_cap.read()
            if cabin_cap is driver_cap:
                ok_cabin, cabin_frame = ok_driver, driver_frame
            else:
                ok_cabin, cabin_frame = cabin_cap.read()
            if not ok_driver or not ok_cabin:
                if args.camera is None:
                    driver_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    if cabin_cap is not driver_cap:
                        cabin_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok_driver, driver_frame = driver_cap.read()
                    if cabin_cap is driver_cap:
                        ok_cabin, cabin_frame = ok_driver, driver_frame
                    else:
                        ok_cabin, cabin_frame = cabin_cap.read()
                    if not (ok_driver and ok_cabin):
                        break
                    print("[PIPE] video loop", flush=True)
                misses += 1
                if misses > 50:
                    raise RuntimeError(f"Camera {args.camera} stopped sending frames")
                time.sleep(0.05)
                continue
            misses = 0
            if index % args.sample_every != 0:
                index += 1
                continue
            now = time.perf_counter()
            elapsed = now - last_tick
            last_tick = now
            if elapsed > 0:
                instant = 1.0 / elapsed
                fps_value = instant if fps_value == 0 else (0.85 * fps_value + 0.15 * instant)
            step_started = time.perf_counter()
            fields = demo.step(driver_frame, cabin_frame, index, fps_value)
            demo.note_step(time.perf_counter() - step_started)
            line = state_line(fields)
            server.send(line)
            log.write(json.dumps(fields) + "\n")
            shown += 1
            if shown % 20 == 0:
                print(f"[PIPE] frame {index} {fields['fps']:.1f} fps {fields['mode']} {fields['message']}")
            index += 1
            time.sleep(args.pace)
    finally:
        driver_cap.release()
        if cabin_cap is not driver_cap:
            cabin_cap.release()
        if look is not None and demo.vlm_waiting:
            deadline = time.perf_counter() + 20
            while demo.vlm_waiting and time.perf_counter() < deadline:
                demo._take_vlm_answer(index)
                time.sleep(0.05)
        log.close()
        server.close()
        if look is not None:
            look.close()
    report = {
        "driver_model": dms.MODEL_PATH.name,
        "vlm_engine": "qwen3.5-0.8b-fp16" if args.vlm else None,
        "vlm_async": True,
        "wait_max_s": round(demo.wait_max_s, 3),
        "samples": shown,
        "episodes": demo.episode,
        "gates": demo.gate_log,
    }
    (RESULTS / "cabin_report.json").write_text(json.dumps(report, indent=2))
    print(f"[PIPE] samples={shown} episodes={demo.episode} gates={len(demo.gate_log)} log={log_path}")


if __name__ == "__main__":
    main()
