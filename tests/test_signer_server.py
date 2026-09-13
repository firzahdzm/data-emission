import json

import pytest

from emission_tracker.signer.btcli import coldkey_password_env_var
from emission_tracker.signer.protocol import (
    OP_DISTRIBUTE,
    OP_PAY,
    OP_SWEEP,
    OP_UNSTAKE,
    SignRequest,
)
from emission_tracker.signer.server import Signer, SignerConfig

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"
DEST = "5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo"
WALLETS = {"wallets": [{"name": "prj1", "ss58_address": CK, "hotkeys": []}]}
HK = "5GcAxvH7oAr9hT2bauAU8y2B8GUiZbvmwrrnj1B4op9toCnY"
# One hotkey, staked on two subnets — the shape that made btcli's own
# --all-hotkeys ask the chain to remove the same alpha twice.
BALANCE = {"balances": {"prj1": {"free": 2.0}}}
STAKE = {"stake_info": {HK: [
    {"netuid": 56, "stake_value": 102.7587},
    {"netuid": 24, "stake_value": 0.0087},
]}}

# Obvious dummy. The unlock value now travels with each request instead of
# being read from disk, so every request in these tests carries one.
UNLOCK = "dummy-unlock-value"

# A real successful transfer's output, captured from btcli 9.23.2.
TRANSFER_OK = (
    "Initiating transfer on network: finney\n"
    "Proceed with transfer? [y/n] (n): y\n"
    "Enter your password: Decrypting...\n"
    "✅ Finalized. Block Hash: "
    "0x14fa5dba1c7a4c048cdc979c5b4f0ddbd75e9440620adf9326bf80498c7d873f\n"
    "✅ Your extrinsic has been included as 9044902-6\n"
)


# A batch unstake's success line, captured from btcli 9.23.2. Note the
# lower-case "finalized" — the transfer's marker would miss it.
UNSTAKE_OK = (
    "✅ Batch finalized. Unstaked across 3 operations.\n"
    "Balance:\n  0.4775 τ ➡ 12.9910 τ\n"
)


def _req(op, coldkey, types=()):
    return SignRequest(op, coldkey, types, secret=UNLOCK)


class _Recorder:
    """Stands in for subprocess.run and records every argv it is given."""

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self._results = results or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))

        class R:
            returncode = 0
            stderr = ""

        if argv[1:3] == ["wallet", "list"]:
            R.stdout = json.dumps(self._results.get("wallets", WALLETS))
        elif argv[1:3] == ["wallet", "balance"]:
            R.stdout = json.dumps(self._results.get("balance", BALANCE))
        elif argv[1:3] == ["stake", "list"]:
            R.stdout = json.dumps(self._results.get("stake", STAKE))
        elif "transfer" in argv:
            # Shaped like a real transfer on btcli 9.23.2 — prose, not
            # JSON, because btcli refuses --json-output alongside the
            # password prompt.
            R.stdout = self._results.get("transfer", TRANSFER_OK)
        else:
            R.stdout = json.dumps(self._results.get("result", {"success": True}))
        return R()


def _config(tmp_path, **over):
    base = dict(
        destination=DEST,
        fees_tao={"text": 0.7, "image": 0.4, "env": 0.6},
        netuid=56,
        wallet_path="/root/.bittensor/wallets",
        max_transfer_tao=2.0,
        daily_cap_tao=30.0,
        state_path=str(tmp_path / "spend.json"),
    )
    base.update(over)
    return SignerConfig(**base)


