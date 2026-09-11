import json

import pytest

from emission_tracker.signer.protocol import OP_PAY, OP_UNSTAKE, SignRequest
from emission_tracker.signer.server import Signer, SignerConfig

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"
DEST = "5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo"
WALLETS = {"wallets": [{"name": "prj1", "ss58_address": CK, "hotkeys": []}]}


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
            R.stdout = json.dumps(WALLETS)
        else:
            R.stdout = json.dumps(self._results.get("result", {"success": True}))
        return R()


def _config(**over):
    base = dict(
        destination=DEST,
        fees_tao={"text": 0.7, "image": 0.4, "env": 0.6},
        netuid=56,
        wallet_path="/root/.bittensor/wallets",
        max_transfer_tao=2.0,
        daily_cap_tao=30.0,
        credentials_dir="/dev/null",
    )
    base.update(over)
    return SignerConfig(**base)


def _signer(run, **over):
    s = Signer(_config(**over), run=run)
    s._passphrase_for = lambda name: "pw"  # no real credential files in tests
    return s


def test_payment_uses_the_signers_own_fee_table():
    rec = _Recorder()
    res = _signer(rec).handle(SignRequest(OP_PAY, CK, ("text", "env")))
    assert res.ok
    # 0.7 + 0.6, computed here — the caller only named the types.
    assert res.amount_rao == 1_300_000_000
    transfer = [c for c in rec.calls if "transfer" in c][0]
    assert transfer[transfer.index("--amount") + 1] == "1.3"


def test_destination_is_the_configured_one_and_cannot_be_influenced():
    rec = _Recorder()
    _signer(rec).handle(SignRequest(OP_PAY, CK, ("text",)))
    transfer = [c for c in rec.calls if "transfer" in c][0]
    assert transfer[transfer.index("--destination") + 1] == DEST


def test_unknown_coldkey_is_refused_before_any_btcli_call():
    rec = _Recorder()
    res = _signer(rec).handle(SignRequest(OP_PAY, "5NOTMINE", ("text",)))
    assert not res.ok
    assert "unknown coldkey" in res.error.lower()
    assert not any("transfer" in c for c in rec.calls)


def test_amount_over_the_per_request_cap_is_refused():
    """A bug that asks for everything must hit a wall in the signer."""
    rec = _Recorder()
    res = _signer(rec, max_transfer_tao=1.0).handle(
        SignRequest(OP_PAY, CK, ("text", "image", "env"))  # 1.7
    )
    assert not res.ok
    assert "cap" in res.error.lower()
    assert not any("transfer" in c for c in rec.calls)


def test_daily_cap_stops_the_second_run():
    rec = _Recorder()
    signer = _signer(rec, daily_cap_tao=1.0)
    first = signer.handle(SignRequest(OP_PAY, CK, ("text",)))   # 0.7
    second = signer.handle(SignRequest(OP_PAY, CK, ("env",)))   # 0.6 → 1.3 total
    assert first.ok
    assert not second.ok
    assert "daily" in second.error.lower()


def test_unstake_resolves_the_wallet_and_passes_the_guard():
    rec = _Recorder()
    res = _signer(rec).handle(SignRequest(OP_UNSTAKE, CK))
    assert res.ok
    unstake = [c for c in rec.calls if "remove" in c][0]
    assert unstake[unstake.index("--wallet-name") + 1] == "prj1"
    assert "--safe-staking" in unstake
    assert unstake[unstake.index("--netuid") + 1] == "56"


def test_btcli_failure_comes_back_as_a_failed_result_not_an_exception():
    """The socket loop must answer every request; a crash would leave the
    tracker's row stuck in `pending` forever."""

    def failing(argv, **kwargs):
        class R:
            returncode = 0 if argv[1:3] == ["wallet", "list"] else 1
            stdout = json.dumps(WALLETS)
            stderr = "insufficient balance"

        return R()

    res = _signer(failing).handle(SignRequest(OP_PAY, CK, ("text",)))
    assert not res.ok
    assert "insufficient balance" in res.error
