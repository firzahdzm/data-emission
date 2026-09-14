"""Builds and runs btcli commands.

Command construction is a pure function so the argument list can be
asserted in tests without executing anything — a test suite that shells
out to btcli would either prompt for a passphrase or move real funds.
"""

import json
import logging
import os
import re
import subprocess
import time

log = logging.getLogger("emission_signer.btcli")

RAO = 10**9
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


def balance_argv(wallet_name: str, wallet_path: str) -> list[str]:
    """Read one wallet's balance. No password: it is public chain data,
    and a read that asked for the unlock value would carry it down a
    path that never signs anything."""
    return [
        BTCLI, "wallet", "balance",
        "--wallet-name", wallet_name,
        "--wallet-path", wallet_path,
        "--json-output",
    ]


def free_balance_rao(payload: dict, wallet_name: str) -> int | None:
    """The wallet's free (unstaked, transferable) balance in rao.

    None when the figure cannot be read — never zero. Zero means "empty,
    nothing to sweep"; unreadable means "we do not know", and the two
    lead to different actions. Collapsing them would make a failed read
    look like a wallet that needed no attention, and a sweep would
    quietly skip it every time.
    """
    try:
        free = (payload.get("balances") or {})[wallet_name]["free"]
    except (AttributeError, KeyError, TypeError):
        return None
    if isinstance(free, bool) or not isinstance(free, (int, float)):
        return None
    return round(free * RAO)


def stake_hotkeys_argv(wallet_name: str, wallet_path: str) -> list[str]:
    """Read the coldkey's stake. No password: this is public chain data."""
    return [
        BTCLI, "stake", "list",
        "--wallet-name", wallet_name,
        "--wallet-path", wallet_path,
        "--json-output",
    ]


def hotkeys_with_stake(
    payload: dict, netuid: int, allowed: list[str] | None = None
) -> list[str]:
    """Hotkeys holding stake on one subnet — each listed once.

    Two jobs, and both are about what NOT to unstake.

    Deduplication: btcli's own `--all-hotkeys` builds its list from the
    coldkey's stake rows, which are one per (hotkey, subnet), and never
    collapses them. A hotkey staked on two subnets is queued twice, and
    with a per-subnet "unstake all" that means asking the chain to
    remove the same alpha twice. Captured on wallet birong — two
    identical rows of 102.7587 α against an account holding 102.7587 α,
    and the chain answering NotEnoughStakeToWithdraw.

    `allowed`: the team's own roster for this coldkey. A coldkey can
    hold stake on hotkeys nobody here registered, and the chain is happy
    to unstake those too. Naming the roster means the button can only
    touch positions the team has declared; anything else is left alone
    and reported rather than sold. Pass None to fall back to whatever
    the chain shows.
    """
    permitted = set(allowed) if allowed is not None else None
    found: list[str] = []
    for hotkey, rows in (payload.get("stake_info") or {}).items():
        if permitted is not None and hotkey not in permitted:
            continue
        for row in rows or []:
            if row.get("netuid") == netuid and (row.get("stake_value") or 0) > 0:
                if hotkey not in found:
                    found.append(hotkey)
                break
    return found


def subnet_stake(payload: dict, netuid: int) -> tuple[int, int]:
    """Total stake on one subnet: (alpha in rao, its value in tao rao).

    btcli's own field names read backwards: `stake_value` is the alpha
    amount and `value` is what it is worth in TAO. Verified against a
    wallet whose figures were known — reading them the other way round
    is how a 44 α position was once reported as 0.71 α.
    """
    alpha = tao = 0.0
    for rows in (payload.get("stake_info") or {}).values():
        for row in rows or []:
            if row.get("netuid") != netuid:
                continue
            alpha += float(row.get("stake_value") or 0)
            tao += float(row.get("value") or 0)
    return round(alpha * RAO), round(tao * RAO)


def unlisted_stake(payload: dict, netuid: int, allowed: list[str]) -> list[str]:
    """Hotkeys holding stake here that the roster does not mention.

    Never unstaked, always surfaced: silence would mean a coldkey quietly
    keeping a position that "Unstake all" claims to have cleared.
    """
    permitted = set(allowed)
    return [
        hotkey
        for hotkey, rows in (payload.get("stake_info") or {}).items()
        if hotkey not in permitted
        and any(
            row.get("netuid") == netuid and (row.get("stake_value") or 0) > 0
            for row in rows or []
        )
    ]