def _signer(run, tmp_path, **over):
    s = Signer(_config(tmp_path, **over), run=run)

    # Transfers go through a pseudo-terminal in production, because btcli
    # reads its password with getpass and never sees a pipe. Tests must
    # not spawn one — they stub the seam and still record the argv, so
    # the assertions about destination, amount and flags keep working.
    def _fake_transfer(argv, env, secret):
        # `run` is a bare function in the tests that exercise a failing
        # btcli; route through it so those still see the failure.
        result = run(argv)
        if getattr(result, "returncode", 0) != 0:
            from emission_tracker.signer.btcli import BtcliError

            raise BtcliError(
                f"btcli exited {result.returncode}: {result.stderr}"
            )
        return getattr(run, "transfer_output", TRANSFER_OK)

    s._run_transfer = _fake_transfer

    def _fake_unstake(argv, env, secret):
        result = run(argv)
        if getattr(result, "returncode", 0) != 0:
            from emission_tracker.signer.btcli import BtcliError

            raise BtcliError(f"btcli exited {result.returncode}: {result.stderr}")
        return getattr(run, "unstake_output", UNSTAKE_OK)

    s._run_unstake = _fake_unstake
    return s


def test_the_environment_carries_no_unlock_value_at_all(tmp_path):
    """Setting BT_PW_* is worse than useless: btcli 9.23 reads its presence
    as "a password is available", skips the prompt, and then fails to
    decrypt with it — "Coldkey Keyfile is corrupt", with no password prompt
    in the exchange. It closed the only channel that works. The value goes
    in on stdin instead."""
    s = _signer(_Recorder(), tmp_path)
    env = s._env_for("prj1", UNLOCK)

    assert set(env) == {"PATH", "HOME", "TERM", "NO_COLOR"}
    assert UNLOCK not in "".join(env.values())
    assert coldkey_password_env_var("/root/.bittensor/wallets", "prj1") not in env
    assert "BT_WALLET_PASSWORD" not in env


def test_the_unlock_value_reaches_only_the_environment_never_the_argv(tmp_path):
    """btcli reads it from the environment. An argv carrying it would be
    visible to every other process on the host via /proc."""
    rec = _Recorder()
    _signer(rec, tmp_path).handle(_req(OP_PAY, CK, ("text",)))
    for argv in rec.calls:
        assert UNLOCK not in argv
        assert not any(UNLOCK in str(part) for part in argv)


def test_payment_uses_the_signers_own_fee_table(tmp_path):
    rec = _Recorder()
    res = _signer(rec, tmp_path).handle(_req(OP_PAY, CK, ("text", "env")))
    assert res.ok
    # 0.7 + 0.6, computed here — the caller only named the types.
    assert res.amount_rao == 1_300_000_000
    transfer = [c for c in rec.calls if "transfer" in c][0]
    assert transfer[transfer.index("--amount") + 1] == "1.300000000"


def test_destination_is_the_configured_one_and_cannot_be_influenced(tmp_path):
    rec = _Recorder()
    _signer(rec, tmp_path).handle(_req(OP_PAY, CK, ("text",)))
    transfer = [c for c in rec.calls if "transfer" in c][0]
    assert transfer[transfer.index("--destination") + 1] == DEST


def test_unknown_coldkey_is_refused_before_any_btcli_call(tmp_path):
    rec = _Recorder()
    res = _signer(rec, tmp_path).handle(_req(OP_PAY, "5NOTMINE", ("text",)))
    assert not res.ok
    assert "unknown coldkey" in res.error.lower()
    assert not any("transfer" in c for c in rec.calls)


def test_amount_over_the_per_request_cap_is_refused(tmp_path):
    """A bug that asks for everything must hit a wall in the signer."""
    rec = _Recorder()
    res = _signer(rec, tmp_path, max_transfer_tao=1.0).handle(
        _req(OP_PAY, CK, ("text", "image", "env"))  # 1.7
    )
    assert not res.ok
    assert "cap" in res.error.lower()
    assert not any("transfer" in c for c in rec.calls)


def test_daily_cap_stops_the_second_run(tmp_path):
    rec = _Recorder()
    signer = _signer(rec, tmp_path, daily_cap_tao=1.0)
    first = signer.handle(_req(OP_PAY, CK, ("text",)))   # 0.7
    second = signer.handle(_req(OP_PAY, CK, ("env",)))   # 0.6 → 1.3 total
    assert first.ok
    assert not second.ok
    assert "daily" in second.error.lower()


