import json

import pytest

from emission_tracker.signer.btcli import (
    BtcliError,
    coldkey_password_env_var,
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


@pytest.mark.parametrize(
    "wallet_path, wallet_name, expected",
    [
        (
            "/root/.bittensor/wallets", "goy",
            "BT_PW__ROOT__BITTENSOR_WALLETS_GOY_COLDKEY",
        ),
        (
            "/root/.bittensor/wallets", "utama",
            "BT_PW__ROOT__BITTENSOR_WALLETS_UTAMA_COLDKEY",
        ),
        (
            "/root/.bittensor/wallets", "prj1",
            "BT_PW__ROOT__BITTENSOR_WALLETS_PRJ1_COLDKEY",
        ),
        (
            "/var/lib/emission-signer/wallets", "goy",
            "BT_PW__VAR_LIB_EMISSION-SIGNER_WALLETS_GOY_COLDKEY",
        ),
        (
            "/tmp/a.b-c", "goy",
            "BT_PW__TMP_A_B-C_GOY_COLDKEY",
        ),
    ],
)
def test_coldkey_password_env_var_matches_bittensor_wallet(
    wallet_path, wallet_name, expected
):
    """Verified against Wallet(...).coldkey_file.env_var_name() on the
    production host -- see deploy/DEPLOY.md's re-verification step."""
    assert coldkey_password_env_var(wallet_path, wallet_name) == expected


def test_transfer_command_is_non_interactive_and_machine_readable():
    argv = transfer_argv("prj1", DEST, 0.7, WP)
    assert argv[:2] == ["btcli", "wallet"]
    assert "transfer" in argv
    assert "--destination" in argv and argv[argv.index("--destination") + 1] == DEST
    assert "--amount" in argv and argv[argv.index("--amount") + 1] == "0.700000000"
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


def test_list_wallets_command_is_non_interactive_and_machine_readable():
    """list_wallets constructs the argv with all required flags."""
    captured_argv = []

    def capture_and_return(argv, capture_output=True, text=True, timeout=None, env=None):
        captured_argv.append(argv)
        return _Completed(stdout=json.dumps({"wallets": []}))

    list_wallets(WP, run=capture_and_return)

    argv = captured_argv[0]
    assert argv[:3] == ["btcli", "wallet", "list"]
    assert "--wallet-path" in argv and argv[argv.index("--wallet-path") + 1] == WP
    assert "--no-prompt" in argv
    assert "--json-output" in argv


def test_list_wallets_maps_coldkey_to_name():
    payload = {
        "wallets": [
            {"name": "prj1", "ss58_address": "5Fnh", "hotkeys": []},
            {"name": "utama", "ss58_address": "5HER", "hotkeys": []},
        ]
    }
    mapping = list_wallets(
        WP, run=lambda *a, **kw: _Completed(stdout=json.dumps(payload))
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


def test_amount_never_uses_scientific_notation():
    """1e-07 on the command line is not an amount btcli will accept."""
    argv = transfer_argv("prj1", DEST, 0.0000001, WP)
    amount = argv[argv.index("--amount") + 1]
    assert "e" not in amount.lower()
    assert amount == "0.000000100"


def test_run_btcli_raises_when_the_payload_is_not_an_object():
    """A JSON array or scalar would only blow up later, at .get() — and for
    a transfer that is after the funds have moved, which the caller reports
    as a failure and then retries."""
    for body in ("[1, 2]", '"done"', "null"):
        with pytest.raises(BtcliError) as exc:
            run_btcli(
                ["btcli", "x"], env={}, timeout=5,
                run=lambda *a, **kw: _Completed(stdout=body),
            )
        assert "not a JSON object" in str(exc.value)
