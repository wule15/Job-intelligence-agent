"""
End-to-end: one full daily run, from the job boards to the Telegram messages.

Every other test file checks one part. This one runs the real pipeline in
order, the way the scheduled task does: search, dedup, description fetch,
filter and score, store, pick the day's digests, send. Only the edges are
replaced, because a test must not depend on live job boards or post to a real
chat:

  - the job boards are fakes returning a fixed set of adverts, one of which
    crashes, as a real source sometimes does
  - the detail page fetch returns a fixed advert
  - the database is a temporary file
  - Telegram's HTTP call is captured instead of sent

Then it checks what would have landed in the chat. The adverts are chosen so
each one proves a single rule: a strong match is sent, a weak match is not, a
dealbreaker is dropped, a duplicate is sent once, a local-language advert is
scored on its full text, a hand-saved job always arrives, and nothing is sent
twice across two days.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_search_smart  # noqa: E402
import telegram_sender  # noqa: E402
from core.config import Config  # noqa: E402
from core.database import Database  # noqa: E402
from core.job_validator import JobValidator  # noqa: E402
from sources import ats  # noqa: E402

from conftest import FAKE_SKILLS  # noqa: E402

REGION = 'Testland'

STRONG = {
    'title': 'Sales Engineer, Industrial Valves', 'company': 'Valveco',
    'description': ('Technical sales of control valves. Valve sizing, Kv '
                    'calculation, P&ID review, ATEX areas, fluid systems and '
                    'commissioning support for process plants.'),
    'link': 'https://boards.greenhouse.io/valveco/jobs/1',
    'location': 'Remote, Europe', 'source': 'Greenhouse',
}
# The same job again from an aggregator, under a different link.
STRONG_DUPLICATE = {**STRONG, 'link': 'https://aggregator.example/jobs/77',
                    'source': 'Remotive'}
WRITER = {
    'title': 'Technical Writer', 'company': 'Docsworks',
    'description': ('Technical writing and documentation for industrial '
                    'products. SEO, content strategy and B2B content.'),
    'link': 'https://aggregator.example/jobs/2',
    'location': 'Remote, Europe', 'source': 'Remotive',
}
WEAK = {
    'title': 'Pastry Chef', 'company': 'Bakery',
    'description': 'Bread, cakes and early mornings.',
    'link': 'https://aggregator.example/jobs/3',
    'location': 'Remote, Europe', 'source': 'Remotive',
}
DEALBREAKER = {
    'title': 'Valve Sales Engineer', 'company': 'Defenseco',
    'description': ('Technical sales, valve sizing, commissioning. '
                    'Active security clearance required.'),
    'link': 'https://aggregator.example/jobs/4',
    'location': 'Remote, Europe', 'source': 'Remotive',
}
LOCAL = {
    'title': 'Inženjer prodaje', 'company': 'Lokalna firma',
    'description': 'Kratak opis oglasa.',
    'link': 'https://poslovi.infostud.com/posao/test/1',
    'location': f'Testgrad, {REGION}', 'source': 'Infostud',
}
LOCAL_FULL_ADVERT = ('Tehnička prodaja industrijskih ventila i pumpi. '
                     'Puštanje u rad, hidraulika i rad sa klijentima.')
SAVED = {
    'title': 'Maintenance Planner', 'company': 'Plantco',
    'description': 'Saved by hand.',
    'link': 'https://plantco.example/careers/9',
    'location': 'Onsite', 'source': 'Gmail Draft',
}


class FakeSource:
    """A job board that returns fixed adverts, whatever it is asked."""

    def __init__(self, jobs=(), crash=False):
        self.jobs, self.crash = list(jobs), crash

    def _result(self, *args, **kwargs):
        if self.crash:
            raise RuntimeError('monthly usage hard limit exceeded')
        return [dict(job) for job in self.jobs]

    search_all = search_jobs = process_draft_jobs = _result


class TelegramCapture:
    """Stands in for requests.post and keeps every message instead of sending."""

    def __init__(self, status=200):
        self.status, self.messages = status, []

    def __call__(self, url, data=None, timeout=None):
        self.messages.append(data['text'])
        return type('Response', (), {'status_code': self.status})()


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """A searcher wired to fake boards and a throwaway database."""
    monkeypatch.setattr(Config, 'DATABASE_PATH', str(tmp_path / 'e2e.db'))
    monkeypatch.setattr(Config, 'TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setattr(Config, 'TELEGRAM_CHAT_ID', '1')
    monkeypatch.setattr(Config, 'DIGEST_LABEL', '', raising=False)
    monkeypatch.setattr(Config, 'REGIONAL_DIGEST_LABEL', '', raising=False)
    monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', [REGION.lower()], raising=False)
    monkeypatch.setattr(Config, 'REGIONAL_JOB_LOCATIONS', [], raising=False)
    monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)
    monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['inzenjer'], raising=False)

    # The advert page fetch for the local board, the only detail fetch here.
    monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Infostud', lambda job: LOCAL_FULL_ADVERT)
    # Link liveness is a network check. Every link here is live.
    monkeypatch.setattr(telegram_sender, 'check_link_live', lambda url, timeout=6: True)

    searcher = job_search_smart.SmartJobSearcher.__new__(job_search_smart.SmartJobSearcher)
    searcher.ats = FakeSource([STRONG])
    searcher.free_search = FakeSource([STRONG_DUPLICATE, WRITER, WEAK, DEALBREAKER, LOCAL])
    searcher.jsearch = FakeSource()
    searcher.linkedin = FakeSource()
    searcher.serpapi = FakeSource()
    searcher.apify = FakeSource(crash=True)
    searcher.ddg = FakeSource()
    searcher.gmail = FakeSource([SAVED])
    searcher.filter = job_search_smart.JobFilter()
    searcher.filter.skills_data = FAKE_SKILLS
    searcher.filter.all_skills = {
        s for cv in FAKE_SKILLS['cvs'].values() for s in cv['skills']}
    searcher.validator = JobValidator()
    searcher.db = Database()
    searcher.skills_data = FAKE_SKILLS
    searcher.build_search_queries = lambda: ['sales engineer', 'technical writer valves']
    searcher.extract_top_skills = lambda limit=10: ['technical sales']
    return searcher


def run_day(searcher, monkeypatch, status=200):
    """One scheduled run: search and store, then send. Returns the messages."""
    searcher.search_all_sources()
    telegram = TelegramCapture(status)
    monkeypatch.setattr(telegram_sender.requests, 'post', telegram)
    telegram_sender.main()
    return telegram.messages


class TestOneDay:
    def test_the_right_jobs_reach_the_chat(self, pipeline, monkeypatch):
        chat = '\n'.join(run_day(pipeline, monkeypatch))
        assert 'Sales Engineer, Industrial Valves' in chat   # strong match
        assert 'Technical Writer' in chat                    # second CV
        assert 'Inženjer prodaje' in chat                    # local-language advert
        assert 'Maintenance Planner' in chat                 # saved by hand
        assert 'Pastry Chef' not in chat                     # weak match
        assert 'Defenseco' not in chat                       # dealbreaker

    def test_a_duplicate_is_sent_once(self, pipeline, monkeypatch):
        # Three layers stop a duplicate: the dedup key in the search, the
        # near-duplicate check, and the database's unique key on title and
        # company. Breaking any one or two of them still passes this test,
        # which is the point: it checks the outcome, not a single layer.
        chat = '\n'.join(run_day(pipeline, monkeypatch))
        assert chat.count('Sales Engineer, Industrial Valves') == 1

    def test_each_job_goes_to_its_own_message(self, pipeline, monkeypatch):
        messages = run_day(pipeline, monkeypatch)
        direct = next(m for m in messages if 'Direct company openings' in m)
        regional = next(m for m in messages if 'Regional jobs' in m)
        assert 'Valveco' in direct
        assert 'Lokalna firma' in regional
        assert 'Lokalna firma' not in direct

    def test_a_crashing_source_does_not_stop_the_run(self, pipeline, monkeypatch):
        messages = run_day(pipeline, monkeypatch)
        assert messages, 'the crash in one source ended the whole run'


class TestAcrossDays:
    def test_nothing_is_sent_twice(self, pipeline, monkeypatch):
        first = run_day(pipeline, monkeypatch)
        second = run_day(pipeline, monkeypatch)
        assert first
        assert second == []

    def test_a_failed_send_is_retried_next_day(self, pipeline, monkeypatch):
        failed = run_day(pipeline, monkeypatch, status=500)
        assert failed, 'the run should still have tried to send'
        retried = '\n'.join(run_day(pipeline, monkeypatch))
        assert 'Sales Engineer, Industrial Valves' in retried
