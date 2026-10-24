"""Application state for the cabin screen.

The risk engine decides severity. This module only turns that decision, and
the latest sensor readings, into stable words the screen can draw. A harsher
level is shown on the same frame. A calmer one waits, so a flickering model
does not flicker the cabin.

The gaze model has no forward class. Middle-Left and Middle-Right are drawn
as SIDE. Every other direction is AWAY.
"""

from __future__ import annotations

FACE_HOLD = 6
LABEL_HOLD = 4
SAFETY_HOLD = 6

SAFETY_RANK = {
    "SAFE": 0,
    "INFO": 1,
    "ATTENTION": 2,
    "WARNING": 3,
    "CRITICAL": 4,
    "FAULT": 5,
}

STATE_FIELDS = [
    "mode",
    "camera",
    "mic",
    "gps",
    "dms",
    "occupancy",
    "severity",
    "alert",
    "vlm",
    "clip",
    "message",
    "fps",
    "cms_front",
    "cms_rear",
    "cms_phone",
    "cms_reach",
    "dms_eyes",
    "dms_gaze",
    "dms_yawn",
    "vlm_text",
    "safety",
    "headline",
    "alert_title",
    "alert_detail",
    "alert_action",
    "driver",
    "attention",
    "drowsy",
    "activity",
    "system",
    "link",
    "record",
    "model",
    "latency",
    "cabin_status",
]

COPY = {
    "drowsiness": (
        "DRIVER DROWSINESS",
        "Signs of drowsiness detected",
        "Take a break when safe",
    ),
    "eye_state": (
        "DRIVER DROWSINESS",
        "Prolonged eye closure detected",
        "Take a break when safe",
    ),
    "yawn": (
        "FATIGUE",
        "Yawning detected",
        "Take a break when safe",
    ),
    "distraction": (
        "DISTRACTION",
        "Attention is off the road",
        "Eyes on the road",
    ),
    "gaze": (
        "GAZE OFF ROAD",
        "Gaze is away from the road",
        "Eyes on the road",
    ),
    "phone": (
        "CABIN PHONE",
        "Phone use in the cabin",
        "Ask the cabin to put it away",
    ),
    "reaching": (
        "REACHING",
        "Someone is reaching in the cabin",
        "Check the cabin when safe",
    ),
    "lying_down": (
        "CABIN POSTURE",
        "Someone may be lying down",
        "Check the cabin when safe",
    ),
    "altercation": (
        "CABIN CONFLICT",
        "A disturbance was detected",
        "Stop when safe",
    ),
    "unusual_object": (
        "CABIN OBJECT",
        "An unusual object was detected",
        "Check the cabin when safe",
    ),
    "sos": (
        "EMERGENCY",
        "SOS sent to your contact",
        "Stop when safe and wait for help",
    ),
    "manual_sos": (
        "EMERGENCY",
        "SOS sent to your contact",
        "Stop when safe and wait for help",
    ),
    "driver_missing": (
        "DRIVER NOT DETECTED",
        "No face in the driver camera",
        "Sit in view of the camera",
    ),
    "camera": (
        "CAMERA DISCONNECTED",
        "The cabin camera is not available",
        "Monitoring is paused",
    ),
    "dms": (
        "DRIVER MONITOR FAULT",
        "Driver monitoring is not available",
        "The safety reading is paused",
    ),
    "starting": (
        "SYSTEM CHECK",
        "Cabin safety is starting",
        "Please wait",
    ),
    "attention": (
        "ATTENTION",
        "Driver state needs a look",
        "Check the driver when safe",
    ),
}

ACTIVITY_COPY = {
    "Phone Use": (
        "PHONE USE",
        "Phone use detected",
        "Put the phone down",
    ),
    "Drinking": (
        "DRINKING",
        "Drinking detected",
        "Keep both hands ready",
    ),
    "Cigarette": (
        "SMOKING",
        "Smoking detected",
        "Keep attention on the road",
    ),
}

HEADLINES = {
    "drowsiness": "DROWSY",
    "eye_state": "DROWSY",
    "yawn": "YAWNING",
    "distraction": "DISTRACTED",
    "gaze": "GAZE AWAY",
    "phone": "PHONE",
    "reaching": "REACHING",
    "lying_down": "LYING DOWN",
    "altercation": "CONFLICT",
    "unusual_object": "OBJECT",
    "sos": "EMERGENCY",
    "manual_sos": "EMERGENCY",
    "attention": "ATTENTION",
}

