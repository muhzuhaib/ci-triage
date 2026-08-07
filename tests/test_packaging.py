"""Guards on the things that are true of the distribution rather than the code.

Both of these exist because they were found wrong rather than imagined: the
version had drifted a release behind in one of the two places that state it, and
``prices.json`` was silently absent from the wheel until the build was told it
was an artefact. Neither failure is visible in a checkout, which is what makes
them worth a test rather than a habit.
"""

from __future__ import annotations

from importlib.metadata import version

import ci_triage
from ci_triage.pricing import load_prices


def test_the_two_places_that_state_the_version_agree():
    """``__version__`` and the packaging metadata are one fact written twice.

    They drifted once already (0.3.0 against 0.4.0), because nothing read them
    together and a release note is not a test.
    """
    assert ci_triage.__version__ == version("ci-triage")


def test_the_price_table_survives_being_packaged():
    """A wheel without ``prices.json`` installs cleanly and fails at the first
    costed call, which is as late as a packaging mistake can possibly surface."""
    assert load_prices().models
