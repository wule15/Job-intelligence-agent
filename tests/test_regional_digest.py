"""
Tests for the regional (home-market) digest, the optional third Telegram
message.

The rules pinned down here:
  - It is inert by default. With no region terms configured, nothing is
    selected and the message never appears, so the public engine is unchanged.
  - When configured, it selects only jobs whose stored location matches a term,
    respects the score gate and the per-company cap, and Montenegro / Bosnia /
    Serbia jobs survive the work-eligibility filter.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_sender  # noqa: E402
from core.database import Database  # noqa: E402
from core.job_filter import matches_region  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'regional.db')
    # Patch the DB path BEFORE anything reads Config.DATABASE_PATH, so no table
    # is ever created against the real personal database during the test.
    monkeypatch.setattr(telegram_sender.Config, 'DATABASE_PATH', path)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


def add(db, title, company, score, location, source='Adzuna'):
    return db.add_job(title, company, 'desc',
                      f'https://example.com/{title}/{company}',
                      source=source, relevance_score=score, location=location)


def set_terms(monkeypatch, match_terms=None, locations=None):
    monkeypatch.setattr(telegram_sender.Config, 'REGIONAL_MATCH_TERMS', match_terms or [])
    monkeypatch.setattr(telegram_sender.Config, 'REGIONAL_JOB_LOCATIONS', locations or [])


class TestMatchesRegion:
    def test_substring_case_insensitive(self):
        assert matches_region('Beograd, Serbia', ['serbia'])
        assert matches_region('NOVI SAD', ['novi sad'])

    def test_no_match(self):
        assert not matches_region('Berlin, Germany', ['serbia', 'bosnia'])

    def test_empty_inputs_never_match(self):
        assert not matches_region('', ['serbia'])
        assert not matches_region('Beograd', [])
        assert not matches_region(None, ['serbia'])


class TestRegionalSelection:
    def test_inert_without_terms(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=[], locations=[])
        add(db, 'Sales Engineer', 'Acme', 80.0, 'Beograd, Serbia')
        assert telegram_sender.get_regional_jobs() == []

    def test_selects_only_matching_locations(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=['serbia', 'beograd', 'sarajevo'])
        add(db, 'RS Job', 'Acme', 80.0, 'Beograd, Serbia')
        add(db, 'BA Job', 'Beta', 70.0, 'Sarajevo, Bosnia and Herzegovina')
        add(db, 'DE Job', 'Gamma', 90.0, 'Berlin, Germany')
        titles = [j[1] for j in telegram_sender.get_regional_jobs()]
        assert 'RS Job' in titles and 'BA Job' in titles
        assert 'DE Job' not in titles

    def test_falls_back_to_locations_when_no_match_terms(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=[], locations=['Serbia'])
        add(db, 'RS Job', 'Acme', 80.0, 'Beograd, Serbia')
        assert [j[1] for j in telegram_sender.get_regional_jobs()] == ['RS Job']

    def test_score_gate_applies(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=['serbia'])
        add(db, 'Weak', 'Acme', 3.0, 'Beograd, Serbia')
        assert telegram_sender.get_regional_jobs() == []

    def test_per_company_cap(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=['serbia'])
        for i in range(5):
            add(db, f'Job {i}', 'SameCorp', 80.0 - i, 'Beograd, Serbia')
        jobs = telegram_sender.get_regional_jobs()
        assert len(jobs) == telegram_sender.MAX_PER_COMPANY

    def test_empty_digest_message_is_skipped(self):
        msg, ids = telegram_sender.format_regional_digest([])
        assert msg == '' and ids == []

    def test_not_starved_by_higher_scoring_nonregional(self, db, monkeypatch):
        """The fix for the top-N-by-score window: many higher-scoring non-region
        jobs beyond the LIMIT must not hide a lower-scoring region job."""
        set_terms(monkeypatch, match_terms=['serbia'])
        for i in range(410):  # more than the query's LIMIT of 400
            add(db, f'Remote {i}', f'Corp {i}', 90.0, 'Berlin, Germany')
        add(db, 'RS Job', 'Acme', 40.0, 'Beograd, Serbia')  # rank ~411 by score
        titles = [j[1] for j in telegram_sender.get_regional_jobs()]
        assert 'RS Job' in titles


class TestBackfillAndCrossMessageDedup:
    def test_backfill_makes_a_locationless_job_regional(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=['serbia'])
        # First sighting has no location, so it is not regional yet.
        db.add_job('Sales Engineer', 'Acme', 'd', 'https://x/1',
                   source='Adzuna', relevance_score=80, location=None)
        assert telegram_sender.get_regional_jobs() == []
        # A later sighting of the same job supplies the location: ON CONFLICT
        # backfills it, and it becomes regional.
        db.add_job('Sales Engineer', 'Acme', 'd', 'https://x/1',
                   source='Adzuna', relevance_score=80, location='Beograd, Serbia')
        assert [j[1] for j in telegram_sender.get_regional_jobs()] == ['Sales Engineer']

    def test_backfill_never_overwrites_a_good_location(self, db, monkeypatch):
        set_terms(monkeypatch, match_terms=['serbia', 'germany'])
        db.add_job('Dev', 'Acme', 'd', 'https://x/2',
                   source='Adzuna', relevance_score=80, location='Beograd, Serbia')
        # A re-sighting with a different location must not clobber the stored one.
        db.add_job('Dev', 'Acme', 'd', 'https://x/2',
                   source='Adzuna', relevance_score=80, location='Berlin, Germany')
        jobs = telegram_sender.get_regional_jobs()
        assert len(jobs) == 1  # still one row, location preserved as Serbia

    def test_direct_job_not_repeated_in_regional(self, db, monkeypatch):
        """A Serbia job from an ATS board goes in the direct message; once that
        marks it sent, the regional query excludes it. No cross-message dup."""
        set_terms(monkeypatch, match_terms=['serbia'])
        add(db, 'QA Engineer', 'Acme', 80.0, 'Beograd, Serbia', source='Workday')
        direct = telegram_sender.get_direct_jobs()
        assert [j[1] for j in direct] == ['QA Engineer']
        telegram_sender.mark_jobs_sent([j[0] for j in direct])
        assert telegram_sender.get_regional_jobs() == []