ACTIVITY_HEADLINE = {
    "Phone Use": "PHONE",
    "Drinking": "DRINKING",
    "Cigarette": "SMOKING",
    "Seatbelt": "SEATBELT",
}

ACTIVITY_WORD = {
    "None": "NONE",
    "Phone Use": "PHONE",
    "Drinking": "DRINK",
    "Cigarette": "SMOKE",
    "Seatbelt": "BELT",
}

LINK_WORD = {
    "idle": "STANDBY",
    "held": "HELD",
    "live": "LIVE",
    "sent": "SENT",
}


class Hold:
    """Keep the current word until a new one has repeated."""

    def __init__(self, frames: int) -> None:
        self.frames = frames
        self.value: str | None = None
        self.pending: str | None = None
        self.count = 0

    def push(self, value: str, urgent: bool = False) -> str:
        if urgent or self.value is None or value == self.value:
            self.value = value
            self.pending = None
            self.count = 0
            return value
        if self.pending != value:
            self.pending = value
            self.count = 1
        else:
            self.count += 1
        if self.count >= self.frames:
            self.value = value
            self.pending = None
            self.count = 0
        return self.value or value


class Display:
    """Stable driver-facing words for one ride."""

    def __init__(self) -> None:
        self.face = Hold(FACE_HOLD)
        self.eyes = Hold(LABEL_HOLD)
        self.gaze = Hold(LABEL_HOLD)
        self.yawn = Hold(LABEL_HOLD)
        self.drowsy = Hold(LABEL_HOLD)
        self.activity = Hold(LABEL_HOLD)
        self.attention = Hold(LABEL_HOLD)
        self.safety = Hold(SAFETY_HOLD)
        self.shown_safety = "INFO"
        self.shown_key = "starting"
        self.shown_task = "starting"
        self.shown_label = ""

    def apply(
        self,
        fields: dict,
        preds: dict | None,
        face: dict | None,
        link: str,
        latency: str,
        *,
        face_known: bool,
    ) -> None:
        face_token = self._face(bool(face) if face_known else None)
        token, key, task, label = _raw_safety(fields, face_token)
        self._push_safety(token, key, task, label)
        headline, title, detail, action = self._words(face_token)
        eyes, gaze, drowsy, yawn, activity, attention = self._metrics(preds, face_token)
        phone = str(fields.get("cms_phone") or "--")
        reach = str(fields.get("cms_reach") or "--")
        cabin = _cabin(fields, phone, reach)

        fields["dms_eyes"] = eyes
        fields["dms_gaze"] = gaze
        fields["dms_yawn"] = yawn
        fields["safety"] = self.shown_safety
        fields["headline"] = headline
        fields["alert_title"] = title
        fields["alert_detail"] = detail
        fields["alert_action"] = action
        fields["driver"] = _presence(face_token)
        fields["attention"] = attention
        fields["drowsy"] = drowsy
        fields["activity"] = activity
        fields["system"] = _system(fields)
        fields["link"] = LINK_WORD.get(link, "OFFLINE")
        fields["record"] = _record(fields)
        fields["model"] = _model(fields)
        fields["latency"] = latency or "--"
        fields["cabin_status"] = cabin
        fields["cms_phone"] = {"Yes": "IN USE", "No": "CLEAR"}.get(phone, "--")
        fields["cms_reach"] = {"Yes": "YES", "No": "CLEAR"}.get(reach, "--")
        if self.shown_safety == "FAULT":
            fields["dms_eyes"] = self.eyes.push("--", urgent=True)
            fields["dms_gaze"] = self.gaze.push("--", urgent=True)
            fields["dms_yawn"] = self.yawn.push("--", urgent=True)
            fields["drowsy"] = self.drowsy.push("UNKNOWN", urgent=True)
            fields["activity"] = self.activity.push("--", urgent=True)
            fields["attention"] = self.attention.push("UNKNOWN", urgent=True)
            if str(fields.get("camera") or "") != "ok":
                fields["occupancy"] = "--"
                fields["cms_front"] = "--"
                fields["cms_rear"] = "--"
                fields["cms_phone"] = "--"
                fields["cms_reach"] = "--"

    def _face(self, present: bool | None) -> str:
        if present is None:
            return "search"
        token = "yes" if present else "no"
        if self.face.value is None:
            if token == "yes":
                return self.face.push("yes", urgent=True)
            self.face.value = "search"
        urgent = token == "yes" and self.face.value != "yes"
        return self.face.push(token, urgent=urgent)

    def _push_safety(self, token: str, key: str, task: str, label: str) -> None:
        current = self.shown_safety
        urgent = SAFETY_RANK[token] > SAFETY_RANK[current]
        if current == "INFO" and token == "SAFE":
            urgent = True
        if token == "SAFE" and self.shown_key == "driver_missing":
            urgent = True
        shown = self.safety.push(token, urgent=urgent)
        if shown == token:
            self.shown_safety = token
            self.shown_key = key
            self.shown_task = task
            self.shown_label = label

    def _words(self, face_token: str) -> tuple[str, str, str, str]:
        safety = self.shown_safety
        if safety == "SAFE":
            headline = "SEARCHING" if face_token == "search" else "ATTENTIVE"
            return headline, "", "", ""
        if safety == "INFO":
            title, detail, action = COPY["starting"]
            return "STARTING", title, detail, action
        if self.shown_key == "driver_missing":
            title, detail, action = COPY["driver_missing"]
            return "NOT DETECTED", title, detail, action
        if self.shown_key == "camera":
            title, detail, action = COPY["camera"]
            return "NO CAMERA", title, detail, action
        if self.shown_key == "dms":
            title, detail, action = COPY["dms"]
            return "MONITOR FAULT", title, detail, action
        title, detail, action = _copy(self.shown_task, self.shown_label)
        return _headline(self.shown_task, self.shown_label), title, detail, action

    def _metrics(
        self, preds: dict | None, face_token: str
    ) -> tuple[str, str, str, str, str, str]:
        if face_token != "yes" or not preds:
            eyes = self.eyes.push("--", urgent=True)
            gaze = self.gaze.push("--", urgent=True)
            drowsy = self.drowsy.push("UNKNOWN", urgent=True)
            yawn = self.yawn.push("--", urgent=True)
            activity = self.activity.push("--", urgent=True)
            attention = self.attention.push("UNKNOWN", urgent=True)
            return eyes, gaze, drowsy, yawn, activity, attention

        eyes = self.eyes.push(_eye_word(preds), urgent=self.eyes.value in {None, "--"})
        gaze = self.gaze.push(_gaze_word(preds), urgent=self.gaze.value in {None, "--"})
        drowsy = self.drowsy.push(
            _drowsy_word(preds), urgent=self.drowsy.value in {None, "UNKNOWN"}
        )
        yawn = self.yawn.push(_yawn_word(preds), urgent=self.yawn.value in {None, "--"})
        activity = self.activity.push(
            _activity_word(preds), urgent=self.activity.value in {None, "--"}
        )
        attention = self.attention.push(
            _attention_word(preds), urgent=self.attention.value in {None, "UNKNOWN"}
        )
        return self._align(eyes, gaze, drowsy, yawn, activity, attention)

    def _align(
        self,
        eyes: str,
        gaze: str,
        drowsy: str,
        yawn: str,
        activity: str,
        attention: str,
    ) -> tuple[str, str, str, str, str, str]:
        if self.shown_safety not in {"ATTENTION", "WARNING", "CRITICAL"}:
            return eyes, gaze, drowsy, yawn, activity, attention
        task = self.shown_task
        label = self.shown_label
        if task == "eye_state":
            eyes = self.eyes.push("CLOSED", urgent=True)
            drowsy = self.drowsy.push("HIGH", urgent=True)
        elif task == "drowsiness":
            drowsy = self.drowsy.push("HIGH", urgent=True)
        elif task == "yawn":
            yawn = self.yawn.push("YAWN", urgent=True)
        elif task == "gaze":
            gaze = self.gaze.push("AWAY", urgent=True)
        elif task == "distraction":
            attention = self.attention.push("DISTRACTED", urgent=True)
        elif task == "activity":
            activity = self.activity.push(ACTIVITY_WORD.get(label, "PHONE"), urgent=True)
        return eyes, gaze, drowsy, yawn, activity, attention


