"""The only component that can sign.

It accepts two intents and decides every consequential value itself: the
destination, the amount, the subnet, the slippage guard. A caller that
could name any of those would make the privilege split decorative.
"""

import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from emission_tracker.signer.btcli import (
    BtcliError,
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
BTCLI_TIMEOUT = 300


@dataclass
class SignerConfig:
    destination: str
    fees_tao: dict
    netuid: int
    wallet_path: str
    max_transfer_tao: float
    daily_cap_tao: float
    credentials_dir: str


@dataclass
class _DaySpend:
    day: str = ""
    rao: int = 0


class Signer:
    def __init__(self, config: SignerConfig, run=subprocess.run, clock=time.time):
        self._config = config
        self._run = run
        self._clock = clock
        self._spent = _DaySpend()

    def handle(self, request: SignRequest) -> SignResult:
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
            return SignResult(
                False, request.op, request.coldkey,
                error=f"unknown coldkey {request.coldkey!r}",
            )

        env = self._env_for(name)

        if request.op == OP_UNSTAKE:
            # Log before attempting: the audit trail must survive a crash
            # between here and the chain.
            log.info("unstake_all coldkey=%s wallet=%s", request.coldkey, name)
            payload = run_btcli(
                unstake_argv(name, self._config.netuid, self._config.wallet_path),
                env=env, timeout=BTCLI_TIMEOUT, run=self._run,
            )
            return SignResult(True, request.op, request.coldkey,
                              tx_hash=_tx_hash(payload))

        amount_rao = sum(
            round(self._config.fees_tao[t] * RAO) for t in request.types
        )
        amount_tao = amount_rao / RAO

        if amount_tao > self._config.max_transfer_tao:
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"{amount_tao} τ exceeds the per-request cap "
                      f"({self._config.max_transfer_tao} τ)",
            )
        if not self._within_daily_cap(amount_rao):
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"daily cap of {self._config.daily_cap_tao} τ would be exceeded",
            )

        log.info("pay_tournament coldkey=%s wallet=%s types=%s amount=%s",
                 request.coldkey, name, ",".join(request.types), amount_tao)
        payload = run_btcli(
            transfer_argv(name, self._config.destination, amount_tao,
                          self._config.wallet_path),
            env=env, timeout=BTCLI_TIMEOUT, run=self._run,
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

    def _passphrase_for(self, wallet_name: str) -> str:
        """Read one passphrase from the systemd credentials directory.

        systemd puts each LoadCredential= item in its own file under
        $CREDENTIALS_DIRECTORY, readable only by this unit. Keeping them
        there instead of the environment means they never show up in
        /proc/<pid>/environ or `systemctl show`.
        """
        path = Path(self._config.credentials_dir) / f"wallet-{wallet_name}"
        return path.read_text().strip()

    def _env_for(self, wallet_name: str) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "BT_WALLET_PASSWORD": self._passphrase_for(wallet_name),
        }
        return env


def _tx_hash(payload: dict) -> str | None:
    for key in ("tx_hash", "transaction_hash", "extrinsic_hash", "hash"):
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
    server.bind(socket_path)
    os.chmod(socket_path, 0o660)
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
