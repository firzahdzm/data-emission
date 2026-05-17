from datetime import datetime, timedelta, timezone

import pytest

from emission_tracker.web.range_parse import parse_range


def test_parse_range_preset_24h():
    now = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)
    frm, to = parse_range(preset="24h", from_str=None, to_str=None, now=now)
    assert to == now
    assert frm == now - timedelta(hours=24)


def test_parse_range_preset_7d():
    now = datetime(2026, 5, 17, 14, 0, tzinfo=timezone.utc)
    frm, to = parse_range(preset="7d", from_str=None, to_str=None, now=now)
    assert (to - frm) == timedelta(days=7)


def test_parse_range_all_uses_epoch():
    now = datetime(2026, 5, 17, tzinfo=timezone.utc)
    frm, to = parse_range(preset="all", from_str=None, to_str=None, now=now)
    assert frm.year == 1970
    assert to == now


def test_parse_range_custom_dates():
    frm, to = parse_range(
        preset=None,
        from_str="2026-05-10",
        to_str="2026-05-17",
        now=datetime(2026, 5, 17, tzinfo=timezone.utc),
    )
    assert frm == datetime(2026, 5, 10, tzinfo=timezone.utc)
    assert to == datetime(2026, 5, 17, tzinfo=timezone.utc)


def test_parse_range_default_is_all():
    now = datetime(2026, 5, 17, tzinfo=timezone.utc)
    frm, to = parse_range(preset=None, from_str=None, to_str=None, now=now)
    assert frm.year == 1970


def test_parse_range_rejects_from_after_to():
    with pytest.raises(ValueError):
        parse_range(
            preset=None,
            from_str="2026-05-20",
            to_str="2026-05-10",
            now=datetime(2026, 5, 17, tzinfo=timezone.utc),
        )
