"""
Regression tests for the link validator.

These exist because of a specific bug. The validator decided a listing was
dead by searching the whole lowercased page body for phrases like "404" and
"page not found". Live job pages contain those strings constantly, in inline
scripts, asset filenames, analytics payloads and client side error handlers.
The validator threw away around 86 percent of perfectly good listings and
logged every rejection at DEBUG, so nothing surfaced.

The tests below pin down the behaviour that was wrong.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_validator import JobValidator  # noqa: E402


# A live page. Nothing about it says the listing is gone, but it contains
# the substrings the old matcher looked for.
LIVE_PAGE_WITH_FALSE_POSITIVES = """
<!doctype html><html><head>
<link rel="stylesheet" href="/static/css/app.404abc12.css">
<script>window.__ERR__={onError:function(c){if(c===404){location='/page-not-found';}}};</script>
</head><body>
<h1>Senior Sales Engineer</h1>
<p>Acme Fluid Systems is hiring. Apply by Friday.</p>
<button>Apply now</button>
</body></html>
"""

GENUINELY_EXPIRED_PAGE = """
<!doctype html><html><body>
<h1>This job is no longer available</h1>
<p>The position has been filled.</p>
</body></html>
"""


@pytest.fixture
def validator():
    return JobValidator()


class TestExpiredPhrases:
    """The phrase list itself, independent of any network call."""

    def test_bare_404_is_not_an_expiry_phrase(self, validator):
        """
        '404' as a bare substring matches asset hashes and script bodies.
        It must not be in the list.
        """
        assert '404' not in validator.EXPIRED_PHRASES, (
            "'404' matches build hashes like app.404abc12.css on live pages"
        )

    def test_page_not_found_is_not_a_bare_substring(self, validator):
        """
        'page not found' appears inside client side error handlers on pages
        that are serving a live listing perfectly well.
        """
        assert 'page not found' not in validator.EXPIRED_PHRASES

    def test_does_not_exist_is_not_an_expiry_phrase(self, validator):
        assert 'does not exist' not in validator.EXPIRED_PHRASES

    def test_live_page_matches_no_expiry_phrase(self, validator):
        """The whole point. A live listing must survive the phrase scan."""
        body = LIVE_PAGE_WITH_FALSE_POSITIVES.lower()
        matched = [p for p in validator.EXPIRED_PHRASES if p in body]
        assert matched == [], f"live page wrongly matched {matched}"

    def test_expired_page_still_matches(self, validator):
        """Removing the loose phrases must not disarm the check entirely."""
        body = GENUINELY_EXPIRED_PAGE.lower()
        matched = [p for p in validator.EXPIRED_PHRASES if p in body]
        assert matched, "a genuinely expired page must still be detected"


class TestRecency:
    def test_missing_date_is_kept(self, validator):
        """No date information must not mean rejection."""
        assert validator.is_recent_posting({}, days=14) is True

    def test_old_posting_is_rejected(self, validator):
        from datetime import datetime, timedelta
        old = (datetime.now() - timedelta(days=40)).isoformat()
        assert validator.is_recent_posting({'extracted_date': old}, days=14) is False

    def test_recent_posting_is_kept(self, validator):
        from datetime import datetime, timedelta
        recent = (datetime.now() - timedelta(days=2)).isoformat()
        assert validator.is_recent_posting({'extracted_date': recent}, days=14) is True

    def test_unparseable_date_is_kept(self, validator):
        """A malformed date must not silently delete a job."""
        assert validator.is_recent_posting({'extracted_date': 'yesterday'}, days=14) is True


class TestValidatePassRate:
    """
    The guard that would have caught the original bug on day one.

    With link checking off, validation only applies the recency rule, so a
    batch of fresh jobs must come through effectively whole.
    """

    def test_fresh_jobs_survive_validation(self, validator):
        from datetime import datetime
        now = datetime.now().isoformat()
        jobs = [
            {'title': f'Job {i}', 'company': 'Acme',
             'link': f'https://example.com/{i}', 'extracted_date': now}
            for i in range(100)
        ]

        kept = validator.validate_jobs(jobs, check_links=False, max_age_days=14)

        pass_rate = len(kept) / len(jobs)
        assert pass_rate == 1.0, (
            f"validation kept {pass_rate:.0%} of fresh jobs, expected 100%. "
            "A sudden drop here is the signature of the link validation bug."
        )
