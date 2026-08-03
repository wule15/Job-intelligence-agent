"""
Tests for how the daily digest is composed.

The rule these pin down: quotas are a ceiling, never a floor. A guaranteed
slot for company careers boards must not become a guaranteed slot for a bad
job when nothing good is available.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_sender  # noqa: E402
from core.database import Database  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'digest.db')
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    monkeypatch.setattr(telegram_sender.Config, 'DATABASE_PATH', path)
    # init_telegram_tracking ran against the real path before the patch, so
    # create the tracking table on the temp database explicitly.
    database.connection.execute(
        'CREATE TABLE IF NOT EXISTS telegram_sent_jobs ('
        'job_id INTEGER PRIMARY KEY, sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    database.connection.commit()
    yield database
    database.close()


def add(db, title, company, score, source):
    return db.add_job(title, company, 'desc', f'https://example.com/{title}/{company}',
                      source=source, relevance_score=score)


class TestScoreGate:
    def test_low_scoring_jobs_never_appear(self, db):
        for i in range(10):
            add(db, f'Weak Job {i}', f'Corp {i}', 5.0, 'Workday')
        assert telegram_sender.get_unsent_jobs() == []

    def test_quota_is_a_ceiling_not_a_floor(self, db):
        """
        The rule that matters. Only one board job qualifies, so the digest
        gets one, not five padded out with rejects.
        """
        add(db, 'Sales Engineer', 'Flowserve', 80.0, 'Workday')
        for i in range(10):
            add(db, f'Irrelevant {i}', f'Corp {i}', 3.0, 'Workday')

        jobs = telegram_sender.get_unsent_jobs()
        assert len(jobs) == 1
        assert jobs[0][1] == 'Sales Engineer'

    def test_score_bar_is_higher_than_storage_cutoff(self):
        """Worth keeping on the dashboard is a lower bar than worth a message."""
        import job_search_smart
        assert telegram_sender.MIN_DIGEST_SCORE > job_search_smart.MIN_RELEVANCE_SCORE


class TestQuotas:
    def test_one_employer_cannot_dominate(self, db):
        """The failure this whole change exists to prevent."""
        for i in range(30):
            add(db, f'Bosch Role {i}', 'Bosch', 70.0, 'SmartRecruiters')
        for i in range(10):
            add(db, f'Aggregator Role {i}', f'Agency {i}', 60.0, 'Free')

        jobs = telegram_sender.get_unsent_jobs()
        bosch = [j for j in jobs if j[2] == 'Bosch']
        assert len(bosch) <= telegram_sender.MAX_PER_COMPANY

    def test_ats_jobs_are_guaranteed_slots(self, db):
        """Company boards must not be crowded out by aggregator volume."""
        for i in range(30):
            add(db, f'Aggregator Role {i}', f'Agency {i}', 90.0, 'Free')
        for i in range(5):
            add(db, f'Board Role {i}', f'Manufacturer {i}', 40.0, 'Workday')

        jobs = telegram_sender.get_unsent_jobs()
        ats = [j for j in jobs if j[2].startswith('Manufacturer')]
        assert len(ats) >= telegram_sender.ATS_SLOTS

    def test_digest_never_exceeds_the_cap(self, db):
        for i in range(60):
            add(db, f'Role {i}', f'Company {i}', 70.0, 'Free')
        assert len(telegram_sender.get_unsent_jobs()) <= telegram_sender.DIGEST_SIZE

    def test_wildcard_absorbs_an_unfilled_quota(self, db):
        """An empty board quota must not shrink the digest."""
        for i in range(30):
            add(db, f'Aggregator Role {i}', f'Agency {i}', 70.0, 'Free')

        jobs = telegram_sender.get_unsent_jobs()
        assert len(jobs) == telegram_sender.DIGEST_SIZE

    def test_manually_saved_jobs_bypass_the_score_gate(self, db):
        """A job you saved by hand is one you already chose."""
        add(db, 'Something I Found Myself', 'Acme', 1.0, 'Gmail Draft')
        jobs = telegram_sender.get_unsent_jobs()
        assert any(j[1] == 'Something I Found Myself' for j in jobs)

    def test_no_duplicates_across_quotas(self, db):
        for i in range(20):
            add(db, f'Role {i}', f'Company {i}', 70.0, 'Workday')
        jobs = telegram_sender.get_unsent_jobs()
        ids = [j[0] for j in jobs]
        assert len(ids) == len(set(ids))

    def test_empty_database_is_safe(self, db):
        assert telegram_sender.get_unsent_jobs() == []


class TestTitlePrescreen:
    """The screen deciding which title-only jobs are worth a description fetch."""

    SKILLS = {'valve sizing', 'kv calculation', 'atex', 'technical writing'}

    def test_target_role_passes(self):
        from core.job_filter import title_prescreen
        assert title_prescreen('Sales Engineer', self.SKILLS)

    def test_sector_keyword_passes(self):
        from core.job_filter import title_prescreen
        assert title_prescreen('Industrial Automation Lead', self.SKILLS)

    def test_cv_skill_in_title_passes(self):
        from core.job_filter import title_prescreen
        assert title_prescreen('ATEX Compliance Officer', self.SKILLS)

    def test_unrelated_title_is_screened_out(self):
        from core.job_filter import title_prescreen
        assert not title_prescreen('Pastry Chef', self.SKILLS)

    def test_empty_title_is_screened_out(self):
        from core.job_filter import title_prescreen
        assert not title_prescreen('', self.SKILLS)

    def test_short_tokens_do_not_match_everything(self):
        """A two-letter skill token must not pass every title."""
        from core.job_filter import title_prescreen
        assert not title_prescreen('Pastry Chef', {'ai', 'qa'})