def unstake_argv(
    wallet_name: str,
    netuid: int,
    wallet_path: str,
    hotkeys: list[str],
    tolerance: float = 0.15,
    mev_protection: bool = False,
) -> list[str]:
    """Unstake everything the named hotkeys hold on one subnet.

    The hotkeys are named explicitly rather than with `--all-hotkeys`,
    which double-counts (see hotkeys_with_stake).

    Deliberately NOT `--unstake-all`. That flag routes to a different
    command in btcli 9.23 — `remove_stake_all()`, which takes no netuid
    at all ("all stakes from all hotkeys in all subnets") and ignores the
    safe-staking options too. Passing `--netuid 56` beside it reads as
    scoped and is not: it would empty every subnet the coldkey holds.

    The rate tolerance is how far the alpha rate may move against us
    mid-unstake before the chain refuses. 5% was too tight for this
    subnet: the shared wallet's unstakes came back as `ReservesTooLow`
    twice, having paid the transaction fee for nothing. 15% accepts a
    worse price rather than no sale — with --allow-partial-stake, what
    fits inside the tolerance still goes through.

    MEV protection is off by default here, and that is a deliberate
    trade. btcli's shield submits the unstake encrypted and waits a fixed
    number of blocks for it to be decrypted and executed; on this subnet
    that wait kept expiring, and btcli then reports no outcome at all —
    the one answer that cannot be acted on. What replaces it is
    safe-staking with a 15% rate tolerance, which refuses a price that
    has moved too far. Set `mev_protection: true` in the signer config
    to put the shield back.

    This path asks, per hotkey, "Unstake all: <amount> α … on netuid:
    56? [y/n/q]" — answered by UNSTAKE_PROMPTS.
    """
    return [
        BTCLI, "stake", "remove",
        "--netuid", str(netuid),
        "--include-hotkeys", ",".join(hotkeys),
        "--safe-staking",
        "--tolerance", f"{tolerance:g}",
        "--allow-partial-stake",
        "--wallet-name", wallet_name,
        "--wallet-path", wallet_path,
        *([] if mev_protection else ["--no-mev-protection"]),
        # No --json-output: like transfer, it cannot be combined with the
        # prompting this command needs to receive an unlock value. The
        # outcome is read from the prose by parse_unstake_output.
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
        # On a pseudo-terminal btcli's rich output turns on colour and a
        # redrawing spinner, and the escape sequences land in the middle
        # of the very words the result is read from. A finalized transfer
        # then parses as "unknown" — which is how a payment that had
        # actually gone through got recorded as one nobody could vouch
        # for. These two ask rich for plain text.
        "TERM": "dumb",
        "NO_COLOR": "1",
    }


_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-B]|\r")


def strip_ansi(text: str) -> str:
    """Remove terminal control sequences.

    Belt and braces beside TERM=dumb: anything that slips through would
    otherwise sit inside the words the outcome is read from, and a
    finalized transfer would be reported as unknown.
    """
    return _ANSI.sub("", text or "")


