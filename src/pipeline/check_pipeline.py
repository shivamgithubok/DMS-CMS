"""Run the driver model on a video and count cabin-gate openings."""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from dms.check_pipeline import main

if __name__ == "__main__":
    main()
