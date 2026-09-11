"""Builds and runs btcli commands.

Command construction is a pure function so the argument list can be
asserted in tests without executing anything — a test suite that shells
out to btcli would either prompt for a passphrase or move real funds.
"""

import json
import logging
import os
import subprocess

log = logging.getLogger("emission_signer.btcli")

BTCLI = "btcli"


class BtcliError(Exception):
    """btcli exited non-zero, timed out, or printed something unparseable."""


def coldkey_password_env_var(wallet_path: str, wallet_name: str) -> str:
    """Return the env var name bittensor_wallet reads a coldkey passphrase
    from, for the wallet at ``<wallet_path>/<wallet_name>``.

    This mirrors ``bittensor_wallet``'s own derivation:
    ``Wallet(name=wallet_name, path=wallet_path).coldkey_file.env_var_name()``,
    which is computed from the coldkey *keyfile path* --
    ``<wallet_path>/<wallet_name>/coldkey`` -- uppercased, with every ``/``
    and ``.`` replaced by ``_``, prefixed with ``BT_PW``. It is NOT a fixed
    name (unlike the wrong, version-mismatched ``BT_WALLET_PASSWORD``) --
    it is derived from the path, so relocating the wallets (as
    deploy/DEPLOY.md recommends) changes it too. This was verified against
    the installed bittensor_wallet on the production host, not guessed;
    deploy/DEPLOY.md carries a step to re-verify the two still agree
    whenever bittensor is upgraded.
    """
    keyfile_path = f"{wallet_path}/{wallet_name}/coldkey"
    suffix = keyfile_path.upper().replace("/", "_").replace(".", "_")
    return f"BT_PW_{suffix}"


def transfer_argv(
    wallet_name: str, destination: str, amount_tao: float, wallet_path: str
) -> list[str]:
    return [
        BTCLI, "wallet", "transfer",
        "--destination", destination,
        "--amount", f"{amount_tao:.9f}",
        "--wallet-name", wallet_name,
        "--wallet-path", wallet_path,
        "--verbose",
        # No --json-output and no --no-prompt, and the two are linked.
        # btcli 9.23 ignores BT_PW_* when it decrypts a coldkey (verified
        # on the host: the same value signs when typed at the prompt and
        # fails as "Keyfile is corrupt" from the environment), so the
        # prompt is the only way in — and btcli refuses --json-output
        # together with prompting: "Cannot specify both '--json-output'
        # and '--prompt'". Reading prose for a money operation is worse
        # than reading JSON; it is btcli's constraint, not a preference.
        # parse_transfer_output does that reading, and refuses to guess.
    ]


def unstake_argv(
    wallet_name: str, netuid: int, wallet_path: str, tolerance: float = 0.05
) -> list[str]:
    return [
        BTCLI, "stake", "remove",
        "--unstake-all",
        "--netuid", str(netuid),
        "--all-hotkeys",
        "--safe-staking",
        "--tolerance", f"{tolerance:g}",
        "--allow-partial-stake",
        "--wallet-name", wallet_name,
        "--wallet-path", wallet_path,
        # See transfer_argv: --no-prompt would suppress the very password
        # prompt this command needs, so it can be answered on stdin.
        "--json-output",
    ]


def base_env() -> dict:
    """The environment every btcli call needs, and nothing more.

    PATH matters more than it looks: subprocess resolves a bare program
    name against the PATH in the environment it is *given*, so an empty
    dict makes Python fall back to `/bin:/usr/bin` — which does not
    contain /usr/local/bin, where btcli is normally installed. A call with
    env={} therefore fails with a bare FileNotFoundError that says nothing
    about why.

    HOME matters because btcli writes into it, and a systemd system user's
    home is /nonexistent; the unit points HOME at its StateDirectory.
    """
    return {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": os.environ.get("HOME", "/tmp"),
    }


def tidy(text: str, limit: int = 300) -> str:
    """Flatten btcli's boxed, multi-line output into one readable line.

    btcli draws errors inside Unicode box borders across several lines.
    Stored raw, that text wrecks any table it is later shown in, and the
    useful sentence is buried among the borders.
    """
    cleaned = "".join(" " if ch in "│╭╮╰╯─━┃┏┓┗┛" else ch for ch in text or "")
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip()


