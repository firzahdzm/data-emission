# Signed On-Chain Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two admin buttons on each coldkey card — pay tournament fees, and unstake everything on subnet 56 — signed by a separate privileged service so a compromised web app cannot move funds anywhere but the tournament address.

**Architecture:** A second systemd unit (`emission-signer`) runs as its own user, reads the root-owned encrypted wallets, and listens on a unix socket. It accepts exactly two operations and hard-codes the destination address and fee table, so callers name intents, never amounts or addresses. The tracker records every attempt in `signed_actions` before calling, and the web UI drives it from the existing coldkey cards.

**Tech Stack:** Python 3.12+, FastAPI, SQLite, systemd, `btcli` 9.23.2 (`--json-output`), unix domain sockets, pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-signed-actions-design.md`

## Global Constraints

- Tournament destination address: `5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo` — hard-coded in the signer, never accepted from a caller.
- Fee table, TAO: `text` 0.7, `image` 0.4, `env` 0.6. The signer owns the authoritative copy; the tracker's copy is for display and the balance check only.
- Subnet: 56. Unstake is `--unstake-all --netuid 56 --all-hotkeys`, never `--all-alpha` (which restakes to Root instead of freeing TAO).
- Slippage guard is mandatory on every unstake: `--safe-staking --tolerance 0.05 --allow-partial-stake`.
- Every btcli invocation uses `--no-prompt --json-output`.
- Neither operation is ever scheduled, retried automatically, or triggered by startup or any timer. Click-only.
- Coldkey → btcli wallet name is always resolved live from `btcli wallet list --json-output`. No hand-maintained mapping.
- Wallets on the server: `/root/.bittensor/wallets`, all 15 coldkeys `$NACL`-encrypted.
- Money amounts are stored as integer rao. Convert with `round(tao * 1e9)`; never store floats.
- Task 1 ships before Task 6 exposes any button.

---

### Task 1: Reject requests that did not come through nginx

The admin check trusts `X-Remote-User` verbatim, and uvicorn listens on `127.0.0.1:8000`. Anything on the host that can reach that port is currently an admin. This must close before any button can spend money.

**Files:**
- Modify: `src/emission_tracker/web/auth.py`
- Modify: `deploy/nginx.conf.example`
- Modify: `deploy/DEPLOY.md`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `emission_tracker.web.auth.proxy_secret_ok(request) -> bool`; `current_user` and `is_admin` return None/False when the secret is configured and absent or wrong.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth.py — append
from types import SimpleNamespace

from fastapi import Request

from emission_tracker.web.auth import current_user, is_admin


def _request(headers: dict, app_state) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "app": SimpleNamespace(state=app_state),
    }
    return Request(scope)


def _state(secret, admins=("alice",)):
    return SimpleNamespace(
        config=SimpleNamespace(admin_users=list(admins), proxy_secret=secret)
    )


class TestProxySecret:
    def test_header_alone_is_not_enough_when_a_secret_is_configured(self, monkeypatch):
        """The whole point: reaching uvicorn directly must not make you admin."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        req = _request({"X-Remote-User": "alice"}, _state("s3cret"))
        assert current_user(req) is None
        assert is_admin(req) is False

    def test_correct_secret_admits_the_forwarded_user(self, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        req = _request(
            {"X-Remote-User": "alice", "X-Auth-Proxy": "s3cret"}, _state("s3cret")
        )
        assert current_user(req) == "alice"
        assert is_admin(req) is True

    def test_wrong_secret_is_rejected(self, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        req = _request(
            {"X-Remote-User": "alice", "X-Auth-Proxy": "wrong"}, _state("s3cret")
        )
        assert is_admin(req) is False

    def test_no_secret_configured_keeps_the_old_behaviour(self, monkeypatch):
        """Existing deployments must not lock themselves out on upgrade."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        req = _request({"X-Remote-User": "alice"}, _state(None))
        assert current_user(req) == "alice"
        assert is_admin(req) is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_auth.py -k ProxySecret -v`
Expected: FAIL — `AttributeError` on `proxy_secret`, and the first test fails because the header alone still admits.

- [ ] **Step 3: Implement the check**

```python
# src/emission_tracker/web/auth.py — add near the top
import hmac

PROXY_SECRET_HEADER = "X-Auth-Proxy"


def proxy_secret_ok(request: Request) -> bool:
    """True when the request carries the secret nginx adds, or when no
    secret is configured.

    uvicorn listens on localhost, so `X-Remote-User` on its own proves
    nothing: any process on the host can set it. nginx is the only party
    that knows the secret, so its presence is what makes the forwarded
    username trustworthy. Configuring no secret keeps the old behaviour,
    so an existing deployment does not lock itself out on upgrade.
    """
    config = getattr(request.app.state, "config", None)
    expected = getattr(config, "proxy_secret", None) if config else None
    if not expected:
        return True
    presented = request.headers.get(PROXY_SECRET_HEADER) or ""
    return hmac.compare_digest(presented, expected)
```

Then guard `current_user`, immediately after the `EMISSION_DEV_USER` block:

```python
    if not proxy_secret_ok(request):
        return None
```

- [ ] **Step 4: Add the config field**

```python
# src/emission_tracker/config.py — inside AppConfig, beside admin_users
    # Shared secret nginx sends as X-Auth-Proxy. Empty disables the check,
    # which is what a deployment without the matching nginx block needs.
    proxy_secret: str = ""
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_auth.py -v && .venv/bin/python -m pytest -q`
Expected: all PASS. The whole suite must stay green — `test_routes_api.py` stubs `app.state.config` with `SimpleNamespace(admin_users=[...])` and no `proxy_secret`, which `getattr` resolves to None, so those tests keep working unchanged.

- [ ] **Step 6: Update the nginx example and deploy notes**

