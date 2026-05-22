import sqlite3

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from emission_tracker.web import queries
from emission_tracker.web.auth import require_admin
from emission_tracker.web.range_parse import parse_range

router = APIRouter()


class SettlementCreateBody(BaseModel):
    note: str | None = None
    total_idr: int | None = None
    base_salary_idr: int | None = None


class SettlementDistributionBody(BaseModel):
    total_idr: int
    base_salary_idr: int


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
    body: SettlementCreateBody = Body(default_factory=SettlementCreateBody),
    user: str = Depends(require_admin),
):
    """Admin only. Freeze per-hotkey cumulative emission since the previous
    settlement into a new settlement record. Resets the live dashboard
    accumulation back to 0 for the next period."""
    try:
        settlement = queries.create_settlement(
            _db(request),
            note=body.note,
            total_idr=body.total_idr,
            base_salary_idr=body.base_salary_idr,
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


@router.put("/settlements/{settlement_id}/distribution")
def set_settlement_distribution_endpoint(
    request: Request,
    settlement_id: int,
    body: SettlementDistributionBody,
    user: str = Depends(require_admin),
):
    """Admin only. Compute or recompute the IDR payout distribution for an
    existing settlement. Safe to call repeatedly — each call overwrites the
    previous distribution."""
    try:
        updated = queries.set_settlement_distribution(
            _db(request),
            settlement_id,
            total_idr=body.total_idr,
            base_salary_idr=body.base_salary_idr,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return updated


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
