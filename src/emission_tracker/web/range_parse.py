from datetime import datetime, timedelta, timezone


PRESETS_HOURS = {
    "today": None,  # special: midnight UTC to now
    "24h": 24,
    "7d": 24 * 7,
    "30d": 24 * 30,
    "mtd": None,    # special: first of month to now
    "all": None,    # special: epoch to now
}


def parse_range(
    preset: str | None,
    from_str: str | None,
    to_str: str | None,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    if from_str or to_str:
        frm = _parse_date(from_str) if from_str else datetime(1970, 1, 1, tzinfo=timezone.utc)
        to = _parse_date(to_str) if to_str else now
        if frm > to:
            raise ValueError(f"from ({frm}) must be <= to ({to})")
        return frm, to

    p = (preset or "all").lower()
    if p == "today":
        frm = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif p == "mtd":
        frm = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif p == "all":
        frm = datetime(1970, 1, 1, tzinfo=timezone.utc)
    else:
        hours = PRESETS_HOURS.get(p)
        if hours is None:
            raise ValueError(f"Unknown preset: {preset!r}")
        frm = now - timedelta(hours=hours)
    return frm, now


def _parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