```nginx
# deploy/nginx.conf.example — inside location /, beside the other proxy_set_header lines
        # Proves the request came through nginx. Must equal proxy_secret in
        # config.yaml. Without it the app ignores X-Remote-User entirely.
        proxy_set_header X-Auth-Proxy       "REPLACE_WITH_A_LONG_RANDOM_STRING";
```

In `deploy/DEPLOY.md`, under step 7, add:

````markdown
### Locking the admin header to nginx

`X-Remote-User` is only meaningful if nginx is the only party that can set
it. Generate a secret, put the same value in both places, and the app will
ignore the header on any request that arrives without it:

```bash
openssl rand -hex 32
# → paste into proxy_set_header X-Auth-Proxy in the nginx site
# → and into proxy_secret in /opt/emission-tracker/config.yaml
sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart emission-tracker
```

Verify from the VPS that bypassing nginx no longer works:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
     -H "X-Remote-User: admin" http://127.0.0.1:8000/api/settlements/999999
# 401 = closed. 404 = the header still works directly; the secret is not matching.
```
````

- [ ] **Step 7: Commit**

```bash
git add src/emission_tracker/web/auth.py src/emission_tracker/config.py \
        tests/test_auth.py deploy/nginx.conf.example deploy/DEPLOY.md
git commit -m "fix: only trust X-Remote-User on requests that came via nginx"
```

---

### Task 2: Signer wire protocol

A tiny module both sides import, so the request shape is defined once and the encoding is tested without any socket or subprocess.

**Files:**
- Create: `src/emission_tracker/signer/__init__.py` (empty)
- Create: `src/emission_tracker/signer/protocol.py`
- Test: `tests/test_signer_protocol.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `TOURNAMENT_TYPES: tuple[str, ...]` = `("text", "image", "env")`
  - `SignRequest(op: str, coldkey: str, types: tuple[str, ...] = ())` with `.to_line() -> bytes` and `SignRequest.from_line(line: bytes) -> SignRequest`
  - `SignResult(ok: bool, op: str, coldkey: str, amount_rao: int = 0, tx_hash: str | None = None, error: str | None = None)` with `.to_line()` / `.from_line()`
  - `OP_PAY = "pay_tournament"`, `OP_UNSTAKE = "unstake_all"`
  - `ProtocolError(Exception)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signer_protocol.py
import pytest

from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    ProtocolError,
    SignRequest,
    SignResult,
)

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"


def test_request_round_trips():
    req = SignRequest(op=OP_PAY, coldkey=CK, types=("text", "env"))
    assert SignRequest.from_line(req.to_line()) == req


def test_request_line_is_newline_terminated():
    """The socket framing is one JSON object per line."""
    line = SignRequest(op=OP_UNSTAKE, coldkey=CK).to_line()
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_unknown_op_is_refused():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b'{"op": "drain_everything", "coldkey": "5F"}\n')


def test_unknown_tournament_type_is_refused():
    """Types index a fee table; an unrecognised one must not reach it."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "pay_tournament", "coldkey": "5F", "types": ["gold"]}\n'
        )


def test_duplicate_types_are_refused():
    """Otherwise ["text","text"] silently doubles the fee."""
    with pytest.raises(ProtocolError):
        SignRequest.from_line(
            b'{"op": "pay_tournament", "coldkey": "5F", "types": ["text", "text"]}\n'
        )


def test_payment_with_no_types_is_refused():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b'{"op": "pay_tournament", "coldkey": "5F"}\n')


def test_amount_is_never_accepted_from_the_wire():
    """The signer computes amounts. A caller-supplied one must be ignored,
    not honoured, or the security boundary is decorative."""
    req = SignRequest.from_line(
        b'{"op": "pay_tournament", "coldkey": "5F", "types": ["text"],'
        b' "amount_tao": 999}\n'
    )
    assert not hasattr(req, "amount_tao")


def test_result_round_trips():
    res = SignResult(ok=True, op=OP_PAY, coldkey=CK, amount_rao=700_000_000,
                     tx_hash="0xabc")
    assert SignResult.from_line(res.to_line()) == res


def test_malformed_json_raises_protocol_error():
    with pytest.raises(ProtocolError):
        SignRequest.from_line(b"not json\n")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_signer_protocol.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'emission_tracker.signer'`

- [ ] **Step 3: Implement the protocol**

```python
# src/emission_tracker/signer/protocol.py
"""Wire format between the tracker and the signer.

One JSON object per line over a unix socket. Kept deliberately small:
the caller names an intent and a coldkey, and nothing else. Amounts and
destinations are the signer's to decide, so they have no place in a
request — a field the caller controls is a field an attacker controls.
"""

import json
from dataclasses import dataclass, field

OP_PAY = "pay_tournament"
OP_UNSTAKE = "unstake_all"
OPS = (OP_PAY, OP_UNSTAKE)

TOURNAMENT_TYPES = ("text", "image", "env")


class ProtocolError(Exception):
    """A request that is malformed or asks for something undefined."""


@dataclass(frozen=True)
class SignRequest:
    op: str
    coldkey: str
    types: tuple[str, ...] = field(default=())

    def to_line(self) -> bytes:
        payload = {"op": self.op, "coldkey": self.coldkey}
        if self.types:
            payload["types"] = list(self.types)
        return (json.dumps(payload) + "\n").encode()

    @classmethod
    def from_line(cls, line: bytes) -> "SignRequest":
        try:
            raw = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed request: {exc}") from exc
        if not isinstance(raw, dict):
            raise ProtocolError("request must be a JSON object")

        op = raw.get("op")
        if op not in OPS:
            raise ProtocolError(f"unknown op {op!r}")

        coldkey = raw.get("coldkey")
        if not isinstance(coldkey, str) or not coldkey:
            raise ProtocolError("coldkey is required")

        types = raw.get("types") or []
        if not isinstance(types, list) or any(not isinstance(t, str) for t in types):
            raise ProtocolError("types must be a list of strings")
        for t in types:
            if t not in TOURNAMENT_TYPES:
                raise ProtocolError(f"unknown tournament type {t!r}")
        if len(set(types)) != len(types):
            raise ProtocolError("duplicate tournament type")
        if op == OP_PAY and not types:
            raise ProtocolError("pay_tournament needs at least one type")

        # Any other key in the payload is dropped here, by construction.
        return cls(op=op, coldkey=coldkey, types=tuple(types))


@dataclass(frozen=True)
class SignResult:
    ok: bool
    op: str
    coldkey: str
    amount_rao: int = 0
    tx_hash: str | None = None
    error: str | None = None

    def to_line(self) -> bytes:
        return (
            json.dumps(
                {
                    "ok": self.ok,
                    "op": self.op,
                    "coldkey": self.coldkey,
                    "amount_rao": self.amount_rao,
                    "tx_hash": self.tx_hash,
                    "error": self.error,
                }
            )
            + "\n"
        ).encode()

    @classmethod
    def from_line(cls, line: bytes) -> "SignResult":
        try:
            raw = json.loads(line.decode())
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"malformed result: {exc}") from exc
        return cls(
            ok=bool(raw.get("ok")),
            op=str(raw.get("op") or ""),
            coldkey=str(raw.get("coldkey") or ""),
            amount_rao=int(raw.get("amount_rao") or 0),
            tx_hash=raw.get("tx_hash"),
            error=raw.get("error"),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signer_protocol.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/emission_tracker/signer/ tests/test_signer_protocol.py
git commit -m "feat: wire protocol for the signer, with no caller-supplied amounts"
```

