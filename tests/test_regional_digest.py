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
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    monkeypatch.setattr(telegram_sender.Config, 'DATABASE_PATH', path)
    database.connection.execute(
        'CREATE TABLE IF NOT EXISTS telegram_sent_jobs ('
        'job_id INTEGER PRIMARY KEY, sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    database.connection.commit()
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
