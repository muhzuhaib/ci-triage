"""The model price table.

Prices live in ``prices.json`` beside this module, not in Python. Three
consequences, all of them the point:

* A price change is a data edit with a visible diff, reviewable by someone who
  does not read Python.
* The table carries ``fetched_at`` and a per-entry ``source`` URL, so the
  question "where did this number come from and when?" always has an answer.
* An operator can point the service at their own table -- negotiated rates,
  self-hosted models, a gateway's marked-up prices -- without a code change.

The unit trick
--------------
Prices are quoted per million tokens, and money in this project is counted in
micro-dollars. Those two units are chosen so that the conversion is the
identity: a model at ``$5.00`` per million tokens costs exactly ``5`` micro-
dollars per token, because ``$5 / 10^6 tokens`` is ``5 x 10^-6`` dollars per
token, which is 5 micro-dollars. So costing a call is a multiplication by the
quoted price and nothing else -- no scaling factor to get backwards.

Stale versus wrong
------------------
The table distinguishes two failure modes, and treats them differently on
purpose:

* **Wrong** -- an entry carries ``price_expires`` and that date has passed. We
  know as a fact that the number is no longer the price, because the provider
  published the change in advance. Pricing with it raises :class:`ExpiredPrice`.
  There is a live example in the shipped table: Claude Sonnet 5's introductory
  rate is published as ending on 2026-08-31.
* **Stale** -- the whole table is older than ``stale_after_days``. That is a
  prompt to re-check, not evidence of an error. It sets :attr:`PriceTable.is_stale`
  and blocks nothing.

Failing hard on mere age would take a working service down for a data-hygiene
issue. Failing soft on a price we *know* has changed would silently under-charge
every run. Neither policy is right for both cases, so they are separate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Mapping

DEFAULT_PRICES_PATH = Path(__file__).with_name("prices.json")


class PricingError(Exception):
    """Base class for price table failures."""


class UnknownModel(PricingError):
    """A price was requested for a model that is not in the table.

    Terminal, and never falls back to a default price. A default would mean the
    one case we cannot cost -- an unrecognised model -- is also the one case we
    let through uncosted, which is how a ceiling stops being a ceiling.
    """


class ExpiredPrice(PricingError):
    """The entry's own published expiry date has passed."""


class MalformedPriceTable(PricingError):
    """The table could not be parsed, or an entry is missing a required field."""


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _decimal(raw: Any, field: str, model: str) -> Decimal:
    """Parse a price field, requiring that it was written as a string.

    JSON has no decimal type, so a bare ``0.75`` in the file is a float by the
    time ordinary parsing is done with it -- already imprecise, silently. Prices
    are therefore written as strings.

    Note what this check is *not*: :meth:`PriceTable.from_json` passes
    ``parse_float=Decimal``, so through that path a bare number arrives here as
    a ``Decimal`` and never exists as a float at all. The refusal below still
    fires, because the number was still written wrongly and the next reader of
    the file should not learn the format by accident. Through
    :meth:`ModelPrice.from_dict`, which takes an already-parsed mapping from
    anywhere, a genuine float can arrive -- so one type check has to cover both,
    and it is the positive one: prices are strings.
    """
    if not isinstance(raw, str):
        raise MalformedPriceTable(
            f"{model}.{field} is a {type(raw).__name__}; write prices as strings so "
            f"they parse to Decimal exactly (got {raw!r})"
        )
    try:
        value = Decimal(raw)
    except ArithmeticError as exc:
        raise MalformedPriceTable(f"{model}.{field}: {raw!r} is not a number") from exc
    if value < 0:
        raise MalformedPriceTable(f"{model}.{field} cannot be negative")
    return value


def _date(raw: Any, field: str, where: str) -> date:
    if not isinstance(raw, str):
        raise MalformedPriceTable(f"{where}.{field} must be an ISO date string")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise MalformedPriceTable(f"{where}.{field}: {raw!r} is not an ISO date") from exc


