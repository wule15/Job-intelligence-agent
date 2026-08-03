"""
Tests for the retrying HTTP session.

These exist because RETRY_ATTEMPTS and RETRY_BACKOFF sat in config.py for
months, read by nothing, while sources silently lost a day's results to rate
limiting. The tests pin down what is retried and, just as importantly, what
is not.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config  # noqa: E402
from core.http_client import (  # noqa: E402
    RETRY_METHODS,
    RETRY_STATUSES,
    build_retry,
    build_session,
)


class TestRetryPolicy:
    def test_reads_the_config_values(self, monkeypatch):
        """The whole point: these constants must actually be used."""
        monkeypatch.setattr(config.Config, 'RETRY_ATTEMPTS', 7)
        monkeypatch.setattr(config.Config, 'RETRY_BACKOFF', 3)
        retry = build_retry()
        assert retry.total == 7
        assert retry.backoff_factor == 3

    def test_explicit_arguments_win(self):
        retry = build_retry(attempts=2, backoff=0)
        assert retry.total == 2
        assert retry.backoff_factor == 0

    @pytest.mark.parametrize('status', [429, 500, 502, 503, 504, 408])
    def test_transient_failures_are_retried(self, status):
        assert status in build_retry().status_forcelist

    @pytest.mark.parametrize('status', [400, 401, 403, 404, 410, 422])
    def test_client_errors_are_not_retried(self, status):
        """
        Repeating a request the server already rejected wastes quota and
        delays the run. A 404 is not going to become a 200.
        """
        assert status not in build_retry().status_forcelist

    def test_retry_after_header_is_respected(self):
        """When a server says how long to wait, obey it rather than guess."""
        assert build_retry().respect_retry_after_header is True

    def test_final_response_is_returned_not_raised(self):
        """
        Callers already branch on status_code. Raising instead would change
        every connector's error handling.
        """
        assert build_retry().raise_on_status is False

    def test_post_is_retried(self):
        """
        Workday lists jobs over POST. It is a read-only query, so retrying
        is safe. Any POST that creates or mutates must not be added.
        """
        assert 'POST' in RETRY_METHODS
        assert 'GET' in RETRY_METHODS

    def test_zero_attempts_disables_retrying(self):
        assert build_retry(attempts=0).total == 0


class TestSession:
    def test_adapter_is_mounted_for_both_schemes(self):
        session = build_session()
        for scheme in ('https://', 'http://'):
            adapter = session.get_adapter(scheme + 'example.com')
            assert adapter.max_retries.total == config.Config.RETRY_ATTEMPTS

    def test_user_agent_is_set(self):
        session = build_session()
        assert session.headers.get('User-Agent')

    def test_user_agent_can_be_overridden(self):
        session = build_session(user_agent='custom-agent/1.0')
        assert session.headers['User-Agent'] == 'custom-agent/1.0'

    def test_retry_statuses_are_all_transient(self):
        """
        Guard against someone adding 403 here. A 403 means the request was
        rejected, and retrying it looks like an attack.
        """
        permanent = {400, 401, 403, 404, 405, 410, 422}
        assert not (set(RETRY_STATUSES) & permanent)


class TestConnectorsUseIt:
    """The policy is worthless if the connectors bypass it."""

    def test_validator_session_retries(self):
        from core.job_validator import JobValidator
        adapter = JobValidator().session.get_adapter('https://example.com')
        assert adapter.max_retries.total == config.Config.RETRY_ATTEMPTS

    def test_linkedin_session_retries(self):
        from sources.linkedin import LinkedInJobSearcher
        adapter = LinkedInJobSearcher().session.get_adapter('https://example.com')
        assert adapter.max_retries.total == config.Config.RETRY_ATTEMPTS

    def test_ats_session_retries(self):
        from sources import ats
        adapter = ats.session.get_adapter('https://example.com')
        assert adapter.max_retries.total == config.Config.RETRY_ATTEMPTS

    def test_ats_module_makes_no_bare_requests_calls(self):
        """
        A bare requests.get bypasses the session and loses the retry policy.
        Catching it here is cheaper than noticing a source went quiet.
        """
        source = (Path(__file__).resolve().parent.parent / 'sources' / 'ats.py').read_text()
        assert 'requests.get(' not in source
        assert 'requests.post(' not in source
