"""One cabin-view signaling room on the guardian page server.

The browser and the Jetson sender speak the same JSON messages as a small
WebRTC demo: register, offer, answer, ice-candidate. The video does not
pass through this socket.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def write_text(wfile, text: str) -> None:
    payload = text.encode()
    size = len(payload)
    frame = bytearray([0x81])
    if size < 126:
        frame.append(size)
    elif size < 65536:
        frame.append(126)
        frame.extend(size.to_bytes(2, "big"))
    else:
        frame.append(127)
        frame.extend(size.to_bytes(8, "big"))
    frame.extend(payload)
    wfile.write(frame)
    wfile.flush()


def read_frame(rfile):
    try:
        header = rfile.read(2)
    except OSError:
        return None
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    size = header[1] & 0x7F
    if size == 126:
        size = int.from_bytes(rfile.read(2), "big")
    elif size == 127:
        size = int.from_bytes(rfile.read(8), "big")
    mask = rfile.read(4) if masked else b""
    data = rfile.read(size)
    if len(data) < size:
        return None
    if masked:
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
    return opcode, data


class SocketPeer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.role: str | None = None
        self._lock = threading.Lock()

    def send(self, payload: dict) -> bool:
        try:
            with self._lock:
                write_text(self.handler.wfile, json.dumps(payload))
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False


class CabinSignal:
    """One sender and one viewer. A second viewer is told the room is busy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sender: SocketPeer | None = None
        self.viewer: SocketPeer | None = None

    def serve(self, handler) -> None:
        key = handler.headers.get("Sec-WebSocket-Key")
        if not key:
            handler.send_response(400)
            handler.end_headers()
            return
        handler.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        accept = accept_key(key)
        handler.wfile.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n"
                "\r\n"
            ).encode()
        )
        handler.wfile.flush()
        peer = SocketPeer(handler)
        try:
            self._talk(peer)
        finally:
            self._drop(peer)

    def _talk(self, peer: SocketPeer) -> None:
        while True:
            frame = read_frame(peer.handler.rfile)
            if frame is None:
                return
            opcode, data = frame
            if opcode == 8:
                return
            if opcode == 9:
                try:
                    pong = bytearray([0x8A, len(data)])
                    pong.extend(data)
                    with peer._lock:
                        peer.handler.wfile.write(pong)
                        peer.handler.wfile.flush()
                except OSError:
                    return
                continue
            if opcode != 1:
                continue
            try:
                message = json.loads(data.decode())
            except json.JSONDecodeError:
                continue
            if message.get("type") == "register":
                self._register(peer, str(message.get("role") or ""))
                continue
            self._relay(peer, data)

    def _register(self, peer: SocketPeer, role: str) -> None:
        if role not in {"sender", "viewer"}:
            return
        peer.role = role
        with self._lock:
            if role == "sender":
                self.sender = peer
                if self.viewer is not None:
                    peer.send({"type": "viewer-joined"})
            else:
                if self.viewer is not None and self.viewer is not peer:
                    peer.send({"type": "busy"})
                    peer.role = None
                    return
                self.viewer = peer
                sender = self.sender
                peer.send({"type": "server-status", "senderReady": sender is not None})
                if sender is not None:
                    sender.send({"type": "viewer-joined"})

    def _relay(self, peer: SocketPeer, raw: bytes) -> None:
        with self._lock:
            target = self.viewer if peer.role == "sender" else self.sender
        if target is None:
            return
        try:
            text = raw.decode()
            payload = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        target.send(payload)

    def _drop(self, peer: SocketPeer) -> None:
        other = None
        with self._lock:
            if peer.role == "sender" and self.sender is peer:
                self.sender = None
                other = self.viewer
            elif peer.role == "viewer" and self.viewer is peer:
                self.viewer = None
                other = self.sender
        if other is not None:
            other.send({"type": "peer-disconnected"})
