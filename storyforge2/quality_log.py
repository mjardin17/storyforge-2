"""
storyforge2/quality_log.py — automatic per-book quality trend tracking.

Why this exists: Josh asked whether the pipeline could "self-improve after
every book." Full autonomous self-modification (the pipeline silently
rewriting its own prompt in patterson_formula.py after each run) was
rejected deliberately — an automatic prompt edit that regresses quality has
no visibility and no rollback, and nobody would notice until a run of bad
books had already shipped. That's the same failure mode
storyforge2/approval.py exists to prevent on the publishing side.

What this module actually does instead, safely: every book automatically
logs its real validate_chapter()/chapter_metrics() numbers here — no gate,
no approval needed, it's pure observability, so it costs nothing to always
run. summarize() then turns that log into a trend report: which checks are
failing most often, whether the numbers are getting better or worse over
recent books, and a short list of concrete next moves. A human (Josh, or a
future review step) reads that report and decides whether to act on it —
same "propose, don't silently apply" pattern as the approval gate.

Storage: books/quality_log.jsonl — one line per chapter, append-only, plain
JSON so it's readable/greppable without extra tooling. Never mutated in
place (no update/delete) — a log is a log.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = ["QualityLogger", "summarize"]


@dataclass
class ChapterQualityRecord:
    logged_at: str
    book_slug: str
    book_title: str
    reading_level: str
    chapter_number: int
    chapter_title: str
    word_count: int
    dialogue_ratio: float
    avg_sentence_length: float
    passive_voice_ratio: float
    has_cliffhanger: bool
    violations: list[str]
    passed: bool


class QualityLogger:
    """Appends one record per chapter per book. Never raises on a logging
    failure — quality tracking must never be able to break a real book
    cycle; see log_manuscript()'s try/except."""

    def __init__(self, work_base: Path | str = "books"):
        self.work_base = Path(work_base)
        self.work_base.mkdir(parents=True, exist_ok=True)
        self.log_path = self.work_base / "quality_log.jsonl"

    def log_manuscript(self, book_slug: str, book_title: str, manuscript) -> None:
        """Log every chapter of a just-generated manuscript. Call this
        BEFORE any formula_clean hard-fail check, so failed cycles (the
        most informative data point of all) still make it into the trend
        log rather than only ever seeing the successes."""
        try:
            from storyforge.patterson_formula import PattersonFormula

            formula = PattersonFormula(reading_level=manuscript.brief.reading_level)
            now = datetime.now(timezone.utc).isoformat()
            with self.log_path.open("a", encoding="utf-8") as f:
                for ch in manuscript.chapters:
                    metrics = formula.chapter_metrics(ch.text)
                    record = ChapterQualityRecord(
                        logged_at=now,
                        book_slug=book_slug,
                        book_title=book_title,
                        reading_level=manuscript.brief.reading_level,
                        chapter_number=ch.number,
                        chapter_title=ch.title,
                        word_count=metrics["word_count"],
                        dialogue_ratio=metrics["dialogue_ratio"],
                        avg_sentence_length=metrics["avg_sentence_length"],
                        passive_voice_ratio=metrics["passive_voice_ratio"],
                        has_cliffhanger=metrics["has_cliffhanger"],
                        violations=list(ch.violations),
                        passed=not ch.violations,
                    )
                    f.write(json.dumps(asdict(record)) + "\n")
        except Exception as e:
            # Logging is observability, not a pipeline stage — it must
            # never be the reason a real book cycle fails. Print and move
            # on rather than raising.
            print(f"[quality_log] WARNING: failed to log manuscript quality: {e}")

    def read_all(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        records = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip a corrupt line rather than failing the whole read
        return records


_VIOLATION_KEYS = [
    ("word count", "word count"),
    ("dialogue ratio", "dialogue ratio"),
    ("sentence length", "sentence length"),
    ("passive voice", "passive voice"),
    ("does not end with a cliffhanger", "cliffhanger"),
]


def _classify(violation_text: str) -> str:
    low = violation_text.lower()
    for needle, label in _VIOLATION_KEYS:
        if needle in low:
            return label
    return "other"


def summarize(work_base: Path | str = "books", last_n_books: int = 10) -> dict[str, Any]:
    """Aggregate the quality log into a trend report. Pure read/analysis —
    never writes anything, never changes the prompt or code. Splits the
    window into an "earlier" and "later" half (by book, not by chapter) to
    show whether each failure type is trending up or down, since a single
    aggregate rate hides whether things are improving or getting worse."""
    logger = QualityLogger(work_base)
    records = logger.read_all()
    if not records:
        return {"books": 0, "chapters": 0, "message": "No quality data logged yet."}

    # Group by book, preserving log order (== chronological, append-only).
    books_order: list[str] = []
    by_book: dict[str, list[dict]] = {}
    for r in records:
        slug = r["book_slug"]
        if slug not in by_book:
            by_book[slug] = []
            books_order.append(slug)
        by_book[slug].append(r)

    recent_books = books_order[-last_n_books:]
    recent_chapters = [r for slug in recent_books for r in by_book[slug]]

    half = max(1, len(recent_books) // 2)
    earlier_books, later_books = recent_books[:half], recent_books[half:]
    earlier_chapters = [r for slug in earlier_books for r in by_book[slug]]
    later_chapters = [r for slug in later_books for r in by_book[slug]] or earlier_chapters

    def failure_rate(chapters: list[dict], label: str) -> Optional[float]:
        if not chapters:
            return None
        fails = sum(
            1 for c in chapters if any(_classify(v) == label for v in c["violations"])
        )
        return round(fails / len(chapters), 3)

    labels = [lbl for _, lbl in _VIOLATION_KEYS]
    trend = {}
    for label in labels:
        earlier_rate = failure_rate(earlier_chapters, label)
        later_rate = failure_rate(later_chapters, label)
        trend[label] = {"earlier_failure_rate": earlier_rate, "later_failure_rate": later_rate}

    overall_pass_rate = round(
        sum(1 for c in recent_chapters if c["passed"]) / len(recent_chapters), 3
    ) if recent_chapters else None

    avg_metrics = {}
    for key in ("word_count", "dialogue_ratio", "avg_sentence_length", "passive_voice_ratio"):
        vals = [c[key] for c in recent_chapters if key in c]
        if vals:
            avg_metrics[key] = {
                "mean": round(statistics.mean(vals), 3),
                "stdev": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
            }

    # Rank failure types by how often they're the reason a chapter fails,
    # over the whole recent window — this is the "what to actually work on
    # next" signal, not just a wall of numbers.
    ranked = sorted(
        ((label, failure_rate(recent_chapters, label) or 0.0) for label in labels),
        key=lambda x: x[1], reverse=True,
    )

    suggestions = []
    for label, rate in ranked:
        if rate < 0.15:
            continue
        t = trend[label]
        direction = ""
        if t["earlier_failure_rate"] is not None and t["later_failure_rate"] is not None:
            if t["later_failure_rate"] > t["earlier_failure_rate"] + 0.05:
                direction = " and WORSENING over this window"
            elif t["later_failure_rate"] < t["earlier_failure_rate"] - 0.05:
                direction = " but IMPROVING over this window"
        suggestions.append(
            f"{label} fails {rate:.0%} of recent chapters{direction} — "
            f"the most persistent failure type worth reviewing first."
        )

    return {
        "books_in_window": len(recent_books),
        "chapters_in_window": len(recent_chapters),
        "overall_pass_rate": overall_pass_rate,
        "failure_rate_by_type": {label: rate for label, rate in ranked},
        "trend_earlier_vs_later": trend,
        "average_metrics": avg_metrics,
        "suggestions": suggestions or ["No failure type exceeds 15% of recent chapters — nothing urgent to review."],
    }


def _print_report(report: dict[str, Any]) -> None:
    if report.get("books") == 0:
        print(report["message"])
        return
    print(f"Quality trend — last {report['books_in_window']} book(s), "
          f"{report['chapters_in_window']} chapter(s)")
    print(f"Overall pass rate: {report['overall_pass_rate']:.0%}" if report['overall_pass_rate'] is not None else "Overall pass rate: n/a")
    print()
    print("Failure rate by type (most common first):")
    for label, rate in report["failure_rate_by_type"].items():
        t = report["trend_earlier_vs_later"][label]
        print(f"  {label:12s} {rate:>5.0%}   (earlier: {t['earlier_failure_rate']}, later: {t['later_failure_rate']})")
    print()
    print("Average metrics:")
    for key, stats in report["average_metrics"].items():
        print(f"  {key:20s} mean={stats['mean']}  stdev={stats['stdev']}")
    print()
    print("Suggestions:")
    for s in report["suggestions"]:
        print(f"  - {s}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Read-only quality trend report over storyforge2's logged "
        "chapter validation history. Never changes any code or prompt — this "
        "is the human-reviewed half of 'self-improving': the pipeline logs "
        "its own numbers every run, this command tells you what to actually "
        "go fix."
    )
    parser.add_argument("--state-dir", type=Path, default=Path("books"))
    parser.add_argument("--last", type=int, default=10, help="how many recent books to summarize")
    args = parser.parse_args()

    report = summarize(args.state_dir, last_n_books=args.last)
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
