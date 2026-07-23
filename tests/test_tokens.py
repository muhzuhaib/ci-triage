from __future__ import annotations

from decimal import Decimal

import pytest

from ci_triage.tokens import chars_that_fit, upper_bound_tokens


def test_token_bounds_round_up():
    # 10 chars at 1.18 chars/token is 8.47 tokens -> 9, not 8.
    assert upper_bound_tokens("x" * 10, Decimal("1.18"), overhead_tokens=0) == 9


def test_chars_that_fit_round_down():
    # 9 tokens at 1.18 is 10.62 chars -> 10, not 11.
    assert chars_that_fit(9, Decimal("1.18"), overhead_tokens=0) == 10


def test_an_empty_string_costs_only_its_framing():
    assert upper_bound_tokens("", Decimal("1.18"), overhead_tokens=8) == 8
    assert upper_bound_tokens("", Decimal("1.18"), overhead_tokens=0) == 0


def test_overhead_can_exhaust_a_tiny_character_budget():
    """Never returns a negative length; zero characters is the honest answer."""
    assert chars_that_fit(3, Decimal("1.18"), overhead_tokens=8) == 0
    assert chars_that_fit(8, Decimal("1.18"), overhead_tokens=8) == 0


@pytest.mark.parametrize("token_budget", [1, 2, 7, 33, 1000, 65_536])
@pytest.mark.parametrize("ratio", ["0.90", "1.18", "4.0"])
def test_chars_then_tokens_never_exceeds_the_original_budget(token_budget, ratio):
    """floor-then-ceil must not overshoot, for any ratio and any budget.

    This is the property the truncation step will rely on: size a log to
    ``chars_that_fit`` and bounding it back with ``upper_bound_tokens`` must
    land at or under where you started. Losing a token to rounding is fine;
    gaining one is a budget breach.
    """
    cpt = Decimal(ratio)
    chars = chars_that_fit(token_budget, cpt, overhead_tokens=0)
    assert upper_bound_tokens("#" * chars, cpt, overhead_tokens=0) <= token_budget


def test_the_folk_constant_would_understate_a_real_log():
    """Guards the finding the ratio is based on, not the ratio itself.

    A docker build log measures 1.18 chars/token (tools/measure_token_density.py).
    Pricing it at the customary 4.0 would claim it is a third of its real size
    -- and a third of its real cost. The assertion is on the *direction and
    size* of that error, because that is what justifies carrying a measured
    per-model ratio at all rather than the constant everyone quotes.
    """
    log = "#" * 4_000
    honest = upper_bound_tokens(log, Decimal("1.18"), overhead_tokens=0)
    folk = upper_bound_tokens(log, Decimal("4.0"), overhead_tokens=0)
    assert honest > folk * 3


def test_a_nonsense_ratio_is_rejected():
    with pytest.raises(ValueError):
        upper_bound_tokens("abc", Decimal("0"))
    with pytest.raises(ValueError):
        chars_that_fit(10, Decimal("-1"))
    with pytest.raises(ValueError):
        upper_bound_tokens("abc", Decimal("1.18"), overhead_tokens=-1)
