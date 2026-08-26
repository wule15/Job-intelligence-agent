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

    def test_regular_digest_is_a_general_pull_by_score(self, db):
        """The regular feed is a best-of by score, not a guaranteed company-board
        slot. Low-scoring company boards must not force their way in over
        higher-scoring aggregator jobs; boards appear on merit, not by quota.
        This is the behaviour the direct-from-company digest exists to cover,
        so the two messages stay distinct."""
        for i in range(30):
            add(db, f'Aggregator Role {i}', f'Agency {i}', 90.0, 'Free')
        for i in range(5):
            add(db, f'Board Role {i}', f'Manufacturer {i}', 40.0, 'Workday')

        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert len(jobs) == 10
        assert all(j[3] == 90.0 for j in jobs)
        assert not any(j[2].startswith('Manufacturer') for j in jobs)

    def test_company_boards_still_appear_on_merit(self, db):
        """Company boards are kept, not excluded: a high-scoring board job is
        included in the general pull like any other source."""
        add(db, 'Great Board Role', 'Flowserve', 85.0, 'Workday')
        for i in range(5):
            add(db, f'Aggregator Role {i}', f'Agency {i}', 60.0, 'Free')
        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert any(j[2] == 'Flowserve' for j in jobs)

    def test_direct_fed_job_does_not_repeat_in_regular(self, db):
        """The user-facing rule: a company job sent in the direct digest is
        marked sent, so the regular digest never repeats it in the same run."""
        jid = add(db, 'Board Role', 'Flowserve', 80.0, 'Workday')
        add(db, 'Aggregator Role', 'Agency', 70.0, 'Free')
        direct = telegram_sender.get_direct_jobs()
        assert any(j[0] == jid for j in direct)
        telegram_sender.mark_jobs_sent([jid])
        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert not any(j[0] == jid for j in jobs)

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


class TestSSRFGuard:
    """The liveness check refuses to fetch private/internal addresses, so a
    scraped apply link cannot be used to probe the local network."""

    def test_internal_targets_are_refused(self):
        for u in ('http://127.0.0.1/x', 'http://10.0.0.1/x', 'http://192.168.1.1/x',
                  'http://169.254.169.254/latest/meta-data', 'ftp://8.8.8.8/x', 'not a url'):
            assert telegram_sender._is_internal_url(u), u

    def test_public_ip_is_allowed(self):
        assert not telegram_sender._is_internal_url('http://8.8.8.8/x')

    def test_check_link_live_refuses_internal_without_fetching(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError('an internal URL must never be fetched')
        monkeypatch.setattr(telegram_sender.requests, 'get', boom)
        assert telegram_sender.check_link_live('http://127.0.0.1/ssrf-probe-xyz') is False


class TestSourceDemotion:
    """Indeed and JSearch are held back to a last-resort fill."""

    def test_indeed_does_not_appear_when_quality_jobs_exist(self, db):
        for i in range(20):
            add(db, f'Quality {i}', f'GoodCo {i}', 70.0, 'Jobicy')
        for i in range(10):
            add(db, f'Indeed Role {i}', f'IndeedCo {i}', 95.0, 'Apify / Indeed')
        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert jobs, 'expected a full digest of quality jobs'
        assert not any('indeed' in (j[6] or '').lower() for j in jobs)

    def test_jsearch_is_demoted_too(self, db):
        for i in range(20):
            add(db, f'Quality {i}', f'GoodCo {i}', 70.0, 'RemoteOK')
        for i in range(10):
            add(db, f'JS Role {i}', f'JSCo {i}', 99.0, 'JSearch')
        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert not any('jsearch' in (j[6] or '').lower() for j in jobs)

    def test_indeed_fills_only_when_nothing_better(self, db):
        for i in range(5):
            add(db, f'Indeed Role {i}', f'IndeedCo {i}', 60.0, 'Apify / Indeed')
        jobs = telegram_sender.get_unsent_jobs(limit=10)
        assert jobs and all('indeed' in (j[6] or '').lower() for j in jobs)


class TestLiveness:
    """The expired-link check on aggregator jobs in the main digest."""

    def test_expired_aggregator_job_is_dropped(self, db):
        add(db, 'Live Role', 'GoodCo', 70.0, 'Jobicy')
        add(db, 'Dead Role', 'DeadCo', 90.0, 'Jobicy')
        dead = {'https://example.com/Dead Role/DeadCo'}
        jobs = telegram_sender.get_unsent_jobs(limit=10, is_live=lambda url: url not in dead)
        titles = [j[1] for j in jobs]
        assert 'Live Role' in titles and 'Dead Role' not in titles

    def test_ats_jobs_bypass_the_liveness_check(self, db):
        add(db, 'Board Role', 'Flowserve', 70.0, 'Workday')
        # is_live rejects everything; an ATS job must still come through.
        jobs = telegram_sender.get_unsent_jobs(limit=10, is_live=lambda url: False)
        assert any(j[1] == 'Board Role' for j in jobs)

    def test_no_check_means_no_network(self, db):
        # Default is_live=None must never call out. A callable that explodes if
        # invoked proves the default path does not touch it.
        def boom(url):
            raise AssertionError('liveness check ran when it should not have')
        add(db, 'Role', 'Co', 70.0, 'Jobicy')
        telegram_sender.get_unsent_jobs(limit=10)  # no is_live -> boom never wired
        # And when passed, it IS used (guards against a silently ignored param).
        import pytest
        with pytest.raises(AssertionError):
            telegram_sender.get_unsent_jobs(limit=10, is_live=boom)


class TestDirectDigest:
    """The separate direct-from-company digest, ATS boards only."""

    def test_only_ats_sources_appear(self, db):
        add(db, 'Board Role', 'Flowserve', 80.0, 'Workday')
        add(db, 'Board Role 2', 'Anthropic', 80.0, 'Greenhouse')
        add(db, 'Aggregator Role', 'Agency', 90.0, 'Free')
        jobs = telegram_sender.get_direct_jobs()
        assert jobs, 'expected the two ATS jobs'
        assert all(j[6] in telegram_sender.ATS_SOURCES for j in jobs)
        assert not any(j[2] == 'Agency' for j in jobs)

    def test_score_gate_applies(self, db):
        add(db, 'Weak Board Role', 'Flowserve', 5.0, 'Workday')
        assert telegram_sender.get_direct_jobs() == []

    def test_per_company_cap(self, db):
        for i in range(6):
            add(db, f'Flowserve Role {i}', 'Flowserve', 70.0, 'Workday')
        jobs = telegram_sender.get_direct_jobs()
        assert len(jobs) <= telegram_sender.MAX_PER_COMPANY

    def test_respects_the_limit(self, db):
        for i in range(40):
            add(db, f'Role {i}', f'Manufacturer {i}', 70.0, 'Workday')
        assert len(telegram_sender.get_direct_jobs()) <= telegram_sender.DIRECT_DIGEST_SIZE

    def test_excludes_already_sent(self, db):
        jid = add(db, 'Board Role', 'Flowserve', 80.0, 'Workday')
        telegram_sender.mark_jobs_sent([jid])
        assert telegram_sender.get_direct_jobs() == []

    def test_empty_direct_digest_sends_nothing(self):
        message, ids = telegram_sender.format_direct_digest([])
        assert ids == [] and message == ''

    def test_empty_database_is_safe(self, db):
        assert telegram_sender.get_direct_jobs() == []


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