def tidy(text: str, limit: int = 300) -> str:
    """Flatten btcli's boxed, multi-line output into one readable line.

    btcli draws errors inside Unicode box borders across several lines.
    Stored raw, that text wrecks any table it is later shown in, and the
    useful sentence is buried among the borders.
    """
    text = strip_ansi(text)
    cleaned = "".join(" " if ch in "│╭╮╰╯─━┃┏┓┗┛" else ch for ch in text)
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
    allow_empty: bool = False,
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
    if payload is None and allow_empty and not (proc.stdout or "").strip():
        # `stake list --json-output` prints nothing at all, and exits 0,
        # for a coldkey that holds no stake anywhere. Only the caller
        # knows whether that is an answer or a failure — for a stake
        # read it means zero, and treating it as a failed read left
        # every empty wallet's card blank.
        return {}
    if payload is None:
        # No JSON object anywhere usually means btcli stopped at a prompt
        # we did not answer, which must not be mistaken for success.
        raise BtcliError(
            "btcli printed no JSON result: "
            # stderr as well as stdout: the first time this happened in
            # production the message was empty, which said only that
            # something had gone wrong and nothing about what.
            f"{tidy(((proc.stdout or '') + ' ' + (proc.stderr or '')).strip())}"
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


def read_with_retry(call, attempts: int = 2, pause: float = 1.0):
    """Run a read that may come back empty, once more before giving up.

    Only for reads. btcli occasionally exits 0 having printed nothing —
    seen in production on `wallet list`, on the second call in a row,
    where it cost a whole balance refresh. A retry is free there because
    nothing is signed and nothing moves; the same forgiveness applied to
    a transfer would be how a payment gets made twice.
    """
    last = None
    for attempt in range(attempts):
        try:
            return call()
        except BtcliError as exc:
            last = exc
            if "printed no JSON result" not in str(exc):
                raise            # a real refusal, not an empty read
            log.warning("empty btcli read (attempt %d/%d): %s",
                        attempt + 1, attempts, exc)
            if attempt + 1 < attempts:
                time.sleep(pause)
    raise last


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

    Carries whatever output was seen, so the caller can still tell a
    transfer stuck at the password prompt (nothing submitted) from one
    that may have reached the chain.

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
    flat = tidy(strip_ansi(text), limit=4000)
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


# What btcli asks during a transfer, and what to answer. Matched against
# the output as it arrives, because the prompts appear one at a time and
# each must be answered before the next is printed.
PASSWORD_PROMPT = "Enter your password"

# Each entry is (marker, answer, repeat). `answer=None` means the wallet
# unlock value. `repeat=True` means btcli asks this one an unpredictable
# number of times — once per hotkey — so it is answered every time it
# appears rather than once.
TRANSFER_PROMPTS = (
    ("Proceed with transfer?", "y", False),
    (PASSWORD_PROMPT, None, False),
)

# `btcli stake remove --netuid N --all-hotkeys` with no --amount asks, for
# every hotkey that holds stake on the subnet:
#
#     Unstake all: 12.3456 α from my-hk on netuid: 56? [y/n/q] (n):
#
# then once, after the summary table:
#
#     Would you like to continue? [y/n] (n):
#     Enter your password:
#
# The per-hotkey question is matched on its choice list, which is what
# rich renders for choices=["y","n","q"] and appears nowhere else in the
# output. How many there are depends on the wallet, hence repeat=True.
UNSTAKE_PROMPTS = (
    ("[y/n/q]", "y", True),
    ("Would you like to continue?", "y", False),
    (PASSWORD_PROMPT, None, False),
)

# Single-operation unstakes print "Finalized"; a batch prints "Batch
# finalized. Unstaked across N operations." — lower-case f, which a
# case-sensitive match on the transfer's marker would miss and report a
# completed unstake as an unknown outcome.
_UNSTAKE_OK_MARKERS = ("Finalized", "finalized", "has been included as")


# Captured from a real unstake on the host (btcli 9.23.2, wallet goy):
#
#     ✅ Your extrinsic has been included as 9045165-8: https://tao.app/…
#     ✅ Finalized
#     Unstaking operations completed.
#
# There is no 0x hash anywhere — the reference is the block-extrinsic id.
_EXTRINSIC_ID = re.compile(r"has been included as\s+(\d+-\d+)")

# btcli says this and stops when the wallet holds nothing on the subnet.
_NOTHING_TO_DO = ("No unstake operations to perform", "No stake to unstake")

# Not every ❌ is a failure. Walking the coldkey's hotkeys, btcli marks
# each one holding nothing on the subnet:
#
#     ❌ No stake to unstake from 5FWSPfZf… on netuid: 56
#
# and then gets on with unstaking the hotkey that does hold something.
# Three of those notices preceded a real unstake on wallet birong; the
# reason recorded for the operator was the first ❌ — a hotkey that was
# never the point — while the actual outcome at the end of the output
# was thrown away by the 300-character clamp.
_BENIGN_NOTICE = re.compile(
    r"❌\s*(No stake to unstake from|No stake found for hotkey|"
    r"Nothing to unstake from)[^❌]*"
)


# btcli's MEV shield submits the unstake encrypted and then watches a
# fixed number of blocks for the inner extrinsic to appear. When it does
# not appear in time btcli gives up watching — but the protected
# extrinsic can still be decrypted and executed afterwards. So this is an
# unknown outcome, not a failure: recorded as "failed" it invites the
# retry that unstakes twice.
_SHIELD_TIMEOUT = (
    "Failed to find outcome of the shield extrinsic",
    "protected extrinsic wasn't decrypted",
)

# Chain errors, in the words the operator needs. The card shows the first
# 60 characters of the reason, and `Subtensor returned
# \`NotEnoughStakeToWithdraw(Module)\` error. This means:` spends all of
# them saying nothing. The original text is kept after the summary.
_CHAIN_HINTS = (
    ("ReservesTooLow",
     "cadangan pool subnet sedang tipis — coba lagi nanti"),
    ("NotEnoughStakeToWithdraw",
     "stake berubah sebelum transaksi masuk — coba lagi"),
    ("Not enough balance",
     "saldo wallet tidak cukup"),
)


def _with_hint(reason: str) -> str:
    for marker, hint in _CHAIN_HINTS:
        if marker in reason:
            return f"{hint} ({reason})"
    return reason


def _real_failure(flat: str) -> str | None:
    """The part of the output that explains a failure, or None.

    Per-hotkey notices are stripped first. What remains is a genuine
    refusal — a chain error, a rejected key — and the text returned
    starts there rather than at the first ❌ in the stream, so the clamp
    keeps the sentence that says why.
    """
    meaningful = _BENIGN_NOTICE.sub(" ", flat)
    if "❌" in meaningful:
        return tidy(meaningful[meaningful.find("❌"):], limit=300)
    if "unstaking failed" in meaningful:
        return tidy(meaningful[meaningful.find("unstaking failed"):], limit=300)
    return None


def parse_unstake_output(text: str) -> str | None:
    """Return the extrinsic reference for a completed unstake.

    Same three outcomes as a transfer, and the same rule: silence is
    never read as success. An unstake that may have reached the chain is
    reported as unknown so nobody submits it twice.
    """
    flat = tidy(text, limit=8000)
    ok = any(marker in flat for marker in _UNSTAKE_OK_MARKERS)
    failure = _real_failure(flat)

    if any(marker in flat for marker in _SHIELD_TIMEOUT):
        # Checked before everything else: the shield's own report is
        # "I stopped looking", and btcli prints it wrapped in ❌ and
        # followed by "Unstaking operations completed." — both of which
        # would otherwise be read as a settled outcome.
        raise TransferUnknown(
            "MEV shield tidak menemukan hasil dalam batas waktunya — "
            "extrinsic terlindungi bisa saja masih dieksekusi. Periksa "
            "stake di chain sebelum mencoba lagi."
        )

    if ok and not failure:
        match = _EXTRINSIC_ID.search(flat)
        if match:
            return match.group(1)
        for token in flat.split():
            if token.startswith("0x") and len(token) > 18:
                return token.rstrip(".,")
        return None
    if failure and not ok:
        raise BtcliError(_with_hint(failure))
    if not ok and any(marker in flat for marker in _NOTHING_TO_DO):
        # A plain failure, not "unknown": nothing was submitted, and
        # "check the chain" would send the operator after an extrinsic
        # that does not exist — which is how a real warning gets cheap.
        raise BtcliError("tidak ada stake untuk di-unstake di subnet ini")
    # Both, or neither. Report the tail, not the head: whatever btcli
    # said last is the part that describes how it ended.
    raise TransferUnknown(tidy(flat[-400:], limit=400) or "btcli printed nothing")


def _normalise(prompt) -> tuple[str, str | None, bool]:
    """Accept both the two- and three-element prompt forms."""
    marker, answer, *rest = prompt
    return marker, answer, bool(rest[0]) if rest else False


def _next_answer(pending, answered: dict, seen: str):
    """The next prompt to answer, or None if btcli has not asked yet.

    A one-shot prompt is answered the first time its marker appears; a
    repeating one every time a fresh copy appears, which is how a
    per-hotkey question with an unpredictable count gets answered without
    the driver knowing the count in advance. Earlier entries win, so the
    order in the table is still the order of the conversation.
    """
    for marker, answer, repeat in pending:
        count = seen.count(marker)
        already = answered.get(marker, 0)
        if count > already and (repeat or already == 0):
            return marker, answer
    return None


def run_btcli_pty(
    argv: list[str],
    env: dict,
    timeout: int,
    secret: str,
    prompts=TRANSFER_PROMPTS,
) -> str:
    """Run btcli attached to a pseudo-terminal, answering its prompts.

    Piping stdin does not work. btcli reads its password through getpass,
    which opens /dev/tty directly when a terminal exists and otherwise
    gets nothing, because the buffered read behind the preceding prompt
    has already swallowed the line. Observed both ways on the host: piped
    from a shell it hangs waiting on the terminal, and under systemd it
    re-asks forever, each round receiving an empty string.

    So give it a terminal. Each prompt is answered when it appears, once,
    in order — not written ahead — because a terminal has no queue and
    anything sent early is simply lost.
    """
    import fcntl
    import pty
    import select

    pending = [_normalise(p) for p in prompts]
    answered: dict[str, int] = {}
    answered_password = 0
    out: list[str] = []
    deadline = time.monotonic() + timeout

    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvpe(argv[0], argv, env)
        finally:
            os._exit(127)

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                classify_timeout("".join(out), timeout)
            ready, _, _ = select.select([fd], [], [], min(remaining, 1.0))
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break           # child closed the pty: it has finished
                if not chunk:
                    break
                out.append(chunk.decode(errors="replace"))
            seen = "".join(out)
            asked = _next_answer(pending, answered, seen)
            if asked is not None:
                marker, answer = asked
                answered[marker] = seen.count(marker)
                if answer is None:
                    answered_password = seen.count(PASSWORD_PROMPT)
                os.write(fd, ((secret if answer is None else answer) + "\n").encode())
            elif (
                answered_password
                and seen.count(PASSWORD_PROMPT) > answered_password
            ):
                # btcli asks again when the value is rejected, and would go
                # on asking until the timeout. The repeat is the rejection:
                # report it now rather than making the operator wait 90s
                # for the same answer.
                raise BtcliError(
                    "btcli rejected the unlock value — nothing was submitted"
                )
            if not ready and os.waitpid(pid, os.WNOHANG)[0]:
                break
    finally:
        os.close(fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return "".join(out)
