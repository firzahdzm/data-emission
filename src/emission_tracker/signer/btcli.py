"""Builds and runs btcli commands.

Command construction is a pure function so the argument list can be
asserted in tests without executing anything — a test suite that shells
out to btcli would either prompt for a passphrase or move real funds.
"""

import json
import subprocess

BTCLI = "btcli"


class BtcliError(Exception):
    """btcli exited non-zero, timed out, or printed something unparseable."""


def transfer_argv(
    wallet_name: str, destination: str, amount_tao: float, wallet_path: str
) -> list[str]:
    return [
        BTCLI, "wallet", "transfer",
        "--destination", destination,
        "--amount", f"{amount_tao:g}",
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


def run_btcli(argv: list[str], env: dict, timeout: int, run=subprocess.run) -> dict:
    try:
        proc = run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise BtcliError(f"btcli timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        raise BtcliError(f"btcli exited {proc.returncode}: {detail}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        # Usually means btcli fell back to a prompt or printed a banner,
        # which must not be mistaken for success.
        raise BtcliError(
            f"btcli output was not JSON: {(proc.stdout or '').strip()[:200]}"
        ) from exc


def list_wallets(run=subprocess.run, wallet_path: str = "", timeout: int = 30) -> dict:
    """Map coldkey ss58 → btcli wallet name.

    Resolved live rather than configured: a hand-maintained mapping drifts
    the first time a wallet is renamed, and the failure would be signing
    from the wrong coldkey.
    """
    argv = [BTCLI, "wallet", "list", "--json-output"]
    if wallet_path:
        argv += ["--wallet-path", wallet_path]
    payload = run_btcli(argv, env={}, timeout=timeout, run=run)
    return {
        w["ss58_address"]: w["name"]
        for w in payload.get("wallets", [])
        if w.get("ss58_address") and w.get("name")
    }
