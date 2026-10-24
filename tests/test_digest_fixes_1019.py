"""
Fixes from the cloud runs of 17 to 19 October: a board that refuses once is
parked for the rest of the run, Arbeitnow is asked a capped number of
queries, and the title-only fetch budget is larger.
"""

import pytest

from sources import ats, free_boards


class _Response:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _fresh_hosts():
    free_boards.reset_refused_hosts()
    yield
    free_boards.reset_refused_hosts()


def test_rate_limited_host_is_asked_only_once(monkeypatch):
    calls = []
    monkeypatch.setattr(free_boards.requests, 'get',
                        lambda url, **kw: calls.append(url) or _Response(429))
    for _ in range(5):
        assert free_boards._get('https://www.arbeitnow.com/api/job-board-api') is None
    assert len(calls) == 1
    assert free_boards.host_refused('https://www.arbeitnow.com/anything')


def test_other_hosts_still_asked(monkeypatch):
    monkeypatch.setattr(free_boards.requests, 'get', lambda url, **kw: _Response(429))
    free_boards._get('https://www.arbeitnow.com/api/job-board-api')
    monkeypatch.setattr(free_boards.requests, 'get', lambda url, **kw: _Response(200, {'ok': 1}))
    assert free_boards._get('https://remotive.com/api/remote-jobs') == {'ok': 1}


def test_server_error_does_not_park_the_host(monkeypatch):
    calls = []
    monkeypatch.setattr(free_boards.requests, 'get',
                        lambda url, **kw: calls.append(url) or _Response(500))
    free_boards._get('https://jobicy.com/api/v2/remote-jobs')
    free_boards._get('https://jobicy.com/api/v2/remote-jobs')
    assert len(calls) == 2


def test_bundesagentur_stops_after_first_refusal(monkeypatch):
    calls = []
    monkeypatch.setattr(free_boards.requests, 'get',
                        lambda url, **kw: calls.append(url) or _Response(403))
    for term in ('Vertriebsingenieur', 'Anwendungstechniker', 'Verfahrensingenieur'):
        assert free_boards.search_bundesagentur(term) == []
    assert len(calls) == 1


def test_title_only_budget_covers_a_normal_run():
    # Live runs had 136 to 144 title-only jobs pass the screen.
    assert ats.DETAIL_MAX_FETCHES >= 145
    assert ats.DETAIL_BUDGET_SECS >= ats.DETAIL_MAX_FETCHES * ats.LINKEDIN_DETAIL_DELAY
