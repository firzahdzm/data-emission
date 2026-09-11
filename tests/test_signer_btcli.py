import json

import pytest

from emission_tracker.signer.btcli import (
    BtcliError,
    list_wallets,
    run_btcli,
    transfer_argv,
    unstake_argv,
)

DEST = "5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo"
WP = "/root/.bittensor/wallets"


class _Completed:
    def __init__(self, returncode=0, stdout="{}", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_transfer_command_is_non_interactive_and_machine_readable():
    argv = transfer_argv("prj1", DEST, 0.7, WP)
    assert argv[:2] == ["btcli", "wallet"]
    assert "transfer" in argv
    assert "--destination" in argv and argv[argv.index("--destination") + 1] == DEST
    assert "--amount" in argv and argv[argv.index("--amount") + 1] == "0.7"
    assert "--wallet-name" in argv and argv[argv.index("--wallet-name") + 1] == "prj1"
    # Without these two the call blocks on a prompt forever and the output
    # cannot be parsed.
    assert "--no-prompt" in argv
    assert "--json-output" in argv


def test_unstake_always_carries_the_slippage_guard():
    """526 alpha into the pool moves the price against itself; an unstake
    without a tolerance sells into that hole."""
    argv = unstake_argv("utama", 56, WP)
    assert "--safe-staking" in argv
    assert "--tolerance" in argv and argv[argv.index("--tolerance") + 1] == "0.05"
    assert "--allow-partial-stake" in argv


def test_unstake_is_scoped_to_one_subnet_and_frees_tao():
    argv = unstake_argv("utama", 56, WP)
    assert "--netuid" in argv and argv[argv.index("--netuid") + 1] == "56"
    assert "--all-hotkeys" in argv
    assert "--unstake-all" in argv
    # --all-alpha restakes to Root instead of freeing TAO, which defeats
    # the purpose of unstaking to fund a fee.
    assert "--all-alpha" not in argv
    assert "--unstake-all-alpha" not in argv


def test_list_wallets_maps_coldkey_to_name():
    payload = {
        "wallets": [
            {"name": "prj1", "ss58_address": "5Fnh", "hotkeys": []},
            {"name": "utama", "ss58_address": "5HER", "hotkeys": []},
        ]
    }
    mapping = list_wallets(
        run=lambda *a, **kw: _Completed(stdout=json.dumps(payload)), wallet_path=WP
    )
    assert mapping == {"5Fnh": "prj1", "5HER": "utama"}


def test_run_btcli_raises_on_failure_and_keeps_the_message():
    with pytest.raises(BtcliError) as exc:
        run_btcli(
            ["btcli", "x"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(returncode=1, stderr="bad wallet"),
        )
    assert "bad wallet" in str(exc.value)


def test_run_btcli_raises_when_output_is_not_json():
    with pytest.raises(BtcliError):
        run_btcli(
            ["btcli", "x"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(stdout="Enter password:"),
        )
