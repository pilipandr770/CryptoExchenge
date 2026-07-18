"""Client-facing margin: the spread this exchanger keeps is baked directly
into the rate shown to the client (FixedFloat/SimpleSwap style), not shown
as a separate fee line. The client is quoted `apply_margin(raw_amount)` and
that is exactly what they receive -- never the raw DEX amount.

`margin_percent` is snapshotted onto the SwapOrder at quote time
(`SwapOrder.margin_percent`) rather than re-read from config at withdrawal
time, so a later change to MARGIN_PERCENT can't retroactively change what a
client was already promised.
"""

from decimal import Decimal


def apply_margin(raw_amount: Decimal, margin_percent: Decimal) -> Decimal:
    """The amount actually promised/paid to the client: raw_amount reduced
    by margin_percent. E.g. apply_margin(100, 1.5) -> 98.5."""
    return raw_amount * (Decimal("1") - margin_percent / Decimal("100"))


def margin_amount(raw_amount: Decimal, margin_percent: Decimal) -> Decimal:
    """The spread itself, in the same asset/units as raw_amount -- what the
    house keeps. raw_amount - apply_margin(raw_amount, margin_percent)."""
    return raw_amount - apply_margin(raw_amount, margin_percent)
