"""
Tests for the SerpApi Google-Jobs market routing and budget.

These are offline: the network call (search_google_jobs) is monkeypatched, so
the tests exercise the routing, budget cap, breadth-first ordering and dedup
without spending any SerpApi quota.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sources.serpapi as serp  # noqa: E402
from sources.serpapi import SerpAPIJobSearcher, resolve_markets, MARKETS  # noqa: E402


def _job(title, company='Acme'):
    return {'title': title, 'company': company, 'description': '', 'link': '',
            'salary': None, 'location': 'x', 'source': 'Google Jobs'}


class TestResolveMarkets:
    def test_maps_known_codes(self):
        markets = resolve_markets(['de', 'FR'])
        assert markets == [MARKETS['de'], MARKETS['fr']]

    def test_skips_unsupported_codes(self):
        # rs/ba/se are markets SerpApi's gl allowlist rejects, so they are not
        # in MARKETS and must be dropped, not raise.
        assert resolve_markets(['rs', 'ba', 'se']) == []

    def test_empty_when_none(self):
        assert resolve_markets(None) == []


class TestBudgetAndRouting:
    def _record(self, monkeypatch):
        calls = []

        def fake(query, market=None, num_results=10):
            calls.append((query, market['location'] if market else None))
            return [_job(f"{query}-{market['gl'] if market else 'remote'}")]

        monkeypatch.setattr(serp, 'search_google_jobs', fake)
        monkeypatch.setenv('SERPAPI_KEY', 'test-key')
        monkeypatch.setattr(serp.time, 'sleep', lambda *_: None)
        return calls

    def test_budget_caps_total_searches(self, monkeypatch):
        calls = self._record(monkeypatch)
        SerpAPIJobSearcher().search_all(
            ['q1', 'q2', 'q3'], countries=['de', 'at', 'fr'], budget=4)
        assert len(calls) == 4

    def test_breadth_first_across_markets(self, monkeypatch):
        calls = self._record(monkeypatch)
        SerpAPIJobSearcher().search_all(
            ['q1', 'q2'], countries=['de', 'at', 'fr'], budget=3)
        # With budget 3 and 3 markets, the top query hits every market first.
        assert calls == [('q1', 'Germany'), ('q1', 'Austria'), ('q1', 'France')]

    def test_no_countries_runs_remote_pass(self, monkeypatch):
        calls = self._record(monkeypatch)
        SerpAPIJobSearcher().search_all(['q1', 'q2'], countries=[], budget=6)
        assert calls == [('q1', None), ('q2', None)]

    def test_remote_code_adds_a_remote_target_first(self, monkeypatch):
        calls = self._record(monkeypatch)
        # "remote" is a fully-remote pass (market None), and it goes first so the
        # budget never drops it.
        SerpAPIJobSearcher().search_all(
            ['q1'], countries=['remote', 'de', 'at'], budget=3)
        assert calls == [('q1', None), ('q1', 'Germany'), ('q1', 'Austria')]

    def test_remote_only(self, monkeypatch):
        calls = self._record(monkeypatch)
        SerpAPIJobSearcher().search_all(['q1', 'q2'], countries=['remote'], budget=6)
        assert calls == [('q1', None), ('q2', None)]

    def test_deduplicates_across_markets(self, monkeypatch):
        monkeypatch.setenv('SERPAPI_KEY', 'test-key')
        monkeypatch.setattr(serp.time, 'sleep', lambda *_: None)
        monkeypatch.setattr(serp, 'search_google_jobs',
                            lambda q, market=None, num_results=10: [_job('Same Role')])
        jobs = SerpAPIJobSearcher().search_all(
            ['q1'], countries=['de', 'at'], budget=6)
        assert len(jobs) == 1

    def test_stops_when_no_key_and_no_jobs(self, monkeypatch):
        calls = []

        def fake(query, market=None, num_results=10):
            calls.append(query)
            return []

        monkeypatch.delenv('SERPAPI_KEY', raising=False)
        monkeypatch.setattr(serp, 'search_google_jobs', fake)
        monkeypatch.setattr(serp.time, 'sleep', lambda *_: None)
        SerpAPIJobSearcher().search_all(['q1', 'q2', 'q3'], countries=['de'], budget=6)
        # No key and first call returns nothing, so it breaks after one attempt.
        assert len(calls) == 1
