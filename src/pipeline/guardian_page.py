"""Serve the guardian phone page and the local MQTT broker."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from guardian.page import main

if __name__ == "__main__":
    main()