def test_unstake_resolves_the_wallet_and_passes_the_guard(tmp_path):
    rec = _Recorder()
    res = _signer(rec, tmp_path).handle(_req(OP_UNSTAKE, CK))
    assert res.ok
    unstake = [c for c in rec.calls if "remove" in c][0]
    assert unstake[unstake.index("--wallet-name") + 1] == "prj1"
    assert "--safe-staking" in unstake
    assert unstake[unstake.index("--netuid") + 1] == "56"
    # The scoped path, not --unstake-all, which in btcli 9.23 ignores
    # --netuid entirely and would empty every subnet.
    assert "--unstake-all" not in unstake


def test_an_unstake_whose_outcome_is_unreadable_is_reported_as_unknown(tmp_path):
    """Never "failed": a second click on a hidden success unstakes again."""
    rec = _Recorder()
    rec.unstake_output = "some output that says neither one thing nor the other"
    res = _signer(rec, tmp_path).handle(_req(OP_UNSTAKE, CK))
    assert not res.ok
    assert res.unknown


def test_btcli_failure_comes_back_as_a_failed_result_not_an_exception(tmp_path):
    """The socket loop must answer every request; a crash would leave the
    tracker's row stuck in `pending` forever."""

    def failing(argv, **kwargs):
        class R:
            returncode = 0 if argv[1:3] == ["wallet", "list"] else 1
            stdout = json.dumps(WALLETS)
            stderr = "insufficient balance"

        return R()

    res = _signer(failing, tmp_path).handle(_req(OP_PAY, CK, ("text",)))
    assert not res.ok
    assert "insufficient balance" in res.error


def test_daily_spend_survives_a_restart(tmp_path):
    """The regression the finding is about: a fresh Signer over the same
    state_path must still see spend recorded by a previous instance."""
    rec = _Recorder()
    state_path = str(tmp_path / "spend.json")
    first = _signer(rec, tmp_path, daily_cap_tao=1.0, state_path=state_path)
    res1 = first.handle(_req(OP_PAY, CK, ("text",)))  # 0.7
    assert res1.ok

    # Simulate a crash/restart: a brand new Signer, same state_path.
    second = _signer(rec, tmp_path, daily_cap_tao=1.0, state_path=state_path)
    res2 = second.handle(_req(OP_PAY, CK, ("env",)))  # 0.6 → 1.3 total
    assert not res2.ok
    assert "daily" in res2.error.lower()


def test_state_file_from_a_previous_day_does_not_count(tmp_path):
    state_path = tmp_path / "spend.json"
    state_path.write_text(json.dumps({"day": "2000-01-01", "rao": 999_000_000_000}))
    rec = _Recorder()
    signer = _signer(rec, tmp_path, daily_cap_tao=1.0, state_path=str(state_path))
    res = signer.handle(_req(OP_PAY, CK, ("text",)))  # 0.7, well under 1.0
    assert res.ok


def test_corrupt_state_file_starts_clean_without_raising(tmp_path):
    state_path = tmp_path / "spend.json"
    state_path.write_text("not json")
    rec = _Recorder()
    signer = _signer(rec, tmp_path, daily_cap_tao=1.0, state_path=str(state_path))
    res = signer.handle(_req(OP_PAY, CK, ("text",)))  # 0.7, under 1.0
    assert res.ok


def test_unwritable_state_path_does_not_turn_a_success_into_a_failure(tmp_path):
    """The transfer already happened by the time _save_spend() runs. A disk
    error there must never be reported back as a failed payment — that's
    the fast path to double-paying a tournament fee."""
    rec = _Recorder()
    # Point state_path at the tmp_path directory itself: writing a file
    # there fails because it is a directory, not a file.
    signer = _signer(rec, tmp_path, state_path=str(tmp_path))
    res = signer.handle(_req(OP_PAY, CK, ("text",)))
    assert res.ok
    assert res.amount_rao == 700_000_000
    # The in-memory counter still advanced, so the cap keeps working for
    # the rest of the process's life even though nothing landed on disk.
    assert signer._spent.rao == 700_000_000


