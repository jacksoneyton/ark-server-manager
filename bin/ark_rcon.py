"""
ark_rcon.py
-----------
Minimal Source RCON protocol client.

ARK: Survival Evolved (and Source-engine games, and Minecraft, which borrowed
the same wire format) all speak this protocol: a length-prefixed binary TCP
packet carrying a request id, a packet type, and a null-terminated body.

Implemented from scratch (rather than pulling in a third-party rcon package)
since this client handles the server's admin password.

Packet layout (little-endian):
  int32 size  (byte count of everything after this field)
  int32 id    (echoed back by the server, used to pair requests/responses)
  int32 type  (3 = auth, 2 = exec command, 0 = response / auth response)
  bytes body  (null-terminated)
  byte  pad   (extra null terminator required by the protocol)
"""

import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0


class RconError(Exception):
    """Raised on connection, auth, or protocol failures."""


class ArkRcon:
    def __init__(self, host, port, password, timeout=5):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._next_id = 1

    def connect(self):
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except OSError as exc:
            raise RconError(f"Could not connect to {self.host}:{self.port}: {exc}")
        self._authenticate()

    def _authenticate(self):
        req_id = self._send(SERVERDATA_AUTH, self.password)
        packet_id, ptype, _body = self._recv_packet()
        # Some servers send an empty SERVERDATA_RESPONSE_VALUE before the
        # actual auth response; drain it if present.
        if ptype == SERVERDATA_RESPONSE_VALUE:
            packet_id, ptype, _body = self._recv_packet()
        if packet_id != req_id or packet_id == -1:
            raise RconError("RCON authentication failed (bad password?)")

    def command(self, cmd):
        req_id = self._send(SERVERDATA_EXECCOMMAND, cmd)
        _packet_id, _ptype, body = self._recv_packet()
        return body.decode("utf-8", errors="replace")

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    # ------------------------------------------------------------------ wire

    def _send(self, ptype, body):
        if self._sock is None:
            raise RconError("Not connected")
        req_id = self._next_id
        self._next_id += 1
        payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
        packet = struct.pack("<i", len(payload)) + payload
        try:
            self._sock.sendall(packet)
        except OSError as exc:
            raise RconError(f"Send failed: {exc}")
        return req_id

    def _recv_packet(self):
        size_bytes = self._recv_exact(4)
        (size,) = struct.unpack("<i", size_bytes)
        data = self._recv_exact(size)
        packet_id, ptype = struct.unpack("<ii", data[:8])
        body = data[8:-2]  # strip the two null terminators
        return packet_id, ptype, body

    def _recv_exact(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            try:
                chunk = self._sock.recv(remaining)
            except OSError as exc:
                raise RconError(f"Recv failed: {exc}")
            if not chunk:
                raise RconError("Connection closed by server")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
