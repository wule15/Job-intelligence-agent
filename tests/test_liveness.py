"""
Is a posting still open when the digest is sent?

The digest draws from a backlog several days old, and three kinds of job went
out after the posting had closed: company-board jobs were exempt from the
check because "a board only lists open roles", which is true when the board is
read and not a week later; the direct and regional messages never checked at
all; and the page check missed boards that answer a closed posting with a
normal page. Greenhouse redirects to "?error=true" with HTTP 200, and an Ashby
page returns 200 even for an invented id.

Now every candidate except a hand-saved one is checked when it is selected.
Company-board jobs ask the vendor's own API (posting_is_open), everything else
gets the page check. A job is dropped only on proof that it is closed: a
timeout, a rate limit or a server error counts as open. The checks are capped
per run, and a job past the cap is sent unverified.

No test here makes a network call. The vendor session, requests.get and the
address guard's DNS lookup are all replaced.
"""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_sender  # noqa: E402
from core.config import Config  # noqa: E402
from core.database import Database  # noqa: E402
from sources import ats  # noqa: E402

ASHBY_OPEN = '11111111-1111-1111-1111-111111111111'
ASHBY_CLOSED = '22222222-2222-2222-2222-222222222222'


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=''):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    """Stands in for the vendor session. `answers` maps a URL fragment to a
    FakeResponse or an exception; every requested URL is recorded."""

    def __init__(self, answers=None, default=None):
        self.answers = answers or {}
        self.default = default or FakeResponse(200)
        self.urls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.urls.append(url)
        for fragment, answer in self.answers.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        if isinstance(self.default, Exception):
            raise self.default
        return self.default


@pytest.fixture
def vendor(monkeypatch, tmp_path):
    """Install a fake vendor session, an empty company list and fresh caches."""
    monkeypatch.setattr(Config, 'CONFIG_DIR', tmp_path, raising=False)
    ats.reset_liveness_caches()

    def install(answers=None, default=None):
        fake = FakeSession(answers, default)
        monkeypatch.setattr(ats, 'detail_session', fake)
        return fake

    yield install
    ats.reset_liveness_caches()


def write_companies(tmp_path, companies):
    (tmp_path / 'companies.json').write_text(
        json.dumps({'companies': companies}), encoding='utf-8')


# ── Greenhouse ────────────────────────────────────────────────────────────────

