import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from emission_tracker.units import format_alpha, rao_to_alpha
from emission_tracker.web import queries
from emission_tracker.web.range_parse import parse_range

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _format_dt_seconds(value) -> str:
    """Render a datetime or ISO-format string trimmed to seconds (drop microseconds).

    Accepts datetime objects or strings like '2026-05-17 18:43:02.250567+00:00'
    (the format SQLite returns via CAST AS TEXT).
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    s = str(value)
    # ISO-ish strings: split on '.' (microseconds prefix), keep the tz suffix if any
    if "." in s:
        head, _dot, tail = s.partition(".")
        # tail may look like "250567+00:00"; keep any timezone suffix
        for tz_marker in ("+", "-", "Z"):
            if tz_marker in tail:
                idx = tail.index(tz_marker)
                return f"{head}{tail[idx:]}"
        return head
    return s


# Register Jinja2 filters: RAO → alpha conversion + format, datetime to seconds
templates.env.filters["alpha"] = format_alpha
templates.env.filters["to_alpha"] = rao_to_alpha
templates.env.filters["dt_s"] = _format_dt_seconds


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db_conn


def _range(preset, frm, to):
    try:
        return parse_range(preset=preset, from_str=frm, to_str=to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def register_pages(app: FastAPI) -> None:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        range: str | None = Query(default="all"),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ):
        from_dt, to_dt = _range(range, from_, to)
        conn = _db(request)
        persons = queries.dashboard_summary(conn, from_dt=from_dt, to_dt=to_dt)
        total = sum(p["cumulative"] for p in persons)
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "persons": persons,
                "total_cumulative": total,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "active_range": range or "all",
                "latest": latest,
            },
        )

    @app.get("/person/{name}", response_class=HTMLResponse)
    def person_detail(
        request: Request,
        name: str,
        range: str | None = Query(default="all"),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ):
        from_dt, to_dt = _range(range, from_, to)
        conn = _db(request)
        series = queries.person_series(conn, name=name, from_dt=from_dt, to_dt=to_dt)
        total = series[-1]["cumulative"] if series else 0.0
        hotkeys = [
            r["ss58"]
            for r in conn.execute(
                """
                SELECT h.ss58 FROM hotkeys h
                JOIN persons p ON p.id = h.person_id
                WHERE p.name = ? ORDER BY h.ss58
                """,
                (name,),
            ).fetchall()
        ]
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "person.html",
            {
                "name": name,
                "hotkeys": hotkeys,
                "total_cumulative": total,
                "chart_labels": [str(s["taken_at"]) for s in series],
                "chart_cumulative": [rao_to_alpha(s["cumulative"]) for s in series],
                "chart_per_snap": [rao_to_alpha(s["per_snapshot_emission"]) for s in series],
                "active_range": range or "all",
                "latest": latest,
            },
        )
