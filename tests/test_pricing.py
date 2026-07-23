from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ci_triage.pricing import (
    DEFAULT_PRICES_PATH,
    ExpiredPrice,
    MalformedPriceTable,
    ModelPrice,
    PriceTable,
    UnknownModel,
    load_prices,
)

TABLE = """
{
  "fetched_at": "2026-07-01",
  "stale_after_days": 30,
  "models": {
    "cheap-1": {
      "provider": "acme",
      "input_per_mtok": "0.20",
      "output_per_mtok": "1.25",
      "chars_per_token": "1.18",
      "source": "https://example.invalid/pricing"
    },
    "expiring-1": {
      "provider": "acme",
      "input_per_mtok": "2.00",
      "output_per_mtok": "10.00",
      "chars_per_token": "0.90",
      "price_expires": "2026-08-31",
      "note": "Introductory rate.",
      "source": "https://example.invalid/pricing"
    }
  }
}
"""


@pytest.fixture()
def table() -> PriceTable:
    return PriceTable.from_json(TABLE)


# --------------------------------------------------------------------- costing


def test_micro_dollars_per_token_equals_dollars_per_million_tokens(table):
    """The unit choice makes the conversion the identity; guard it.

    A model at $0.20 per million tokens costs 0.2 micro-dollars per token, so a
    million tokens costs 200_000 micros == $0.20. If someone "helpfully" adds a
    scaling factor, this is the test that catches it.
    """
    price = table.get("cheap-1")
    assert price.input_micros(1_000_000) == 200_000
    assert price.output_micros(1_000_000) == 1_250_000


def test_costs_round_up_never_down(table):
    price = table.get("cheap-1")
    # One token at $0.20/MTok is 0.2 micro-dollars: a fraction of the smallest
    # unit we count. It must cost 1, not 0 -- free tokens are how a running
    # total drifts below the truth.
    assert price.input_micros(1) == 1
    assert price.input_micros(4) == 1
    assert price.input_micros(5) == 1
    assert price.input_micros(6) == 2


def test_zero_tokens_cost_nothing(table):
    price = table.get("cheap-1")
    assert price.input_micros(0) == 0
    assert price.output_micros(0) == 0


def test_negative_token_counts_are_rejected(table):
    with pytest.raises(ValueError):
        table.get("cheap-1").input_micros(-1)


# --------------------------------------------------------------- unknown model


def test_unknown_model_raises_rather_than_defaulting(table):
    with pytest.raises(UnknownModel) as exc:
        table.get("not-a-model")
    # The message must name what *is* known, because the fix is a data edit and
    # the person making it may not be the person who saw the traceback.
    assert "cheap-1" in str(exc.value)


def test_membership_and_length(table):
    assert "cheap-1" in table
    assert "nope" not in table
    assert len(table) == 2
    assert sorted(table) == ["cheap-1", "expiring-1"]


# ------------------------------------------------------------------- freshness


def test_expired_price_is_a_hard_error(table):
    """A published end date that has passed is a known-wrong number, not an old one."""
    table.get("expiring-1", today=date(2026, 8, 31))  # last valid day: fine

    with pytest.raises(ExpiredPrice) as exc:
        table.get("expiring-1", today=date(2026, 9, 1))
    message = str(exc.value)
    assert "2026-08-31" in message
    assert "Introductory rate." in message  # the note explains what to replace it with
    assert "https://example.invalid/pricing" in message  # and where to look


def test_a_model_without_an_expiry_never_expires(table):
    table.get("cheap-1", today=date(2099, 1, 1))


def test_staleness_is_advisory_not_fatal(table):
    """Age is a prompt to re-check, so it reports rather than raising."""
    assert table.age_days(date(2026, 7, 21)) == 20
    assert not table.is_stale(date(2026, 7, 21))
    assert table.is_stale(date(2026, 9, 1))
    # Still usable while stale -- an old table is not a wrong table.
    assert table.get("cheap-1", today=date(2026, 9, 1)).input_per_mtok == Decimal("0.20")


# ------------------------------------------------------------------ validation


def test_a_price_written_as_a_json_number_is_rejected(table):
    """The bug this guards is invisible: ordinary parsing turns 0.20 into a float."""
    bad = TABLE.replace('"input_per_mtok": "0.20"', '"input_per_mtok": 0.20')
    with pytest.raises(MalformedPriceTable, match="write prices as strings"):
        PriceTable.from_json(bad)


def test_a_real_float_is_rejected_when_the_mapping_did_not_come_from_json():
    """from_dict() takes a mapping from anywhere, so the float path is live.

    Through from_json() a bare number is already a Decimal (parse_float), so
    this is the entry point where an actual float can reach the parser -- and
    the same check has to catch it.
    """
    with pytest.raises(MalformedPriceTable, match="write prices as strings"):
        ModelPrice.from_dict(
            "floaty",
            {
                "input_per_mtok": 0.2,
                "output_per_mtok": "1.25",
                "chars_per_token": "1.18",
                "source": "https://example.invalid",
            },
        )


