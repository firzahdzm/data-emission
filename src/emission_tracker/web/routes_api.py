import sqlite3

from fastapi import APIRouter, HTTPException, Query, Request

from emission_tracker.web import queries
from emission_tracker.web.range_parse import parse_range

router = APIRouter()


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