def state_line(fields: dict) -> str:
    """One screen packet. Pipe characters in values are replaced."""
    parts: list[str] = []
    for key in STATE_FIELDS:
        if key == "fps":
            try:
                text = f"{float(fields.get(key) or 0):.1f}"
            except (TypeError, ValueError):
                text = "0.0"
        else:
            text = str(fields.get(key, ""))
        parts.append(text.replace("|", "/").replace("\n", " ").replace("\r", " "))
    return "|".join(parts)


def _raw_safety(fields: dict, face_token: str) -> tuple[str, str, str, str]:
    mode = str(fields.get("mode") or "")
    severity = str(fields.get("severity") or "none")
    task, label = _split_alert(str(fields.get("alert") or ""))
    if mode == "sos" or severity == "critical":
        if task not in COPY and task != "activity":
            task = "sos"
        return "CRITICAL", task, task, label
    if str(fields.get("camera") or "ok") != "ok":
        return "FAULT", "camera", "camera", ""
    if str(fields.get("dms") or "ok") != "ok":
        return "FAULT", "dms", "dms", ""
    if mode == "self_test":
        return "INFO", "starting", "starting", ""
    if mode == "incident" or severity == "high":
        task = task or "attention"
        return "CRITICAL", task, task, label
    if mode == "warning" or severity == "medium":
        task = task or "attention"
        return "WARNING", task, task, label
    if severity == "low":
        task = task or "attention"
        return "ATTENTION", task, task, label
    if face_token == "no" and mode == "monitoring":
        return "ATTENTION", "driver_missing", "driver_missing", ""
    return "SAFE", "", "", ""


