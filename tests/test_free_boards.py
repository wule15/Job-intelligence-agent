"""
Tests for the free job board connectors.

These exist because of a bug this file now pins down. search_jobicy sent
geo=worldwide on every call. The Jobicy API validates geo against a list of
real regions and answers HTTP 400 to anything outside it, so every call
failed, the connector returned an empty list, and the run carried on. Jobicy
contributed nothing for as long as it had been wired in.

Nothing here makes a network call. The connectors are tested by reading what
they would send, which is the same approach test_http_client.py takes to
assert the ATS module never bypasses the retrying session.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import free_boards  # noqa: E402


# Values that look like they mean "everywhere" and are rejected by the API.
# Omitting geo is what actually means worldwide.
REJECTED_GEO_VALUES = ['worldwide', 'anywhere', 'global', 'remote']


class TestJobicyGeoRegression:
    """
    A source that returns nothing looks exactly like a source with no matches.
    That is why this was invisible, and why the assertion is on what gets sent
    rather than on what comes back.
    """

    def test_does_not_send_a_rejected_geo_value(self):
        source = inspect.getsource(free_boards.search_jobicy)
        params_line = [
            line for line in source.splitlines()
            if "'geo'" in line or '"geo"' in line
        ]
        for line in params_line:
            for rejected in REJECTED_GEO_VALUES:
                assert f"'{rejected}'" not in line.lower(), (
                    f"search_jobicy sends geo={rejected}, which the API answers "
                    f"400 to. Omit geo instead, that is what worldwide means."
                )

    def test_the_reason_is_written_down(self):
        """
        The docstring has to keep explaining this. Without it the next person
        reads a request with no geo, assumes it was an oversight, and puts
        worldwide back.
        """
        doc = free_boards.search_jobicy.__doc__ or ''
        assert 'geo' in doc.lower()
        assert '400' in doc


class TestMuseFiltersServerSide:
    """
    Same failure mode as Jobicy, reached a different way. The connector
    worked, returned a valid empty-ish list, and nobody noticed it was
    scanning a hundred listings to find one.
    """

    def test_asks_the_api_for_remote_jobs(self):
        source = inspect.getsource(free_boards.search_the_muse)
        assert "'location'" in source or '"location"' in source, (
            'search_the_muse must filter by location server-side. Without it '
            'the endpoint returns every job The Muse lists and the client-side '
            'check discards about 99 percent of what was fetched.'
        )

    def test_uses_the_exact_location_string_the_api_expects(self):
        assert free_boards.MUSE_REMOTE_LOCATION == 'Flexible / Remote'

    def test_keeps_the_client_side_remote_check_as_a_safety_net(self):
        """
        Belt and braces. If the parameter silently stops filtering, on-site
        jobs must still not reach the digest.
        """
        source = inspect.getsource(free_boards.search_the_muse)
        assert 'is_remote' in source


class TestSearchAllSurvivesOneDeadSource:
    """
    One source failing must never end the run. This is the property the
    per-source try/except in search_all exists to provide.
    """

    def test_every_source_call_is_individually_guarded(self):
        source = inspect.getsource(free_boards.FreeJobSearcher.search_all)
        assert source.count('try:') >= 5, (
            'each source call in search_all should be wrapped on its own, so '
            'one failure cannot take the rest of the run with it'
        )
        assert 'except' in source
