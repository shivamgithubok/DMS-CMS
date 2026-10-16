"""Cabin signaling relay. No camera and no GStreamer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import tempfile
import unittest
import urllib.request

from guardian import journey
from guardian.page import PageServer, TripView
from guardian.signaling import GUID, accept_key

PORT = 8097


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    got = 0
    while got < size:
        piece = sock.recv(size - got)
        if not piece:
            raise ConnectionError("closed")
        chunks.append(piece)
        got += len(piece)
    return b"".join(chunks)


def _send(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload).encode()
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    frame = bytearray([0x81, 0x80 | len(data)])
    frame.extend(mask)
    frame.extend(masked)
    sock.sendall(frame)


def _recv(sock: socket.socket) -> dict:
    header = _read_exact(sock, 2)
    size = header[1] & 0x7F
    if size == 126:
        size = int.from_bytes(_read_exact(sock, 2), "big")
    data = _read_exact(sock, size)
    return json.loads(data.decode())


def _connect() -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=2)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            "GET /cabin-ws HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{PORT}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode()
    )
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        buffer += sock.recv(4096)
    head = buffer.split(b"\r\n\r\n", 1)[0].decode()
    accept = ""
    for line in head.split("\r\n"):
        if line.lower().startswith("sec-websocket-accept:"):
            accept = line.split(":", 1)[1].strip()
    if accept != accept_key(key):
        raise AssertionError(head)
    return sock


class CabinSignalTest(unittest.TestCase):
    def test_accept_key_matches_the_websocket_guid(self) -> None:
        self.assertEqual(
            accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            base64.b64encode(hashlib.sha1(("dGhlIHNhbXBsZSBub25jZQ==" + GUID).encode()).digest()).decode(),
        )

    def test_viewer_receives_the_sender_offer(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = journey.JourneyStore(path=__import__("pathlib").Path(tmp.name) / "journey.sqlite")
        page = PageServer(PORT, TripView(), store)
        self.addCleanup(page.stop)
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/t/local", timeout=2) as response:
            html = response.read().decode()
        self.assertIn("View cabin", html)
        self.assertIn("/cabin-ws", html)
        sender = _connect()
        viewer = _connect()
        self.addCleanup(sender.close)
        self.addCleanup(viewer.close)
        _send(sender, {"type": "register", "role": "sender"})
        _send(viewer, {"type": "register", "role": "viewer"})
        self.assertEqual(_recv(viewer)["type"], "server-status")
        self.assertEqual(_recv(sender)["type"], "viewer-joined")
        _send(sender, {"type": "offer", "sdp": {"type": "offer", "sdp": "v=0"}})
        offer = _recv(viewer)
        self.assertEqual(offer["type"], "offer")
        self.assertEqual(offer["sdp"]["sdp"], "v=0")
