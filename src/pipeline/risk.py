"""Risk score for one cabin decision.

Points are a starting policy so the decision is testable.
They are not a validated safety model. Voice, GPS, emotion, and age add nothing.
"""

from __future__ import annotations

VLM_MIN_CONFIDENCE = 0.60
VLM_POINTS = {
    "phone": 2,
    "reaching": 2,
    "lying_down": 2,
    "unusual_object": 3,
    "altercation": 5,
}
FATIGUE_POINTS = {
    "drowsiness": 3,
    "eye_state": 3,
    "yawn": 1,
}
ACTIVITY_POINTS = {
    "Phone Use": 3,
    "Drinking": 2,
    "Cigarette": 2,
}


def assess(dms_reasons, vlm, sos: bool) -> dict:
    """Turn one DMS hold and one closed cabin event into a score."""
    if sos:
        return {
            "score": 11,
            "severity": "critical",
            "action": "incident",
            "parts": ["sos"],
        }

    parsed = [_parse(item) for item in (dms_reasons or [])]
    fatigue = _fatigue(parsed)
    attention = _attention(parsed)
    activity = _activity(parsed)
    event, vlm_points = _vlm_points(vlm, parsed)

    parts = []
    if fatigue:
        parts.append(f"fatigue:{fatigue}")
    if attention:
        parts.append(f"attention:{attention}")
    if activity:
        parts.append(f"activity:{activity}")
    if vlm_points:
        parts.append(f"vlm:{event}:{vlm_points}")

    score = fatigue + attention + activity + vlm_points
    severity = _severity(score)
    if score >= 5:
        action = "incident"
    elif score >= 3:
        action = "warning"
    else:
        action = "none"
    return {
        "score": score,
        "severity": severity,
        "action": action,
        "parts": parts,
    }


def message(assessment: dict) -> str:
    """One screen line: severity, score, then each contribution."""
    text = f"{assessment['severity']} {assessment['score']}"
    parts = "  ".join(assessment.get("parts") or [])
    if parts:
        text = f"{text}  {parts}"
    return text


def _parse(item) -> tuple[str, str]:
    body, sep, _confidence = str(item).rpartition("@")
    if not sep:
        body = str(item)
    task, sep, label = body.partition(":")
    if not sep:
        return task, ""
    return task, label


def _fatigue(parsed: list[tuple[str, str]]) -> int:
    scores = [FATIGUE_POINTS[task] for task, _label in parsed if task in FATIGUE_POINTS]
    return max(scores) if scores else 0


def _attention(parsed: list[tuple[str, str]]) -> int:
    score = 0
    if any(task == "distraction" for task, _label in parsed):
        score += 2
    if any(task == "gaze" for task, _label in parsed):
        score += 1
    return min(score, 3)


def _activity(parsed: list[tuple[str, str]]) -> int:
    scores = [
        ACTIVITY_POINTS[label]
        for task, label in parsed
        if task == "activity" and label in ACTIVITY_POINTS
    ]
    return max(scores) if scores else 0


def _vlm_points(vlm, parsed: list[tuple[str, str]]) -> tuple[str, int]:
    if not isinstance(vlm, dict):
        return "", 0
    event = str(vlm.get("event") or "")
    if event not in VLM_POINTS:
        return "", 0
    confidence = _confidence(vlm.get("confidence"))
    if confidence is None or confidence < VLM_MIN_CONFIDENCE:
        return "", 0
    points = VLM_POINTS[event]
    if event == "phone" and _phone_context(parsed):
        points = 3
    return event, points


def _phone_context(parsed: list[tuple[str, str]]) -> bool:
    for task, label in parsed:
        if task == "distraction" or task == "gaze":
            return True
        if task == "activity" and label == "Phone Use":
            return True
    return False


def _confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _severity(score: int) -> str:
    if score >= 5:
        return "high"
    if score >= 3:
        return "medium"
    if score >= 1:
        return "low"
    return "none"
