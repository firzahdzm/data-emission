import sqlite3
from datetime import datetime, timedelta, timezone
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

# Display timezone for the dashboard (Indonesia Western Time, WIB = UTC+7)
DISPLAY_TZ = timezone(timedelta(hours=7))
DISPLAY_TZ_LABEL = "WIB"


def _to_datetime(value) -> datetime | None:
    """Coerce value (datetime, ISO string, or None) to a tz-aware datetime.
    Strings without tz info are assumed UTC.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    # SQLite TEXT timestamps may use space separator and ".microseconds"
    # datetime.fromisoformat (Py3.11+) handles both 'T' and ' ', + tz, + microseconds
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_dt_seconds(value) -> str:
    """Render a datetime/ISO string in WIB (UTC+7), trimmed to seconds.

    Accepts:
      - datetime objects (tz-aware preferred; naive assumed UTC)
      - ISO strings like '2026-05-17 18:43:02.250567+00:00' (SQLite CAST AS TEXT)
      - None → ''

    Output format: '2026-05-18 01:43:02 WIB'
    """
    dt = _to_datetime(value)
    if dt is None:
        return ""
    local = dt.astimezone(DISPLAY_TZ).replace(microsecond=0)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {DISPLAY_TZ_LABEL}"


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
        range: str | None = Query(default=None),
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = Query(default=None),
    ):
        # Default to "all" only if neither preset nor explicit dates are given
        effective_preset = range
        if range is None and from_ is None and to is None:
            effective_preset = "all"
        from_dt, to_dt = _range(effective_preset, from_, to)
        conn = _db(request)
        rows = queries.dashboard_hotkey_summary(conn, from_dt=from_dt, to_dt=to_dt)
        total_cumulative = sum(r["cumulative"] for r in rows)
        total_registered = sum(1 for r in rows if r["is_registered"])
        total_hotkeys = len(rows)
        total_deregistered = total_hotkeys - total_registered
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "rows": rows,
                "total_cumulative": total_cumulative,
                "total_registered": total_registered,
                "total_hotkeys": total_hotkeys,
                "total_deregistered": total_deregistered,
                "from_dt": from_dt,
                "to_dt": to_dt,
                "from_input": from_ or "",
                "to_input": to or "",
                "active_range": effective_preset if (from_ is None and to is None) else "",
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
        status_map = queries.current_registration_status(conn)
        person_status = status_map.get(
            name, {"active": 0, "total": 0, "deregistered_hotkeys": []}
        )
        return templates.TemplateResponse(
            request,
            "person.html",
            {
                "name": name,
                "hotkeys": hotkeys,
                "deregistered_set": set(person_status["deregistered_hotkeys"]),
                "active_count": person_status["active"],
                "total_count": person_status["total"],
                "total_cumulative": total,
                "chart_labels": [str(s["taken_at"]) for s in series],
                "chart_cumulative": [rao_to_alpha(s["cumulative"]) for s in series],
                "chart_per_snap": [rao_to_alpha(s["per_snapshot_emission"]) for s in series],
                "active_range": range or "all",
                "latest": latest,
            },
        )
