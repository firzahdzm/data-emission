"""Wire format between the tracker and the signer.

One JSON object per line over a unix socket. Kept deliberately small:
the caller names an intent and a coldkey, and nothing else. Amounts and
destinations are the signer's to decide, so they have no place in a
request — a field the caller controls is a field an attacker controls.
"""

import json
from dataclasses import dataclass, field

OP_PAY = "pay_tournament"
OP_UNSTAKE = "unstake_all"
OPS = (OP_PAY, OP_UNSTAKE)

TOURNAMENT_TYPES = ("text", "image", "env")


class ProtocolError(Exception):
    """A request that is malformed or asks for something undefined."""


@dataclass(frozen=True)
class SignRequest:
    op: str
    coldkey: str
    types: tuple[str, ...] = field(default=())

    def to_line(self) -> bytes:
        payload = {"op": self.op, "coldkey": self.coldkey}
        if self.types:
            payload["types"] = list(self.types)
        return (json.dumps(payload) + "\n").encode()

    @classmethod
    def from_line(cls, line: bytes) -> "SignRequest":
        try:
            raw = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed request: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProtocolError("request must be a JSON object")

        op = raw.get("op")
        if op not in OPS:
            raise ProtocolError(f"unknown op {op!r}")

        coldkey = raw.get("coldkey")
        if not isinstance(coldkey, str) or not coldkey:
            raise ProtocolError("coldkey is required")

        types = raw.get("types") or []
        if not isinstance(types, list) or any(not isinstance(t, str) for t in types):
            raise ProtocolError("types must be a list of strings")
        for t in types:
            if t not in TOURNAMENT_TYPES:
                raise ProtocolError(f"unknown tournament type {t!r}")
        if len(set(types)) != len(types):
            raise ProtocolError("duplicate tournament type")
        if op == OP_PAY and not types:
            raise ProtocolError("pay_tournament needs at least one type")

        # Any other key in the payload is dropped here, by construction.
        return cls(op=op, coldkey=coldkey, types=tuple(types))


@dataclass(frozen=True)
class SignResult:
    ok: bool
    op: str
    coldkey: str
    amount_rao: int = 0
    tx_hash: str | None = None
    error: str | None = None

    def to_line(self) -> bytes:
        return (
            json.dumps(
                {
                    "ok": self.ok,
                    "op": self.op,
                    "coldkey": self.coldkey,
                    "amount_rao": self.amount_rao,
                    "tx_hash": self.tx_hash,
                    "error": self.error,
                }
            )
            + "\n"
        ).encode()

    @classmethod
    def from_line(cls, line: bytes) -> "SignResult":
        try:
            raw = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed result: {exc}") from exc
        return cls(
            ok=bool(raw.get("ok")),
            op=str(raw.get("op") or ""),
            coldkey=str(raw.get("coldkey") or ""),
            amount_rao=int(raw.get("amount_rao") or 0),
            tx_hash=raw.get("tx_hash"),
            error=raw.get("error"),
        )
