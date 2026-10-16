"""Send the cabin picture to the guardian page.

The page server only forwards the offer, the answer, and the ICE candidates.
Frames come from a V4L2 camera, or from a cabin file when no camera is attached.
The picture is the 640x360 cabin preview, without the driver face crop.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import socket
import threading
from pathlib import Path

import cv2
import numpy as np
from aioice import Candidate
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.rtcicetransport import candidate_from_aioice
from av import VideoFrame

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PREVIEW = (640, 360)


def write_text(sock: socket.socket, text: str) -> None:
    payload = text.encode()
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    size = len(payload)
    frame = bytearray([0x81, 0x80 | (size if size < 126 else 126)])
    if size >= 126:
        frame.extend(size.to_bytes(2, "big"))
    frame.extend(mask)
    frame.extend(masked)
    sock.sendall(frame)


def _read_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = []
    got = 0
    while got < size:
        piece = sock.recv(size - got)
        if not piece:
            return None
        chunks.append(piece)
        got += len(piece)
    return b"".join(chunks)


def read_frame(sock: socket.socket):
    header = _read_exact(sock, 2)
    if header is None:
        return None
    opcode = header[0] & 0x0F
    size = header[1] & 0x7F
    if size == 126:
        extra = _read_exact(sock, 2)
        if extra is None:
            return None
        size = int.from_bytes(extra, "big")
    elif size == 127:
        extra = _read_exact(sock, 8)
        if extra is None:
            return None
        size = int.from_bytes(extra, "big")
    data = _read_exact(sock, size)
    if data is None:
        return None
    return opcode, data


class CabinTrack(VideoStreamTrack):
    def __init__(self, capture: cv2.VideoCapture) -> None:
        super().__init__()
        self.capture = capture
        self._lock = threading.Lock()

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()
        frame = await asyncio.to_thread(self._read)
        image = cv2.resize(frame, PREVIEW)
        cv2.rectangle(image, (0, 0), (PREVIEW[0], 36), (16, 16, 16), -1)
        cv2.putText(image, "CMS  cabin", (12, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        video = VideoFrame.from_ndarray(image, format="bgr24")
        video.pts = pts
        video.time_base = time_base
        return video

    def _read(self):
        with self._lock:
            ok, frame = self.capture.read()
            if not ok:
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self.capture.read()
            if not ok or frame is None:
                return np.zeros((PREVIEW[1], PREVIEW[0], 3), dtype=np.uint8)
            return frame


def open_capture(camera: int | None, video: Path | None) -> cv2.VideoCapture:
    if camera is not None:
        device = f"/dev/video{camera}"
        if not Path(device).exists():
            raise SystemExit(
                f"No camera at {device}. This machine has no V4L2 camera node. "
                "Plug in a camera, or pass --file videos/cabin_passenger.mp4 to test the page."
            )
        capture = cv2.VideoCapture(camera, cv2.CAP_V4L2)
        if not capture.isOpened():
            raise SystemExit(f"Could not open camera {device}")
        return capture
    if video is None:
        raise SystemExit("Pass --camera N or --file PATH")
    path = video.resolve()
    if not path.is_file():
        raise SystemExit(f"Cabin file not found: {path}")
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise SystemExit(f"Could not open {path}")
    return capture


class CabinSender:
    def __init__(self, capture: cv2.VideoCapture) -> None:
        self.capture = capture
        self.sock: socket.socket | None = None
        self.pc: RTCPeerConnection | None = None
        self.remote_set = False
        self.pending_ice: list[dict] = []
        self._send_lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self, host: str, port: int) -> None:
        self.loop = asyncio.get_running_loop()
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(None)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._handshake(host, port)
        threading.Thread(target=self._read_loop, name="cabin-signal", daemon=True).start()
        self._send({"type": "register", "role": "sender"})
        print("[CABIN] sender waiting for the guardian page", flush=True)
        await asyncio.Future()

    def _handshake(self, host: str, port: int) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /cabin-ws HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        assert self.sock is not None
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            piece = self.sock.recv(4096)
            if not piece:
                raise RuntimeError("signaling server closed during handshake")
            buffer += piece
        head = buffer.split(b"\r\n\r\n", 1)[0].decode()
        if "101" not in head.split("\r\n", 1)[0]:
            raise RuntimeError(f"signaling handshake failed: {head}")
        accept = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("sec-websocket-accept:"):
                accept = line.split(":", 1)[1].strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if accept != expected:
            raise RuntimeError("signaling handshake key did not match")

    def _send(self, payload: dict) -> None:
        if self.sock is None:
            return
        with self._send_lock:
            write_text(self.sock, json.dumps(payload))

    def _read_loop(self) -> None:
        assert self.sock is not None and self.loop is not None
        while True:
            frame = read_frame(self.sock)
            if frame is None:
                print("[CABIN] signaling closed", flush=True)
                return
            opcode, data = frame
            if opcode != 1:
                continue
            try:
                message = json.loads(data.decode())
            except json.JSONDecodeError:
                continue
            asyncio.run_coroutine_threadsafe(self._on_message(message), self.loop)

    async def _on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "viewer-joined":
            print("[CABIN] viewer joined, starting the picture", flush=True)
            await self._offer()
        elif kind == "answer":
            await self._apply_answer(message.get("sdp") or {})
        elif kind == "ice-candidate":
            await self._apply_ice(message.get("candidate") or {})
        elif kind == "peer-disconnected":
            print("[CABIN] viewer left", flush=True)

    async def _offer(self) -> None:
        if self.pc is not None:
            return
        self.pc = RTCPeerConnection(
            RTCConfiguration(iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")])
        )
        self.pc.addTrack(CabinTrack(self.capture))

        @self.pc.on("connectionstatechange")
        def _state() -> None:
            assert self.pc is not None
            print(f"[CABIN] connection {self.pc.connectionState}", flush=True)

        await self.pc.setLocalDescription(await self.pc.createOffer())
        local = self.pc.localDescription
        assert local is not None
        self._send({"type": "offer", "sdp": {"type": local.type, "sdp": local.sdp}})
        print("[CABIN] offer sent", flush=True)

    async def _apply_answer(self, sdp: dict) -> None:
        text = str(sdp.get("sdp") or "")
        if not text or self.pc is None:
            return
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=text, type="answer"))
        self.remote_set = True
        pending = self.pending_ice
        self.pending_ice = []
        for candidate in pending:
            await self._apply_ice(candidate)
        print("[CABIN] answer set", flush=True)

    async def _apply_ice(self, candidate: dict) -> None:
        line = str(candidate.get("candidate") or "")
        if not line or self.pc is None:
            return
        if not self.remote_set:
            self.pending_ice.append(candidate)
            return
        if line.startswith("candidate:"):
            line = line.split(":", 1)[1]
        parsed = candidate_from_aioice(Candidate.from_sdp(line))
        parsed.sdpMLineIndex = int(candidate.get("sdpMLineIndex") or 0)
        mid = candidate.get("sdpMid")
        parsed.sdpMid = None if mid is None else str(mid)
        await self.pc.addIceCandidate(parsed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--camera", type=int, default=None)
    parser.add_argument("--file", type=Path, default=None)
    args = parser.parse_args()
    capture = open_capture(args.camera, args.file)
    try:
        asyncio.run(CabinSender(capture).start(args.host, args.port))
    finally:
        capture.release()


if __name__ == "__main__":
    main()
