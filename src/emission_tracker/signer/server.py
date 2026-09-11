"""The only component that can sign.

It accepts two intents and decides every consequential value itself: the
destination, the amount, the subnet, the slippage guard. A caller that
could name any of those would make the privilege split decorative.
"""

import json
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from emission_tracker.signer.btcli import (
    BtcliError,
    base_env,
    coldkey_password_env_var,
    list_wallets,
    run_btcli,
    transfer_argv,
    unstake_argv,
)
from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    ProtocolError,
    SignRequest,
    SignResult,
)

log = logging.getLogger("emission_signer")

RAO = 10**9
# Measured on the target host: a refused transfer returns in ~1.3s and a
# wallet list in ~0.4s. 300s was chosen with no evidence and cost us a
# four-minute hang that blocked every other wallet, because the signer
# handles one request at a time. These are generous against the observed
# numbers while keeping a stuck call from holding the queue for minutes.
TRANSFER_TIMEOUT = 90
# Unstake can wait on chain submission and slippage checks, so it gets
# more room than a transfer — but still far less than five minutes.
UNSTAKE_TIMEOUT = 180
BTCLI_TIMEOUT = UNSTAKE_TIMEOUT


@dataclass
class SignerConfig:
    destination: str
    fees_tao: dict
    netuid: int
    wallet_path: str
    max_transfer_tao: float
    daily_cap_tao: float
    state_path: str = "/var/lib/emission-signer/spend.json"


@dataclass
class _DaySpend:
    day: str = ""
    rao: int = 0


class Signer:
    def __init__(self, config: SignerConfig, run=subprocess.run, clock=time.time):
        self._config = config
        self._run = run
        self._clock = clock
        self._spent = self._load_spend()

    def handle(self, request: SignRequest) -> SignResult:
        # Rule for everything below run_btcli(): once run_btcli has returned
        # for a transfer, the money has moved and the result is `ok`. No
        # later step — parsing, bookkeeping, persistence — may raise its way
        # into `ok=False`, because the caller reads that as "nothing
        # happened" and clicks Pay again. Validation that can fail belongs
        # before the call, not after it.
        try:
            return self._handle(request)
        except BtcliError as exc:
            log.warning("op=%s coldkey=%s failed: %s",
                        request.op, request.coldkey, exc)
            return SignResult(False, request.op, request.coldkey, error=str(exc))
        except Exception as exc:  # never let the socket loop die
            log.exception("op=%s coldkey=%s crashed", request.op, request.coldkey)
            return SignResult(False, request.op, request.coldkey, error=repr(exc))

    def _handle(self, request: SignRequest) -> SignResult:
        wallets = list_wallets(run=self._run, wallet_path=self._config.wallet_path)
        name = wallets.get(request.coldkey)
        if name is None:
            # Logged, not just returned: the signer's log is the only audit
            # trail that survives the web app being compromised, and a
            # dashboard probing for signable coldkeys must leave a mark.
            log.warning("refused: unknown coldkey %s (op=%s)",
                        request.coldkey, request.op)
            return SignResult(
                False, request.op, request.coldkey,
                error=f"unknown coldkey {request.coldkey!r}",
            )

        if request.op == OP_UNSTAKE:
            env = self._env_for(name, request.secret)
            # Log before attempting: the audit trail must survive a crash
            # between here and the chain.
            log.info("unstake_all coldkey=%s wallet=%s", request.coldkey, name)
            payload = run_btcli(
                unstake_argv(name, self._config.netuid, self._config.wallet_path),
                env=env, timeout=UNSTAKE_TIMEOUT, run=self._run,
            )
            return SignResult(True, request.op, request.coldkey,
                              tx_hash=_tx_hash(payload))

        amount_rao = sum(
            round(self._config.fees_tao[t] * RAO) for t in request.types
        )
        amount_tao = amount_rao / RAO

        # Caps are checked before the environment is built: a refused request
        # has no business copying the unlock value anywhere.
        if amount_tao > self._config.max_transfer_tao:
            log.warning(
                "refused: coldkey=%s wallet=%s amount=%s τ exceeds the "
                "per-request cap (%s τ)",
                request.coldkey, name, amount_tao, self._config.max_transfer_tao,
            )
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"{amount_tao} τ exceeds the per-request cap "
                      f"({self._config.max_transfer_tao} τ)",
            )
        if not self._within_daily_cap(amount_rao):
            log.warning(
                "refused: coldkey=%s wallet=%s amount=%s τ would exceed the "
                "daily cap (%s τ)",
                request.coldkey, name, amount_tao, self._config.daily_cap_tao,
            )
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"daily cap of {self._config.daily_cap_tao} τ would be exceeded",
            )

        env = self._env_for(name, request.secret)

        log.info("pay_tournament coldkey=%s wallet=%s types=%s amount=%s",
                 request.coldkey, name, ",".join(request.types), amount_tao)
        payload = run_btcli(
            transfer_argv(name, self._config.destination, amount_tao,
                          self._config.wallet_path),
            env=env, timeout=TRANSFER_TIMEOUT, run=self._run,
        )
        self._record_spend(amount_rao)
        return SignResult(True, request.op, request.coldkey,
                          amount_rao=amount_rao, tx_hash=_tx_hash(payload))

    def _within_daily_cap(self, amount_rao: int) -> bool:
        today = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        if self._spent.day != today:
            return amount_rao <= round(self._config.daily_cap_tao * RAO)
        return self._spent.rao + amount_rao <= round(self._config.daily_cap_tao * RAO)

    def _record_spend(self, amount_rao: int) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime(self._clock()))
        if self._spent.day != today:
            self._spent = _DaySpend(day=today, rao=0)
        self._spent.rao += amount_rao
        self._save_spend()

    def _load_spend(self) -> "_DaySpend":
        """Restore today's running total so a crash or deploy can't zero
        the daily cap. A missing or unreadable file just means nothing has
        been spent today yet — it must never stop the service starting.
        """
        try:
            raw = json.loads(Path(self._config.state_path).read_text())
            return _DaySpend(day=str(raw["day"]), rao=int(raw["rao"]))
        except FileNotFoundError:
            return _DaySpend()
        except Exception as exc:
            log.warning("could not read spend state %s: %s",
                        self._config.state_path, exc)
            return _DaySpend()

    def _save_spend(self) -> None:
        """Best-effort. The transfer has already gone through by the time
        this runs, so a write failure here must never turn a successful
        payment into a reported failure — that would push the caller
        straight into paying twice. Losing the on-disk counter for a
        restart is a far smaller problem than that.
        """
        path = Path(self._config.state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"day": self._spent.day, "rao": self._spent.rao}))
        except Exception as exc:
            log.warning("could not persist spend state to %s: %s", path, exc)

    def _env_for(self, wallet_name: str, secret: str) -> dict:
        env = {
            # Same base as every other btcli call — see base_env() for why
            # PATH here is load-bearing rather than decoration.
            **base_env(),
            # The env var name btcli/bittensor_wallet reads the coldkey
            # passphrase from is derived from the coldkey keyfile path (see
            # coldkey_password_env_var), not a fixed name — getting it wrong
            # makes btcli fall back to a prompt, which --no-prompt turns
            # into a non-zero exit — every button fails. Verify it against
            # the installed bittensor before going live: see "Verify the
            # passphrase environment variable" in deploy/DEPLOY.md.
            #
            # The value arrives with the request and lives only for this
            # subprocess. Nothing is kept on disk, so a host compromise
            # yields the encrypted keyfiles and nothing to open them with.
            coldkey_password_env_var(self._config.wallet_path, wallet_name):
                secret,
        }
        return env