---

### Task 3: btcli wrapper

Builds the exact command lines and parses `--json-output`. Tests assert on the argument list without executing anything, so the suite never touches a wallet or the chain.

**Files:**
- Create: `src/emission_tracker/signer/btcli.py`
- Test: `tests/test_signer_btcli.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BtcliError(Exception)`
  - `transfer_argv(wallet_name: str, destination: str, amount_tao: float, wallet_path: str) -> list[str]`
  - `unstake_argv(wallet_name: str, netuid: int, wallet_path: str, tolerance: float = 0.05) -> list[str]`
  - `list_wallets(run=subprocess.run, wallet_path: str = ...) -> dict[str, str]` mapping coldkey ss58 → wallet name
  - `run_btcli(argv: list[str], env: dict, timeout: int, run=subprocess.run) -> dict` — parsed JSON, raises `BtcliError` on non-zero exit or unparseable output

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signer_btcli.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_signer_btcli.py -v`
Expected: FAIL — `ModuleNotFoundError: emission_tracker.signer.btcli`

- [ ] **Step 3: Implement the wrapper**

```python
# src/emission_tracker/signer/btcli.py
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
```

Note: `run_btcli` is called with `env={}` here only for wallet listing, which needs no passphrase. Task 4 passes a real environment.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signer_btcli.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/emission_tracker/signer/btcli.py tests/test_signer_btcli.py
git commit -m "feat: btcli command construction with a mandatory slippage guard"
```

---

### Task 4: The signer service

**Files:**
- Create: `src/emission_tracker/signer/server.py`
- Create: `src/emission_tracker/signer/__main__.py`
- Create: `deploy/emission-signer.service`
- Create: `deploy/signer.example.yaml`
- Test: `tests/test_signer_server.py`

**Interfaces:**
- Consumes: `protocol.SignRequest`, `protocol.SignResult`, `btcli.transfer_argv`, `btcli.unstake_argv`, `btcli.list_wallets`, `btcli.run_btcli`, `btcli.BtcliError`.
- Produces:
  - `SignerConfig(destination: str, fees_tao: dict[str, float], netuid: int, wallet_path: str, max_transfer_tao: float, daily_cap_tao: float, credentials_dir: str)`
  - `Signer(config, run=subprocess.run, clock=time.time)` with `handle(request: SignRequest) -> SignResult`
  - `serve(socket_path: str, signer: Signer) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signer_server.py
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_signer_server.py -v`
Expected: FAIL — `ModuleNotFoundError: emission_tracker.signer.server`

- [ ] **Step 3: Implement the signer**

