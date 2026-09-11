import json
import subprocess
import sys

import pytest

from emission_tracker.signer.btcli import (
    BtcliError,
    UNSTAKE_PROMPTS,
    base_env,
    coldkey_password_env_var,
    list_wallets,
    parse_unstake_output,
    run_btcli,
    run_btcli_pty,
    strip_ansi,
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


def test_transfer_command_keeps_the_prompts_it_needs_to_answer():
    argv = transfer_argv("prj1", DEST, 0.7, WP)
    assert argv[:2] == ["btcli", "wallet"]
    assert "transfer" in argv
    assert "--destination" in argv and argv[argv.index("--destination") + 1] == DEST
    assert "--amount" in argv and argv[argv.index("--amount") + 1] == "0.700000000"
    assert "--wallet-name" in argv and argv[argv.index("--wallet-name") + 1] == "prj1"
    # Neither flag, and they are linked: btcli ignores BT_PW_* when
    # decrypting, so the password prompt is the only way in — and btcli
    # refuses --json-output together with prompting ("Cannot specify both
    # '--json-output' and '--prompt'"). Both verified on the host.
    assert "--no-prompt" not in argv
    assert "--json-output" not in argv


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
    # NOT --unstake-all. In btcli 9.23 that flag selects a different code
    # path which takes no netuid — "all stakes from all hotkeys in all
    # subnets" — so the --netuid beside it would be silently ignored and
    # the button would empty every subnet the coldkey holds.
    assert "--unstake-all" not in argv
    # --json-output cannot be combined with the prompting that carries the
    # unlock value; the outcome is read from the prose instead.
    assert "--json-output" not in argv
    # --all-alpha restakes to Root instead of freeing TAO, which defeats
    # the purpose of unstaking to fund a fee.
    assert "--all-alpha" not in argv
    assert "--unstake-all-alpha" not in argv


def test_list_wallets_command_is_non_interactive_and_machine_readable():
    """list_wallets constructs the argv with all required flags."""
    captured_argv = []

    def capture_and_return(argv, **kwargs):
        captured_argv.append(argv)
        # stdin must be closed off, not inherited: from a shell btcli would
        # otherwise get a terminal and could block on a prompt.
        assert kwargs.get("stdin") is subprocess.DEVNULL
        return _Completed(stdout=json.dumps({"wallets": []}))

    list_wallets(WP, run=capture_and_return)

    argv = captured_argv[0]
    assert argv[:3] == ["btcli", "wallet", "list"]
    assert "--wallet-path" in argv and argv[argv.index("--wallet-path") + 1] == WP
    assert "--json-output" in argv
    # Deliberately absent: `btcli wallet list` rejects --no-prompt and exits
    # 2. Verified against btcli 9.23.2 on the target host. It reads public
    # keyfile metadata and never prompts, so there is nothing to suppress.
    assert "--no-prompt" not in argv


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


def test_run_btcli_raises_when_there_is_no_json_object():
    """A JSON array or scalar would only blow up later, at .get() — and for
    a transfer that is after the funds have moved, which the caller reports
    as a failure and then retries. None of these carry a result object."""
    for body in ("[1, 2]", '"done"', "null"):
        with pytest.raises(BtcliError) as exc:
            run_btcli(
                ["btcli", "x"], env={}, timeout=5,
                run=lambda *a, **kw: _Completed(stdout=body),
            )
        assert "no JSON result" in str(exc.value)


def test_list_wallets_passes_a_usable_PATH():
    """subprocess resolves a bare program name against the PATH in the
    environment it is handed. An empty env falls back to /bin:/usr/bin,
    which does not contain /usr/local/bin where btcli lives — and the
    failure is a bare FileNotFoundError that explains nothing. This was a
    real production failure; every test injects a fake run, so nothing
    else would catch it."""
    seen = {}

    def spy(argv, **kwargs):
        seen.update(kwargs)

        class R:
            returncode = 0
            stdout = json.dumps({"wallets": []})
            stderr = ""

        return R()

    list_wallets(WP, run=spy)
    assert "/usr/local/bin" in seen["env"]["PATH"]


def test_base_env_carries_only_what_btcli_needs():
    from emission_tracker.signer.btcli import base_env

    assert set(base_env()) == {"PATH", "HOME", "TERM", "NO_COLOR"}


def test_success_false_is_a_failure_even_though_btcli_exits_zero():
    """btcli refuses a transfer with {"success": false} and still exits 0.
    Trusting the exit code recorded failed payments as successful — the
    worst reading a money log can give. Observed live on a wrong unlock
    value: exit 0, {"success": false, "extrinsic_identifier": null}."""
    with pytest.raises(BtcliError):
        run_btcli(
            ["btcli", "wallet", "transfer"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(
                stdout=json.dumps({"success": False, "extrinsic_identifier": None})
            ),
        )


def test_success_true_still_passes_through():
    payload = run_btcli(
        ["btcli", "x"], env={}, timeout=5,
        run=lambda *a, **kw: _Completed(
            stdout=json.dumps({"success": True, "extrinsic_identifier": "0xabc"})
        ),
    )
    assert payload["extrinsic_identifier"] == "0xabc"


def test_a_payload_without_a_success_field_is_not_treated_as_failed():
    """`btcli wallet list` has no success field; only an explicit false
    means refused."""
    payload = run_btcli(
        ["btcli", "wallet", "list"], env={}, timeout=5,
        run=lambda *a, **kw: _Completed(stdout=json.dumps({"wallets": []})),
    )
    assert payload == {"wallets": []}


def test_error_text_is_flattened_for_storage_and_display():
    """btcli draws errors inside box borders across several lines; stored
    raw that text wrecks the table it is later shown in."""
    from emission_tracker.signer.btcli import tidy

    boxed = (
        "Usage: btcli wallet list [OPTIONS]\n"
        "╭─ Error ────────────────────╮\n"
        "│ No such option: --no-prompt │\n"
        "╰────────────────────────────╯\n"
    )
    out = tidy(boxed)
    assert "\n" not in out
    assert "│" not in out and "╭" not in out and "─" not in out
    assert "No such option: --no-prompt" in out


def test_a_refusal_without_a_reason_says_so_instead_of_guessing():
    """In JSON mode btcli gives no reason at all — a wrong unlock value and
    an insufficient balance are byte-identical. Verified on the host. The
    error must not imply we know which it was."""
    with pytest.raises(BtcliError) as exc:
        run_btcli(
            ["btcli", "wallet", "transfer"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(
                stdout=json.dumps({"success": False, "extrinsic_identifier": None})
            ),
        )
    assert "without giving a reason" in str(exc.value)


def test_text_printed_alongside_the_json_is_salvaged_into_the_error():
    """--verbose makes btcli print a little context before the JSON. It is
    not the reason, but it is more than nothing."""
    with pytest.raises(BtcliError) as exc:
        run_btcli(
            ["btcli", "wallet", "transfer"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(
                stdout="[Verbose]: Fetching existential and fee\n\n"
                       + json.dumps({"success": False}),
            ),
        )
    assert "Fetching existential and fee" in str(exc.value)


def test_transfer_asks_btcli_to_be_verbose():
    assert "--verbose" in transfer_argv("prj1", DEST, 0.4, WP)


def test_the_unlock_value_goes_in_on_stdin_not_the_environment():
    """btcli 9.23 ignores BT_PW_* when decrypting a coldkey — verified on
    the host: the same value signs fine typed at the prompt and fails as
    "Keyfile is corrupt" from the environment. stdin is the only channel
    that works."""
    from emission_tracker.signer.btcli import transfer_answers

    seen = {}

    def spy(argv, **kwargs):
        seen.update(kwargs)

        class R:
            returncode = 0
            stdout = json.dumps({"success": True, "extrinsic_identifier": "0xa"})
            stderr = ""

        return R()

    run_btcli(["btcli", "x"], env={}, timeout=5, run=spy,
              answers=transfer_answers("dummy-unlock-value"))
    assert seen["input"] == "y\ndummy-unlock-value\n"
    # input= and stdin= are mutually exclusive in subprocess.
    assert "stdin" not in seen


def test_answers_end_with_a_newline():
    """Without it btcli waits for the rest of the line and the call hangs
    until the timeout."""
    from emission_tracker.signer.btcli import transfer_answers

    assert transfer_answers("x").endswith("\n")


def test_transfer_must_not_pass_no_prompt():
    """--no-prompt stops btcli asking for the password at all, which is
    exactly why every transfer came back success=false."""
    assert "--no-prompt" not in transfer_argv("prj1", DEST, 0.4, WP)
    assert "--no-prompt" not in unstake_argv("utama", 56, WP)


def test_json_is_extracted_from_output_that_also_carries_prompts():
    """btcli writes its prompts to stdout before the JSON, so the stream
    as a whole never parses."""
    mixed = (
        "Proceed with transfer? [y/n] (n): Enter your password: Decrypting...\n"
        + json.dumps({"success": True, "extrinsic_identifier": "0xbeef"})
    )
    payload = run_btcli(
        ["btcli", "x"], env={}, timeout=5,
        run=lambda *a, **kw: _Completed(stdout=mixed),
    )
    assert payload["extrinsic_identifier"] == "0xbeef"


def test_output_with_no_json_at_all_is_an_error():
    """Usually means btcli stopped at a prompt nobody answered."""
    with pytest.raises(BtcliError) as exc:
        run_btcli(
            ["btcli", "x"], env={}, timeout=5,
            run=lambda *a, **kw: _Completed(stdout="Enter your password: "),
        )
    assert "no JSON result" in str(exc.value)


def test_nested_objects_do_not_shadow_the_outer_result():
    """Scanning for the last decodable object walks into the payload's own
    braces: {"wallets": [{"name": …}]} would come back as {"name": …} and
    every coldkey would look unknown."""
    from emission_tracker.signer.btcli import extract_json

    payload = {"wallets": [{"name": "prj1", "ss58_address": "5Fnh"}]}
    assert extract_json("prompt text\n" + json.dumps(payload)) == payload


def test_the_last_top_level_object_wins():
    """btcli prints progress objects before the result on some commands."""
    from emission_tracker.signer.btcli import extract_json

    text = json.dumps({"step": 1}) + "\nnoise\n" + json.dumps({"success": True})
    assert extract_json(text) == {"success": True}


class TestTransferOutcomeIsNeverGuessed:
    """btcli refuses --json-output alongside the password prompt, so a
    transfer's outcome has to be read from prose. Three outcomes, and the
    third one matters most: a payment that may have happened must not be
    recorded as a failure, because a failure invites a retry."""

    OK = (
        "Proceed with transfer? [y/n] (n): y\nEnter your password: Decrypting...\n"
        "✅ Finalized. Block Hash: 0x14fa5dba1c7a4c048cdc979c5b4f0ddbd75e94406\n"
        "✅ Your extrinsic has been included as 9044902-6\n"
    )

    def test_a_finalized_transfer_yields_its_hash(self):
        from emission_tracker.signer.btcli import parse_transfer_output

        assert parse_transfer_output(self.OK).startswith("0x14fa5dba")

    def test_a_refusal_raises_with_btclis_own_words(self):
        from emission_tracker.signer.btcli import parse_transfer_output

        with pytest.raises(BtcliError) as exc:
            parse_transfer_output(
                "Initiating transfer\n❌ Not enough balance: amount 999 τ\n"
            )
        assert "Not enough balance" in str(exc.value)

    def test_silence_is_unknown_not_success(self):
        from emission_tracker.signer.btcli import (
            TransferUnknown,
            parse_transfer_output,
        )

        for text in ("", "Enter your password: ", "Initiating transfer\n"):
            with pytest.raises(TransferUnknown):
                parse_transfer_output(text)

    def test_both_markers_present_is_unknown_not_success(self):
        """Never resolve an ambiguous transfer in the optimistic direction."""
        from emission_tracker.signer.btcli import (
            TransferUnknown,
            parse_transfer_output,
        )

        with pytest.raises(TransferUnknown):
            parse_transfer_output(self.OK + "\n❌ something else went wrong\n")

    def test_a_timeout_is_unknown_because_it_may_have_landed(self):
        from emission_tracker.signer.btcli import (
            TransferUnknown,
            run_btcli_text,
        )

        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="btcli", timeout=90)

        with pytest.raises(TransferUnknown):
            run_btcli_text(["btcli", "x"], env={}, timeout=90, run=boom)


class TestTimeoutClassification:
    """"Unknown" has to stay rare enough to be believed. A timeout while
    btcli was still at the password prompt submitted nothing, and saying
    so beats crying wolf."""

    def _timeout(self, partial):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(
                cmd="btcli", timeout=90, output=partial, stderr=""
            )

        from emission_tracker.signer.btcli import run_btcli_text

        return lambda: run_btcli_text(
            ["btcli", "x"], env={}, timeout=90, run=boom, answers="y\nv\n"
        )

    def test_stuck_at_the_password_prompt_is_a_plain_failure(self):
        run = self._timeout(
            "Proceed with transfer? [y/n] (n): Enter your password: "
            "Enter your password: "
        )
        with pytest.raises(BtcliError) as exc:
            run()
        assert "nothing was submitted" in str(exc.value)

    def test_a_timeout_after_submission_stays_unknown(self):
        from emission_tracker.signer.btcli import TransferUnknown

        run = self._timeout(
            "Enter your password: Decrypting...\nSubmitting extrinsic...\n"
        )
        with pytest.raises(TransferUnknown):
            run()

    def test_a_timeout_with_no_output_at_all_stays_unknown(self):
        from emission_tracker.signer.btcli import TransferUnknown

        with pytest.raises(TransferUnknown):
            self._timeout("")()


class TestPseudoTerminalDriver:
    """btcli reads its password with getpass, which opens /dev/tty and
    never sees a pipe — piped from a shell it waits on the terminal, and
    under systemd it re-asks forever on an empty string. These run a stand
    -in that prompts exactly the way btcli does."""

    FAKE = """
import getpass, sys
print("Initiating transfer on network: finney", flush=True)
if input("Proceed with transfer? [y/n] (n): ").strip().lower() != "y":
    print("Aborted."); sys.exit(0)
if getpass.getpass("Enter your password: ") == "rahasia-benar":
    print("Finalized. Block Hash: 0xdeadbeef0123456789abcdef")
else:
    print("\\u274c Failed: Coldkey Keyfile is corrupt")
"""

    def _argv(self, tmp_path):
        script = tmp_path / "fake_btcli.py"
        script.write_text(self.FAKE)
        return [sys.executable, str(script)]

    def _run(self, tmp_path, secret):
        from emission_tracker.signer.btcli import run_btcli_pty

        return run_btcli_pty(
            self._argv(tmp_path), {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            timeout=20, secret=secret,
        )

    def test_the_right_value_gets_through_and_the_transfer_finalizes(self, tmp_path):
        from emission_tracker.signer.btcli import parse_transfer_output

        out = self._run(tmp_path, "rahasia-benar")
        assert parse_transfer_output(out).startswith("0xdeadbeef")

    def test_a_wrong_value_comes_back_as_btclis_own_refusal(self, tmp_path):
        from emission_tracker.signer.btcli import parse_transfer_output

        out = self._run(tmp_path, "salah")
        with pytest.raises(BtcliError) as exc:
            parse_transfer_output(out)
        assert "Keyfile is corrupt" in str(exc.value)

    def test_the_confirmation_is_answered_so_it_is_never_aborted(self, tmp_path):
        assert "Aborted" not in self._run(tmp_path, "rahasia-benar")


def test_a_repeated_password_prompt_is_reported_at_once(tmp_path):
    """btcli re-asks when the value is rejected and would go on until the
    timeout — 15 times in 30 seconds, observed. The repeat *is* the
    rejection, so waiting 90s to say so only punishes the operator."""
    import time as _time

    from emission_tracker.signer.btcli import run_btcli_pty

    script = tmp_path / "loop.py"
    script.write_text(
        "import getpass\n"
        "print('Proceed with transfer? [y/n] (n): ', end='', flush=True)\n"
        "input()\n"
        "for _ in range(20):\n"
        "    getpass.getpass('Enter your password: ')\n"
    )
    started = _time.monotonic()
    with pytest.raises(BtcliError) as exc:
        run_btcli_pty(
            [sys.executable, str(script)],
            {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            timeout=30, secret="salah",
        )
    assert "rejected the unlock value" in str(exc.value)
    assert _time.monotonic() - started < 15, "should not wait for the timeout"


class TestTerminalNoiseDoesNotHideTheResult:
    """On a pseudo-terminal btcli turns on colour and a redrawing spinner,
    and the escape sequences land inside the very words the outcome is
    read from. A finalized transfer then parsed as "unknown" — which is
    how a payment that had gone through got recorded as one nobody could
    vouch for, inviting exactly the retry that pays twice."""

    NOISY_OK = (
        "\x1b[0m\x1b[91mInitiating transfer on network: finney\x1b[0m\r\n"
        "\x1b[?25l⠋ Checking balance and fees on chain\r"
        "\x1b[2K⠙ Checking balance and fees on chain\r\x1b[2K"
        "\x1b[32m✅ Finalized. Block Hash: "
        "0x14fa5dba1c7a4c048cdc979c5b4f0ddbd75e9440620adf9326bf80498c7d873f"
        "\x1b[0m\r\n\x1b[?25h"
    )

    def test_a_finalized_transfer_is_still_recognised(self):
        from emission_tracker.signer.btcli import parse_transfer_output

        assert parse_transfer_output(self.NOISY_OK).startswith("0x14fa5dba")

    def test_a_refusal_is_still_recognised(self):
        from emission_tracker.signer.btcli import parse_transfer_output

        noisy_fail = (
            "\x1b[?25l⠋ Checking balance\r\x1b[2K"
            "\x1b[31m❌ Failed: Coldkey Keyfile is corrupt\x1b[0m\r\n"
        )
        with pytest.raises(BtcliError) as exc:
            parse_transfer_output(noisy_fail)
        assert "Keyfile is corrupt" in str(exc.value)

    def test_stored_text_carries_no_escape_sequences(self):
        """Raw, they wreck the history table and read as mojibake."""
        from emission_tracker.signer.btcli import tidy

        assert "\x1b" not in tidy(self.NOISY_OK, limit=4000)
        assert "[0m" not in tidy(self.NOISY_OK, limit=4000)

    def test_the_environment_asks_btcli_for_plain_output(self):
        from emission_tracker.signer.btcli import base_env

        env = base_env()
        assert env["TERM"] == "dumb"
        assert env["NO_COLOR"] == "1"


class TestDrivingAnUnstakesPrompts:
    """`btcli stake remove --netuid N --all-hotkeys` asks one y/n/q per
    hotkey holding stake, and the count is only knowable from the wallet.
    A driver that answers each prompt once — the transfer's shape — stops
    at the second hotkey and sits there until the timeout."""

    FAKE_BTCLI = '''
import sys
n = int(sys.argv[1])
for i in range(n):
    print("Unstake all: 1.5 \\u03b1 from hk%d on netuid: 56? [y/n/q] (n): " % i,
          end="", flush=True)
    assert sys.stdin.readline().strip() == "y", "hotkey prompt unanswered"
print("Would you like to continue? [y/n] (n): ", end="", flush=True)
assert sys.stdin.readline().strip() == "y"
print("Enter your password: ", end="", flush=True)
if sys.stdin.readline().strip() != "sesame":
    print("\\u274c Failed: wrong")
    sys.exit(0)
print("\\u2705 Batch finalized. Unstaked across %d operations." % n)
'''

    def _run(self, tmp_path, hotkeys, secret="sesame"):
        script = tmp_path / "fake_btcli.py"
        script.write_text(self.FAKE_BTCLI)
        return run_btcli_pty(
            [sys.executable, str(script), str(hotkeys)],
            base_env(), timeout=20, secret=secret, prompts=UNSTAKE_PROMPTS,
        )

    @pytest.mark.parametrize("hotkeys", [1, 4])
    def test_every_hotkey_question_is_answered(self, tmp_path, hotkeys):
        out = self._run(tmp_path, hotkeys)
        assert parse_unstake_output(out) is None  # finalized, no hash printed
        assert "Batch finalized" in strip_ansi(out)

    def test_a_wrong_unlock_value_is_a_failure_not_a_success(self, tmp_path):
        out = self._run(tmp_path, 2, secret="wrong")
        with pytest.raises(BtcliError):
            parse_unstake_output(out)


def test_a_repeating_prompt_is_answered_once_per_appearance():
    from emission_tracker.signer.btcli import _next_answer

    pending = [("[y/n/q]", "y", True), ("Enter your password", None, False)]
    answered: dict = {}

    seen = "Unstake all: … [y/n/q] (n): "
    assert _next_answer(pending, answered, seen) == ("[y/n/q]", "y")
    answered["[y/n/q]"] = 1
    # Nothing new yet — answering again here would send a stray keystroke
    # into the next prompt.
    assert _next_answer(pending, answered, seen) is None

    seen += "y\nUnstake all: … [y/n/q] (n): "
    assert _next_answer(pending, answered, seen) == ("[y/n/q]", "y")
    answered["[y/n/q]"] = 2

    seen += "y\nEnter your password: "
    assert _next_answer(pending, answered, seen) == ("Enter your password", None)


def test_the_unstake_success_marker_covers_the_batch_wording():
    """A batch prints "Batch finalized" — lower-case f. Matching only the
    transfer's "Finalized" reported a completed unstake as unknown."""
    assert parse_unstake_output("✅ Batch finalized. Unstaked across 3 operations.") is None
    assert parse_unstake_output("✅ Finalized\nExtrinsic 0x" + "a" * 40).startswith("0x")