@dataclass(frozen=True)
class ModelPrice:
    """One model's rates, plus what is needed to audit them."""

    model: str
    provider: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    chars_per_token: Decimal
    source: str
    chars_per_token_basis: str = ""
    price_expires: date | None = None
    note: str = ""

    def check_current(self, today: date | None = None) -> None:
        """Raise :class:`ExpiredPrice` if this entry's published expiry has passed."""
        if self.price_expires is None:
            return
        today = today or _today()
        if today > self.price_expires:
            detail = f" {self.note}" if self.note else ""
            raise ExpiredPrice(
                f"the price for {self.model!r} was published as valid only to "
                f"{self.price_expires.isoformat()}; today is {today.isoformat()}."
                f"{detail} Re-fetch {self.source} and update prices.json"
            )

    def input_micros(self, tokens: int) -> int:
        """Cost in micro-dollars of ``tokens`` input tokens, rounded up."""
        return self._micros(tokens, self.input_per_mtok)

    def output_micros(self, tokens: int) -> int:
        """Cost in micro-dollars of ``tokens`` output tokens, rounded up."""
        return self._micros(tokens, self.output_per_mtok)

    @staticmethod
    def _micros(tokens: int, price_per_mtok: Decimal) -> int:
        if tokens < 0:
            raise ValueError("token count cannot be negative")
        # Micro-dollars per token == dollars per million tokens; see the module
        # docstring. Round up so a fractional micro-dollar is never free.
        return int(
            (Decimal(tokens) * price_per_mtok).to_integral_value(rounding=ROUND_CEILING)
        )

    @classmethod
    def from_dict(cls, model: str, raw: Mapping[str, Any]) -> ModelPrice:
        missing = {"input_per_mtok", "output_per_mtok", "chars_per_token", "source"} - set(raw)
        if missing:
            raise MalformedPriceTable(
                f"{model} is missing required field(s): {', '.join(sorted(missing))}"
            )
        chars_per_token = _decimal(raw["chars_per_token"], "chars_per_token", model)
        if chars_per_token <= 0:
            raise MalformedPriceTable(f"{model}.chars_per_token must be positive")
        return cls(
            model=model,
            provider=str(raw.get("provider", "")),
            input_per_mtok=_decimal(raw["input_per_mtok"], "input_per_mtok", model),
            output_per_mtok=_decimal(raw["output_per_mtok"], "output_per_mtok", model),
            chars_per_token=chars_per_token,
            source=str(raw["source"]),
            chars_per_token_basis=str(raw.get("chars_per_token_basis", "")),
            price_expires=(
                _date(raw["price_expires"], "price_expires", model)
                if "price_expires" in raw
                else None
            ),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class PriceTable:
    """A dated set of model prices."""

    fetched_at: date
    stale_after_days: int
    models: Mapping[str, ModelPrice]

    @classmethod
    def load(cls, path: Path | str | None = None) -> PriceTable:
        path = Path(path) if path is not None else DEFAULT_PRICES_PATH
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MalformedPriceTable(f"cannot read price table {path}: {exc}") from exc
        return cls.from_json(text, where=str(path))

    @classmethod
    def from_json(cls, text: str, *, where: str = "<string>") -> PriceTable:
        try:
            # parse_float=Decimal is belt-and-braces: prices are supposed to be
            # strings, and _decimal() rejects them if they are not, but nothing
            # in this file should ever become a float on the way in.
            raw = json.loads(text, parse_float=Decimal)
        except ValueError as exc:
            raise MalformedPriceTable(f"{where} is not valid JSON: {exc}") from exc

        if "fetched_at" not in raw:
            raise MalformedPriceTable(
                f"{where} has no fetched_at; an undated price table cannot be audited"
            )
        fetched_at = _date(raw["fetched_at"], "fetched_at", where)

        stale_after_days = raw.get("stale_after_days", 45)
        if isinstance(stale_after_days, Decimal):
            stale_after_days = int(stale_after_days)
        if not isinstance(stale_after_days, int) or stale_after_days <= 0:
            raise MalformedPriceTable(f"{where}.stale_after_days must be a positive integer")

        models_raw = raw.get("models")
        if not isinstance(models_raw, dict) or not models_raw:
            raise MalformedPriceTable(f"{where} has no models")

        models = {
            name: ModelPrice.from_dict(name, entry) for name, entry in models_raw.items()
        }
        return cls(fetched_at=fetched_at, stale_after_days=stale_after_days, models=models)

    # ------------------------------------------------------------------ access

    def get(self, model: str, *, today: date | None = None) -> ModelPrice:
        """Look up a model, refusing unknown ones and expired prices."""
        try:
            price = self.models[model]
        except KeyError:
            raise UnknownModel(
                f"no price for model {model!r}. Known models: "
                f"{', '.join(sorted(self.models))}. Add it to prices.json -- there is "
                f"deliberately no default price, because a default would let the one "
                f"model we cannot cost through uncosted"
            ) from None
        price.check_current(today)
        return price

    def __contains__(self, model: object) -> bool:
        return model in self.models

    def __iter__(self):
        return iter(self.models)

    def __len__(self) -> int:
        return len(self.models)

    # ------------------------------------------------------------- freshness

    def age_days(self, today: date | None = None) -> int:
        return ((today or _today()) - self.fetched_at).days

    def is_stale(self, today: date | None = None) -> bool:
        """True when the table is older than its own ``stale_after_days``.

        Advisory. Age is a reason to re-check the sources, not evidence that any
        particular number is wrong -- so this reports rather than raises. The
        hard failure is :class:`ExpiredPrice`, which fires only when a provider
        published an end date and it has passed.
        """
        return self.age_days(today) > self.stale_after_days


def load_prices(path: Path | str | None = None) -> PriceTable:
    """Convenience loader for the table shipped with the package."""
    return PriceTable.load(path)