```python
# src/emission_tracker/signer/server.py
"""The only component that can sign.

It accepts two intents and decides every consequential value itself: the
destination, the amount, the subnet, the slippage guard. A caller that
could name any of those would make the privilege split decorative.
"""

import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from emission_tracker.signer.btcli import (
    BtcliError,
    list_wallets,
    run_btcli,
    transfer_argv,
    unstake_argv,
)
from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    ProtocolError,
    SignRequest,
    SignResult,
)

log = logging.getLogger("emission_signer")

RAO = 10**9
BTCLI_TIMEOUT = 300


@dataclass
class SignerConfig:
    destination: str
    fees_tao: dict
    netuid: int
    wallet_path: str
    max_transfer_tao: float
    daily_cap_tao: float
    credentials_dir: str


@dataclass
class _DaySpend:
    day: str = ""
    rao: int = 0


class Signer:
    def __init__(self, config: SignerConfig, run=subprocess.run, clock=time.time):
        self._config = config
        self._run = run
        self._clock = clock
        self._spent = _DaySpend()

    def handle(self, request: SignRequest) -> SignResult:
        try:
            return self._handle(request)
        except BtcliError as exc:
            log.warning("op=%s coldkey=%s failed: %s",
                        request.op, request.coldkey, exc)
            return SignResult(False, request.op, request.coldkey, error=str(exc))
        except Exception as exc:  # never let the socket loop die
            log.exception("op=%s coldkey=%s crashed", request.op, request.coldkey)
            return SignResult(False, request.op, request.coldkey, error=repr(exc))

    def _handle(self, request: SignRequest) -> SignResult:
        wallets = list_wallets(run=self._run, wallet_path=self._config.wallet_path)
        name = wallets.get(request.coldkey)
        if name is None:
            return SignResult(
                False, request.op, request.coldkey,
                error=f"unknown coldkey {request.coldkey!r}",
            )

        env = self._env_for(name)

        if request.op == OP_UNSTAKE:
            # Log before attempting: the audit trail must survive a crash
            # between here and the chain.
            log.info("unstake_all coldkey=%s wallet=%s", request.coldkey, name)
            payload = run_btcli(
                unstake_argv(name, self._config.netuid, self._config.wallet_path),
                env=env, timeout=BTCLI_TIMEOUT, run=self._run,
            )
            return SignResult(True, request.op, request.coldkey,
                              tx_hash=_tx_hash(payload))

        amount_rao = sum(
            round(self._config.fees_tao[t] * RAO) for t in request.types
        )
        amount_tao = amount_rao / RAO

        if amount_tao > self._config.max_transfer_tao:
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"{amount_tao} τ exceeds the per-request cap "
                      f"({self._config.max_transfer_tao} τ)",
            )
        if not self._within_daily_cap(amount_rao):
            return SignResult(
                False, request.op, request.coldkey, amount_rao=amount_rao,
                error=f"daily cap of {self._config.daily_cap_tao} τ would be exceeded",
            )

        log.info("pay_tournament coldkey=%s wallet=%s types=%s amount=%s",
                 request.coldkey, name, ",".join(request.types), amount_tao)
        payload = run_btcli(
            transfer_argv(name, self._config.destination, amount_tao,
                          self._config.wallet_path),
            env=env, timeout=BTCLI_TIMEOUT, run=self._run,
        )
        self._record_spend(amount_rao)
        return SignResult(True, request.op, request.coldkey,
                          amount_rao=amount_rao, tx_hash=_tx_hash(payload))

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

    def _passphrase_for(self, wallet_name: str) -> str:
        """Read one passphrase from the systemd credentials directory.

        systemd puts each LoadCredential= item in its own file under
        $CREDENTIALS_DIRECTORY, readable only by this unit. Keeping them
        there instead of the environment means they never show up in
        /proc/<pid>/environ or `systemctl show`.
        """
        path = Path(self._config.credentials_dir) / f"wallet-{wallet_name}"
        return path.read_text().strip()

    def _env_for(self, wallet_name: str) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
            "BT_WALLET_PASSWORD": self._passphrase_for(wallet_name),
        }
        return env


def _tx_hash(payload: dict) -> str | None:
    for key in ("tx_hash", "transaction_hash", "extrinsic_hash", "hash"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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
    server.bind(socket_path)
    os.chmod(socket_path, 0o660)
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
```

```python
# src/emission_tracker/signer/__main__.py
"""Entry point: `python -m emission_tracker.signer /etc/emission-signer/config.yaml`"""

import logging
import os
import sys

import yaml

from emission_tracker.signer.server import Signer, SignerConfig, serve


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/emission-signer/config.yaml"
    raw = yaml.safe_load(open(config_path))
    socket_path = raw.pop("socket_path", "/run/emission-signer.sock")
    raw.setdefault(
        "credentials_dir", os.environ.get("CREDENTIALS_DIRECTORY", "/run/credentials")
    )
    serve(socket_path, Signer(SignerConfig(**raw)))


if __name__ == "__main__":
    main()
```

```yaml
# deploy/signer.example.yaml
socket_path: /run/emission-signer.sock
destination: 5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo
fees_tao:
  text: 0.7
  image: 0.4
  env: 0.6
netuid: 56
wallet_path: /root/.bittensor/wallets
# Hard ceilings enforced here, where a buggy or compromised web app
# cannot reach them.
max_transfer_tao: 2.0
daily_cap_tao: 30.0
```

```ini
# deploy/emission-signer.service
[Unit]
Description=Gradient Emission Tracker — transaction signer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=signer
Group=emission
WorkingDirectory=/opt/emission-tracker
ExecStart=/opt/emission-tracker/.venv/bin/python -m emission_tracker.signer \
    /etc/emission-signer/config.yaml

# One credential per wallet. systemd exposes these as files under
# $CREDENTIALS_DIRECTORY, readable only by this unit — unlike environment
# variables, they do not appear in /proc/<pid>/environ or systemctl show.
# Add one line per wallet name from `btcli wallet list`.
LoadCredential=wallet-prj1:/etc/emission-signer/passphrases/prj1
LoadCredential=wallet-prj2:/etc/emission-signer/passphrases/prj2
LoadCredential=wallet-utama:/etc/emission-signer/passphrases/utama
# … one per wallet

Restart=on-failure
RestartSec=10

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/run
# This unit is the one component that may read the wallets.
ReadOnlyPaths=/root/.bittensor/wallets

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signer_server.py -v && .venv/bin/python -m pytest -q`
Expected: 7 passed in the new file, whole suite green.

- [ ] **Step 5: Verify the unstake scoping on-chain before anything depends on it**

The spec flags this as unverified, and no amount of reading settles it. On the VPS, pick the smallest wallet (`5D4TxRCyga…`, Goy, 0.24 α) and run the command by hand with prompts **on**, so you can read the plan and abort:

```bash
btcli stake remove --unstake-all --netuid 56 --all-hotkeys \
    --safe-staking --tolerance 0.05 --allow-partial-stake \
    --wallet-name goy --wallet-path /root/.bittensor/wallets
```

Read what it says it will unstake **before confirming**. If it lists stake on any netuid other than 56, stop: `--unstake-all` is not subnet-scoped, and `unstake_argv` must be rewritten (likely `--amount` per hotkey instead) before Task 6 exposes the button. Record the outcome in the plan file.

- [ ] **Step 6: Commit**

```bash
git add src/emission_tracker/signer/ deploy/emission-signer.service \
        deploy/signer.example.yaml tests/test_signer_server.py
git commit -m "feat: signing service with hard-coded destination and spend caps"
```

---

### Task 5: Tracker side — config, table, client, endpoints

