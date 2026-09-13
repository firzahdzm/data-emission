"""Wire format between the tracker and the signer.

One JSON object per line over a unix socket. Kept deliberately small:
the caller names an intent, a coldkey, and the secret that authorises it.
Amounts and destinations are the signer's to decide wherever it can
decide them — a field the caller controls is a field an attacker
controls. Distribution is the one place where it cannot: only a person
knows how much each wallet needs. So that op, and only that op, carries
an amount and a destination, and the signer refuses any destination
outside its own roster of the team's coldkeys.

The wallet secret is the one deliberate exception, and it is a different
kind of field: it does not choose *what* happens, only proves the person
asking is allowed to ask. Carrying it per request is what lets the
deployment keep no wallet secrets on disk at all. It is declared
repr=False so an accidental log of a request object cannot print it.
"""

import json
from dataclasses import dataclass, field

OP_PAY = "pay_tournament"
OP_UNSTAKE = "unstake_all"
# Treasury moves, both restricted to the team's own coldkeys by the
# signer's roster — see the sweep/distribute handling in server.py.
OP_SWEEP = "sweep"
OP_DISTRIBUTE = "distribute"
OPS = (OP_PAY, OP_UNSTAKE, OP_SWEEP, OP_DISTRIBUTE)

TOURNAMENT_TYPES = ("text", "image", "env")


class ProtocolError(Exception):
    """A request that is malformed or asks for something undefined."""


@dataclass(frozen=True)
class SignRequest:
    op: str
    coldkey: str
    types: tuple[str, ...] = field(default=())
    # repr=False: this object gets passed around and could land in a log
    # line or a traceback. The value must not be printable by accident.
    secret: str = field(default="", repr=False)
    # distribute only. A sweep's amount is read from the chain and its
    # destination is the treasury wallet named in the signer's config,
    # so a sweep carrying either is a request that came from somewhere
    # it should not have.
    destination: str = ""
    amount_rao: int = 0

    def to_line(self) -> bytes:
        payload = {"op": self.op, "coldkey": self.coldkey}
        if self.types:
            payload["types"] = list(self.types)
        if self.destination:
            payload["destination"] = self.destination
        if self.amount_rao:
            payload["amount_rao"] = self.amount_rao
        if self.secret:
            payload["secret"] = self.secret
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

        destination = raw.get("destination") or ""
        amount = raw.get("amount_rao") or 0
        if op == OP_DISTRIBUTE:
            if not isinstance(destination, str) or not destination:
                raise ProtocolError("distribute needs a destination")
            # bool is an int in Python, and True would sail through as 1
            # rao. Rejected explicitly rather than relying on the reader
            # to remember that.
            if isinstance(amount, bool) or not isinstance(amount, int) \
                    or amount <= 0:
                raise ProtocolError("distribute needs a positive amount_rao")
        else:
            if destination:
                raise ProtocolError(f"{op} takes no destination")
            if amount:
                raise ProtocolError(f"{op} takes no amount_rao")

        secret = raw.get("secret")
        # Stripped here as well as in the web tier: the signer has to be
        # defensible on its own, and whitespace reaches btcli as an unset
        # variable, which makes it prompt — and --no-prompt turns that into
        # an opaque non-zero exit rather than a legible error.
        if not isinstance(secret, str) or not secret.strip():
            raise ProtocolError("wallet secret is required")

        # Any other key in the payload is dropped here, by construction.
        return cls(
            op=op, coldkey=coldkey, types=tuple(types), secret=secret,
            destination=destination, amount_rao=amount,
        )


@dataclass(frozen=True)
class SignResult:
    ok: bool
    op: str
    coldkey: str
    amount_rao: int = 0
    tx_hash: str | None = None
    error: str | None = None
    # Neither success nor failure: btcli said something we could not read,
    # or timed out after the request may already have reached the chain.
    # Distinct from ok=False because "it failed" invites a retry, and
    # retrying a payment that did go through pays twice.
    unknown: bool = False

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
                    "unknown": self.unknown,
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
        if not isinstance(raw, dict):
            raise ProtocolError("result must be a JSON object")
        return cls(
            ok=bool(raw.get("ok")),
            op=str(raw.get("op") or ""),
            coldkey=str(raw.get("coldkey") or ""),
            amount_rao=int(raw.get("amount_rao") or 0),
            tx_hash=raw.get("tx_hash"),
            error=raw.get("error"),
            unknown=bool(raw.get("unknown")),
        )