def test_refusals_are_logged_so_grinding_leaves_a_trail(caplog, tmp_path):
    """The signer's log is the only audit trail that survives the web app
    being compromised — and the caps exist precisely for a compromised
    caller, so refusing one silently destroys the evidence."""
    rec = _Recorder()
    with caplog.at_level("WARNING", logger="emission_signer"):
        _signer(rec, tmp_path).handle(_req(OP_PAY, "5NOTMINE", ("text",)))
        _signer(rec, tmp_path, max_transfer_tao=1.0).handle(
            _req(OP_PAY, CK, ("text", "image", "env"))
        )
        signer = _signer(rec, tmp_path, daily_cap_tao=1.0)
        signer.handle(_req(OP_PAY, CK, ("text",)))
        signer.handle(_req(OP_PAY, CK, ("env",)))

    messages = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown coldkey" in m and "5NOTMINE" in m for m in messages)
    assert any("per-request cap" in m and "1.7" in m for m in messages)
    assert any("daily cap" in m and "0.6" in m for m in messages)


def test_a_capped_request_never_builds_the_unlock_environment(tmp_path):
    """The cap must be decided before the unlock value is copied into a
    subprocess environment — a refused request should touch nothing."""
    rec = _Recorder()
    signer = Signer(_config(tmp_path, max_transfer_tao=1.0), run=rec)

    def _boom(name, secret):
        raise AssertionError("environment built for a refused request")

    signer._env_for = _boom
    res = signer.handle(_req(OP_PAY, CK, ("text", "image", "env")))
    assert not res.ok
    assert "cap" in res.error.lower()
    assert not any("transfer" in c for c in rec.calls)


def test_the_unstake_tolerance_is_configurable_and_reaches_btcli(tmp_path):
    """The right tolerance is a property of the subnet's liquidity, not
    of this code — 5% cost two refused unstakes and their fees before
    anyone could change it without a deploy."""
    rec = _Recorder()
    calls = []
    signer = _signer(rec, tmp_path, unstake_tolerance=0.25)
    signer._run_unstake = lambda argv, env, secret: (
        calls.append(argv) or UNSTAKE_OK
    )
    signer.handle(_req(OP_UNSTAKE, CK))
    argv = calls[0]
    assert argv[argv.index("--tolerance") + 1] == "0.25"


def test_a_failing_unstake_logs_the_whole_exchange(tmp_path, caplog):
    """The stored reason is one clamped sentence. Diagnosing the
    NotEnoughStakeToWithdraw failures meant reasoning about a transcript
    nobody had kept, and produced three wrong answers in a row."""
    rec = _Recorder()
    signer = _signer(rec, tmp_path)
    signer._run_unstake = lambda argv, env, secret: (
        "Unstake all: 102.7587 from 5GcA on netuid: 56? y\n"
        "❌ Batch unstaking failed: Subtensor returned "
        "`NotEnoughStakeToWithdraw(Module)` error.\n"
    )
    with caplog.at_level("ERROR", logger="emission_signer"):
        res = signer.handle(_req(OP_UNSTAKE, CK))

    assert not res.ok
    transcript = "\n".join(r.getMessage() for r in caplog.records)
    assert "Unstake all: 102.7587" in transcript
    assert UNLOCK not in transcript


def test_unstake_touches_only_the_rostered_hotkeys(tmp_path, caplog):
    """The roster lives in the signer's config, not in the request: a
    caller that could name hotkeys would make the privilege split
    decorative."""
    STRAY = "5C7vE26G77n7CvUkAdgHKjT7scqfiNhWcaCg8WVyB8A57Mt1"
    rec = _Recorder(results={"stake": {"stake_info": {
        HK: [{"netuid": 56, "stake_value": 102.7587}],
        STRAY: [{"netuid": 56, "stake_value": 33.0155}],
    }}})
    calls = []
    signer = _signer(rec, tmp_path, hotkeys={CK: [HK]})
    signer._run_unstake = lambda argv, env, secret: (
        calls.append(argv) or UNSTAKE_OK
    )
    with caplog.at_level("WARNING", logger="emission_signer"):
        res = signer.handle(_req(OP_UNSTAKE, CK))

    assert res.ok
    argv = calls[0]
    assert argv[argv.index("--include-hotkeys") + 1] == HK
    # And the position it did not touch is stated, not swallowed.
    assert any(STRAY in r.getMessage() for r in caplog.records)


