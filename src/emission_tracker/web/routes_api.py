import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from emission_tracker.web import queries
from emission_tracker.web.auth import require_admin
from emission_tracker.web.range_parse import parse_range

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
