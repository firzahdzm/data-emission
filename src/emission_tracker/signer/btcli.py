"""Builds and runs btcli commands.

Command construction is a pure function so the argument list can be
asserted in tests without executing anything — a test suite that shells
out to btcli would either prompt for a passphrase or move real funds.
"""

import json
import os
import subprocess

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
        "--no-prompt", "--json-output",
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
        "--no-prompt", "--json-output",
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


def run_btcli(argv: list[str], env: dict, timeout: int, run=subprocess.run) -> dict:
    try:
        # stdin=DEVNULL explicitly rather than inheriting: systemd happens to
        # give this unit /dev/null, but a run from a shell would hand btcli a
        # terminal, and a prompt we did not anticipate would then block until
        # the timeout with no indication why.
        proc = run(
            argv, capture_output=True, text=True, timeout=timeout, env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise BtcliError(f"btcli timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise BtcliError(
            f"btcli exited {proc.returncode}: "
            f"{tidy(proc.stderr or proc.stdout or '')}"
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        # Usually means btcli fell back to a prompt or printed a banner,
        # which must not be mistaken for success.
        raise BtcliError(
            f"btcli output was not JSON: {(proc.stdout or '').strip()[:200]}"
        ) from exc
    if not isinstance(payload, dict):
        # Callers index this payload (fee tables, tx hashes). A list or a
        # scalar would blow up at the first .get() — and for a transfer that
        # happens *after* the money has moved, which the caller then reports
        # as a failure and retries. Fail here instead, before the subprocess
        # result is ever acted on.
        raise BtcliError(
            f"btcli output was not a JSON object: {type(payload).__name__}"
        )

    # btcli reports a refused transfer as {"success": false} and still exits
    # 0. Trusting the exit code alone recorded failed payments as successful
    # — the worst reading a money log can give, because the operator then
    # believes a fee was paid that never left. Observed on a real wrong
    # unlock value: exit 0, {"success": false, "extrinsic_identifier": null}.
    if payload.get("success") is False:
        detail = tidy(
            str(payload.get("error") or payload.get("message") or "")
        ) or tidy(proc.stderr or "") or "btcli reported success=false"
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