def test_a_roster_hotkey_that_holds_nothing_fails_before_any_signing(tmp_path):
    rec = _Recorder(results={"stake": {"stake_info": {}}})
    res = _signer(rec, tmp_path, hotkeys={CK: [HK]}).handle(_req(OP_UNSTAKE, CK))
    assert not res.ok
    assert "tidak ada stake" in res.error


class TestTheSharedWalletsManyHotkeys:
    """Eleven hotkeys hold stake under the shared coldkey. btcli would
    submit them as one Utility.batch_all — atomic, so one refusal takes
    the other ten with it, and ReservesTooLow is exactly the refusal a
    large combined unstake provokes. One run per hotkey also stays
    inside its timeout, where one combined run may not."""

    HOTKEYS = [f"5Test{n:039d}" for n in range(11)]

    def _signer_with(self, tmp_path, outcomes):
        stake = {"stake_info": {hk: [{"netuid": 56, "stake_value": 1.0}]
                                for hk in self.HOTKEYS}}
        rec = _Recorder(results={"stake": stake})
        signer = _signer(rec, tmp_path, hotkeys={CK: self.HOTKEYS})
        self.runs = []

        def run_one(argv, env, secret):
            hotkey = argv[argv.index("--include-hotkeys") + 1]
            self.runs.append(hotkey)
            return outcomes(hotkey)

        signer._run_unstake = run_one
        return signer

    def test_each_hotkey_is_its_own_run(self, tmp_path):
        signer = self._signer_with(tmp_path, lambda hk: UNSTAKE_OK)
        res = signer.handle(_req(OP_UNSTAKE, CK))
        assert res.ok
        assert self.runs == self.HOTKEYS
        assert res.error is None

    def test_one_refusal_does_not_cost_the_other_ten(self, tmp_path):
        def outcome(hotkey):
            if hotkey == self.HOTKEYS[3]:
                return ("❌ Batch unstaking failed: Subtensor returned "
                        "`ReservesTooLow(Module)` error.\n")
            return UNSTAKE_OK

        res = self._signer_with(tmp_path, outcome).handle(_req(OP_UNSTAKE, CK))
        assert len(self.runs) == 11          # it kept going
        assert res.ok                        # ten of them worked
        assert "10/11" in res.error          # and it says so
        assert "cadangan pool" in res.error

    def test_a_partly_done_run_is_not_reported_as_unknown(self, tmp_path):
        """"Check the chain before trying again" applied to the whole
        request would put the successes up for a retry too."""
        def outcome(hotkey):
            return ("nothing readable" if hotkey == self.HOTKEYS[0]
                    else UNSTAKE_OK)

        res = self._signer_with(tmp_path, outcome).handle(_req(OP_UNSTAKE, CK))
        assert res.ok
        assert not res.unknown
        assert "tidak pasti" in res.error    # still named, per hotkey

    def test_nothing_done_and_something_unresolved_is_unknown(self, tmp_path):
        res = self._signer_with(
            tmp_path, lambda hk: "nothing readable"
        ).handle(_req(OP_UNSTAKE, CK))
        assert not res.ok
        assert res.unknown

    def test_a_rejected_unlock_value_stops_after_the_first_hotkey(self, tmp_path):
        """Ten more attempts would each be another wrong-password round
        trip, and would say nothing the first one did not."""
        from emission_tracker.signer.btcli import BtcliError

        def outcome(hotkey):
            raise BtcliError("btcli rejected the unlock value — nothing was submitted")

        res = self._signer_with(tmp_path, outcome).handle(_req(OP_UNSTAKE, CK))
        assert len(self.runs) == 1
        assert not res.ok

    def test_every_successful_reference_is_kept(self, tmp_path):
        signer = self._signer_with(
            tmp_path,
            lambda hk: f"✅ Your extrinsic has been included as 90{hk[-2:]}-1\n✅ Finalized\n",
        )
        res = signer.handle(_req(OP_UNSTAKE, CK))
        assert res.tx_hash.count(",") == 10


