from decimal import Decimal

from app.pricing.margin import apply_margin, margin_amount


def test_apply_margin_reduces_amount_by_percent():
    assert apply_margin(Decimal("100"), Decimal("1.5")) == Decimal("98.5")


def test_apply_margin_zero_percent_is_unchanged():
    assert apply_margin(Decimal("100"), Decimal("0")) == Decimal("100")


def test_margin_amount_is_the_difference():
    raw = Decimal("2500")
    percent = Decimal("2")
    assert margin_amount(raw, percent) == raw - apply_margin(raw, percent)
    assert margin_amount(raw, percent) == Decimal("50")


def test_apply_margin_and_margin_amount_sum_to_raw():
    raw = Decimal("1234.56789")
    percent = Decimal("3.25")
    assert apply_margin(raw, percent) + margin_amount(raw, percent) == raw