**Files:**
- Modify: `src/emission_tracker/config.py`
- Modify: `src/emission_tracker/db.py`
- Create: `src/emission_tracker/web/signer_client.py`
- Modify: `src/emission_tracker/web/queries.py`
- Modify: `src/emission_tracker/web/routes_api.py`
- Modify: `src/emission_tracker/main.py`
- Modify: `config.example.yaml`
- Test: `tests/test_signed_actions.py`

**Interfaces:**
- Consumes: `signer.protocol.SignRequest`, `SignResult`, `OP_PAY`, `OP_UNSTAKE`, `TOURNAMENT_TYPES`.
- Produces:
  - `config.TournamentConfig(address: str, fees_tao: dict[str, float])`, `AppConfig.tournament: TournamentConfig | None`, `AppConfig.signer_socket: str`
  - `web.signer_client.SignerClient(socket_path, timeout=300)` with `.send(request: SignRequest) -> SignResult`; raises `SignerUnavailable`
  - `queries.pending_action(conn, coldkey) -> dict | None`
  - `queries.record_action(conn, coldkey, op, types, amount_rao, requested_by) -> int`
  - `queries.finish_action(conn, action_id, ok, tx_hash, error) -> None`
  - `queries.recent_actions(conn, limit=50) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_signed_actions.py
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.signer.protocol import OP_PAY, SignResult
from emission_tracker.web import queries
from emission_tracker.web.routes_api import router as api_router

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"
HK = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"


class _FakeSigner:
    def __init__(self, result=None, boom=None):
        self.sent = []
        self._result = result
        self._boom = boom

    def send(self, request):
        self.sent.append(request)
        if self._boom:
            raise self._boom
        return self._result or SignResult(
            True, request.op, request.coldkey,
            amount_rao=700_000_000, tx_hash="0xdead",
        )


@pytest.fixture
def app():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    sync_team(conn, [PersonConfig(name="Alice",
                                  hotkeys=[{"hotkey": HK, "coldkey": CK}])],
              subnet_id=56)
    # Enough free balance for a 0.7 τ fee.
    conn.execute(
        "INSERT INTO coldkey_balances (coldkey_ss58, fetched_at, balance_free_rao, "
        "tournament_seen) VALUES (?, '2026-09-11T00:00:00+00:00', 900000000, 1)",
        (CK,),
    )
    conn.commit()
    a = FastAPI()
    a.include_router(api_router, prefix="/api")
    a.state.db_conn = conn
    a.state.config = SimpleNamespace(
        admin_users=["alice"],
        proxy_secret="",
        tournament=SimpleNamespace(
            address="5Ef5", fees_tao={"text": 0.7, "image": 0.4, "env": 0.6}
        ),
    )
    yield a
    conn.close()


def _post(app, path, json=None, user="alice"):
    return TestClient(app).post(path, json=json, headers={"X-Remote-User": user})


def test_admin_pays_and_the_attempt_is_recorded(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake

    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 200
    assert fake.sent[0].op == OP_PAY
    assert fake.sent[0].types == ("text",)

    row = queries.recent_actions(app.state.db_conn)[0]
    assert row["status"] == "ok"
    assert row["tx_hash"] == "0xdead"
    assert row["requested_by"] == "alice"


def test_the_request_never_carries_an_amount(app, monkeypatch):
    """The signer decides amounts; if the tracker could name one, moving
    the boundary to the signer would have bought nothing."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    _post(app, f"/api/tournament/pay/{CK}", {"types": ["text", "env"]})
    assert not hasattr(fake.sent[0], "amount_tao")
    assert not hasattr(fake.sent[0], "amount_rao")


def test_non_admin_cannot_pay(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]}, user="mallory")
    assert r.status_code == 403
    assert fake.sent == []


def test_insufficient_balance_is_refused_before_signing(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    # 1.7 τ needed, 0.9 τ available.
    r = _post(app, f"/api/tournament/pay/{CK}",
              {"types": ["text", "image", "env"]})
    assert r.status_code == 409
    assert fake.sent == []


def test_a_pending_action_blocks_a_second_one(app, monkeypatch):
    """A double click or a reload must not pay twice."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    queries.record_action(app.state.db_conn, CK, OP_PAY, ["text"],
                          700_000_000, "alice")
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 409
    assert fake.sent == []


def test_unknown_coldkey_is_refused(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, "/api/tournament/pay/5NOPE", {"types": ["text"]})
    assert r.status_code == 404
    assert fake.sent == []


def test_signer_failure_marks_the_row_failed_and_reports_it(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    app.state.signer = _FakeSigner(
        result=SignResult(False, OP_PAY, CK, error="insufficient balance")
    )
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 502
    row = queries.recent_actions(app.state.db_conn)[0]
    assert row["status"] == "failed"
    assert "insufficient balance" in row["error"]
    # Crucially not left pending, or the coldkey is blocked forever.
    assert queries.pending_action(app.state.db_conn, CK) is None


def test_unstake_endpoint_sends_no_types(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/stake/unstake-all/{CK}")
    assert r.status_code == 200
    assert fake.sent[0].types == ()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_signed_actions.py -v`
Expected: FAIL — the endpoints do not exist (404) and `queries.recent_actions` is undefined.

- [ ] **Step 3: Add the table**

```python
# src/emission_tracker/db.py — new entry in SCHEMA_STATEMENTS, before the indexes
    """
    CREATE TABLE IF NOT EXISTS signed_actions (
        id            INTEGER PRIMARY KEY,
        coldkey_ss58  TEXT      NOT NULL,
        op            TEXT      NOT NULL,
        types         TEXT,
        amount_rao    INTEGER   NOT NULL DEFAULT 0,
        -- 'pending' is written before the signer is called, so a crash
        -- mid-flight leaves evidence rather than silence.
        status        TEXT      NOT NULL CHECK (status IN ('pending','ok','failed')),
        tx_hash       TEXT,
        error         TEXT,
        requested_by  TEXT      NOT NULL,
        requested_at  TIMESTAMP NOT NULL,
        finished_at   TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_signed_actions_coldkey "
    "ON signed_actions(coldkey_ss58, status)",
```