def extract_json(text: str) -> dict | None:
    """Pull the JSON object out of output that also carries prompt text.

    btcli writes its prompts to stdout and then appends the JSON, so the
    stream is not parseable as a whole. Scanning for the last object that
    decodes cleanly is what survives that — and it stays correct if btcli
    ever adds a line after the JSON too.
    """
    decoder = json.JSONDecoder()
    text = text or ""
    found = None
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found = obj
        # Resume *after* what was just consumed. Without this the scan
        # walks into the object's own nested braces and the last success
        # is an inner object — `{"wallets": [{"name": …}]}` would come
        # back as `{"name": …}` and every coldkey would look unknown.
        i = end
    return found


def run_btcli(
    argv: list[str],
    env: dict,
    timeout: int,
    run=subprocess.run,
    answers: str | None = None,
) -> dict:
    """Run one btcli command and return its JSON payload.

    `answers` is fed to stdin — btcli's prompts are the only channel it
    accepts a coldkey unlock value through in 9.23, since it ignores
    BT_PW_* when decrypting. Without answers, stdin is closed rather than
    inherited: from a shell btcli would otherwise get a terminal and an
    unanticipated prompt would block until the timeout.
    """
    try:
        stdin_kw = (
            {"input": answers} if answers is not None
            else {"stdin": subprocess.DEVNULL}
        )
        proc = run(
            argv, capture_output=True, text=True, timeout=timeout, env=env,
            **stdin_kw,
        )
    except subprocess.TimeoutExpired as exc:
        raise BtcliError(f"btcli timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise BtcliError(
            f"btcli exited {proc.returncode}: "
            f"{tidy(proc.stderr or proc.stdout or '')}"
        )
    # Not json.loads on the whole stream: btcli prints its prompts to
    # stdout before the JSON, so the stream as a whole never parses.
    payload = extract_json(proc.stdout)
    if payload is None:
        # No JSON object anywhere usually means btcli stopped at a prompt
        # we did not answer, which must not be mistaken for success.
        raise BtcliError(
            f"btcli printed no JSON result: {tidy(proc.stdout or '')}"
        )

    # btcli reports a refused transfer as {"success": false} and still exits
    # 0. Trusting the exit code alone recorded failed payments as successful
    # — the worst reading a money log can give, because the operator then
    # believes a fee was paid that never left. Observed on a real wrong
    # unlock value: exit 0, {"success": false, "extrinsic_identifier": null}.
    if payload.get("success") is False:
        # btcli gives no reason in JSON mode — a wrong unlock value and an
        # insufficient balance produce byte-identical output. Verified on
        # the host. So salvage anything it printed alongside the JSON, and
        # when there is nothing, say plainly that btcli withheld the reason
        # rather than implying we know. The deploy guide carries the manual
        # command that does print it.
        noise = tidy(
            "\n".join(
                line for line in (proc.stdout or "").splitlines()
                if line.strip() and not line.lstrip().startswith("{")
            )
        )
        detail = (
            tidy(str(payload.get("error") or payload.get("message") or ""))
            or tidy(proc.stderr or "")
            or noise
            or "btcli refused it without giving a reason — see "
               "'Why a transfer was refused' in deploy/DEPLOY.md"
        )
        raise BtcliError(detail)

    return payload


def list_wallets(wallet_path: str, run=subprocess.run, timeout: int = 30) -> dict:
    """Map coldkey ss58 → btcli wallet name.

    Resolved live rather than configured: a hand-maintained mapping drifts
    the first time a wallet is renamed, and the failure would be signing
    from the wrong coldkey.
    """
    # No --no-prompt here, unlike transfer and unstake: `btcli wallet list`
    # does not accept the option and exits 2 if given it. Verified against
    # btcli 9.23.2 on the target host — the command reads public keyfile
    # metadata and never prompts, so there is nothing to suppress.
    argv = [
        BTCLI, "wallet", "list",
        "--wallet-path", wallet_path,
        "--json-output",
    ]
    payload = run_btcli(argv, env=base_env(), timeout=timeout, run=run)
    return {
        w["ss58_address"]: w["name"]
        for w in payload.get("wallets", [])
        if w.get("ss58_address") and w.get("name")
    }


