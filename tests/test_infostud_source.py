"""
Tests for the Infostud source parser.

The network fetch is separated from the parsing so the parsing is tested
offline against the shape of Infostud's Next.js __NEXT_DATA__ payload.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.free_boards import (  # noqa: E402
    _extract_next_data, _parse_infostud_jobs,
)


def _payload(primary):
    return {'props': {'pageProps': {'initialSearchResults': {'jobs': {'primary': primary}}}}}


class TestParseInfostudJobs:
    def test_extracts_and_stamps_serbia(self):
        data = _payload([
            {'title': 'Process Lead', 'companyName': 'BAT Vranje',
             'location': 'Vranje', 'url': 'https://poslovi.infostud.com/posao/x/y/1',
             'salary': None, 'jobSummary': 'Lead the process line.'},
        ])
        jobs = _parse_infostud_jobs(data)
        assert len(jobs) == 1
        j = jobs[0]
        assert j['title'] == 'Process Lead'
        assert j['company'] == 'BAT Vranje'
        assert j['location'] == 'Vranje, Serbia'   # city stamped with country
        assert j['source'] == 'Infostud'
        assert j['link'].endswith('/1')

    def test_location_defaults_to_serbia_when_city_missing(self):
        jobs = _parse_infostud_jobs(_payload([
            {'title': 'QA Engineer', 'companyName': 'Acme', 'url': 'u'},
        ]))
        assert jobs[0]['location'] == 'Serbia'

    def test_titleless_rows_are_dropped(self):
        jobs = _parse_infostud_jobs(_payload([
            {'title': '', 'companyName': 'X'},
            {'companyName': 'Y'},
        ]))
        assert jobs == []

    def test_bad_shapes_return_empty(self):
        assert _parse_infostud_jobs({}) == []
        assert _parse_infostud_jobs({'props': {}}) == []
        assert _parse_infostud_jobs({'props': {'pageProps': {'initialSearchResults': {}}}}) == []


class TestExtractNextData:
    def test_reads_next_data_script(self):
        html = ('<html><body>'
                '<script id="__NEXT_DATA__" type="application/json">{"a": 1}</script>'
                '</body></html>')
        assert _extract_next_data(html) == {'a': 1}

    def test_missing_or_bad_json_returns_none(self):
        assert _extract_next_data('<html>no script</html>') is None
        assert _extract_next_data(
            '<script id="__NEXT_DATA__">{bad json}</script>') is None


class TestInfostudCityTargeting:
    """Infostud filters a search by city when the city is added as a path
    segment, /oglasi-za-posao-<query>/<city>. The fetch is stubbed, no network."""

    def _capture(self, monkeypatch):
        import sources.free_boards as fb
        seen = []

        class _Resp:
            status_code = 200
            text = ''

        def fake_get(url, **kwargs):
            seen.append(url)
            return _Resp()

        monkeypatch.setattr(fb.requests, 'get', fake_get)
        monkeypatch.setattr(fb.time, 'sleep', lambda *_: None)
        return fb, seen

    def test_no_city_searches_nationwide(self, monkeypatch):
        fb, seen = self._capture(monkeypatch)
        fb.search_infostud('primer upita')
        assert seen == ['https://poslovi.infostud.com/oglasi-za-posao-primer-upita']

    def test_city_is_added_as_a_path_segment(self, monkeypatch):
        fb, seen = self._capture(monkeypatch)
        fb.search_infostud('primer upita', city='Test Grad')
        assert seen == [
            'https://poslovi.infostud.com/oglasi-za-posao-primer-upita/test-grad']

    def test_regional_pass_runs_each_query_in_each_city(self, monkeypatch):
        import sources.free_boards as fb
        calls = []
        monkeypatch.setattr(fb, 'search_infostud',
                            lambda q, city=None, pages=1: calls.append((q, city)) or [])
        monkeypatch.setattr(fb.time, 'sleep', lambda *_: None)
        jobs = fb._run_regional_boards(['infostud'], ['test query'],
                                       infostud_cities=['grad-a', 'grad-b'])
        assert jobs == []
        assert calls == [('test query', 'grad-a'), ('test query', 'grad-b')]

    def test_regional_pass_without_cities_is_unchanged(self, monkeypatch):
        import sources.free_boards as fb
        calls = []
        monkeypatch.setattr(fb, 'search_infostud',
                            lambda q, city=None, pages=1: calls.append((q, city)) or [])
        monkeypatch.setattr(fb.time, 'sleep', lambda *_: None)
        fb._run_regional_boards(['infostud'], ['test query'], infostud_cities=[])
        assert calls == [('test query', None)]


class TestInfostudFullDescription:
    """Infostud search results carry a 270 character teaser, not the advert.
    Scored on that, almost every Infostud job fell under the cutoff and none
    reached the digest. The full text sits in the advert page's own data."""

    def test_detail_parser_reads_the_full_advert(self):
        from sources.free_boards import _parse_infostud_detail
        data = {'props': {'pageProps': {'job': {
            'textAd': '<h3>Sales Manager</h3><p>Prodaja gra&#273;evinske '
                      'mehanizacije, CRM, technical sales.</p>'}}}}
        text = _parse_infostud_detail(data)
        assert 'Sales Manager' in text
        assert 'technical sales' in text
        assert '<' not in text

    def test_detail_parser_bad_shape_returns_empty(self):
        from sources.free_boards import _parse_infostud_detail
        assert _parse_infostud_detail({}) == ''
        assert _parse_infostud_detail(None) == ''
        assert _parse_infostud_detail(
            {'props': {'pageProps': {'job': {'textAd': None}}}}) == ''

    def test_enrichment_replaces_the_teaser(self, monkeypatch):
        from sources import ats
        full = 'Full advert text about valve sizing and technical sales.'
        monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Infostud', lambda job: full)
        job = {'source': 'Infostud', 'title': 'Sales Manager',
               'link': 'https://poslovi.infostud.com/posao/x/y/1',
               'description': 'Short teaser.'}
        enriched, _ = ats.enrich_descriptions([job], should_fetch=lambda j: True)
        assert enriched == 1
        assert job['description'] == full

    def test_empty_detail_keeps_the_teaser(self, monkeypatch):
        """Some adverts are an image with no text. Keep what we had."""
        from sources import ats
        monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Infostud', lambda job: '')
        job = {'source': 'Infostud', 'title': 'T', 'link': 'u',
               'description': 'Short teaser.'}
        ats.enrich_descriptions([job], should_fetch=lambda j: True)
        assert job['description'] == 'Short teaser.'

    def test_other_sources_with_text_are_not_refetched(self, monkeypatch):
        from sources import ats
        calls = []
        monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Workday',
                            lambda job: calls.append(job) or 'x')
        job = {'source': 'Workday', 'title': 'T', 'link': 'u',
               'description': 'Already has a description.'}
        ats.enrich_descriptions([job], should_fetch=lambda j: True)
        assert calls == []