def _tx_hash(payload: dict) -> str | None:
    # extrinsic_identifier is what btcli 9.23 actually returns; the rest are
    # kept for other versions.
    for key in ("extrinsic_identifier", "tx_hash", "transaction_hash",
                "extrinsic_hash", "hash"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def serve(socket_path: str, signer: Signer) -> None:
    """One request per connection, answered and closed.

    Socket permissions are the access control: 0660 owned by the signer
    user and the tracker's group means only the tracker can connect. There
    is no authentication inside the protocol because there is no one else
    who can reach it.
    """
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Create the socket file at 0660 atomically: between bind() and a
    # later chmod() it would otherwise sit on disk at the umask's mode.
    old_umask = os.umask(0o117)
    try:
        server.bind(socket_path)
    finally:
        os.umask(old_umask)
    os.chmod(socket_path, 0o660)  # belt-and-braces
    server.listen(4)
    log.info("listening on %s", socket_path)

    while True:
        conn, _ = server.accept()
        try:
            conn.settimeout(BTCLI_TIMEOUT + 30)
            line = b""
            while not line.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    break
                line += chunk
                if len(line) > 8192:
                    raise ProtocolError("request too large")
            if not line:
                continue
            try:
                request = SignRequest.from_line(line)
            except ProtocolError as exc:
                conn.sendall(
                    SignResult(False, "", "", error=str(exc)).to_line()
                )
                continue
            conn.sendall(signer.handle(request).to_line())
        except Exception:
            log.exception("connection failed")
        finally:
            conn.close()
