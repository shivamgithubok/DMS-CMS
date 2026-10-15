"""One cabin still through the local Qwen engine.

The Edge-LLM interpreter runs this file. The dashboard environment does not
contain that runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from cms.cabin_vlm import main

if __name__ == "__main__":
    main()
