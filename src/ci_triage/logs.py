"""Turning raw CI logs into the largest useful prompt a budget can buy.

The estimator already answers "how many characters of log can this run afford?"
(:func:`~ci_triage.estimate.budget_input_chars`). This module answers the
question that follows, which is not arithmetic: *which* characters.

Three decisions, each measured rather than assumed.

**1. Strip the per-line timestamp, and it is not a tidiness change.** Every line
of a downloaded Actions log is prefixed with ``2026-07-29T23:12:55.4607494Z ``,
28 characters plus a space. Measured on a real 547-line job log from this
project's own CI (``tools/measure_timestamp_cost.py`` reproduces it): the
prefixes are **27.0% of the characters but 39.2% of the tokens**, because a
run of digits and punctuation tokenises far worse than the English and code
around it. Dropping them therefore buys **1.64x more real log for the same
money**, which is a larger effect than any cleverness in the truncation itself.
The information is not lost so much as relocated: the interesting relative
timing is already visible in the step ordering, and the absolute wall-clock of a
line is not what a diagnosis turns on.

**2. Keep the tail, not the head.** A CI log opens with runner provisioning,
checkout and dependency resolution, all of which are identical on the runs that
pass. The failing assertion, the traceback and the ``Process completed with exit
code 1`` are at the end. Truncating from the front therefore discards the part
that is the same every time and keeps the part that is different this time.

**3. The truncation note is paid for out of the budget, not added to it.** A
note that says what was dropped is itself input, and a bound that is exceeded by
the machinery announcing the bound is not a bound. The same rule covers the
per-job headers below. Everything this module emits is counted.

Multiple failed jobs share the budget by water filling: each job is offered an
equal share, a job whose log is smaller than its share returns the remainder,
and the surplus is re-offered to the jobs that are still over. One enormous
matrix leg therefore cannot starve the three short ones next to it, and three
short ones cannot waste the budget the long one needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NamedTuple

#: The prefix ``actions/runner`` writes on every line of a downloaded log.
#: Anchored per line and tolerant of the fractional-second width, which is not
#: documented as fixed.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z ", re.MULTILINE)

#: Written where lines were removed. The count is filled in per call; the
#: formatted length is charged against the budget before any log is kept.
_NOTE = "[... {dropped} earlier lines dropped to fit the run's budget ...]"

_HEADER = "===== {name} ====="

#: The log download is UTF-8 with a byte-order mark. Spelled out rather than
#: written literally, so it survives every editor that touches this file.
_BOM = "\ufeff"


def strip_timestamps(text: str) -> str:
    """Remove the runner's per-line ISO timestamp prefix.

    Also drops a leading byte-order mark: the log download carries one, and left
    in place it would defeat the timestamp match on the very first line, which is
    the line most likely to be quoted back in a diagnosis.
    """
    return _TIMESTAMP.sub("", text.lstrip(_BOM))


class Tail(NamedTuple):
    """What survived a truncation, and what it cost to say so.

    ``kept_chars`` counts the log itself, not the note that reports the
    dropping. The two are separated because the note is machinery: a footer that
    told a reader "12,715 of 12,690 characters" because it counted its own
    apparatus would undermine exactly the trust it is there to build.
    """

    text: str
    dropped_lines: int
    kept_chars: int


def tail_to_chars(text: str, max_chars: int) -> Tail:
    """Keep the last whole lines of ``text`` that fit in ``max_chars``.

    Returns the kept text, the number of lines dropped, and how many characters
    of actual log survived. The result never exceeds ``max_chars``, including
    the note that reports the dropping, and it never ends mid-line: half a stack
    frame is worse than no stack frame, because it reads as a complete one.
    """
    if max_chars < 0:
        raise ValueError("max_chars cannot be negative")
    if len(text) <= max_chars:
        return Tail(text, 0, len(text))

    lines = text.splitlines(keepends=True)
    # Reserve the note at its worst-case width now: the note's own length
    # depends on the number it reports, so it has to be paid for before the
    # decision it participates in.
    note_room = len(_NOTE.format(dropped=len(lines))) + 1
    room = max_chars - note_room
    if room <= 0:
        # Not even the explanation fits. Say the least misleading thing that
        # does, and never exceed the ceiling to do it.
        return Tail(_NOTE.format(dropped=len(lines))[:max_chars], len(lines), 0)

    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > room:
            break
        kept.append(line)
        used += len(line)
    kept.reverse()
    dropped = len(lines) - len(kept)
    body = "".join(kept)
    if dropped == 0:  # pragma: no cover - implied by the length check above
        return Tail(body, 0, len(body))
    return Tail(_NOTE.format(dropped=dropped) + "\n" + body, dropped, len(body))


@dataclass(frozen=True)
class LogSection:
    """One job's log, before any preparation."""

    name: str
    text: str


@dataclass(frozen=True)
class PreparedLog:
    """The prompt-ready log, and what had to be given up to get it."""

    text: str
    original_chars: int
    dropped_lines: int
    kept_chars: int

    @property
    def prompt_chars(self) -> int:
        """Everything that goes to the provider, headers and notes included."""
        return len(self.text)

    @property
    def truncated(self) -> bool:
        return self.dropped_lines > 0


def _allocate(sizes: list[int], budget: int) -> list[int]:
    """Water-fill ``budget`` across ``sizes``: equal shares, surplus re-offered.

    Sorting by size and settling the smallest first is what makes one pass
    correct. A job under its share can only ever release room, and releasing it
    to the jobs still above their share is the only place it can do any good.
    """
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    grants = [0] * len(sizes)
    remaining = budget
    left = len(sizes)
    for i in order:
        share = remaining // left
        grant = min(sizes[i], share)
        grants[i] = grant
        remaining -= grant
        left -= 1
    return grants


def prepare_log(
    sections: list[LogSection],
    max_chars: int,
    *,
    strip: bool = True,
) -> PreparedLog:
    """Fit one or more job logs into ``max_chars`` characters, tails first.

    ``max_chars`` comes from :func:`~ci_triage.estimate.budget_input_chars`, so
    it is what the run can still afford rather than what the log happens to be.
    The headers and any truncation notes are counted inside it.
    """
    if max_chars < 0:
        raise ValueError("max_chars cannot be negative")
    if not sections:
        return PreparedLog("", 0, 0, 0)

    bodies = [strip_timestamps(s.text) if strip else s.text for s in sections]
    original = sum(len(b) for b in bodies)

    # Headers are input too, so they are charged first and the log competes for
    # what is left. A single section still gets a header: which job failed is
    # part of the diagnosis, not decoration.
    headers = [_HEADER.format(name=s.name) + "\n" for s in sections]
    # Each section is charged its header plus the newline that separates it from
    # the next one, so the assembled text cannot exceed the ceiling and no line
    # has to be cut afterwards to make it fit.
    room = max_chars - sum(len(h) + 1 for h in headers)
    if room <= 0:
        return PreparedLog("", original, sum(len(b.splitlines()) for b in bodies), 0)

    grants = _allocate([len(b) for b in bodies], room)

    parts: list[str] = []
    dropped_total = 0
    kept_total = 0
    for header, body, grant in zip(headers, bodies, grants):
        tail = tail_to_chars(body, grant)
        dropped_total += tail.dropped_lines
        kept_total += tail.kept_chars
        parts.append(header + tail.text + "\n")

    return PreparedLog("".join(parts), original, dropped_total, kept_total)
