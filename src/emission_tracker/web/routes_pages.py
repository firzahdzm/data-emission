import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from emission_tracker.units import format_alpha, rao_to_alpha
from emission_tracker.web import queries
from emission_tracker.web.auth import is_admin
from emission_tracker.web.range_parse import parse_range

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _asset_version() -> str:
    """Cache-buster for /static assets — uses style.css mtime so the browser
    fetches the new file when we edit CSS."""
    try:
        return str(int((STATIC_DIR / "style.css").stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = _asset_version

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


def _format_dt_short(value) -> str:
    """Compact WIB timestamp 'MM-DD HH:MM' for table column headers."""
    dt = _to_datetime(value)
    if dt is None:
        return ""
    local = dt.astimezone(DISPLAY_TZ)
    return local.strftime("%m-%d %H:%M")


# Register Jinja2 filters: RAO → alpha conversion + format, datetime helpers
templates.env.filters["alpha"] = format_alpha
templates.env.filters["to_alpha"] = rao_to_alpha
templates.env.filters["dt_s"] = _format_dt_seconds
templates.env.filters["dt_short"] = _format_dt_short


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
        last_settle = queries.last_settlement(conn)
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
                "last_settle": last_settle,
                "active_page": "dashboard",
                "is_admin": is_admin(request),
            },
        )

    @app.get("/archive", response_class=HTMLResponse)
    def archive(request: Request, limit: int = Query(default=50, ge=1, le=500)):
        conn = _db(request)
        settlements = queries.list_settlements(conn, limit=limit)
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "archive.html",
            {
                "settlements": settlements,
                "limit": limit,
                "latest": latest,
                "active_page": "archive",
                "is_admin": is_admin(request),
            },
        )

    @app.get("/archive/{settlement_id}", response_class=HTMLResponse)
    def archive_detail(request: Request, settlement_id: int):
        conn = _db(request)
        detail = queries.settlement_detail(conn, settlement_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Settlement not found")
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "archive_detail.html",
            {
                "settlement": detail,
                "latest": latest,
                "active_page": "archive",
                "is_admin": is_admin(request),
            },
        )

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, limit: int = Query(default=50, ge=1, le=500)):
        conn = _db(request)
        rows = queries.snapshot_history(conn, limit=limit)
        latest = queries.latest_snapshot(conn)
        # Top-level aggregates for the page
        ok = sum(1 for r in rows if r["status"] == "ok")
        partial = sum(1 for r in rows if r["status"] == "partial")
        failed = sum(1 for r in rows if r["status"] == "failed")
        in_progress = sum(1 for r in rows if r["status"] == "in_progress")
        return templates.TemplateResponse(
            request,
            "history.html",
            {
                "rows": rows,
                "limit": limit,
                "counts": {
                    "ok": ok,
                    "partial": partial,
                    "failed": failed,
                    "in_progress": in_progress,
                    "total": len(rows),
                },
                "latest": latest,
                "active_page": "history",
            },
        )

    @app.get("/captures", response_class=HTMLResponse)
    def captures(request: Request, limit: int = Query(default=20, ge=1, le=200)):
        conn = _db(request)
        table = queries.captures_table(conn, limit=limit)
        latest = queries.latest_snapshot(conn)
        return templates.TemplateResponse(
            request,
            "captures.html",
            {
                "snapshots": table["snapshots"],
                "rows": table["rows"],
                "limit": limit,
                "latest": latest,
                "active_page": "captures",
            },
        )