class TestLocalBoardHasItsOwnFetchBudget:
    """Live runs had 121 to 148 jobs waiting for a description, against a
    budget of 60 fetches. The local board's jobs came last, so they kept their
    teaser text and failed the score, and the regional digest fell from 15
    jobs to 2. The teaser-only board now has its own budget, fetched first."""

    @staticmethod
    def _jobs(source, count, description=''):
        return [{'source': source, 'title': f'{source} job {i}',
                 'link': f'https://example.com/{source}/{i}', 'description': description}
                for i in range(count)]

    @pytest.fixture
    def fetched(self, monkeypatch):
        from sources import ats
        calls = []
        for source in ('Infostud', 'Workday'):
            monkeypatch.setitem(ats.DETAIL_FETCHERS, source,
                                lambda job, s=source: calls.append(s) or 'Full advert.')
        return calls

    def test_local_adverts_are_fetched_when_the_shared_budget_is_spent(self, fetched):
        from sources import ats
        jobs = self._jobs('Workday', 70) + self._jobs('Infostud', 5, 'Short teaser.')
        enriched, over_budget = ats.enrich_descriptions(jobs, should_fetch=lambda j: True)
        assert fetched.count('Infostud') == 5
        assert fetched.count('Workday') == 60
        assert (enriched, over_budget) == (65, 10)
        assert all(job['description'] == 'Full advert.' for job in jobs[-5:])

    def test_the_local_budget_has_its_own_cap(self, fetched, monkeypatch):
        from sources import ats
        monkeypatch.setattr(ats, 'SNIPPET_MAX_FETCHES', 3)
        jobs = self._jobs('Infostud', 5, 'Short teaser.') + self._jobs('Workday', 2)
        enriched, over_budget = ats.enrich_descriptions(jobs, should_fetch=lambda j: True)
        assert fetched.count('Infostud') == 3
        assert fetched.count('Workday') == 2
        assert (enriched, over_budget) == (5, 2)

    def test_each_budget_stops_on_its_own_clock(self, fetched, monkeypatch):
        """Each fetch takes 20 seconds on this clock. The local pass stops at
        its own time limit, and the other boards still get theirs, so the whole
        pass ends within the two limits added together."""
        from sources import ats
        clock = {'now': 0.0}
        monkeypatch.setattr(ats.time, 'monotonic', lambda: clock['now'])

        def slow(job, s):
            clock['now'] += 20
            fetched.append(s)
            return 'Full advert.'
        for source in ('Infostud', 'Workday'):
            monkeypatch.setitem(ats.DETAIL_FETCHERS, source,
                                lambda job, s=source: slow(job, s))
        monkeypatch.setattr(ats, 'SNIPPET_BUDGET_SECS', 60)
        monkeypatch.setattr(ats, 'DETAIL_BUDGET_SECS', 90)
        jobs = self._jobs('Infostud', 10, 'Short teaser.') + self._jobs('Workday', 10)
        ats.enrich_descriptions(jobs, should_fetch=lambda j: True)
        assert fetched.count('Infostud') == 4   # at 0, 20, 40 and 60 seconds
        assert fetched.count('Workday') == 5    # at 80 to 160 seconds
        assert clock['now'] <= 60 + 90 + 2 * 20
