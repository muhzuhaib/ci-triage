"""Measure what the runner's per-line timestamps cost in tokens.

Why this script exists
----------------------
:mod:`ci_triage.logs` throws away the ``2026-07-29T23:12:55.4607494Z`` prefix
that ``actions/runner`` writes on every line of a downloaded log. That looks like
tidying, and if it were only tidying it would not be worth a design decision. It
is not: the prefixes are a quarter of the characters and nearly *two fifths of
the tokens*, because a run of digits and punctuation tokenises far worse than the
English and code around it. Stripping them is therefore the single largest
change available to how much real log a fixed budget can buy.

That claim is measured rather than asserted, on a real log from this project's
own CI rather than on a sample written to make the point:

    python -m pip install tiktoken
    python tools/measure_timestamp_cost.py tools/samples/actions-job.log

``tiktoken`` is deliberately not a dependency of the package, for the same
reason it is not one in ``measure_token_density.py``: it is needed to justify a
constant, not to use it.

The committed sample is this repository's own CI: job 90733611451 of run
30498795235, downloaded unmodified so the numbers above can be checked against
something that was not chosen to flatter them.

Any log works. Download one with:

    gh api repos/OWNER/REPO/actions/jobs/JOB_ID/logs > job.log
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ci_triage.logs import strip_timestamps  # noqa: E402

DEFAULT_SAMPLE = Path(__file__).resolve().parent / "samples" / "actions-job.log"


def main(argv: list[str]) -> int:
    try:
        import tiktoken
    except ImportError:
        print("pip install tiktoken to run this measurement", file=sys.stderr)
        return 1

    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_SAMPLE
    if not path.exists():
        print(f"no such log: {path}", file=sys.stderr)
        return 1

    # utf-8-sig: the download carries a byte-order mark.
    raw = path.read_text(encoding="utf-8-sig")
    stripped = strip_timestamps(raw)
    enc = tiktoken.get_encoding("o200k_base")
    raw_tokens = len(enc.encode(raw))
    stripped_tokens = len(enc.encode(stripped))

    print(f"sample: {path} ({len(raw.splitlines()):,} lines)\n")
    print(f"{'':<12}{'chars':>10}{'tokens':>10}{'chars/token':>14}")
    print("-" * 46)
    print(f"{'raw':<12}{len(raw):>10,}{raw_tokens:>10,}{len(raw) / raw_tokens:>14.2f}")
    print(
        f"{'stripped':<12}{len(stripped):>10,}{stripped_tokens:>10,}"
        f"{len(stripped) / stripped_tokens:>14.2f}"
    )
    print("-" * 46)
    char_share = (len(raw) - len(stripped)) / len(raw) * 100
    token_share = (raw_tokens - stripped_tokens) / raw_tokens * 100
    print(f"timestamps are {char_share:.1f}% of the characters")
    print(f"timestamps are {token_share:.1f}% of the tokens")
    print(
        f"\nso the same token budget buys {raw_tokens / stripped_tokens:.2f}x more "
        f"real log once they are gone."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