- [ ] **Step 4: Add the queries**

```python
# src/emission_tracker/web/queries.py — append

def pending_action(conn: sqlite3.Connection, coldkey: str) -> dict | None:
    """An in-flight action for this coldkey, if any.

    Used to refuse a second request: paying twice because someone clicked
    twice is the failure this guards against.
    """
    row = conn.execute(
        "SELECT * FROM signed_actions WHERE coldkey_ss58 = ? AND status = 'pending' "
        "ORDER BY id DESC LIMIT 1",
        (coldkey,),
    ).fetchone()
    return dict(row) if row else None


def record_action(
    conn: sqlite3.Connection,
    coldkey: str,
    op: str,
    types: list[str],
    amount_rao: int,
    requested_by: str,
) -> int:
    cursor = conn.execute(
        "INSERT INTO signed_actions (coldkey_ss58, op, types, amount_rao, status, "
        "requested_by, requested_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (coldkey, op, ",".join(types), amount_rao, requested_by,
         datetime.now(timezone.utc)),
    )
    conn.commit()
    return cursor.lastrowid


def finish_action(
    conn: sqlite3.Connection,
    action_id: int,
    ok: bool,
    tx_hash: str | None,
    error: str | None,
) -> None:
    conn.execute(
        "UPDATE signed_actions SET status = ?, tx_hash = ?, error = ?, "
        "finished_at = ? WHERE id = ?",
        ("ok" if ok else "failed", tx_hash, error,
         datetime.now(timezone.utc), action_id),
    )
    conn.commit()


def recent_actions(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM signed_actions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Add the socket client**

```python
# src/emission_tracker/web/signer_client.py
"""Talks to the signer over its unix socket.

Deliberately thin: one connection per request, no retry. A money-moving
call that retries itself turns one intended payment into several, and the
caller cannot tell whether a timed-out transfer landed.
"""

import socket

from emission_tracker.signer.protocol import ProtocolError, SignRequest, SignResult


class SignerUnavailable(Exception):
    """The signer could not be reached or gave an unreadable answer."""


class SignerClient:
    def __init__(self, socket_path: str, timeout: float = 300.0):
        self._socket_path = socket_path
        self._timeout = timeout

    def send(self, request: SignRequest) -> SignResult:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self._timeout)
                sock.connect(self._socket_path)
                sock.sendall(request.to_line())
                line = b""
                while not line.endswith(b"\n"):
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    line += chunk
        except OSError as exc:
            raise SignerUnavailable(f"signer unreachable: {exc}") from exc
        if not line:
            raise SignerUnavailable("signer closed without answering")
        try:
            return SignResult.from_line(line)
        except ProtocolError as exc:
            raise SignerUnavailable(str(exc)) from exc
```

- [ ] **Step 6: Add the config**

```python
# src/emission_tracker/config.py
class TournamentConfig(BaseModel):
    address: str
    # Display and the balance check only — the signer keeps the copy that
    # actually decides what gets transferred.
    fees_tao: dict[str, float]


# inside AppConfig
    tournament: TournamentConfig | None = None
    signer_socket: str = "/run/emission-signer.sock"
```

```yaml
# config.example.yaml — append
# Tournament buy-ins. These drive the dashboard's arithmetic and its
# balance check; the signer holds its own authoritative copy and ignores
# this one.
tournament:
  address: 5Ef5JgNv14LY4UEQFHbRQkf8TnegDV3AfAbcsJe5T2w6VQdo
  fees_tao:
    text: 0.7
    image: 0.4
    env: 0.6

signer_socket: /run/emission-signer.sock
```

- [ ] **Step 7: Add the endpoints**

```python
# src/emission_tracker/web/routes_api.py — append

