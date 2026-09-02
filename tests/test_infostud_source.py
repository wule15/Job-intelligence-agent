"""
Tests for the Infostud source parser.

The network fetch is separated from the parsing so the parsing is tested
offline against the shape of Infostud's Next.js __NEXT_DATA__ payload.
"""

import sys
from pathlib import Path

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
