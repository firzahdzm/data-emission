import logging
import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, SecretStr

from emission_tracker.signer.protocol import (
    OP_PAY,
    OP_UNSTAKE,
    TOURNAMENT_TYPES,
    SignRequest,
)
from emission_tracker.web import queries
from emission_tracker.web.auth import require_admin
from emission_tracker.web.range_parse import parse_range
from emission_tracker.web.signer_client import SignerUnavailable

log = logging.getLogger(__name__)

router = APIRouter()


class SettlementCreateBody(BaseModel):
    token_price_usd: float
    note: str | None = None
    # Optional: explicit boundary. When None, the latest ok/partial snapshot
    # since the last settlement is used (preserves pre-existing behavior).
    settled_through_snapshot_id: int | None = None


class KasDistributionBody(BaseModel):
    amount_usd: float
    note: str | None = None


class SalaryPaymentBody(BaseModel):
    amount_per_person_usd: float
    note: str | None = None


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db_conn


def _range(preset: str | None, frm: str | None, to: str | None):
    try:
        return parse_range(preset=preset, from_str=frm, to_str=to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/healthz")
def healthz():
    return "ok"


@router.get("/persons")
def get_persons(
    request: Request,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    rows = queries.dashboard_summary(_db(request), from_dt=from_dt, to_dt=to_dt)
    return {"persons": rows, "range": {"from": from_dt.isoformat(), "to": to_dt.isoformat()}}


@router.get("/persons/{name}/series")
def get_person_series(
    request: Request,
    name: str,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    series = queries.person_series(_db(request), name=name, from_dt=from_dt, to_dt=to_dt)
    return {"name": name, "series": series}


@router.get("/hotkeys/{ss58}/series")
def get_hotkey_series(
    request: Request,
    ss58: str,
    range: str | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
):
    from_dt, to_dt = _range(range, from_, to)
    series = queries.hotkey_series(_db(request), hotkey=ss58, from_dt=from_dt, to_dt=to_dt)
    return {"hotkey": ss58, "series": series}


@router.get("/snapshots/latest")
def get_latest_snapshot(request: Request):
    snap = queries.latest_snapshot(_db(request))
    if snap is None:
        raise HTTPException(status_code=404, detail="No snapshots yet")
    return snap


@router.get("/settlements/settleable-snapshots")
def settleable_snapshots_endpoint(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
):
    """Snapshots that can be chosen as the boundary for the next Close Period.

    A snapshot is settleable when it is past the last settlement boundary
    AND its status is ok/partial. Returned newest first, capped by `limit`.
    """
    return {
        "snapshots": queries.settleable_snapshots(_db(request), limit=limit),
        "limit": limit,
    }


@router.get("/settlements")
def list_settlements(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    """Recent settlements (close-period events), newest first."""
    return {
        "settlements": queries.list_settlements(_db(request), limit=limit),
        "limit": limit,
    }


@router.get("/settlements/{settlement_id}")
def settlement_detail(request: Request, settlement_id: int):
    """One settlement plus its per-hotkey lines."""
    detail = queries.settlement_detail(_db(request), settlement_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return detail


@router.post("/settlements", status_code=201)
def create_settlement_endpoint(
    request: Request,
    body: SettlementCreateBody,
    user: str = Depends(require_admin),
):
    """Admin only. Atomically freeze the current period AND compute payout:

    - reward_usd = 30% × (emission × token_price_usd)   ← per person
    - kas_contribution_usd = 70% × …                    ← into kas bersama

    token_price_usd is captured immutably at settle time. To adjust the
    price, the admin must delete the settlement and re-create it.
    """
    try:
        settlement = queries.create_settlement(
            _db(request),
            token_price_usd=body.token_price_usd,
            note=body.note,
            settled_through_snapshot_id=body.settled_through_snapshot_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return settlement


@router.delete("/settlements/{settlement_id}", status_code=204)
def delete_settlement_endpoint(
    request: Request,
    settlement_id: int,
    user: str = Depends(require_admin),
):
    """Admin only. Remove a settlement (cascades to its lines). The dashboard
    will reopen the period covered by that settlement."""
    if not queries.delete_settlement(_db(request), settlement_id):
        raise HTTPException(status_code=404, detail="Settlement not found")
    return None


@router.post("/settlements/{settlement_id}/mark-paid")
def mark_settlement_paid_endpoint(
    request: Request,
    settlement_id: int,
    user: str = Depends(require_admin),
):
    """Admin only. Stamp `paid_at = now` on a settlement to record that the
    payout has been disbursed to team members. Idempotent (clicking again
    just refreshes the timestamp). Returns the updated settlement detail."""
    result = queries.set_settlement_paid(_db(request), settlement_id, paid=True)
    if result is None:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return result


@router.post("/settlements/{settlement_id}/mark-unpaid")
def mark_settlement_unpaid_endpoint(
    request: Request,
    settlement_id: int,
    user: str = Depends(require_admin),
):
    """Admin only. Clear `paid_at` (set NULL) so the settlement returns to
    'unpaid' state — useful if a mark-paid click was a mistake."""
    result = queries.set_settlement_paid(_db(request), settlement_id, paid=False)
    if result is None:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return result


# ---- Kas Bersama endpoints ----


@router.get("/kas/balance")
def get_kas_balance(request: Request):
    """Running balance of kas bersama: total contributed (70% × every
    settlement's emission_idr) minus total already distributed."""
    return queries.kas_totals(_db(request))


@router.get("/kas/preview")
def preview_kas(request: Request, amount_usd: float = Query(ge=0)):
    """Read-only preview of how `amount_usd` would split across all-time
    contributors. Useful for the Distribusi kas form before confirming."""
    try:
        return {"shares": queries.preview_kas_distribution(_db(request), amount_usd)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/kas/distributions")
def list_kas(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "distributions": queries.list_kas_distributions(_db(request), limit=limit),
        "limit": limit,
    }


@router.get("/kas/distributions/{distribution_id}")
def kas_distribution_detail_endpoint(request: Request, distribution_id: int):
    detail = queries.kas_distribution_detail(_db(request), distribution_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Kas distribution not found")
    return detail


@router.post("/kas/distributions", status_code=201)
def create_kas_distribution_endpoint(
    request: Request,
    body: KasDistributionBody,
    user: str = Depends(require_admin),
):
    """Admin only. Freeze a kas-bersama distribution: per-person share
    proportional to their all-time emission (across all settlement_lines).
    Decrements the running kas balance."""
    try:
        return queries.create_kas_distribution(
            _db(request), amount_usd=body.amount_usd, note=body.note
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/kas/distributions/{distribution_id}", status_code=204)
def delete_kas_distribution_endpoint(
    request: Request,
    distribution_id: int,
    user: str = Depends(require_admin),
):
    """Admin only. Remove a kas distribution. Its amount returns to the
    running balance."""
    if not queries.delete_kas_distribution(_db(request), distribution_id):
        raise HTTPException(status_code=404, detail="Kas distribution not found")
    return None


@router.get("/salary/payments")
def list_salary(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
):
    return {
        "payments": queries.list_salary_payments(_db(request), limit=limit),
        "limit": limit,
    }


@router.get("/salary/payments/{payment_id}")
def salary_payment_detail_endpoint(request: Request, payment_id: int):
    detail = queries.salary_payment_detail(_db(request), payment_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Salary payment not found")
    return detail


@router.post("/salary/payments", status_code=201)
def create_salary_payment_endpoint(
    request: Request,
    body: SalaryPaymentBody,
    user: str = Depends(require_admin),
):
    """Admin only. Pay an equal base salary to every person on the team,
    deducted from the Fund balance. Unrelated to emission-based rewards."""
    try:
        return queries.create_salary_payment(
            _db(request),
            amount_per_person_usd=body.amount_per_person_usd,
            note=body.note,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/salary/payments/{payment_id}", status_code=204)
def delete_salary_payment_endpoint(
    request: Request,
    payment_id: int,
    user: str = Depends(require_admin),
):
    """Admin only. Remove a salary payment. Its total returns to the
    running Fund balance."""
    if not queries.delete_salary_payment(_db(request), payment_id):
        raise HTTPException(status_code=404, detail="Salary payment not found")
    return None


@router.get("/snapshots")
def list_snapshots(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
):
    """Recent snapshots (all statuses), newest first."""
    cursor = _db(request).execute(
        "SELECT id, CAST(taken_at AS TEXT) AS taken_at, block_number, status "
        "FROM snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return {"snapshots": [dict(r) for r in cursor.fetchall()], "limit": limit}


@router.get("/balances/status")
def balance_refresh_status(request: Request):
    """Whether a balance refresh is in flight, and how long one takes.

    The dashboard polls this after a click so the button can show progress
    instead of leaving the admin guessing for four minutes.
    """
    runner = getattr(request.app.state, "balance_runner", None)
    if runner is None:
        return {"available": False, "running": False}
    target = runner.target
    # Report the run in flight, not the roster: a single-coldkey refresh that
    # advertised the full count would show a four-minute countdown for work
    # that finishes in seconds.
    coldkey_count = len(target) if target else runner.coldkey_count()
    return {
        "available": True,
        "running": runner.is_running,
        "started_at": runner.started_at.isoformat() if runner.started_at else None,
        "coldkey_count": coldkey_count,
        "estimated_seconds": runner.estimate_seconds(coldkey_count),
        # Which coldkeys the running refresh covers; null means all of them.
        # Lets the dashboard spin one card instead of freezing every card.
        "target": target,
    }


@router.post("/balances/refresh", status_code=202)
def trigger_balance_refresh(
    request: Request,
    user: str = Depends(require_admin),
):
    """Start an out-of-band balance refresh. Admin only.

    Returns 202 immediately — the run takes minutes, so holding the request
    open would just time out the browser. 409 when one is already running:
    a second pass over the same coldkeys would double the API spend for an
    identical result.
    """
    runner = getattr(request.app.state, "balance_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Balance refresh not configured")

    coldkey_count = runner.coldkey_count()
    if not runner.start():
        raise HTTPException(
            status_code=409,
            detail="A balance refresh is already running",
        )
    log.info("balance refresh triggered by %s", user)
    return {
        "started": True,
        "coldkey_count": coldkey_count,
        "estimated_seconds": runner.estimate_seconds(coldkey_count),
    }


@router.post("/balances/refresh/{coldkey}", status_code=202)
def trigger_single_balance_refresh(
    request: Request,
    coldkey: str,
    user: str = Depends(require_admin),
):
    """Re-read one coldkey's balances. Admin only.

    Two API calls instead of thirty, so this returns in seconds rather than
    minutes — the point of the per-card button. It still takes the same lock
    as a full run: both share one TaoStats rate limiter, and letting them
    overlap would only make each slower.
    """
    runner = getattr(request.app.state, "balance_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="Balance refresh not configured")

    conn = _db(request)
    known = conn.execute(
        "SELECT 1 FROM hotkeys WHERE coldkey_ss58 = ? LIMIT 1", (coldkey,)
    ).fetchone()
    if known is None:
        raise HTTPException(status_code=404, detail=f"Unknown coldkey {coldkey!r}")

    if not runner.start([coldkey]):
        raise HTTPException(
            status_code=409,
            detail="A balance refresh is already running",
        )
    log.info("balance refresh for %s triggered by %s", coldkey, user)
    return {
        "started": True,
        "coldkey": coldkey,
        "coldkey_count": 1,
        "estimated_seconds": runner.estimate_seconds(1),
    }


@router.get("/price/alpha")
def get_alpha_price(request: Request):
    """Live price of one alpha, for pre-filling the Close-period dialog.

    Readable by any authenticated user — it is public market data, and the
    admin gate belongs on settling, not on looking at a price.

    Returns `available: false` rather than a guess when the feed cannot be
    read. The number is about to be frozen into a settlement that cannot be
    edited, so a silent fallback would be the worst possible answer.
    """
    cache = getattr(request.app.state, "alpha_price", None)
    if cache is None:
        return {"available": False, "reason": "price feed not configured"}
    price = cache.get()
    if price is None:
        return {"available": False, "reason": "price feed unavailable"}
    return {"available": True, **price}
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


def _require_secret(body) -> None:
    """Reject a blank wallet unlock value before anything else happens.

    An empty string would reach btcli as an empty environment variable, which
    it treats as no value at all — it would fall back to a prompt, and
    --no-prompt turns that into an opaque non-zero exit. Failing here says
    what actually went wrong, and does it before any audit row is written.
    """
    if not body.secret.get_secret_value().strip():
        raise HTTPException(status_code=400, detail="Wallet unlock value is required")


def _run_signed_action(request: Request, sign_request, amount_rao: int, user: str):
    """Record, send, record the outcome. Shared by both endpoints so the
    audit row can never be skipped by one of them."""
    conn = _db(request)
    # Resolved before any row is written: if the signer isn't configured,
    # raising here leaves no pending row behind to strand the coldkey.
    signer = _signer(request)

    if queries.pending_action(conn, sign_request.coldkey):
        raise HTTPException(
            status_code=409, detail="An action for this coldkey is already running"
        )
    try:
        action_id = queries.record_action(
            conn, sign_request.coldkey, sign_request.op,
            list(sign_request.types), amount_rao, user,
        )
    except sqlite3.IntegrityError:
        # The partial unique index caught a concurrent request that slipped
        # past the check above — the same 409 the explicit check gives.
        raise HTTPException(
            status_code=409, detail="An action for this coldkey is already running"
        )

    try:
        result = signer.send(sign_request)
    except SignerUnavailable as exc:
        queries.finish_action(conn, action_id, False, None, str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception:
        # Belt and braces: any other unanticipated failure must still
        # resolve the row, or the coldkey is blocked forever.
        queries.finish_action(conn, action_id, False, None, "unexpected error")
        raise

    queries.finish_action(
        conn, action_id, result.ok, result.tx_hash, result.error,
        # On success, replace our estimate with the signer's actual amount.
        amount_rao=result.amount_rao if result.ok else None,
        outcome_unknown=result.unknown,
    )
    if result.unknown:
        # 409, not 502: a failure invites the obvious retry, and here the
        # money may already have moved. The caller has to go and look.
        raise HTTPException(
            status_code=409,
            detail=f"Hasil tidak pasti — periksa chain sebelum mencoba lagi. "
                   f"{result.error or ''}".strip(),
        )
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
    # SecretStr so an accidental log or traceback of this model prints
    # '**********' instead of the value. Nothing is stored on the server:
    # it is forwarded to the signer for one btcli call and then gone.
    secret: SecretStr


class UnstakeBody(BaseModel):
    secret: SecretStr


@router.post("/tournament/pay/{coldkey}")
def pay_tournament(
    request: Request,
    coldkey: str,
    body: TournamentPayBody,
    user: str = Depends(require_admin),
):
    _known_coldkey(request, coldkey)
    _require_secret(body)
    config = getattr(request.app.state, "config", None)
    tournament = getattr(config, "tournament", None) if config else None
    if tournament is None:
        raise HTTPException(status_code=503, detail="Tournament fees not configured")

    for t in body.types:
        if t not in TOURNAMENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown type {t!r}")
        if t not in tournament.fees_tao:
            raise HTTPException(
                status_code=400, detail=f"Type {t!r} has no configured fee"
            )
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
    free = row["balance_free_rao"] if row else None
    # NULL means the last fetch failed, which is "unknown", not "zero".
    # Treating the two alike let one TaoStats hiccup block payments from a
    # funded wallet until the next daily refresh — up to a day. btcli
    # checks the real balance on chain regardless, so when we do not know,
    # defer to it rather than refuse.
    if free is not None and free < amount_rao:
        raise HTTPException(
            status_code=409,
            detail=f"Balance {free / 1e9:.4f} τ is short of {amount_rao / 1e9:.4f} τ",
        )

    return _run_signed_action(
        request,
        SignRequest(
            OP_PAY, coldkey, tuple(body.types),
            secret=body.secret.get_secret_value(),
        ),
        amount_rao,
        user,
    )


@router.post("/stake/unstake-all/{coldkey}")
def unstake_all(
    request: Request,
    coldkey: str,
    body: UnstakeBody,
    user: str = Depends(require_admin),
):
    _known_coldkey(request, coldkey)
    _require_secret(body)
    return _run_signed_action(
        request,
        SignRequest(OP_UNSTAKE, coldkey, secret=body.secret.get_secret_value()),
        0,
        user,
    )


@router.get("/actions/recent")
def recent_signed_actions(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    user: str = Depends(require_admin),
):
    """Audit trail of signed actions. Admin only.

    Unlike the other read endpoints this one is gated: it names who moved
    money and carries btcli's error text, which is operational detail the
    whole team has no reason to see.

    `running` lets the page decide whether to keep polling without
    re-deriving it from the rows on the client.
    """
    rows = queries.recent_actions(_db(request), limit=limit)
    return {
        "actions": rows,
        "running": any(r["status"] == "pending" for r in rows),
    }
