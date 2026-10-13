"""Cabin gate and event fusion.

The driver engine still runs every frame. This module decides when one cabin
look is worth taking, and how a closed vision-language result combines with
the driver signals.
"""

from __future__ import annotations

import time

CLOSED_EVENTS = {
    "none",
    "phone",
    "reaching",
    "lying_down",
    "altercation",
    "unusual_object",
}


class CabinGate:
    def __init__(self, cooldown_s: float = 12.0, timer_s: float = 30.0) -> None:
        self.cooldown_s = cooldown_s
        self.timer_s = timer_s
        self.last_front: int | None = None
        self.last_rear: int | None = None
        self.pending: str | None = None
        self.last_call = 0.0
        self.started = time.perf_counter()

    def cooling(self, now: float) -> bool:
        return self.last_call > 0.0 and (now - self.last_call) < self.cooldown_s

    def decide(self, now: float, front: int, rear: int, dms_ready: bool, sos: bool) -> str | None:
        if self.last_front is not None and (front != self.last_front or rear != self.last_rear):
            self.pending = "occupancy"
        self.last_front = front
        self.last_rear = rear
        if sos:
            self.pending = "sos"
        elif dms_ready:
            self.pending = "dms"
        elif (now - (self.last_call or self.started)) >= self.timer_s:
            self.pending = self.pending or "timer"
        if not self.pending or self.cooling(now):
            return None
        reason = self.pending
        self.pending = None
        self.last_call = now
        return reason


def apply_vlm(fields: dict, vlm: dict | None) -> dict:
    """Copy a closed cabin event onto the dashboard fields.

    Free text and unknown event names are ignored. Phone and reaching stay
    blank until a closed event says otherwise.
    """
    fields["cms_phone"] = "--"
    fields["cms_reach"] = "--"
    event = vlm.get("event") if isinstance(vlm, dict) else None
    if event not in CLOSED_EVENTS or event == "none":
        return fields
    if event == "phone":
        fields["cms_phone"] = "Yes"
    elif event == "reaching":
        fields["cms_reach"] = "Yes"
    else:
        fields["cms_phone"] = "No"
        fields["cms_reach"] = "No"
    return fields