def _signer(request: Request):
    client = getattr(request.app.state, "signer", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Signer not configured")
    return client


def _known_coldkey(request: Request, coldkey: str) -> None:
    row = _db(request).execute(
        "SELECT 1 FROM hotkeys WHERE coldkey_ss58 = ? LIMIT 1", (coldkey,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown coldkey {coldkey!r}")


def _run_signed_action(request: Request, sign_request, amount_rao: int, user: str):
    """Record, send, record the outcome. Shared by both endpoints so the
    audit row can never be skipped by one of them."""
    conn = _db(request)
    if queries.pending_action(conn, sign_request.coldkey):
        raise HTTPException(
            status_code=409, detail="An action for this coldkey is already running"
        )
    action_id = queries.record_action(
        conn, sign_request.coldkey, sign_request.op,
        list(sign_request.types), amount_rao, user,
    )
    try:
        result = _signer(request).send(sign_request)
    except SignerUnavailable as exc:
        queries.finish_action(conn, action_id, False, None, str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    queries.finish_action(conn, action_id, result.ok, result.tx_hash, result.error)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error or "signing failed")
    return {
        "ok": True,
        "action_id": action_id,
        "amount_rao": result.amount_rao,
        "tx_hash": result.tx_hash,
    }


class TournamentPayBody(BaseModel):
    types: list[str]


@router.post("/tournament/pay/{coldkey}")
def pay_tournament(
    request: Request,
    coldkey: str,
    body: TournamentPayBody,
    user: str = Depends(require_admin),
):
    _known_coldkey(request, coldkey)
    config = getattr(request.app.state, "config", None)
    tournament = getattr(config, "tournament", None) if config else None
    if tournament is None:
        raise HTTPException(status_code=503, detail="Tournament fees not configured")

    for t in body.types:
        if t not in TOURNAMENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown type {t!r}")
    if not body.types or len(set(body.types)) != len(body.types):
        raise HTTPException(status_code=400, detail="Pick each type at most once")

    amount_rao = sum(round(tournament.fees_tao[t] * 10**9) for t in body.types)

    # Pre-flight only. The signer re-decides the amount and btcli checks the
    # real balance; this exists so the common case fails fast and visibly
    # rather than as a chain error.
    row = _db(request).execute(
        "SELECT balance_free_rao FROM coldkey_balances WHERE coldkey_ss58 = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (coldkey,),
    ).fetchone()
    free = (row["balance_free_rao"] if row else None) or 0
    if free < amount_rao:
        raise HTTPException(
            status_code=409,
            detail=f"Balance {free / 1e9:.4f} τ is short of {amount_rao / 1e9:.4f} τ",
        )

    return _run_signed_action(
        request, SignRequest(OP_PAY, coldkey, tuple(body.types)), amount_rao, user
    )


@router.post("/stake/unstake-all/{coldkey}")
def unstake_all(
    request: Request, coldkey: str, user: str = Depends(require_admin)
):
    _known_coldkey(request, coldkey)
    return _run_signed_action(
        request, SignRequest(OP_UNSTAKE, coldkey), 0, user
    )
```

Add to the imports at the top of `routes_api.py`:

```python
from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    TOURNAMENT_TYPES,
    SignRequest,
)
from emission_tracker.web.signer_client import SignerUnavailable
```

- [ ] **Step 8: Wire the client into the app**

```python
# src/emission_tracker/main.py — in lifespan, beside app.state.balance_runner
        app.state.signer = SignerClient(config.signer_socket)
```

with `from emission_tracker.web.signer_client import SignerClient` at the top.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signed_actions.py -v && .venv/bin/python -m pytest -q`
Expected: 8 passed in the new file, whole suite green. `tests/test_db.py::test_init_schema_creates_all_tables` pins the table list — add `"signed_actions"` in alphabetical position when it fails.

- [ ] **Step 10: Commit**

```bash
git add src/emission_tracker/ config.example.yaml tests/test_signed_actions.py \
        tests/test_db.py
git commit -m "feat: tracker endpoints for tournament payment and unstake"
```

---

### Task 6: The card controls

**Files:**
- Modify: `src/emission_tracker/web/templates/dashboard.html`
- Modify: `src/emission_tracker/web/static/style.css`
- Modify: `src/emission_tracker/web/routes_pages.py`
- Test: `tests/test_routes_pages.py`

**Interfaces:**
- Consumes: `POST /api/tournament/pay/{coldkey}`, `POST /api/stake/unstake-all/{coldkey}`, card fields from `queries.coldkey_cards`.
- Produces: no Python interface.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_routes_pages.py — append to TestDashboardAdminScripts

    def test_admin_sees_the_tournament_controls_and_their_handler(
        self, app, monkeypatch
    ):
        html = self._html(app, monkeypatch, "alice")
        assert 'class="tournament-type"' in html
        assert "/api/tournament/pay/" in html
        assert "/api/stake/unstake-all/" in html
        # Irreversible and slippage-bearing: must not be a bare click.
        assert "confirm" in html.lower()

    def test_non_admin_sees_no_money_controls(self, app, monkeypatch):
        html = self._html(app, monkeypatch, "mallory")
        assert 'class="tournament-type"' not in html
        assert "/api/tournament/pay/" not in html
        assert "/api/stake/unstake-all/" not in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_routes_pages.py -k AdminScripts -v`
Expected: FAIL — the markup is absent.

- [ ] **Step 3: Pass the fee table to the template**

```python
# src/emission_tracker/web/routes_pages.py — in the dashboard handler,
# beside the existing subnet_id lookup
        tournament = getattr(
            getattr(request.app.state, "config", None), "tournament", None
        )
```

and add `"tournament": tournament,` to the context dict.

- [ ] **Step 4: Add the markup**

Inside the card, after the Tournament stat row and before `</article>`:

```html
            {% if is_admin and tournament %}
            <div class="coldkey-actions">
                <div class="tournament-types">
                    {% for t, fee in tournament.fees_tao.items() %}
                    <label class="tournament-type">
                        <input type="checkbox" data-coldkey="{{ c.coldkey }}"
                               data-fee="{{ fee }}" value="{{ t }}">
                        {{ t }} <span class="text-muted">{{ fee }} τ</span>
                    </label>
                    {% endfor %}
                </div>
                <div class="coldkey-action-row">
                    <span class="tournament-total text-muted"
                          data-coldkey="{{ c.coldkey }}"
                          data-free="{{ c.balance_free_rao or 0 }}">—</span>
                    <button class="btn-subtle pay-fee" data-coldkey="{{ c.coldkey }}"
                            disabled>Pay fee</button>
                </div>
                <button class="btn-subtle unstake-all" data-coldkey="{{ c.coldkey }}"
                        data-label="{{ c.name }}">Unstake all</button>
            </div>
            {% endif %}
```

- [ ] **Step 5: Add the handler**

Inside the existing admin `<script>` block (which must stay **before**
`{% endblock %}` — a script after it never renders), append before the
closing `})();`:

```javascript
    // --- tournament fees and unstake --------------------------------
    const moneyButtons = Array.from(
        document.querySelectorAll('.pay-fee, .unstake-all')
    );

    function totalFor(coldkey) {
        return Array.from(document.querySelectorAll(
            `.tournament-type input[data-coldkey="${coldkey}"]:checked`
        )).reduce((sum, el) => sum + parseFloat(el.dataset.fee), 0);
    }

    function refreshTotals() {
        document.querySelectorAll('.tournament-total').forEach(el => {
            const ck = el.dataset.coldkey;
            const total = totalFor(ck);
            const freeTao = parseInt(el.dataset.free || '0', 10) / 1e9;
            const btn = document.querySelector(`.pay-fee[data-coldkey="${ck}"]`);
            if (total === 0) {
                el.textContent = '—';
                btn.disabled = true;
                return;
            }
            const short = total > freeTao;
            el.textContent = short
                ? `${total.toFixed(2)} τ — saldo kurang`
                : `${total.toFixed(2)} τ`;
            el.classList.toggle('is-short', short);
            btn.disabled = short;
        });
    }

    document.querySelectorAll('.tournament-type input').forEach(el => {
        el.addEventListener('change', refreshTotals);
    });
    refreshTotals();

    async function runMoneyAction(url, body, button) {
        moneyButtons.forEach(b => { b.disabled = true; });
        const original = button.textContent;
        button.textContent = 'Sending…';
        try {
            const r = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined,
            });
            const payload = await r.json().catch(() => ({}));
            if (!r.ok) {
                note.textContent = payload.detail || `failed (${r.status})`;
                button.textContent = original;
                moneyButtons.forEach(b => { b.disabled = false; });
                refreshTotals();
                return;
            }
            note.textContent = 'done — reloading';
            location.reload();
        } catch (e) {
            note.textContent = 'request failed';
            button.textContent = original;
            moneyButtons.forEach(b => { b.disabled = false; });
            refreshTotals();
        }
    }

    document.querySelectorAll('.pay-fee').forEach(b => {
        b.addEventListener('click', () => {
            const ck = b.dataset.coldkey;
            const types = Array.from(document.querySelectorAll(
                `.tournament-type input[data-coldkey="${ck}"]:checked`
            )).map(el => el.value);
            if (!types.length) return;
            if (!window.confirm(
                `Kirim ${totalFor(ck).toFixed(2)} τ fee turnamen `
                + `(${types.join(', ')}) dari wallet ini?`)) return;
            runMoneyAction(`/api/tournament/pay/${encodeURIComponent(ck)}`,
                           { types }, b);
        });
    });

    document.querySelectorAll('.unstake-all').forEach(b => {
        b.addEventListener('click', () => {
            // Typed confirmation, not a yes/no: this is irreversible and
            // costs slippage, so it should be hard to do by reflex.
            const label = b.dataset.label;
            const typed = window.prompt(
                `Unstake SEMUA di subnet 56 untuk wallet ini?\n`
                + `Tidak bisa dibatalkan dan kena slippage.\n\n`
                + `Ketik "${label}" untuk melanjutkan:`);
            if (typed !== label) return;
            runMoneyAction(
                `/api/stake/unstake-all/${encodeURIComponent(b.dataset.coldkey)}`,
                null, b);
        });
    });
