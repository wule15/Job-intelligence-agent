"""
Connector tests. The gap the README names as the largest in the project.

The problem these solve
-----------------------
Ten sources make live network calls and, until now, none were covered. Two
bugs were found there by hand in one afternoon, and both had been live for
months:

  Jobicy sent a geo value the API rejects, so every call returned 400 and
  that source contributed nothing on every run.

  The Muse fetched the unfiltered feed and discarded 99 percent of it in
  Python, so it yielded roughly one job per hundred fetched.

Neither crashed. Neither logged an error. A source returning almost nothing
is indistinguishable from a quiet market, which is why nobody noticed.

How these tests work
--------------------
Not by mocking ten third-party APIs, which the README correctly calls a
larger job than this project justifies. Instead one real response per source
was captured, trimmed to two jobs, and saved in tests/fixtures. The tests
feed that fixture through the parser and assert the job record that comes
out.

That covers the half of a connector that actually breaks silently: the
mapping from somebody else's JSON shape into ours. It does not cover the
network, and the fixtures go stale if a provider changes their schema, which
is stated here so nobody mistakes green tests for a live system.

No test in this file touches the network.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import ats, free_boards  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures'


def load(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


# Every field a downstream stage reads. A connector that omits one of these
# does not crash, it produces a job that scores badly or dedupes wrongly.
REQUIRED_FIELDS = {'title', 'company', 'description', 'link', 'location', 'source'}


class TestApplicantTrackingSystems:
    """
    The five ATS fetchers share a shape: fetch JSON, map it to our record.
    _get_json is the seam, so the parser is exercised without a request.
    """

    @pytest.mark.parametrize('fixture, fetcher, expected_source', [
        ('greenhouse', ats.fetch_greenhouse, 'Greenhouse'),
        ('lever', ats.fetch_lever, 'Lever'),
        ('ashby', ats.fetch_ashby, 'Ashby'),
    ])
    def test_parses_a_real_response_into_our_record(
            self, fixture, fetcher, expected_source, monkeypatch):
        monkeypatch.setattr(ats, '_get_json', lambda url: load(fixture))
        jobs = fetcher('any-slug', 'Acme')

        assert jobs, f'{fixture} fixture produced no jobs'
        for job in jobs:
            missing = REQUIRED_FIELDS - set(job)
            assert not missing, f'{fixture} job is missing {missing}'
            assert job['source'] == expected_source
            assert job['company'] == 'Acme'
            assert job['title'], 'a job with no title cannot be scored or deduped'

    @pytest.mark.parametrize('fixture, fetcher', [
        ('greenhouse', ats.fetch_greenhouse),
        ('lever', ats.fetch_lever),
        ('ashby', ats.fetch_ashby),
    ])
    def test_the_description_is_plain_text_not_html(
            self, fixture, fetcher, monkeypatch):
        """
        Descriptions feed keyword scoring. Leftover markup inflates the score
        of any job whose HTML happens to contain a skill term in a class name
        or a tracking attribute.
        """
        monkeypatch.setattr(ats, '_get_json', lambda url: load(fixture))
        for job in fetcher('any-slug', 'Acme'):
            assert '<' not in job['description'], 'markup survived the strip'
            assert '&nbsp;' not in job['description'], 'entities were not unescaped'

    @pytest.mark.parametrize('fixture, fetcher', [
        ('greenhouse', ats.fetch_greenhouse),
        ('ashby', ats.fetch_ashby),
    ])
    def test_max_jobs_is_honoured(self, fixture, fetcher, monkeypatch):
        """
        The cap exists so one large employer cannot flood the digest. Bosch
        alone lists over four thousand postings.
        """
        monkeypatch.setattr(ats, '_get_json', lambda url: load(fixture))
        assert len(fetcher('any-slug', 'Acme', max_jobs=1)) <= 1

    def test_links_are_canonicalised(self, monkeypatch):
        """Tracking parameters have to go, or deduplication sees two jobs."""
        monkeypatch.setattr(ats, '_get_json', lambda url: load('greenhouse'))
        for job in ats.fetch_greenhouse('any-slug', 'Acme'):
            assert 'utm_' not in job['link']
            assert 'gh_src' not in job['link']


class TestSuccessFactors:
    """
    SuccessFactors has no public JSON API, so its adapter parses the Google
    Jobs RSS feed at /sitemap.xml. _sf_jobs_from_stream is the seam, exercised
    on a fixed feed with no request.
    """

    FEED = b'''<?xml version="1.0" encoding="UTF-8" ?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>
<title>Careers feed title</title>
<item><title>Sales Engineer (Berlin, DE)</title>
<description><![CDATA[&lt;p&gt;Kv &lt;b&gt;valve sizing&lt;/b&gt; and flow control.&lt;/p&gt;]]></description>
<link>https://jobs.example.com/job/123/?utm_source=x</link>
<g:location>Berlin, DE</g:location><g:employer>Acme</g:employer></item>
<item><title>Process Engineer (Prague, CZ)</title>
<description><![CDATA[Piping and instrumentation.]]></description>
<link>https://jobs.example.com/job/456/</link><g:location>Prague, CZ</g:location></item>
</channel></rss>'''

    def _parse(self, max_jobs=None):
        import io
        return ats._sf_jobs_from_stream(io.BytesIO(self.FEED), 'Acme', 'jobs.example.com', max_jobs)

    def test_parses_the_feed_into_our_record(self):
        jobs = self._parse()
        assert len(jobs) == 2, 'both feed items should parse'
        for job in jobs:
            missing = REQUIRED_FIELDS - set(job)
            assert not missing, f'job is missing {missing}'
            assert job['source'] == 'SuccessFactors'
            assert job['company'] == 'Acme'
            assert job['title']

    def test_description_is_plain_text_and_location_is_read(self):
        job = self._parse()[0]
        assert '<' not in job['description'] and '&lt;' not in job['description']
        assert 'valve sizing' in job['description'], 'CDATA HTML should unescape then strip'
        assert job['location'] == 'Berlin, DE'

    def test_links_are_canonicalised(self):
        assert 'utm_' not in self._parse()[0]['link']

    def test_max_jobs_is_honoured(self):
        assert len(self._parse(max_jobs=1)) == 1

    def test_successfactors_is_a_registered_fetcher(self):
        assert ats.FETCHERS.get('successfactors') is ats.fetch_successfactors


class TestFreeBoards:
    """Same approach for the keyless aggregators."""

    @pytest.mark.parametrize('fixture, call', [
        ('remoteok', lambda: free_boards.search_remoteok(['engineer', 'developer'])),
        ('remotive', lambda: free_boards.search_remotive(['engineer', 'developer'])),
        ('jobicy', lambda: free_boards.search_jobicy('engineering')),
    ])
    def test_parses_a_real_response(self, fixture, call, monkeypatch):
        monkeypatch.setattr(free_boards, '_get', lambda url, params=None: load(fixture))
        jobs = call()
        for job in jobs:
            missing = REQUIRED_FIELDS - set(job)
            assert not missing, f'{fixture} job is missing {missing}'
            assert job['title']


class TestOneDeadSourceCannotEndTheRun:
    """
    The property the whole per-source wrapper exists to provide, checked
    against the three ways a source actually dies.
    """

    @pytest.mark.parametrize('failure', [
        RuntimeError('HTTP 500'),
        TimeoutError('read timed out'),
        ValueError('Expecting value: line 1 column 1'),   # HTML where JSON was expected
    ])
    def test_a_failing_ats_call_raises_rather_than_returning_junk(
            self, failure, monkeypatch):
        """
        It must raise so the caller's wrapper can record the source as failed.
        Silently returning an empty list is the Jobicy failure mode: it looks
        identical to a source with no matches.
        """
        def boom(url):
            raise failure
        monkeypatch.setattr(ats, '_get_json', boom)
        with pytest.raises(type(failure)):
            ats.fetch_greenhouse('any-slug', 'Acme')

    def test_a_missing_board_is_a_distinct_error(self, monkeypatch):
        """A wrong slug should be distinguishable from an outage."""
        class Response:
            status_code = 404
        monkeypatch.setattr(ats.session, 'get', lambda *a, **k: Response())
        with pytest.raises(LookupError, match='slug'):
            ats._get_json('https://example.com/boards/nope')

    @pytest.mark.parametrize('empty', [
        {'jobs': []},
        {},
    ])
    def test_an_empty_board_returns_no_jobs_without_raising(self, empty, monkeypatch):
        """An employer with nothing open is normal, not an error."""
        monkeypatch.setattr(ats, '_get_json', lambda url: empty)
        assert ats.fetch_greenhouse('any-slug', 'Acme') == []


class TestTheFixturesAreRealisticEnoughToBeWorthHaving:
    """
    A fixture that has been trimmed into something the parser finds easy
    tests nothing. These assert the captured responses still look like the
    messy originals.
    """

    @pytest.mark.parametrize('fixture', ['greenhouse', 'lever', 'ashby'])
    def test_every_fixture_holds_at_least_one_job(self, fixture):
        data = load(fixture)
        items = data if isinstance(data, list) else data.get('jobs', [])
        assert items, f'{fixture} fixture is empty and tests nothing'

    def test_at_least_one_fixture_still_contains_raw_markup(self):
        """
        Otherwise the strip-html assertions above pass for the wrong reason.
        """
        raw = (FIXTURES / 'greenhouse.json').read_text(encoding='utf-8')
        assert '&lt;' in raw or '<' in raw, (
            'the greenhouse fixture no longer contains markup, so the '
            'description-is-plain-text test is not proving anything'
        )
