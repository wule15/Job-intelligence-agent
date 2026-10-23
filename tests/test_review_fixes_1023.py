"""
Fixes from the 23 October system review: a refused Telegram send now fails
the run, API keys stay out of logs, JSearch spends one request per query,
Serbia and Bosnia no longer take the non-Europe downrank, and the direct and
regional messages need a target-role title.
"""

import logging

import pytest
import requests

import telegram_sender
from core import source_health
from core.config import Config
from core.job_filter import is_pure_dev_title, is_target_role_title, region_preference_multiplier
from sources import free_boards
from sources.jsearch import JSearchJobScraper as JSearchAPI


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = ''

    def json(self):
        return self._payload


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(Config, 'TELEGRAM_BOT_TOKEN', 'TOKEN')
    monkeypatch.setattr(Config, 'TELEGRAM_CHAT_ID', '1')
    monkeypatch.setattr(telegram_sender.time, 'sleep', lambda s: None)
    calls = []

    def post_with(*responses):
        queue = list(responses)

        def post(url, data=None, timeout=None):
            calls.append(url)
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(telegram_sender.requests, 'post', post)
        return calls
    return post_with


# ── Telegram send ─────────────────────────────────────────────────────────────

def test_429_is_retried_once_then_succeeds(telegram):
    calls = telegram(_Resp(429, {'parameters': {'retry_after': 3}}), _Resp(200))
    assert telegram_sender.send_telegram_message('hi') is True
    assert len(calls) == 2


def test_server_error_retried_once_then_fails(telegram):
    calls = telegram(_Resp(502), _Resp(502))
    assert telegram_sender.send_telegram_message('hi') is False
    assert len(calls) == 2


def test_bad_request_not_retried(telegram):
    calls = telegram(_Resp(400))
    assert telegram_sender.send_telegram_message('hi') is False
    assert len(calls) == 1


def test_read_timeout_not_retried_because_it_may_have_arrived(telegram):
    calls = telegram(requests.exceptions.ReadTimeout('slow'))
    assert telegram_sender.send_telegram_message('hi') is False
    assert len(calls) == 1


def test_connection_error_retried(telegram):
    calls = telegram(requests.exceptions.ConnectionError('down'), _Resp(200))
    assert telegram_sender.send_telegram_message('hi') is True
    assert len(calls) == 2


def _quiet_main(monkeypatch, send_ok):
    """main() with every database and network step stubbed out."""
    for name, value in {
        'init_telegram_tracking': lambda: None, 'load_exclusions': lambda: [],
        'suppress_repeats': lambda ex: 0, 'suppress_ineligible': lambda: 0,
        'reset_liveness': lambda: None, '_report_liveness': lambda: None,
        'get_direct_jobs': lambda **kw: [], 'get_regional_jobs': lambda **kw: [],
        'get_unsent_jobs': lambda **kw: [(1, 'Sales Engineer', 'Acme', 50, 'https://x', '', 'Workday', 0)],
        'format_job_digest': lambda jobs: ('digest', [1]), '_health_note': lambda: '',
        '_regional_configured': lambda: False, 'mark_jobs_sent': lambda ids: True,
        'send_telegram_message': lambda message: send_ok,
    }.items():
        monkeypatch.setattr(telegram_sender, name, value)


def test_main_returns_1_when_the_digest_is_refused(monkeypatch):
    _quiet_main(monkeypatch, send_ok=False)
    assert telegram_sender.main() == 1


def test_main_returns_0_when_everything_sends(monkeypatch):
    _quiet_main(monkeypatch, send_ok=True)
    assert telegram_sender.main() == 0


# ── Keys stay out of logs ─────────────────────────────────────────────────────

def test_free_board_connection_error_does_not_log_the_key(monkeypatch, caplog):
    free_boards.reset_refused_hosts()

    def fail(url, **kwargs):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool: Max retries exceeded with url: /v1/api/jobs/gb/search/1?app_id=ID123&app_key=SECRET456")
    monkeypatch.setattr(free_boards.requests, 'get', fail)
    with caplog.at_level(logging.WARNING):
        assert free_boards._get('https://api.adzuna.com/v1/api/jobs/gb/search/1') is None
    assert 'SECRET456' not in caplog.text and 'ID123' not in caplog.text


def test_health_line_hides_keys_and_schemeless_paths():
    error = ("ConnectionError: Max retries exceeded with url: "
             "/search.json?engine=google_jobs&api_key=SECRETKEY")
    short = source_health._short_error(error)
    assert 'SECRETKEY' not in short
    assert source_health._short_error('bad token=abc&x=1').startswith('bad token=<hidden>')


# ── JSearch spends one request per query ─────────────────────────────────────

def test_jsearch_one_page_is_one_request(monkeypatch):
    calls = []
    monkeypatch.setattr('sources.jsearch.requests.get',
                        lambda *a, **k: calls.append(1) or _Resp(200, {'data': []}))
    api = JSearchAPI()
    api.api_key = 'KEY'
    api.search_jobs('sales engineer', num_pages=1)
    assert len(calls) == 1


def test_jsearch_stops_when_quota_header_reaches_zero(monkeypatch):
    calls = []
    monkeypatch.setattr('sources.jsearch.requests.get',
                        lambda *a, **k: calls.append(1) or _Resp(
                            200, {'data': []}, {'X-RateLimit-Requests-Remaining': '0'}))
    api = JSearchAPI()
    api.api_key = 'KEY'
    api.search_jobs('a', num_pages=1)
    api.search_jobs('b', num_pages=1)
    assert len(calls) == 1 and api.quota_exhausted


# ── Ranking ───────────────────────────────────────────────────────────────────

def test_allowed_non_eu_countries_are_not_downranked(monkeypatch):
    monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', ['RS', 'BA', 'EU', 'GB', 'CH'])
    monkeypatch.setattr(Config, 'NON_EUROPE_PREFERENCE', 0.85)
    for place in ('Indjija, Serbia', 'Sarajevo, Bosnia and Herzegovina', 'London, United Kingdom'):
        assert region_preference_multiplier({'title': 'Sales Engineer', 'location': place}) == 1.0
    assert region_preference_multiplier({'title': 'Sales Engineer', 'location': 'Austin, TX, United States'}) == 0.85


@pytest.mark.parametrize('title,wanted', [
    ('Internal Sales Engineer', True),
    ('Inženjer prodaje', True),
    ('Junior inženjer za održavanje sistema upravljanja', True),
    ('Inżynier Budowy Branży Elektrycznej', True),
    ('Application Design Engineer - Data Center Solutions', True),
    ('Group Finance Applications Analyst', False),
    ('Produktionsmitarbeiter (m/w/d) - befristet', False),
    ('SAP Dangerous Good Management Consultant', False),
    ('Modelar u tekstilnoj konfekciji', False),
    ('UX/Product Designer', False),
])
def test_target_role_titles(title, wanted):
    assert is_target_role_title(title) is wanted


def test_local_screen_terms_count_as_roles(monkeypatch):
    monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['komercijal'])
    assert is_target_role_title('Komercijalista za industrijsku opremu')


def test_mobile_stack_titles_are_pure_development():
    assert is_pure_dev_title('Mobile Engineer - React Native (Paris or Remote France)')
    assert is_pure_dev_title('Flutter Developer')
    assert not is_pure_dev_title('Mobile Engineer')
