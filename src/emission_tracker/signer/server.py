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
from dataclasses import dataclass, field
from pathlib import Path

from emission_tracker.signer.btcli import (
    BtcliError,
    base_env,
    coldkey_password_env_var,
    balance_argv,
    free_balance_rao,
    hotkeys_with_stake,
    list_wallets,
    unlisted_stake,
    run_btcli,
    stake_hotkeys_argv,
    TransferUnknown,
    UNSTAKE_PROMPTS,
    parse_transfer_output,
    parse_unstake_output,
    run_btcli_pty,
    strip_ansi,
    transfer_argv,
    unstake_argv,
)
from emission_tracker.signer.protocol import (
    OP_BALANCES,
    OP_DISTRIBUTE,
    OP_PAY,
    OP_SWEEP,
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
# Reading the coldkey's stake is one chain query and needs no password.
STAKE_LIST_TIMEOUT = 90
# One chain read, no password.
BALANCE_TIMEOUT = 60
# A whole unstake request, across every hotkey. Below the web client's
# own timeout on purpose: a caller that gives up first sees a failure
# for work that is still running.
UNSTAKE_BUDGET = 420
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
    # How far the alpha rate may move against us mid-unstake before the
    # chain refuses. Tunable without a deploy because the right value is
    # a property of the subnet's liquidity, not of this code.
    unstake_tolerance: float = 0.15
    # btcli's encrypted-submission shield. Off because its fixed wait
    # kept expiring on this subnet, turning every unstake into an unknown
    # outcome and leaving duplicate extrinsics pending behind each retry.
    # See unstake_argv for the full account.
    mev_protection: bool = False
    # coldkey ss58 -> the hotkeys the team has registered under it. The
    # roster lives here rather than arriving with the request: the
    # signer decides what may be touched, and a caller that could name
    # hotkeys would make that split decorative. Empty means "no roster
    # configured" and falls back to whatever the chain shows.
    hotkeys: dict = field(default_factory=dict)
    # The team's treasury wallet: where a sweep sends, and the only
    # wallet a distribution may spend from. Empty means neither
    # operation is available — a deployment that has not named it fails
    # closed rather than guessing a destination for other people's TAO.
    parent_coldkey: str = ""
    # Left behind by a sweep so the wallet can still pay for its next
    # transaction. Transaction fees come out of the amount sent, so the
    # remainder ends a hair under this.
    sweep_leave_tao: float = 0.015


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
        # Rule for everything below the btcli call: once it has returned
        # for a transfer, the money has moved and the result is `ok`. No
        # later step — parsing, bookkeeping, persistence — may raise its way
        # into `ok=False`, because the caller reads that as "nothing
        # happened" and clicks Pay again. Validation that can fail belongs
        # before the call, not after it.
        try:
            return self._handle(request)
        except TransferUnknown as exc:
            # Loudest log in the service: the money may have moved and
            # nobody knows. Everything else here is recoverable by trying
            # again; this is the one case where trying again can pay twice.
            log.error("op=%s coldkey=%s OUTCOME UNKNOWN — check the chain: %s",
                      request.op, request.coldkey, exc)
            return SignResult(
                False, request.op, request.coldkey,
                error=str(exc), unknown=True,
            )
        except BtcliError as exc:
            log.warning("op=%s coldkey=%s failed: %s",
                        request.op, request.coldkey, exc)
            return SignResult(False, request.op, request.coldkey, error=str(exc))
        except Exception as exc:  # never let the socket loop die
            log.exception("op=%s coldkey=%s crashed", request.op, request.coldkey)
            return SignResult(False, request.op, request.coldkey, error=repr(exc))

    def _handle(self, request: SignRequest) -> SignResult:
        if request.op == OP_BALANCES:
            # Answered before any wallet is resolved: this op names none.
            return SignResult(True, request.op, "", balances=self.balances())

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
            log.info("unstake_all coldkey=%s wallet=%s netuid=%s unlock_len=%d",
                     request.coldkey, name, self._config.netuid,
                     len(request.secret))
            # Same pseudo-terminal as a transfer, and for the same reason:
            # btcli reads the unlock value with getpass, which never sees
            # a pipe. The prompt table differs — see UNSTAKE_PROMPTS.
            # Which hotkeys actually hold stake here is decided before
            # the unstake runs, and each is named once. btcli's own
            # --all-hotkeys queues a hotkey once per subnet it is staked
            # on, then unstakes "all" from each — asking the chain for
            # twice the alpha that exists. See hotkeys_with_stake.
            stake = run_btcli(
                stake_hotkeys_argv(name, self._config.wallet_path),
                env=base_env(), timeout=STAKE_LIST_TIMEOUT, run=self._run,
            )
            roster = self._config.hotkeys.get(request.coldkey) or None
            hotkeys = hotkeys_with_stake(stake, self._config.netuid, roster)
            if roster:
                # Loud, because "Unstake all" did not: a position left
                # behind must never be discovered by surprise later.
                for stray in unlisted_stake(stake, self._config.netuid, roster):
                    log.warning(
                        "coldkey=%s holds stake on %s, which is not in the "
                        "roster — left untouched", request.coldkey, stray,
                    )
            if not hotkeys:
                raise BtcliError(
                    "tidak ada stake untuk di-unstake di subnet ini"
                )
            log.info("unstake_all coldkey=%s wallet=%s hotkeys=%d",
                     request.coldkey, name, len(hotkeys))
            return self._unstake_each(request, name, env, hotkeys)

        if request.op == OP_SWEEP:
            return self._sweep(request, name, env=self._env_for(name, request.secret))

        if request.op == OP_DISTRIBUTE:
            return self._distribute(request, name,
                                    env=self._env_for(name, request.secret))

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

        # Length and shape only — never the value. When btcli rejects an
        # unlock value the operator is sure of, the question is whether
        # what arrived is what they typed; a browser autofilling a saved
        # password over the field looks exactly like a wrong passphrase
        # from here, and the length is enough to tell them apart.
        secret = request.secret
        log.info(
            "pay_tournament coldkey=%s wallet=%s types=%s amount=%s "
            "unlock_len=%d leading_space=%s trailing_space=%s",
            request.coldkey, name, ",".join(request.types), amount_tao,
            len(secret), secret[:1].isspace(), secret[-1:].isspace(),
        )
        # A pseudo-terminal, not a pipe: btcli reads its password through
        # getpass, which never sees piped stdin. See run_btcli_pty.
        output = self._run_transfer(
            transfer_argv(name, self._config.destination, amount_tao,
                          self._config.wallet_path),
            env, request.secret,
        )
        tx_hash = parse_transfer_output(output)
        self._record_spend(amount_rao)
        return SignResult(True, request.op, request.coldkey,
                          amount_rao=amount_rao, tx_hash=tx_hash)

    def _roster(self) -> set:
        return set(self._config.hotkeys or {})

    def _free_rao(self, wallet_name: str) -> int | None:
        payload = run_btcli(
            balance_argv(wallet_name, self._config.wallet_path),
            env=base_env(), timeout=BALANCE_TIMEOUT, run=self._run,
        )
        return free_balance_rao(payload, wallet_name)

    def balances(self) -> dict:
        """Free balance per rostered coldkey, straight from the chain.

        Signs nothing and takes no unlock value: it exists so the sweep
        and distribute dialogs can show figures fresher than the
        dashboard's once-a-day TaoStats read. A wallet that cannot be
        read comes back as None, never as zero.
        """
        wallets = list_wallets(run=self._run, wallet_path=self._config.wallet_path)
        out = {}
        for coldkey in self._roster():
            name = wallets.get(coldkey)
            out[coldkey] = self._free_rao(name) if name else None
        return out

    def _sweep(self, request, name, env) -> SignResult:
        parent = self._config.parent_coldkey
        if not parent:
            log.warning("refused sweep: no parent_coldkey configured")
            return SignResult(False, request.op, request.coldkey,
                              error="wallet induk belum dikonfigurasi di signer")
        if request.coldkey == parent:
            return SignResult(False, request.op, request.coldkey,
                              error="wallet induk tidak menyapu dirinya sendiri")
        if request.coldkey not in self._roster():
            log.warning("refused sweep: %s is not in the roster", request.coldkey)
            return SignResult(False, request.op, request.coldkey,
                              error="coldkey tidak ada di roster")

        free = self._free_rao(name)
        if free is None:
            return SignResult(False, request.op, request.coldkey,
                              error="saldo wallet tidak terbaca")
        leave = round(self._config.sweep_leave_tao * RAO)
        amount_rao = free - leave
        if amount_rao <= 0:
            # Not an error and not unknown: there is nothing worth
            # moving, and sweeping would spend a fee to shift dust.
            return SignResult(
                False, request.op, request.coldkey,
                error=f"saldo {free / RAO:.4f} τ di bawah ambang "
                      f"{self._config.sweep_leave_tao} τ",
            )

        log.info("sweep coldkey=%s wallet=%s free=%s amount=%s unlock_len=%d",
                 request.coldkey, name, free, amount_rao, len(request.secret))
        return self._send(request, name, env, parent, amount_rao)

    def _distribute(self, request, name, env) -> SignResult:
        parent = self._config.parent_coldkey
        if not parent:
            log.warning("refused distribute: no parent_coldkey configured")
            return SignResult(False, request.op, request.coldkey,
                              error="wallet induk belum dikonfigurasi di signer")
        if request.coldkey != parent:
            # Only the treasury spends here. A member wallet paying on
            # another member's behalf is not a flow this has, and
            # allowing it would let one forged request reach any wallet
            # rather than only the one the operator chose.
            log.warning("refused distribute: %s is not the treasury",
                        request.coldkey)
            return SignResult(False, request.op, request.coldkey,
                              error="hanya wallet induk yang boleh distribusi")
        if request.destination == parent:
            return SignResult(False, request.op, request.coldkey,
                              error="tujuan sama dengan wallet induk")
        if request.destination not in self._roster():
            # This is what stands in for an amount cap. With it, the
            # worst a compromised web tier can do is shuffle money
            # between the team's own wallets.
            log.warning("refused distribute: destination %s is not in the roster",
                        request.destination)
            return SignResult(False, request.op, request.coldkey,
                              error="tujuan tidak ada di roster")

        log.info("distribute from=%s to=%s amount=%s unlock_len=%d",
                 request.coldkey, request.destination, request.amount_rao,
                 len(request.secret))
        return self._send(request, name, env, request.destination,
                          request.amount_rao)

    def _send(self, request, name, env, destination, amount_rao) -> SignResult:
        """The transfer itself, shared by both treasury operations.

        Same pseudo-terminal path as a tournament payment: btcli reads
        the unlock value with getpass, which never sees a pipe.
        """
        output = self._run_transfer(
            transfer_argv(name, destination, amount_rao / RAO,
                          self._config.wallet_path),
            env, request.secret,
        )
        tx_hash = parse_transfer_output(output)
        return SignResult(True, request.op, request.coldkey,
                          amount_rao=amount_rao, tx_hash=tx_hash)

    def _run_transfer(self, argv, env, secret) -> str:
        """Seam for tests, which must never spawn a real pty."""
        return run_btcli_pty(argv, env, TRANSFER_TIMEOUT, secret)

    def _unstake_each(self, request, name, env, hotkeys) -> SignResult:
        """One btcli run per hotkey, not one batch over all of them.

        btcli submits multiple operations as a single Utility.batch_all,
        which is atomic: on the shared wallet — eleven hotkeys holding
        stake — one refusal takes the other ten down with it, and
        `ReservesTooLow` is exactly the kind of refusal a large combined
        unstake provokes. Eleven separate runs also stay well inside
        their timeouts, where one combined run does not: a request that
        times out mid-batch is the one outcome nobody can act on.

        The cost is that a run can end partly done. That is reported as
        such — never as a plain success, and never as a plain failure
        while some of it went through.
        """
        done, failed, unknown = [], [], []
        deadline = time.monotonic() + UNSTAKE_BUDGET

        for hotkey in hotkeys:
            if time.monotonic() > deadline:
                failed.append((hotkey, "kehabisan waktu sebelum dicoba"))
                continue
            argv = unstake_argv(
                name, self._config.netuid, self._config.wallet_path, [hotkey],
                tolerance=self._config.unstake_tolerance,
                mev_protection=self._config.mev_protection,
            )
            output = ""
            try:
                output = self._run_unstake(argv, env, request.secret)
                done.append((hotkey, parse_unstake_output(output)))
            except (TransferUnknown, BtcliError) as exc:
                if output:
                    # The whole exchange, once, when it did not plainly
                    # work. The stored reason is a single clamped
                    # sentence, and several wrong diagnoses in a row came
                    # from reasoning about a failure nobody had the
                    # transcript of. btcli never echoes the unlock value,
                    # and journald is the signer's own trust boundary.
                    log.error("unstake transcript coldkey=%s hotkey=%s:\n%s",
                              request.coldkey, hotkey, strip_ansi(output)[-4000:])
                if isinstance(exc, TransferUnknown):
                    unknown.append((hotkey, str(exc)))
                else:
                    failed.append((hotkey, str(exc)))
                    if "unlock value" in str(exc):
                        # Every remaining hotkey would fail the same way,
                        # each one another wrong-password round trip.
                        log.warning("stopping: the unlock value was rejected")
                        break

        for hotkey, reason in failed + unknown:
            log.warning("unstake coldkey=%s hotkey=%s: %s",
                        request.coldkey, hotkey, reason)

        summary = _summarise(len(hotkeys), done, failed, unknown)
        refs = ",".join(ref for _, ref in done if ref)[:200] or None
        return SignResult(
            bool(done), request.op, request.coldkey,
            tx_hash=refs,
            error=None if (done and not failed and not unknown) else summary,
            # Only when nothing succeeded and something is genuinely
            # unresolved: a run with successes must not read as "check
            # the chain before touching anything", or the successes get
            # retried too.
            unknown=bool(unknown) and not done,
        )

    def _run_unstake(self, argv, env, secret) -> str:
        """Seam for tests, which must never spawn a real pty."""
        return run_btcli_pty(argv, env, UNSTAKE_TIMEOUT, secret,
                             prompts=UNSTAKE_PROMPTS)

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
        """The environment for a signing call — deliberately without the
        unlock value.

        BT_PW_* is not merely useless here, it is actively harmful. btcli
        9.23 treats the variable's presence as "a password is already
        available" and therefore never prompts, then fails to decrypt with
        it anyway: "Coldkey Keyfile is corrupt". Setting it closed the one
        channel that does work. Captured from a real run:

            Proceed with transfer? [y/n] (n): ❌ Failed: Coldkey Keyfile is
            corrupt, non-writable, or non-readable, or non-existent.

        — the confirmation answered, and no password prompt at all. With
        the variable absent, btcli asks, and the answer goes in on stdin.

        `wallet_name` and `secret` stay in the signature: both are part of
        what a caller must supply to sign, and dropping them would make
        this look like a generic environment rather than a deliberate
        omission.
        """
        return base_env()


def _summarise(total: int, done, failed, unknown) -> str:
    """What happened, per hotkey, in one line the card can show."""
    parts = [f"{len(done)}/{total} hotkey berhasil"]
    if failed:
        parts.append(f"{len(failed)} gagal ({failed[0][1]})")
    if unknown:
        parts.append(f"{len(unknown)} tidak pasti — periksa chain")
    return "; ".join(parts)


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