class TestGreenhouse:
    LINK = 'https://job-boards.greenhouse.io/acme/jobs/123'

    def test_404_from_the_api_means_closed(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open(self.LINK, 'Greenhouse') is False
        assert fake.urls == ['https://boards-api.greenhouse.io/v1/boards/acme/jobs/123']

    def test_410_means_closed(self, vendor):
        vendor(default=FakeResponse(410))
        assert ats.posting_is_open(self.LINK, 'Greenhouse') is False

    def test_200_means_open(self, vendor):
        vendor(default=FakeResponse(200, {'id': 123}))
        assert ats.posting_is_open(self.LINK, 'Greenhouse') is True

    @pytest.mark.parametrize('answer', [
        requests.exceptions.Timeout('slow'),
        requests.exceptions.ConnectionError('down'),
        FakeResponse(429),
        FakeResponse(503),
    ])
    def test_a_failure_is_not_proof(self, vendor, answer):
        vendor(default=answer)
        assert ats.posting_is_open(self.LINK, 'Greenhouse') is True

    def test_the_old_board_host_works_too(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open('https://boards.greenhouse.io/acme/jobs/77', 'Greenhouse') is False
        assert fake.urls[0].endswith('/boards/acme/jobs/77')

    def test_the_id_can_come_from_gh_jid(self, vendor):
        fake = vendor(default=FakeResponse(404))
        link = 'https://job-boards.greenhouse.io/acme?gh_jid=456'
        assert ats.posting_is_open(link, 'Greenhouse') is False
        assert fake.urls[0].endswith('/boards/acme/jobs/456')

    def test_a_custom_domain_finds_the_slug_by_company_name(self, vendor, tmp_path):
        write_companies(tmp_path, [
            {'name': 'Acme Valves', 'ats': 'greenhouse', 'slug': 'acmevalves'},
            {'name': 'Other', 'ats': 'lever', 'slug': 'other'},
        ])
        fake = vendor(default=FakeResponse(404))
        link = 'https://careers.acme.example/open-roles?gh_jid=555'
        assert ats.posting_is_open(link, 'Greenhouse', 'acme valves') is False
        assert fake.urls == ['https://boards-api.greenhouse.io/v1/boards/acmevalves/jobs/555']

    def test_an_unknown_slug_cannot_tell(self, vendor):
        fake = vendor(default=FakeResponse(404))
        link = 'https://careers.unknown.example/jobs/999'
        assert ats.posting_is_open(link, 'Greenhouse', 'Unknown Co') is None
        assert fake.urls == []

    def test_no_id_cannot_tell(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open('https://job-boards.greenhouse.io/acme', 'Greenhouse') is None
        assert fake.urls == []


# ── Ashby ─────────────────────────────────────────────────────────────────────

def ashby_board(*ids):
    return FakeResponse(200, {'jobs': [
        {'id': job_id, 'jobUrl': f'https://jobs.ashbyhq.com/acme/{job_id}'} for job_id in ids]})


class TestAshby:
    def test_missing_from_the_board_means_closed(self, vendor):
        vendor(default=ashby_board(ASHBY_OPEN))
        assert ats.posting_is_open(f'https://jobs.ashbyhq.com/acme/{ASHBY_CLOSED}', 'Ashby') is False

    def test_present_on_the_board_means_open(self, vendor):
        vendor(default=ashby_board(ASHBY_OPEN))
        assert ats.posting_is_open(f'https://jobs.ashbyhq.com/acme/{ASHBY_OPEN}', 'Ashby') is True

    def test_the_board_is_fetched_once_per_run(self, vendor):
        fake = vendor(default=ashby_board(ASHBY_OPEN))
        for job_id in (ASHBY_OPEN, ASHBY_CLOSED, ASHBY_OPEN):
            ats.posting_is_open(f'https://jobs.ashbyhq.com/acme/{job_id}', 'Ashby')
        assert len(fake.urls) == 1
        assert 'api.ashbyhq.com/posting-api/job-board/acme' in fake.urls[0]

    def test_a_board_error_is_not_proof(self, vendor):
        vendor(default=requests.exceptions.Timeout('slow'))
        assert ats.posting_is_open(f'https://jobs.ashbyhq.com/acme/{ASHBY_CLOSED}', 'Ashby') is True

    def test_an_empty_board_cannot_tell(self, vendor):
        """An empty list may be a glitch. Dropping every job on it, and
        deleting the rows, is too much to do on that."""
        vendor(default=FakeResponse(200, {'jobs': []}))
        assert ats.posting_is_open(f'https://jobs.ashbyhq.com/acme/{ASHBY_CLOSED}', 'Ashby') is None


# ── SmartRecruiters, Lever, Workday, SuccessFactors ──────────────────────────

class TestOtherVendors:
    SR = 'https://jobs.smartrecruiters.com/AcmeGroup/744000123'

    def test_smartrecruiters_404_means_closed(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open(self.SR, 'SmartRecruiters') is False
        assert fake.urls == [
            'https://api.smartrecruiters.com/v1/companies/AcmeGroup/postings/744000123']

    def test_smartrecruiters_inactive_means_closed(self, vendor):
        vendor(default=FakeResponse(200, {'active': False}))
        assert ats.posting_is_open(self.SR, 'SmartRecruiters') is False

    def test_smartrecruiters_active_means_open(self, vendor):
        vendor(default=FakeResponse(200, {'active': True}))
        assert ats.posting_is_open(self.SR, 'SmartRecruiters') is True

    def test_smartrecruiters_400_cannot_tell(self, vendor):
        vendor(default=FakeResponse(400))
        assert ats.posting_is_open(self.SR, 'SmartRecruiters') is None

    def test_lever_404_means_closed(self, vendor):
        fake = vendor(default=FakeResponse(404))
        link = 'https://jobs.lever.co/acme/0b1c2d3e-aaaa-bbbb-cccc-111122223333'
        assert ats.posting_is_open(link, 'Lever') is False
        assert fake.urls == [
            'https://api.lever.co/v0/postings/acme/0b1c2d3e-aaaa-bbbb-cccc-111122223333']

    def test_workday_404_on_the_json_endpoint_means_closed(self, vendor):
        fake = vendor(default=FakeResponse(404))
        link = 'https://acme.wd5.myworkdayjobs.com/External/job/City/Sales-Engineer_R1'
        assert ats.posting_is_open(link, 'Workday') is False
        assert fake.urls == [
            'https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/job/City/Sales-Engineer_R1']

    def test_workday_link_off_the_vendor_host_cannot_tell(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open('https://intranet.example/External/job/1', 'Workday') is None
        assert fake.urls == []

    def test_successfactors_cannot_tell(self, vendor):
        fake = vendor(default=FakeResponse(404))
        assert ats.posting_is_open('https://jobs.acme.example/job/1', 'SuccessFactors') is None
        assert fake.urls == []


# ── The page check ────────────────────────────────────────────────────────────

class TestPageCheck:
    @pytest.fixture(autouse=True)
    def offline(self, monkeypatch):
        telegram_sender.check_link_live.cache_clear()
        monkeypatch.setattr(telegram_sender, '_is_internal_url', lambda url: False)
        yield
        telegram_sender.check_link_live.cache_clear()

    def test_greenhouse_error_redirect_means_closed(self, monkeypatch):
        """The verified soft 404: a closed Greenhouse job redirects to the
        board with ?error=true and answers 200."""
        def fake_get(url, **kwargs):
            if 'error=true' in url:
                return FakeResponse(200, text='<html>All open roles</html>')
            return FakeResponse(302, headers={
                'Location': 'https://job-boards.greenhouse.io/acme?error=true'})
        monkeypatch.setattr(telegram_sender.requests, 'get', fake_get)
        assert telegram_sender.check_link_live(
            'https://job-boards.greenhouse.io/acme/jobs/6210353004') is False

    def test_an_ordinary_page_is_live(self, monkeypatch):
        monkeypatch.setattr(telegram_sender.requests, 'get',
                            lambda url, **kwargs: FakeResponse(200, text='<html>Apply now</html>'))
        assert telegram_sender.check_link_live('https://jobs.example.com/role/1') is True

    def test_an_error_redirect_off_greenhouse_is_not_proof(self, monkeypatch):
        """On another site ?error=true can be a login or a bot check."""
        def fake_get(url, **kwargs):
            if 'error=true' in url:
                return FakeResponse(200, text='<html>Please sign in</html>')
            return FakeResponse(302, headers={'Location': 'https://careers.example.com/?error=true'})
        monkeypatch.setattr(telegram_sender.requests, 'get', fake_get)
        assert telegram_sender.check_link_live('https://careers.example.com/jobs/1') is True


# ── Routing and the budget ───────────────────────────────────────────────────

class Recorder:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.result


@pytest.fixture
def routes(monkeypatch):
    """Replace both checks with recorders and start a fresh budget."""
    vendor_check, page_check = Recorder(True), Recorder(True)
    monkeypatch.setattr(telegram_sender, 'posting_is_open', vendor_check)
    monkeypatch.setattr(telegram_sender, 'check_link_live', page_check)
    telegram_sender.reset_liveness()
    yield vendor_check, page_check
    telegram_sender.reset_liveness()


class TestIsJobLive:
    def test_company_boards_ask_the_vendor(self, routes):
        vendor_check, page_check = routes
        vendor_check.result = False
        assert telegram_sender.is_job_live('https://jobs.lever.co/a/1', 'Lever', 'A') is False
        assert vendor_check.calls == [('https://jobs.lever.co/a/1', 'Lever', 'A')]
        assert page_check.calls == []

    def test_a_vendor_that_cannot_tell_falls_back_to_the_page(self, routes):
        vendor_check, page_check = routes
        vendor_check.result = None
        page_check.result = False
        assert telegram_sender.is_job_live('https://jobs.acme.example/1', 'SuccessFactors') is False
        assert len(page_check.calls) == 1

    def test_aggregators_get_the_page_check(self, routes):
        vendor_check, page_check = routes
        assert telegram_sender.is_job_live('https://agg.example/1', 'Remotive') is True
        assert vendor_check.calls == [] and len(page_check.calls) == 1

    def test_hand_saved_jobs_are_never_checked(self, routes):
        vendor_check, page_check = routes
        vendor_check.result = page_check.result = False
        assert telegram_sender.is_job_live('https://mine.example/1', 'Gmail Draft') is True
        assert vendor_check.calls == [] and page_check.calls == []

    def test_a_crashing_check_counts_as_open(self, routes, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError('check bug')
        monkeypatch.setattr(telegram_sender, 'check_link_live', boom)
        assert telegram_sender.is_job_live('https://agg.example/1', 'Remotive') is True

    def test_only_the_vendor_proves_a_closure_worth_deleting(self, routes):
        vendor_check, page_check = routes
        vendor_check.result = False
        page_check.result = False
        telegram_sender.is_job_live('https://jobs.lever.co/a/1', 'Lever')
        telegram_sender.is_job_live('https://agg.example/1', 'Remotive')
        summary = telegram_sender.liveness_summary()
        assert summary['closed'] == ['https://jobs.lever.co/a/1']
        assert summary['closed_page'] == ['https://agg.example/1']

    def test_a_link_is_checked_once_per_run(self, routes):
        vendor_check, _ = routes
        vendor_check.result = False
        for _ in range(3):
            assert telegram_sender.is_job_live('https://jobs.lever.co/a/1', 'Lever') is False
        assert len(vendor_check.calls) == 1
        assert telegram_sender.liveness_summary()['closed'] == ['https://jobs.lever.co/a/1']


class TestBudget:
    def test_checks_past_the_cap_are_sent_unverified(self, routes, monkeypatch):
        vendor_check, _ = routes
        vendor_check.result = False
        monkeypatch.setattr(telegram_sender, 'LIVENESS_MAX_CHECKS', 2)
        results = [telegram_sender.is_job_live(f'https://jobs.lever.co/a/{i}', 'Lever')
                   for i in range(5)]
        assert results == [False, False, True, True, True]
        assert len(vendor_check.calls) == 2
        summary = telegram_sender.liveness_summary()
        assert summary['checked'] == 2 and summary['unverified'] == 3

    def test_cached_answers_do_not_spend_the_budget(self, routes, monkeypatch):
        vendor_check, _ = routes
        monkeypatch.setattr(telegram_sender, 'LIVENESS_MAX_CHECKS', 1)
        for _ in range(3):
            telegram_sender.is_job_live('https://jobs.lever.co/a/1', 'Lever')
        assert telegram_sender.liveness_summary()['unverified'] == 0

    def test_checks_past_the_time_budget_are_sent_unverified(self, routes, monkeypatch):
        vendor_check, _ = routes
        vendor_check.result = False

        class LateClock:
            @staticmethod
            def monotonic():
                return 10 ** 9
        monkeypatch.setattr(telegram_sender, 'time', LateClock)
        assert telegram_sender.is_job_live('https://jobs.lever.co/a/1', 'Lever') is True
        assert vendor_check.calls == []
        assert telegram_sender.liveness_summary()['unverified'] == 1


# ── The three digests ────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'liveness.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
    monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['testland'], raising=False)
    monkeypatch.setattr(Config, 'REGIONAL_JOB_LOCATIONS', [], raising=False)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


def add(db, title, company, score, source='Greenhouse', link=None):
    link = link or f'https://example.com/{company}/{title.replace(" ", "-")}'
    job_id = db.add_job(title, company, 'desc', link, source=source,
                        relevance_score=score, location='Testcity, Testland')
    db.mark_job_link_seen(link, job_id)
    return job_id


def link_of(title, company):
    return f'https://example.com/{company}/{title.replace(" ", "-")}'


def boom(*args, **kwargs):
    raise AssertionError('the liveness check ran when it should not have')


@pytest.mark.parametrize('pick', [telegram_sender.get_direct_jobs,
                                  telegram_sender.get_regional_jobs],
                         ids=['direct', 'regional'])
class TestDirectAndRegional:
    def test_a_closed_job_gives_its_slot_to_the_next(self, db, pick):
        add(db, 'Closed Role', 'Closedco', 90.0)
        open_id = add(db, 'Open Role', 'Openco', 70.0)
        closed = link_of('Closed Role', 'Closedco')
        chosen = pick(limit=1, is_live=lambda link, source, company: link != closed)
        assert [job[0] for job in chosen] == [open_id]

    def test_no_check_means_no_network(self, db, pick):
        add(db, 'Open Role', 'Openco', 70.0)
        assert pick()  # is_live defaults to None and is never called
        with pytest.raises(AssertionError):
            pick(is_live=boom)


class TestMainDigest:
    def test_a_closed_company_board_job_is_now_dropped(self, db):
        add(db, 'Board Role', 'Flowserve', 70.0, source='Workday')
        jobs = telegram_sender.get_unsent_jobs(limit=10, is_live=lambda *args: False)
        assert not any(job[1] == 'Board Role' for job in jobs)

    def test_a_hand_saved_job_still_skips_the_check(self, db):
        add(db, 'Saved Role', 'Mine', 1.0, source='Gmail Draft')
        jobs = telegram_sender.get_unsent_jobs(limit=10, is_live=boom)
        assert [job[1] for job in jobs] == ['Saved Role']


# ── A whole run ──────────────────────────────────────────────────────────────

class TelegramCapture:
    def __init__(self):
        self.messages = []

    def __call__(self, url, data=None, timeout=None):
        self.messages.append(data['text'])
        return type('Response', (), {'status_code': 200})()


def rows(sql, *params):
    with closing(sqlite3.connect(Config.DATABASE_PATH)) as conn:
        return conn.execute(sql, params).fetchall()


class TestRun:
    @pytest.fixture
    def sending(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'TELEGRAM_BOT_TOKEN', 'test-token')
        monkeypatch.setattr(Config, 'TELEGRAM_CHAT_ID', '1')
        monkeypatch.setattr(telegram_sender, 'check_link_live', lambda url, timeout=6: True)
        telegram = TelegramCapture()
        monkeypatch.setattr(telegram_sender.requests, 'post', telegram)
        return telegram

    def test_closed_rows_are_deleted_and_logged(self, db, sending, monkeypatch, capsys):
        closed_id = add(db, 'Closed Role', 'Closedco', 90.0)
        add(db, 'Open Role', 'Openco', 70.0)
        closed = link_of('Closed Role', 'Closedco')
        monkeypatch.setattr(telegram_sender, 'posting_is_open',
                            lambda link, source, company='': link != closed)

        telegram_sender.main()

        chat = '\n'.join(sending.messages)
        assert 'Open Role' in chat and 'Closed Role' not in chat
        assert rows('SELECT id FROM jobs WHERE id = ?', closed_id) == []
        out = capsys.readouterr().out
        assert f'[closed] id={closed_id} | Closed Role | Closedco | {closed}' in out
        # Still known as seen, so the next search does not store it again.
        assert db.is_job_link_seen(closed)

    def test_a_sent_row_is_never_deleted(self, db):
        sent_id = add(db, 'Sent Role', 'Sentco', 80.0)
        unsent_id = add(db, 'Unsent Role', 'Unsentco', 80.0)
        telegram_sender.mark_jobs_sent([sent_id])
        deleted = telegram_sender.drop_closed_postings(
            [link_of('Sent Role', 'Sentco'), link_of('Unsent Role', 'Unsentco')])
        assert deleted == 1
        remaining = {row[0] for row in rows('SELECT id FROM jobs')}
        assert sent_id in remaining and unsent_id not in remaining

    def test_a_row_with_a_cover_letter_is_never_deleted(self, db):
        kept_id = add(db, 'Applied Role', 'Appliedco', 80.0)
        with closing(sqlite3.connect(Config.DATABASE_PATH)) as conn:
            conn.execute('INSERT INTO cover_letters_sent (job_id, job_title, company) '
                         'VALUES (?, ?, ?)', (kept_id, 'Applied Role', 'Appliedco'))
            conn.commit()
        assert telegram_sender.drop_closed_postings([link_of('Applied Role', 'Appliedco')]) == 0
        assert kept_id in {row[0] for row in rows('SELECT id FROM jobs')}

    def test_a_page_that_reads_closed_is_skipped_not_deleted(self, db, sending, monkeypatch,
                                                             capsys):
        """The page check reads loose markers, so one closed answer only
        skips the job for the day. The vendor's API is proof enough to delete."""
        page_id = add(db, 'Page Role', 'Pageco', 90.0, source='Remotive')
        add(db, 'Open Role', 'Openco', 70.0, source='Remotive')
        page_link = link_of('Page Role', 'Pageco')
        monkeypatch.setattr(telegram_sender, 'check_link_live',
                            lambda url, timeout=6: url != page_link)

        telegram_sender.main()

        chat = '\n'.join(sending.messages)
        assert 'Open Role' in chat and 'Page Role' not in chat
        assert rows('SELECT id FROM jobs WHERE id = ?', page_id) == [(page_id,)]
        assert f'[closed-page, skipped today] {page_link}' in capsys.readouterr().out

    def test_nothing_closed_deletes_nothing(self, db):
        add(db, 'Open Role', 'Openco', 80.0)
        assert telegram_sender.drop_closed_postings([]) == 0
        assert len(rows('SELECT id FROM jobs')) == 1
