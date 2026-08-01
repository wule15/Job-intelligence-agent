"""
Per source accounting.

The problem this solves: on 30 July the run reported success. Underneath,
JSearch was out of quota, Apify was over its hard limit, SerpAPI had never
been configured and LinkedIn RSS was returning something that was not XML.
One source produced 134 of 135 results. Nothing in the output said so.

A source that quietly dies and a genuinely slow week look identical unless
something records yield per source over time. That is all this module does.
"""

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from config import Config

# A source returning nothing this many runs in a row is reported as stale.
#
# Stale is a label, never a skip. Every source is called on every run,
# however long it has been quiet. Most of these sources are free monthly
# tiers: JSearch resets its RapidAPI quota on the billing date, Apify
# resets its usage limit, and a rate limited scraper recovers on its own.
# A source that stopped being called could never be observed recovering,
# so the run would report it dead forever. Report, do not skip.
STALE_AFTER_EMPTY_RUNS = 3


@dataclass
class SourceResult:
    """What one source produced in one run."""

    name: str
    jobs: list = field(default_factory=list)
    queries: int = 0
    duration_s: float = 0.0
    error: str | None = None

    @property
    def count(self):
        return len(self.jobs)

    @property
    def ok(self):
        return self.error is None

    @property
    def status(self):
        if self.error:
            return 'error'
        if not self.jobs:
            return 'empty'
        return 'ok'


@contextmanager
def track(name, queries=0):
    """
    Run a source and capture whatever happens.

    An exception is recorded as an error and swallowed, because one dead
    source must not end the run. Nothing is hidden: the error string is
    stored and printed in the summary table.

        with track('Adzuna', queries=3) as result:
            result.jobs = adzuna.search_all(queries)
    """
    result = SourceResult(name=name, queries=queries)
    started = time.monotonic()
    try:
        yield result
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.jobs = []
    finally:
        result.duration_s = round(time.monotonic() - started, 2)


def init_tables(db_path=None):
    """Create the source_runs table. Safe to call on every run."""
    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS source_runs (
                id INTEGER PRIMARY KEY,
                run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                source TEXT NOT NULL,
                queries INTEGER DEFAULT 0,
                raw_count INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0.0,
                status TEXT,
                error TEXT
            )
        ''')
        conn.execute(
            'CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source, run_at)')
        conn.commit()
    finally:
        conn.close()


def record(results, db_path=None):
    """Write one row per source for this run."""
    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    try:
        conn.executemany(
            '''INSERT INTO source_runs (source, queries, raw_count, duration_s, status, error)
               VALUES (?, ?, ?, ?, ?, ?)''',
            [(r.name, r.queries, r.count, r.duration_s, r.status, r.error) for r in results],
        )
        conn.commit()
    finally:
        conn.close()


def summary_table(results):
    """
    Render the per source table printed at the end of a run.

    Replaces six scattered print lines with one block you can read in two
    seconds and paste into a message.
    """
    if not results:
        return "no sources ran"

    name_width = max(len(r.name) for r in results)
    name_width = max(name_width, 6)

    lines = [
        f"{'source'.ljust(name_width)}  {'jobs':>5}  {'queries':>7}  {'time':>6}  status",
        f"{'-' * name_width}  {'-' * 5}  {'-' * 7}  {'-' * 6}  ------",
    ]

    for r in sorted(results, key=lambda x: x.count, reverse=True):
        status = r.status
        if r.error:
            status = f"error: {r.error[:60]}"
        lines.append(
            f"{r.name.ljust(name_width)}  {r.count:>5}  {r.queries:>7}  "
            f"{r.duration_s:>5.1f}s  {status}"
        )

    total = sum(r.count for r in results)
    lines.append(f"{'-' * name_width}  {'-' * 5}  {'-' * 7}  {'-' * 6}  ------")
    lines.append(f"{'total'.ljust(name_width)}  {total:>5}")

    # Concentration warning. One source carrying everything is the failure
    # mode that hid four dead sources for weeks.
    if total > 0:
        top = max(results, key=lambda r: r.count)
        share = top.count / total
        if share >= 0.9 and len(results) > 1:
            lines.append('')
            lines.append(
                f"WARNING  {top.name} produced {share:.0%} of all results. "
                f"{sum(1 for r in results if r.count == 0)} of {len(results)} sources returned nothing."
            )

    return '\n'.join(lines)


def stale_sources(db_path=None, runs=STALE_AFTER_EMPTY_RUNS):
    """
    Sources that returned nothing for the last `runs` consecutive runs.

    Returns a list of (source, consecutive_empty_runs, last_error).
    Used by the weekly digest so a dead source is named in Telegram rather
    than sitting unnoticed in a log file.
    """
    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    stale = []
    try:
        sources = [
            row['source']
            for row in conn.execute('SELECT DISTINCT source FROM source_runs')
        ]

        for source in sources:
            rows = conn.execute(
                '''SELECT raw_count, error FROM source_runs
                   WHERE source = ? ORDER BY run_at DESC, id DESC LIMIT ?''',
                (source, runs),
            ).fetchall()

            if len(rows) < runs:
                continue
            if all(row['raw_count'] == 0 for row in rows):
                last_error = next((r['error'] for r in rows if r['error']), None)
                stale.append((source, len(rows), last_error))
    finally:
        conn.close()

    return stale


def recovered_sources(results, db_path=None, runs=STALE_AFTER_EMPTY_RUNS):
    """
    Sources that produced jobs this run after being stale before it.

    This is the payoff for never skipping a stale source. A free monthly
    tier that reset overnight shows up as a RECOVERED line rather than
    silently rejoining the pack, so you can see the quota cycle.

    Call with this run's results, before record() is invoked.
    """
    producing_now = {r.name for r in results if r.count > 0}
    if not producing_now:
        return []

    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    recovered = []
    try:
        for name in producing_now:
            rows = conn.execute(
                '''SELECT raw_count FROM source_runs
                   WHERE source = ? ORDER BY run_at DESC, id DESC LIMIT ?''',
                (name, runs),
            ).fetchall()
            if len(rows) == runs and all(row[0] == 0 for row in rows):
                recovered.append(name)
    finally:
        conn.close()

    return sorted(recovered)


def days_since_last_result(source, db_path=None):
    """
    Whole days since this source last returned at least one job.

    Returns None if it has never produced anything. Used to say how long a
    source has been quiet rather than just that it is quiet.
    """
    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    try:
        row = conn.execute(
            '''SELECT CAST(julianday('now') - julianday(MAX(run_at)) AS INTEGER)
               FROM source_runs WHERE source = ? AND raw_count > 0''',
            (source,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else None


def yield_by_source(db_path=None, days=7):
    """
    Total jobs per source over the last N days, highest first.

    Feeds the weekly digest so trend is visible, not just today's number.
    """
    conn = sqlite3.connect(db_path or Config.DATABASE_PATH)
    try:
        rows = conn.execute(
            '''SELECT source, SUM(raw_count) AS total, COUNT(*) AS runs
               FROM source_runs
               WHERE run_at >= datetime('now', ?)
               GROUP BY source
               ORDER BY total DESC''',
            (f'-{int(days)} days',),
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1] or 0, r[2]) for r in rows]
