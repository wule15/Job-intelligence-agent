"""
Support-function titles: payroll, HR, recruiting, benefits, marketing,
communications, design.

A company careers board is taken whole, not searched, and its adverts share
paragraphs about AI, cloud and the platform. A payroll role there matched as
many CV skills as an engineering role, so for ten days the direct digest was
filled with HR, payroll, recruiter, marketing and communications jobs. No
score cutoff separates them, because the matching words sit in the
boilerplate.

The fix is a private list of title terms (EXCLUDED_TITLE_TERMS), matched as
whole words with accents folded, that rules a job out on its title alone. The
public default is empty, so the engine excludes nothing until a user sets it.
Every test pins the settings it reads, so the result does not depend on a
local .env.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config, term_list  # noqa: E402
from core.job_filter import is_excluded_title, title_drop_reason  # noqa: E402

TERMS = ['payroll', 'benefits', 'motion designer', 'communications', 'hr',
         'racunovodja', 'fp&a', 'talent acquisition']

# A description full of what every advert on a software firm's board says,
# plus enough CV skills to clear any cutoff.
BOILERPLATE = ('Valve sizing, Kv calculation, technical sales, fluid systems, '
               'commissioning, documentation, P&ID review and ATEX areas. '
               'Join our AI platform team in the cloud.')


@pytest.fixture
def pinned(monkeypatch):
    """Turn off every other rule that reads the user's .env."""
    for key in ('ALLOWED_COUNTRIES', 'SPONSORSHIP_ONLY_COUNTRIES',
                'EXCLUDED_LOCATIONS', 'NON_FLUENT_LANGUAGES'):
        monkeypatch.setattr(Config, key, [], raising=False)
    monkeypatch.setattr(Config, 'EXCLUDED_TITLE_TERMS', list(TERMS), raising=False)


class TestIsExcludedTitle:
    @pytest.mark.parametrize('title', [
        'Payroll Lead',
        'Global Benefits and Mobility Partner',
        'Sr. Motion Designer',
        'Executive Communications Lead',
        'HR Business Partner',
        'FP&A Analyst',
        'Talent-Acquisition Partner',
    ])
    def test_support_titles_are_excluded(self, title):
        assert is_excluded_title(title, TERMS)

    @pytest.mark.parametrize('title', [
        'Sales Manager',
        'Project Manager',
        'Account Manager',
        'Mechanical Designer',
        'Hydraulics Engineer',
        # "hr" sits inside both words: only a whole word counts.
        'Throughput Engineer',
        'Synchronous Motor Specialist',
        # "hr" starts both words, so the right-hand boundary matters too.
        'Tehnolog hrane',
        'Prodavac Hrvatska',
    ])
    def test_wanted_titles_are_kept(self, title):
        assert not is_excluded_title(title, TERMS)

    def test_case_and_accents_are_ignored(self):
        assert is_excluded_title('Računovođa', TERMS)
        assert is_excluded_title('Racunovodja', ['računovođa'])
        assert is_excluded_title('PAYROLL SPECIALIST', TERMS)

    def test_empty_terms_exclude_nothing(self):
        assert not is_excluded_title('Payroll Lead', [])

    def test_default_reads_the_setting(self, monkeypatch):
        monkeypatch.setattr(Config, 'EXCLUDED_TITLE_TERMS', [], raising=False)
        assert not is_excluded_title('Payroll Specialist')
        monkeypatch.setattr(Config, 'EXCLUDED_TITLE_TERMS', ['payroll'], raising=False)
        assert is_excluded_title('Payroll Specialist')

    def test_is_a_title_drop_reason(self, pinned):
        assert title_drop_reason('Payroll Specialist', 'Greenhouse') == 'excluded_title'
        assert title_drop_reason('Valve Sales Engineer', 'Greenhouse') is None


