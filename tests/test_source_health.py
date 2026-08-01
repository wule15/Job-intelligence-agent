"""
Tests for per source accounting.

This is the module that answers "did that source die or was it a slow week".
It has to keep working when a source raises, because the whole point is that
a failure gets recorded rather than ending the run.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import source_health  # noqa: E402
from source_health import SourceResult, summary_table, track  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / 'health.db')
    source_health.init_tables(db_path=path)
    return path


class TestTrack:
    def test_successful_source_is_recorded(self):
        with track('Demo', queries=2) as result:
            result.jobs = [{'title': 'a'}, {'title': 'b'}]
        assert result.count == 2
        assert result.ok
        assert result.status == 'ok'
        assert result.error is None

    def test_empty_source_is_not_an_error(self):
        with track('Demo') as result:
            result.jobs = []
        assert result.status == 'empty'
        assert result.ok

    def test_exception_is_captured_not_raised(self):
        """A dead source must never end the run."""
        with track('Demo') as result:
            raise RuntimeError('quota exhausted')
        assert result.status == 'error'
        assert 'quota exhausted' in result.error
        assert result.jobs == []

    def test_duration_is_measured(self):
        with track('Demo') as result:
            result.jobs = []
        assert result.duration_s >= 0.0


class TestSummaryTable:
    def test_lists_every_source(self):
        results = [
            SourceResult('Alpha', jobs=[{}] * 3),
            SourceResult('Beta', jobs=[]),
        ]
        table = summary_table(results)
        assert 'Alpha' in table
        assert 'Beta' in table

    def test_shows_the_error(self):
        r = SourceResult('Alpha')
        r.error = 'RuntimeError: quota exhausted'
        assert 'quota exhausted' in summary_table([r])

    def test_warns_on_concentration(self):
        """
        The 30 July failure mode: one source produced 134 of 135 results and
        nothing said so.
        """
        results = [
            SourceResult('Free aggregators', jobs=[{}] * 134),
            SourceResult('JSearch', jobs=[]),
            SourceResult('Apify', jobs=[]),
            SourceResult('SerpAPI', jobs=[]),
            SourceResult('LinkedIn', jobs=[{}]),
        ]
        table = summary_table(results)
        assert 'WARNING' in table
        assert 'Free aggregators' in table

    def test_no_warning_when_spread_is_healthy(self):
        results = [
            SourceResult('Alpha', jobs=[{}] * 30),
            SourceResult('Beta', jobs=[{}] * 25),
            SourceResult('Gamma', jobs=[{}] * 20),
        ]
        assert 'WARNING' not in summary_table(results)

    def test_empty_input_is_safe(self):
        assert summary_table([]) == 'no sources ran'


class TestStaleDetection:
    def test_source_empty_three_runs_is_stale(self, db_path):
        for _ in range(3):
            source_health.record([SourceResult('Dead', jobs=[])], db_path=db_path)

        stale = source_health.stale_sources(db_path=db_path)
        assert [name for name, _, _ in stale] == ['Dead']

    def test_two_empty_runs_is_not_yet_stale(self, db_path):
        for _ in range(2):
            source_health.record([SourceResult('Quiet', jobs=[])], db_path=db_path)
        assert source_health.stale_sources(db_path=db_path) == []

    def test_a_producing_source_is_not_stale(self, db_path):
        for _ in range(5):
            source_health.record([SourceResult('Alive', jobs=[{}] * 4)], db_path=db_path)
        assert source_health.stale_sources(db_path=db_path) == []

    def test_recovery_clears_stale(self, db_path):
        for _ in range(3):
            source_health.record([SourceResult('Flaky', jobs=[])], db_path=db_path)
        assert source_health.stale_sources(db_path=db_path)

        source_health.record([SourceResult('Flaky', jobs=[{}] * 5)], db_path=db_path)
        assert source_health.stale_sources(db_path=db_path) == []

    def test_last_error_is_reported(self, db_path):
        for _ in range(3):
            r = SourceResult('Dead', jobs=[])
            r.error = 'RuntimeError: HTTP 429'
            source_health.record([r], db_path=db_path)

        (_, _, last_error), = source_health.stale_sources(db_path=db_path)
        assert '429' in last_error


class TestRecovery:
    """
    Stale is a label, not a skip. Several sources are free monthly tiers
    that come back on their own when the quota resets, so the run has to be
    able to observe that happening.
    """

    def test_recovery_after_stale_is_reported(self, db_path):
        for _ in range(3):
            source_health.record([SourceResult('Quota', jobs=[])], db_path=db_path)

        results = [SourceResult('Quota', jobs=[{}] * 12)]
        assert source_health.recovered_sources(results, db_path=db_path) == ['Quota']

    def test_a_source_that_was_never_stale_is_not_a_recovery(self, db_path):
        source_health.record([SourceResult('Steady', jobs=[{}] * 5)], db_path=db_path)
        results = [SourceResult('Steady', jobs=[{}] * 5)]
        assert source_health.recovered_sources(results, db_path=db_path) == []

    def test_still_empty_is_not_a_recovery(self, db_path):
        for _ in range(3):
            source_health.record([SourceResult('Dead', jobs=[])], db_path=db_path)
        results = [SourceResult('Dead', jobs=[])]
        assert source_health.recovered_sources(results, db_path=db_path) == []

    def test_days_since_last_result(self, db_path):
        source_health.record([SourceResult('Quota', jobs=[{}] * 3)], db_path=db_path)
        assert source_health.days_since_last_result('Quota', db_path=db_path) == 0

    def test_days_since_last_result_is_none_when_never_produced(self, db_path):
        source_health.record([SourceResult('Never', jobs=[])], db_path=db_path)
        assert source_health.days_since_last_result('Never', db_path=db_path) is None


class TestYieldBySource:
    def test_totals_are_summed_and_sorted(self, db_path):
        source_health.record([
            SourceResult('Big', jobs=[{}] * 50),
            SourceResult('Small', jobs=[{}] * 2),
        ], db_path=db_path)
        source_health.record([
            SourceResult('Big', jobs=[{}] * 30),
            SourceResult('Small', jobs=[{}] * 1),
        ], db_path=db_path)

        rows = source_health.yield_by_source(db_path=db_path, days=7)
        assert rows[0][0] == 'Big'
        assert rows[0][1] == 80
        assert rows[1][1] == 3