def transfer_answers(secret: str) -> str:
    """The keystrokes btcli asks for during a transfer, in order.

    Observed against btcli 9.23.2 on the target host:

        Proceed with transfer? [y/n] (n):   -> y
        Enter your password:                -> the wallet unlock value

    This is the only channel that works. btcli ignores BT_PW_* when it
    decrypts a coldkey: the same value signs fine typed here and fails as
    "Keyfile is corrupt" when supplied in the environment.

    A trailing newline on the last line matters — without it btcli waits
    for the rest of the line and the call hangs until the timeout.
    """
    return f"y\n{secret}\n"


class TransferUnknown(Exception):
    """btcli's output said neither success nor failure.

    Raised instead of guessing. The money may or may not have moved, and
    the only way to find out is the chain — so the caller must surface
    that, never record an outcome it does not know.
    """


# Markers from a real transfer on btcli 9.23.2:
#   ✅ Finalized. Block Hash: 0x14fa5dba…
#   ✅ Your extrinsic has been included as 9044902-6: https://tao.app/…
# and on refusal:
#   ❌ Not enough balance: …
_OK_MARKERS = ("Finalized", "has been included as")
_FAIL_MARKER = "❌"


def parse_transfer_output(text: str) -> str | None:
    """Return the transaction reference for a completed transfer.

    `btcli wallet transfer` refuses `--json-output` together with
    prompting, and prompting is the only way to give it an unlock value —
    so this has to read prose. That is worse than JSON and is not a
    choice: it is btcli's constraint.

    Raises BtcliError when btcli clearly refused, and TransferUnknown when
    the output says neither. Silence is never read as success: for a
    transfer, guessing wrong in that direction records a payment that
    never happened.
    """
    flat = tidy(text, limit=4000)
    ok = any(marker in flat for marker in _OK_MARKERS)
    failed = _FAIL_MARKER in flat

    if ok and not failed:
        for token in flat.split():
            if token.startswith("0x") and len(token) > 18:
                return token.rstrip(".,")
        # Finalized but no hash in the text — still a success, and the
        # audit row simply has no reference to show.
        return None
    if failed and not ok:
        marker = flat.find(_FAIL_MARKER)
        raise BtcliError(tidy(flat[marker:], limit=300))
    raise TransferUnknown(tidy(flat, limit=400) or "btcli printed nothing")


def run_btcli_text(
    argv: list[str],
    env: dict,
    timeout: int,
    run=subprocess.run,
    answers: str | None = None,
) -> str:
    """Run a btcli command that cannot use --json-output, return its output.

    A non-zero exit is still a clear failure. Anything subtler is the
    caller's to interpret — see parse_transfer_output.
    """
    try:
        proc = run(
            argv, capture_output=True, text=True, timeout=timeout, env=env,
            input=answers if answers is not None else "",
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout normally means we cannot know whether the chain saw it.
        # One case we can know: btcli was still sitting at the password
        # prompt, so nothing was ever submitted. btcli re-asks when the
        # value is rejected, and with stdin exhausted it waits there until
        # the timeout — the common shape of a wrong unlock value. Calling
        # every one of those "unknown" would train the operator to ignore
        # the warning that matters.
        partial = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        stuck_at_password = "Enter your password" in partial and not any(
            m in partial for m in (*_OK_MARKERS, "Submitting", "Extrinsic")
        )
        if stuck_at_password:
            raise BtcliError(
                "btcli kept asking for the unlock value and never accepted "
                f"it (timed out after {timeout}s) — nothing was submitted"
            ) from exc
        raise TransferUnknown(
            f"btcli timed out after {timeout}s — check the chain"
        ) from exc
    if proc.returncode != 0:
        raise BtcliError(
            f"btcli exited {proc.returncode}: "
            f"{tidy(proc.stderr or proc.stdout or '')}"
        )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Log the whole exchange when it did not plainly succeed. Which prompts
    # btcli asks, and in what order, is the one thing we cannot see from
    # the outside — and getting that order wrong feeds the unlock value to
    # the wrong question. The value itself is redacted: btcli never echoes
    # it, but a log line is the wrong place to find out we were mistaken.
    if answers and "Finalized" not in combined:
        redacted = combined
        for line in (answers or "").splitlines():
            if len(line) > 2:
                redacted = redacted.replace(line, "<redacted>")
        log.warning("btcli exchange (no success marker): %s",
                    tidy(redacted, limit=1200))
    return combined
