"""Open the guardian page and a cabin sender for a local camera test.

The map and the diagnosis stay on MQTT and HTTP. This process adds the
signaling socket and starts the GStreamer sender. Driver Accept / Reject is
not on the screen yet: View cabin on the page starts the picture directly.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from guardian.page import serve_in_thread

SENDER = Path(__file__).resolve().parent / "cabin_sender.py"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--camera", type=int, default=None, help="V4L2 camera index, for example 0")
    parser.add_argument("--file", type=Path, default=None, help="Cabin clip used when no camera is attached")
    args = parser.parse_args()
    if args.camera is None and args.file is None:
        args.file = ROOT / "videos" / "cabin_passenger.mp4"

    serve_in_thread(port=args.port)
    command = [sys.executable, str(SENDER), "--port", str(args.port)]
    if args.camera is not None:
        command.extend(["--camera", str(args.camera)])
        source = f"camera /dev/video{args.camera}"
    else:
        command.extend(["--file", str(args.file)])
        source = f"file {args.file}"
    print(f"[CABIN] page http://127.0.0.1:{args.port}/t/local", flush=True)
    print(f"[CABIN] source {source}", flush=True)
    print("[CABIN] open that page and press View cabin", flush=True)
    subprocess.Popen(command, start_new_session=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