class TestSweep:
    """Each member coldkey sends its free balance to the treasury,
    keeping a little back for transaction fees."""

    PARENT = "5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB"

    def _signer(self, tmp_path, free_tao=2.0, **over):
        rec = _Recorder(results={"balance": {
            "balances": {"prj1": {"free": free_tao}}
        }})
        opts = dict(parent_coldkey=self.PARENT, hotkeys={CK: [HK]})
        opts.update(over)
        s = _signer(rec, tmp_path, **opts)
        self.rec = rec
        return s

    def test_it_sends_everything_above_the_reserve(self, tmp_path):
        res = self._signer(tmp_path, free_tao=2.0).handle(_req(OP_SWEEP, CK))
        assert res.ok
        # 2.0 - 0.015, the reserve that leaves the wallet able to pay for
        # its next transaction.
        assert res.amount_rao == 1_985_000_000
        transfer = [c for c in self.rec.calls if "transfer" in c][0]
        assert transfer[transfer.index("--amount") + 1] == "1.985000000"
        assert transfer[transfer.index("--destination") + 1] == self.PARENT

    def test_the_amount_comes_from_the_chain_not_the_caller(self, tmp_path):
        """The dashboard's figures are a day old. If the caller could
        name the amount, every stale card would become a wrong transfer
        or a chain rejection."""
        signer = self._signer(tmp_path, free_tao=0.5)
        res = signer.handle(_req(OP_SWEEP, CK))
        assert res.amount_rao == 485_000_000
        assert any(c[1:3] == ["wallet", "balance"] for c in self.rec.calls)

    @pytest.mark.parametrize("free", [0.015, 0.0149, 0.0])
    def test_a_balance_at_or_below_the_reserve_moves_nothing(
        self, tmp_path, free
    ):
        """Not an error and not "unknown": there is simply nothing to
        sweep, and sweeping would cost a fee to move dust."""
        signer = self._signer(tmp_path, free_tao=free)
        res = signer.handle(_req(OP_SWEEP, CK))
        assert not res.ok
        assert not res.unknown
        assert "ambang" in res.error
        assert not any("transfer" in c for c in self.rec.calls)

    def test_an_unreadable_balance_does_not_become_a_zero_transfer(
        self, tmp_path
    ):
        rec = _Recorder(results={"balance": {"balances": {}}})
        res = _signer(
            rec, tmp_path, parent_coldkey=self.PARENT, hotkeys={CK: [HK]}
        ).handle(_req(OP_SWEEP, CK))
        assert not res.ok
        assert not any("transfer" in c for c in rec.calls)

    def test_the_treasury_does_not_sweep_itself(self, tmp_path):
        signer = self._signer(
            tmp_path, hotkeys={CK: [HK], self.PARENT: []},
        )
        res = signer.handle(_req(OP_SWEEP, self.PARENT))
        assert not res.ok
        assert not any("transfer" in c for c in self.rec.calls)

    def test_without_a_configured_treasury_nothing_moves(self, tmp_path):
        """A deployment that has not named its treasury wallet must fail
        closed, not guess a destination."""
        signer = self._signer(tmp_path, parent_coldkey="")
        res = signer.handle(_req(OP_SWEEP, CK))
        assert not res.ok
        assert not any("transfer" in c for c in self.rec.calls)

    def test_the_reserve_is_configurable(self, tmp_path):
        signer = self._signer(tmp_path, free_tao=2.0, sweep_leave_tao=0.5)
        assert signer.handle(_req(OP_SWEEP, CK)).amount_rao == 1_500_000_000