class TestSetting:
    def test_terms_are_parsed_lowercase(self):
        assert term_list('Payroll, HR ,,Talent Acquisition') == [
            'payroll', 'hr', 'talent acquisition']
        assert term_list('') == []
        assert term_list(None) == []

    def test_public_default_is_empty(self):
        import os
        if not os.getenv('EXCLUDED_TITLE_TERMS'):
            assert Config.EXCLUDED_TITLE_TERMS == []


class TestFilterJobs:
    def test_a_high_scoring_payroll_role_is_dropped(self, job_filter, make_job, pinned):
        control = make_job(title='Valve Sales Engineer', description=BOILERPLATE,
                           source='Greenhouse')
        payroll = make_job(title='Payroll Specialist', description=BOILERPLATE,
                           source='Greenhouse')
        kept = job_filter.filter_jobs([control, payroll], min_score=10)
        assert [job['title'] for job in kept] == ['Valve Sales Engineer']
        assert job_filter.last_rejected['excluded_title'] == 1

    def test_a_hand_saved_job_is_kept(self, job_filter, make_job, pinned):
        saved = make_job(title='Payroll Specialist', description=BOILERPLATE,
                         source='Gmail Draft')
        assert len(job_filter.filter_jobs([saved], min_score=10)) == 1


class TestFetchBudget:
    """A title the filter will drop must not spend the description fetches."""

    def test_an_excluded_title_is_not_fetched(self, pinned, monkeypatch):
        import job_search_smart
        job = {'title': 'Payroll Automation Specialist', 'source': 'Workday',
               'location': 'Remote', 'link': 'https://x.example/1'}
        assert not job_search_smart.worth_fetching(job, {'automation'})
        monkeypatch.setattr(Config, 'EXCLUDED_TITLE_TERMS', [], raising=False)
        assert job_search_smart.worth_fetching(job, {'automation'})

    def test_the_budget_goes_to_other_jobs(self, pinned, monkeypatch):
        import job_search_smart
        from sources import ats
        fetched = []
        monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Workday',
                            lambda job: fetched.append(job['title']) or 'text')
        jobs = [{'title': title, 'source': 'Workday', 'location': 'Remote',
                 'description': '', 'link': f'https://x.example/{i}'}
                for i, title in enumerate(['Payroll Automation Specialist',
                                           'Automation Sales Engineer'])]
        ats.enrich_descriptions(
            jobs, should_fetch=lambda job: job_search_smart.worth_fetching(job, {'automation'}),
            max_fetches=1)
        assert fetched == ['Automation Sales Engineer']


class TestStoredRows:
    """A support-function row stored before the setting existed must not be
    sent after it. The digest's recheck of stored rows holds it back, and the
    next job takes its slot in each of the three messages."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch, pinned):
        import telegram_sender
        from core.database import Database
        path = str(tmp_path / 'excluded.db')
        monkeypatch.setattr(Config, 'DATABASE_PATH', path)
        monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
        monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['testland'], raising=False)
        database = Database(db_path=path)
        database.init_database()
        telegram_sender.init_telegram_tracking()
        yield database
        database.close()

    def add(self, db, title, company, score, source='Greenhouse'):
        return db.add_job(title, company, BOILERPLATE,
                          f'https://example.com/{company}/{title.replace(" ", "-")}',
                          source=source, relevance_score=score,
                          location='Testcity, Testland')

    def test_backlog_row_is_held_back_and_the_next_job_fills_the_slot(self, db, capsys):
        import telegram_sender
        payroll = self.add(db, 'Payroll Specialist', 'Softco', 90.0)
        valve = self.add(db, 'Valve Sales Engineer', 'Valveco', 70.0)
        saved = self.add(db, 'Payroll Analyst', 'Saved Co', 10.0, source='Gmail Draft')

        assert telegram_sender.suppress_ineligible() == 1
        assert f'[ineligible:excluded_title] id={payroll}' in capsys.readouterr().out

        for pick in (telegram_sender.get_direct_jobs, telegram_sender.get_regional_jobs):
            assert [job[0] for job in pick(limit=1)] == [valve]
        main = [job[0] for job in telegram_sender.get_unsent_jobs(limit=2)]
        assert main == [saved, valve]