```

- [ ] **Step 6: Add the styles**

```css
/* Money controls, set apart from the read-only figures above them so a
   click here never feels like the same kind of act as a refresh. */
.coldkey-actions {
    margin-top: 0.6rem;
    padding-top: 0.55rem;
    border-top: 1px solid var(--sn-border);
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
}
.tournament-types { display: flex; flex-direction: column; gap: 0.15rem; }
.tournament-type {
    font-size: 0.7rem;
    display: flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
}
.tournament-type input { margin: 0; }
.coldkey-action-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.4rem;
}
.tournament-total { font-size: 0.72rem; font-variant-numeric: tabular-nums; }
.tournament-total.is-short { color: var(--sn-warn); }
.unstake-all { width: 100%; color: var(--sn-warn); }
.unstake-all:hover:not(:disabled) { border-color: var(--sn-warn); }
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: whole suite green.

- [ ] **Step 8: Verify against a running server**

```bash
pkill -f "uvicorn emission_tracker"
EMISSION_CONFIG_PATH=config.local.yaml EMISSION_DEV_USER=admin \
  .venv/bin/uvicorn emission_tracker.main:create_app --factory --port 8001 &
sleep 7
curl -s http://127.0.0.1:8001/ | grep -c "tournament-type"     # expect 45 (15 × 3)
curl -s http://127.0.0.1:8001/ | grep -c "unstake-all"
```

With no signer socket present locally, clicking must surface `503 signer unreachable` in the note line rather than hanging or silently doing nothing. Confirm that, then stop the server.

- [ ] **Step 9: Commit**

```bash
git add src/emission_tracker/web/ tests/test_routes_pages.py
git commit -m "feat: tournament fee and unstake controls on the coldkey cards"
```

---

## Deployment notes

Not a task — the steps an operator runs once, after all six land.

```bash
# 1. signer user, able to read the wallets
sudo useradd -r -s /usr/sbin/nologin -G emission signer
sudo setfacl -R -m u:signer:rX /root/.bittensor/wallets

# 2. config and passphrases, root-only
sudo install -d -m 700 /etc/emission-signer/passphrases
sudo cp /opt/emission-tracker/deploy/signer.example.yaml \
        /etc/emission-signer/config.yaml
# one file per wallet name from `btcli wallet list`, mode 600, root-owned
sudo chmod 600 /etc/emission-signer/passphrases/*

# 3. unit — edit LoadCredential= to list every wallet first
sudo cp /opt/emission-tracker/deploy/emission-signer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now emission-signer
sudo systemctl status emission-signer
```

Then the proxy secret from Task 1, and restart the tracker.

Check the socket is reachable only by the tracker:

```bash
ls -l /run/emission-signer.sock      # srw-rw---- signer emission
sudo -u emission test -w /run/emission-signer.sock && echo "tracker can reach it"
sudo -u nobody test -w /run/emission-signer.sock || echo "others cannot"
```

## Self-review notes

- **Spec coverage.** Second service (T4), unix socket + 0660 (T4), hard-coded destination (T3/T4), signer-owned fee table (T4), derived wallet mapping (T3), caps (T4), audit log before attempt (T4 log, T5 row), X-Remote-User fix (T1), `signed_actions` table (T5), both endpoints (T5), pending guard (T5), checkboxes and balance-gated button (T6), typed confirmation (T6), never-automatic (no scheduler touched in any task; Task 4 Step 5 is a manual one-off).
- **Open risk carried forward.** Task 4 Step 5 is a gate, not a formality: if `--unstake-all --netuid 56` turns out not to be subnet-scoped, `unstake_argv` and its test change before Task 6 ships the button.
- **`--wallet-path` on every invocation** because the signer runs as `signer`, whose `$HOME` is not where the wallets live; relying on the default path would look for them in the wrong place.
