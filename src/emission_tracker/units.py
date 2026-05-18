"""Unit conversion for Bittensor alpha/TAO emissions.

TaoStats API returns emission values in RAO (the smallest unit). One alpha = 10^9 RAO,
analogous to satoshi/BTC or wei/ETH. UI displays alpha for human readability while DB
preserves raw RAO values.
"""

RAO_PER_ALPHA = 10**9


def rao_to_alpha(rao: float | int | None) -> float | None:
    """Convert RAO integer/float to alpha (divide by 10^9). Returns None if input None."""
    if rao is None:
        return None
    return rao / RAO_PER_ALPHA


def format_alpha(rao: float | int | None, decimals: int = 4) -> str:
    """Format a RAO value as alpha string with α suffix.

    >>> format_alpha(36036546831)
    '36.0365 α'
    >>> format_alpha(60191618)
    '0.0602 α'
    >>> format_alpha(0)
    '0.0000 α'
    >>> format_alpha(None)
    '— α'
    """
    alpha = rao_to_alpha(rao)
    if alpha is None:
        return "— α"
    return f"{alpha:.{decimals}f} α"