def _split_alert(alert: str) -> tuple[str, str]:
    body = alert.split("@", 1)[0]
    task, _, label = body.partition(":")
    return task, label


def _copy(task: str, label: str) -> tuple[str, str, str]:
    if task == "activity":
        found = ACTIVITY_COPY.get(label)
        if found:
            return found
    return COPY.get(task, COPY["attention"])


def _headline(task: str, label: str) -> str:
    if task == "activity":
        return ACTIVITY_HEADLINE.get(label, "ACTIVITY")
    return HEADLINES.get(task, "ATTENTION")


def _presence(face_token: str) -> str:
    if face_token == "yes":
        return "In frame"
    if face_token == "no":
        return "Not in frame"
    return "Looking for the driver"


def _label(preds: dict, name: str) -> str:
    item = preds.get(name) or {}
    return str(item.get("label") or "")


def _eye_word(preds: dict) -> str:
    return {"Open": "OPEN", "Closed": "CLOSED"}.get(_label(preds, "eye_state"), "--")


def _gaze_word(preds: dict) -> str:
    label = _label(preds, "gaze")
    if label in {"Middle-Left", "Middle-Right"}:
        return "SIDE"
    if not label:
        return "--"
    return "AWAY"


def _drowsy_word(preds: dict) -> str:
    return {"Alert": "LOW", "Drowsy": "HIGH"}.get(_label(preds, "drowsiness"), "UNKNOWN")


def _yawn_word(preds: dict) -> str:
    return {"No Yawn": "NONE", "Yawning": "YAWN"}.get(_label(preds, "yawn"), "--")


def _activity_word(preds: dict) -> str:
    return ACTIVITY_WORD.get(_label(preds, "activity"), "--")


def _attention_word(preds: dict) -> str:
    return {"Safe": "ATTENTIVE", "Distracted": "DISTRACTED"}.get(
        _label(preds, "distraction"), "UNKNOWN"
    )


def _system(fields: dict) -> str:
    if str(fields.get("camera") or "ok") != "ok" or str(fields.get("dms") or "ok") != "ok":
        return "FAULT"
    if str(fields.get("clip") or "") == "fail":
        return "DEGRADED"
    if str(fields.get("mode") or "") == "self_test":
        return "READY"
    return "MONITORING"


def _record(fields: dict) -> str:
    clip = str(fields.get("clip") or "no")
    if clip == "fail":
        return "FAILED"
    if clip == "saved":
        return "SAVED"
    if str(fields.get("mode") or "") in {"incident", "sos"}:
        return "RECORDING"
    return "OFF"


def _model(fields: dict) -> str:
    if str(fields.get("mode") or "") == "self_test":
        return "LOADING"
    if str(fields.get("dms") or "ok") != "ok":
        return "FAIL"
    return "READY"


def _cabin(fields: dict, phone: str, reach: str) -> str:
    if str(fields.get("camera") or "ok") != "ok":
        return "OFFLINE"
    vlm = str(fields.get("vlm") or "")
    if vlm.startswith("gate:") or vlm == "planned":
        return "CHECKING"
    if phone == "Yes":
        return "PHONE"
    if reach == "Yes":
        return "REACHING"
    event = vlm.split(" ", 1)[0]
    if str(fields.get("mode") or "") == "self_test" and event in {"", "idle"}:
        return "STANDBY"
    return {
        "lying_down": "POSTURE",
        "altercation": "CONFLICT",
        "unusual_object": "OBJECT",
        "phone": "PHONE",
        "reaching": "REACHING",
        "none": "CALM",
        "idle": "CALM",
        "": "CALM",
    }.get(event, "CALM")
