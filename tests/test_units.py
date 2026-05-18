import pytest

from emission_tracker.units import RAO_PER_ALPHA, format_alpha, rao_to_alpha


def test_rao_to_alpha_basic():
    assert rao_to_alpha(10**9) == 1.0
    assert rao_to_alpha(0) == 0.0
    assert rao_to_alpha(36_036_546_831) == pytest.approx(36.036546831)


def test_rao_to_alpha_handles_none():
    assert rao_to_alpha(None) is None


def test_format_alpha_default_decimals():
    assert format_alpha(36_036_546_831) == "36.0365 α"
    assert format_alpha(60_191_618) == "0.0602 α"
    assert format_alpha(0) == "0.0000 α"


def test_format_alpha_handles_none():
    assert format_alpha(None) == "— α"


def test_format_alpha_custom_decimals():
    assert format_alpha(36_036_546_831, decimals=2) == "36.04 α"
    assert format_alpha(36_036_546_831, decimals=6) == "36.036547 α"


def test_rao_per_alpha_constant():
    assert RAO_PER_ALPHA == 1_000_000_000