def test_an_undated_table_is_rejected():
    bad = json.loads(TABLE)
    del bad["fetched_at"]
    with pytest.raises(MalformedPriceTable, match="cannot be audited"):
        PriceTable.from_json(json.dumps(bad))


def test_a_missing_required_field_is_rejected():
    bad = json.loads(TABLE)
    del bad["models"]["cheap-1"]["source"]
    with pytest.raises(MalformedPriceTable, match="source"):
        PriceTable.from_json(json.dumps(bad))


def test_malformed_json_is_rejected():
    with pytest.raises(MalformedPriceTable, match="not valid JSON"):
        PriceTable.from_json("{ not json")


def test_negative_and_zero_ratios_are_rejected():
    bad = json.loads(TABLE)
    bad["models"]["cheap-1"]["chars_per_token"] = "0"
    with pytest.raises(MalformedPriceTable, match="positive"):
        PriceTable.from_json(json.dumps(bad))

    bad = json.loads(TABLE)
    bad["models"]["cheap-1"]["input_per_mtok"] = "-1"
    with pytest.raises(MalformedPriceTable, match="negative"):
        PriceTable.from_json(json.dumps(bad))


def test_a_bad_date_is_rejected():
    bad = TABLE.replace('"fetched_at": "2026-07-01"', '"fetched_at": "July 2026"')
    with pytest.raises(MalformedPriceTable, match="not an ISO date"):
        PriceTable.from_json(bad)


def test_a_missing_file_is_a_pricing_error(tmp_path: Path):
    with pytest.raises(MalformedPriceTable, match="cannot read"):
        PriceTable.load(tmp_path / "absent.json")


# ------------------------------------------------------- the shipped table
#
# These assert the *hygiene* of the data we ship, not the numbers themselves --
# prices change, and a test that pinned them would be a test that fails when
# someone correctly updates the file.


def test_the_shipped_table_loads():
    shipped = load_prices()
    assert len(shipped) >= 4


def test_every_shipped_entry_can_be_audited():
    """No price without a source URL and a stated basis for its token ratio."""
    for name, price in load_prices().models.items():
        assert price.source.startswith("https://"), name
        assert price.provider, name
        assert price.chars_per_token_basis, name


def test_no_shipped_price_is_written_as_a_json_number():
    """Belt and braces: catch a float before Decimal ever sees it."""
    raw = json.loads(DEFAULT_PRICES_PATH.read_text(encoding="utf-8"))
    for name, entry in raw["models"].items():
        for field in ("input_per_mtok", "output_per_mtok", "chars_per_token"):
            assert isinstance(entry[field], str), f"{name}.{field} must be a string"


def test_shipped_token_ratios_are_at_or_below_the_measured_worst_case():
    """1.18 chars/token is the worst case measured on real CI log content.

    Anything above it is optimistic on the payload this service actually reads,
    and optimistic means under-charged. Models on Anthropic's post-4.7
    tokeniser sit lower still, which is correct -- it emits ~30% more tokens
    for the same text.
    """
    for name, price in load_prices().models.items():
        assert price.chars_per_token <= Decimal("1.18"), name
        assert price.chars_per_token >= Decimal("0.5"), f"{name}: implausibly pessimistic"


def test_the_shipped_table_is_not_absurdly_old():
    """Catches a table that was copied forward for years without a re-check."""
    shipped = load_prices()
    assert shipped.fetched_at >= date(2026, 1, 1)
    assert shipped.fetched_at <= date.today()


def test_the_readme_block_stays_a_readme_block():
    """The _readme key documents the format in the file itself; keep it there."""
    raw = json.loads(DEFAULT_PRICES_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw["_readme"], list)
    assert "_readme" not in load_prices().models


def test_model_price_is_immutable(table):
    price = table.get("cheap-1")
    with pytest.raises(Exception):
        price.input_per_mtok = Decimal("0")  # type: ignore[misc]


def test_direct_construction_still_costs_correctly():
    """The table is a convenience; the price object stands on its own."""
    price = ModelPrice(
        model="hand-made",
        provider="test",
        input_per_mtok=Decimal("3"),
        output_per_mtok=Decimal("15"),
        chars_per_token=Decimal("1"),
        source="https://example.invalid",
    )
    assert price.input_micros(1_000) == 3_000
    assert price.output_micros(1_000) == 15_000


def test_source_urls_are_the_provider_not_a_blog():
    """A price sourced from a third-party summary is a price nobody can verify."""
    allowed = re.compile(r"^https://(developers\.openai\.com|platform\.claude\.com)/")
    for name, price in load_prices().models.items():
        assert allowed.match(price.source), f"{name} cites {price.source}"
