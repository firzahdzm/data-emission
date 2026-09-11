"""Talks to the signer over its unix socket.

Deliberately thin: one connection per request, no retry. A money-moving
call that retries itself turns one intended payment into several, and the
caller cannot tell whether a timed-out transfer landed.
"""

import socket

from emission_tracker.signer.protocol import ProtocolError, SignRequest, SignResult


class SignerUnavailable(Exception):
    """The signer could not be reached or gave an unreadable answer."""


class SignerClient:
    def __init__(self, socket_path: str, timeout: float = 300.0):
        self._socket_path = socket_path
        self._timeout = timeout

    def send(self, request: SignRequest) -> SignResult:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(self._socket_path)
                sock.sendall(request.to_line())
                line = b""
                while not line.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    line += chunk
        except OSError as exc:
            raise SignerUnavailable(f"signer unreachable: {exc}") from exc
        if not line:
            raise SignerUnavailable("signer closed without answering")
        try:
            return SignResult.from_line(line)
        except ProtocolError as exc:
            raise SignerUnavailable(str(exc)) from exc
