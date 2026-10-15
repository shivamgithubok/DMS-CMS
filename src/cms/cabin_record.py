"""Closed cabin record.

A vision-language reply may be used only when the event name is one of the
six cabin events. Confidence, subjects, description, and severity are stored
with that event. Free text and unknown names become event "none".
"""

from __future__ import annotations

import json

from .cabin import CLOSED_EVENTS

SEVERITIES = {"low", "medium", "high"}
SUBJECTS = {"driver", "passenger", "unknown"}


def parse_record(text: str) -> dict:
    payload = _best_object(text)
    event = str(payload.get("event") or "").strip().lower().replace(" ", "_")
    if event not in CLOSED_EVENTS:
        return _empty(text)
    subjects = _subjects(payload.get("subjects"))
    return {
        "event": event,
        "confidence": _confidence(payload.get("confidence")),
        "subjects": subjects,
        "description": _description(payload.get("description")),
        "severity": _severity(payload.get("severity")),
        "raw": text.strip(),
    }


def label(record: dict | None) -> str:
    """Short cabin-card text. The description stays in the stored record."""
    if not isinstance(record, dict):
        return "idle"
    event = str(record.get("event") or "none")
    if event not in CLOSED_EVENTS:
        return "idle"
    parts = [event]
    confidence = record.get("confidence")
    if isinstance(confidence, float):
        parts.append(f"{confidence:.2f}")
    severity = record.get("severity")
    if severity in SEVERITIES:
        parts.append(str(severity))
    return " ".join(parts)


def _empty(text: str) -> dict:
    return {
        "event": "none",
        "confidence": None,
        "subjects": [],
        "description": "",
        "severity": "",
        "raw": text.strip(),
    }


def _best_object(text: str) -> dict:
    """One cabin object. A list of guesses keeps the strongest closed event."""
    payload = _json_value(text)
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, list):
        return {}
    best: dict | None = None
    best_confidence = -1.0
    for item in payload:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip().lower().replace(" ", "_")
        if event not in CLOSED_EVENTS or event == "none":
            continue
        confidence = _confidence(item.get("confidence"))
        score = confidence if confidence is not None else -1.0
        if best is None or score > best_confidence:
            best = item
            best_confidence = score
    return best or {}


def _json_value(text: str):
    array_at = text.find("[")
    object_at = text.find("{")
    if array_at >= 0 and (object_at < 0 or array_at < object_at):
        end = text.rfind("]")
        if end > array_at:
            try:
                payload = json.loads(text[array_at:end + 1])
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, list):
                return payload
    if object_at < 0:
        return {}
    end = text.rfind("}")
    if end <= object_at:
        return {}
    try:
        payload = json.loads(text[object_at:end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return max(0.0, min(1.0, number))


def _subjects(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names = []
    for item in value:
        name = str(item).strip().lower()
        if name in SUBJECTS and name not in names:
            names.append(name)
    return names


def _description(value) -> str:
    text = " ".join(str(value or "").split())
    return text.replace("|", "/")[:80]


def _severity(value) -> str:
    name = str(value or "").strip().lower()
    return name if name in SEVERITIES else ""