class TestDistribute:
    """The treasury sends a named amount to a member coldkey. The only
    op where the caller names both — and so the only one where the
    roster is doing real work."""

    PARENT = "5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB"
    MEMBER = "5GxjPJokWhd8sZ7kecxQ9JWiqX8vV8R6Hg29SVNPcs6mu8YL"

    def _req(self, destination, amount_rao=2_000_000_000, coldkey=None):
        return SignRequest(
            OP_DISTRIBUTE, coldkey or self.PARENT, secret=UNLOCK,
            destination=destination, amount_rao=amount_rao,
        )

    def _signer(self, tmp_path, **over):
        wallets = {"wallets": [
            {"name": "utama", "ss58_address": self.PARENT, "hotkeys": []},
            {"name": "birong", "ss58_address": self.MEMBER, "hotkeys": []},
        ]}
        rec = _Recorder(results={"wallets": wallets})
        opts = dict(
            parent_coldkey=self.PARENT,
            hotkeys={self.PARENT: [], self.MEMBER: [], CK: [HK]},
        )
        opts.update(over)
        s = _signer(rec, tmp_path, **opts)
        self.rec = rec
        return s

    def test_it_sends_the_named_amount_from_the_treasury(self, tmp_path):
        res = self._signer(tmp_path).handle(self._req(self.MEMBER))
        assert res.ok
        assert res.amount_rao == 2_000_000_000
        transfer = [c for c in self.rec.calls if "transfer" in c][0]
        assert transfer[transfer.index("--wallet-name") + 1] == "utama"
        assert transfer[transfer.index("--destination") + 1] == self.MEMBER
        assert transfer[transfer.index("--amount") + 1] == "2.000000000"

    def test_a_destination_outside_the_roster_is_refused(self, tmp_path):
        """This is what replaces an amount cap. With it, the worst a
        compromised web tier can do is shuffle money between the team's
        own wallets; without it, there is nothing between a stranger and
        the treasury."""
        res = self._signer(tmp_path).handle(self._req("5EvilAddress"))
        assert not res.ok
        assert "roster" in res.error.lower()
        assert not any("transfer" in c for c in self.rec.calls)

    def test_the_treasury_cannot_pay_itself(self, tmp_path):
        res = self._signer(tmp_path).handle(self._req(self.PARENT))
        assert not res.ok
        assert not any("transfer" in c for c in self.rec.calls)

    def test_only_the_treasury_may_distribute(self, tmp_path):
        """A member wallet spending on another member's behalf is not a
        flow this button has; allowing it would let a single compromised
        request drain any wallet, not just the one the operator chose."""
        res = self._signer(tmp_path).handle(
            self._req(self.MEMBER, coldkey=CK)
        )
        assert not res.ok
        assert not any("transfer" in c for c in self.rec.calls)

    def test_the_unlock_value_never_reaches_the_argv(self, tmp_path):
        self._signer(tmp_path).handle(self._req(self.MEMBER))
        for argv in self.rec.calls:
            assert not any(UNLOCK in str(part) for part in argv)


class TestReadingEveryBalance:
    """What fills both dialogs. It signs nothing, so it takes no unlock
    value — and the dialogs are worth having only if their figures are
    fresher than the dashboard's once-a-day read."""

    def test_it_reports_each_rostered_coldkey(self, tmp_path):
        rec = _Recorder(results={"balance": {
            "balances": {"prj1": {"free": 3.25}}
        }})
        signer = _signer(rec, tmp_path, hotkeys={CK: [HK]})
        out = signer.balances()
        assert out == {CK: 3_250_000_000}

    def test_a_wallet_that_cannot_be_read_is_reported_as_unknown(
        self, tmp_path
    ):
        rec = _Recorder(results={"balance": {"balances": {}}})
        signer = _signer(rec, tmp_path, hotkeys={CK: [HK]})
        assert signer.balances() == {CK: None}
