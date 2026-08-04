from __future__ import annotations

import pytest

from ci_triage.logs import (
    LogSection,
    prepare_log,
    strip_timestamps,
    tail_to_chars,
)

RUNNER_LINES = "\n".join(
    f"2026-07-29T23:12:5{i % 10}.460749{i}Z step {i} did something"
    for i in range(20)
)


def test_strip_timestamps_removes_the_runner_prefix():
    stripped = strip_timestamps(RUNNER_LINES)

    assert "2026-07-29T" not in stripped
    assert stripped.splitlines()[0] == "step 0 did something"
    assert len(stripped.splitlines()) == 20


def test_strip_timestamps_survives_the_byte_order_mark():
    """The download starts with a BOM, which would otherwise shield line one.

    Line one is the line most likely to matter (``Current runner version``, or
    the first thing a job printed), so a stripper that silently skips it would
    fail exactly where it is least visible.
    """
    with_bom = "\ufeff" + RUNNER_LINES

    assert strip_timestamps(with_bom).startswith("step 0 did something")


def test_strip_timestamps_leaves_text_that_merely_contains_a_timestamp():
    line = "assert deadline == 2026-07-29T23:12:55.4607494Z end"

    assert strip_timestamps(line) == line


def test_tail_keeps_the_end_because_that_is_where_failures_are():
    text = "".join(f"line {i}\n" for i in range(100))

    kept, dropped, _ = tail_to_chars(text, 200)

    assert "line 99" in kept
    assert "line 0\n" not in kept
    assert dropped > 0


@pytest.mark.parametrize("budget", [0, 1, 30, 61, 200, 5_000])
def test_tail_never_exceeds_the_budget_including_its_own_note(budget):
    """The note announcing the truncation is charged against the ceiling.

    A truncation that reports itself in characters the caller did not budget for
    is the same class of bug as a ledger that forgets to count its own fees.
    """
    text = "".join(f"line {i}\n" for i in range(500))

    kept, _, _ = tail_to_chars(text, budget)

    assert len(kept) <= budget


def test_tail_never_cuts_a_line_in_half():
    text = "".join(f"{i:04d} a stack frame that is reasonably long\n" for i in range(50))

    kept, dropped, _ = tail_to_chars(text, 300)

    body = [line for line in kept.splitlines() if not line.startswith("[...")]
    assert dropped > 0
    assert all(line.endswith("reasonably long") for line in body)


def test_untruncated_text_is_returned_unchanged():
    text = "short enough\n"

    assert tail_to_chars(text, 1_000) == (text, 0, len(text))


def test_prepare_log_charges_headers_and_notes_to_the_budget():
    sections = [
        LogSection("tests (sqlite, py3.10)", "".join(f"a{i}\n" for i in range(400))),
        LogSection("tests (postgres)", "".join(f"b{i}\n" for i in range(400))),
    ]

    prepared = prepare_log(sections, 500)

    assert prepared.prompt_chars <= 500
    assert "tests (postgres)" in prepared.text
    assert "tests (sqlite, py3.10)" in prepared.text
    assert prepared.truncated


def test_prepare_log_gives_a_small_job_its_surplus_back():
    """Water filling: a short log must not hold budget it cannot use.

    An equal split would give the one-line job half the ceiling and leave the
    log that actually failed truncated twice as hard as it needed to be.
    """
    short = LogSection("lint", "one line\n")
    long = LogSection("tests", "".join(f"line {i}\n" for i in range(1_000)))

    prepared = prepare_log([short, long], 2_000)

    assert "one line" in prepared.text
    # The long job got everything the short one did not need, minus the headers.
    assert prepared.prompt_chars > 1_800
    assert prepared.prompt_chars <= 2_000


def test_prepare_log_strips_timestamps_by_default():
    prepared = prepare_log([LogSection("tests", RUNNER_LINES)], 10_000)

    assert "2026-07-29T" not in prepared.text
    assert prepared.original_chars == len(strip_timestamps(RUNNER_LINES))


def test_prepare_log_with_no_room_for_headers_returns_nothing_rather_than_overspending():
    sections = [LogSection("a job with a long name", "x" * 1_000)]

    prepared = prepare_log(sections, 5)

    assert prepared.text == ""
    assert prepared.truncated


def test_prepare_log_of_nothing_is_empty():
    prepared = prepare_log([], 100)

    assert prepared.text == ""
    assert prepared.original_chars == 0
    assert not prepared.truncated


def test_negative_budgets_are_a_programming_error():
    with pytest.raises(ValueError):
        prepare_log([LogSection("a", "b")], -1)
    with pytest.raises(ValueError):
        tail_to_chars("a", -1)
