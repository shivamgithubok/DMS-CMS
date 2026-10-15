"""Background cabin look.

The dashboard process asks. A separate Edge-LLM process answers. submit()
returns immediately so the driver camera can keep moving. poll() picks up
the closed event on a later frame.
"""

from __future__ import annotations

import json
import select
import subprocess
import time
from pathlib import Path


class VlmClient:
    def __init__(self, command: list[str], log_path: Path) -> None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = log_path.open("w")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            bufsize=1,
        )
        self.busy = False
        ready = self._read_until(lambda line: line == "ready", 180)
        if ready != "ready":
            self.close()
            raise RuntimeError(f"VLM did not start: {ready!r}")

    def _read(self, timeout: float) -> str:
        if self.proc.stdout is None:
            return ""
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return ""
        return self.proc.stdout.readline().strip()

    def _read_until(self, match, timeout: float) -> str:
        deadline = time.perf_counter() + timeout
        last = ""
        while time.perf_counter() < deadline:
            if self.proc.poll() is not None:
                break
            last = self._read(min(2.0, max(0.0, deadline - time.perf_counter())))
            if last and match(last):
                return last
        return last

    def submit(self, image: Path) -> bool:
        """Hand one still to the worker. Returns immediately."""
        if self.busy or self.proc.poll() is not None or self.proc.stdin is None:
            return False
        self.proc.stdin.write(str(image) + "\n")
        self.proc.stdin.flush()
        self.busy = True
        return True

    def poll(self) -> dict | None:
        """Return a finished answer, or None while the look is still running.

        Log lines from the worker are drained so the pipe cannot fill and
        stall the cabin process.
        """
        if self.proc.stdout is None:
            return None
        found = None
        while True:
            ready, _, _ = select.select([self.proc.stdout], [], [], 0)
            if not ready:
                break
            line = self.proc.stdout.readline().strip()
            if not line.startswith("{"):
                continue
            self.busy = False
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                found = payload
        return found

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if not self.log.closed:
            self.log.close()


def open_qwen(root: Path, results: Path) -> VlmClient:
    python = root / "third_party" / "edgellm-venv" / "bin" / "python"
    script = root / "src" / "pipeline" / "cabin_vlm.py"
    return VlmClient([str(python), "-u", str(script), "--serve"], results / "vlm_worker.log")
