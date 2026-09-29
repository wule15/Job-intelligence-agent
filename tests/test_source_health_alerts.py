"""
A broken source is named in the Telegram digest, not only in a log file.

The run already records every source's result. Until now the only place a
dead source showed up was a warning in a log nobody opens, so a source out of
quota for a week looked exactly like a quiet week.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import source_health  # noqa: E402
from core.source_health import SourceResult, health_alerts  # noqa: E402
from telegram_sender import format_health_note  # noqa: E402


def _run(db, **sources):
    """Record one run. Each source is a job count, or an error string."""
    results = []
    for name, outcome in sources.items():
        r = SourceResult(name=name)
        if isinstance(outcome, str):
            r.error = outcome
        else:
            r.jobs = [{}] * outcome
        results.append(r)
    source_health.record(results, db_path=db)


def _db(tmp_path):
    db = str(tmp_path / 'health.db')
    source_health.init_tables(db_path=db)
    return db


class TestHealthAlerts:
    def test_healthy_run_has_no_alerts(self, tmp_path):
        db = _db(tmp_path)
        _run(db, ATS=5, LinkedIn=3)
        assert health_alerts(db_path=db) == []

    def test_a_source_that_failed_this_run_is_named(self, tmp_path):
        db = _db(tmp_path)
        _run(db, ATS=5, Apify='RuntimeError: monthly usage hard limit exceeded')
        alerts = health_alerts(db_path=db)
        assert len(alerts) == 1
        assert 'Apify' in alerts[0]
        assert 'monthly usage hard limit' in alerts[0]

    def test_one_empty_run_is_not_an_alert(self, tmp_path):
        db = _db(tmp_path)
        _run(db, ATS=5, JSearch=0)
        assert health_alerts(db_path=db) == []

    def test_three_empty_runs_is_an_alert(self, tmp_path):
        db = _db(tmp_path)
        for _ in range(3):
            _run(db, ATS=5, JSearch=0)
        alerts = health_alerts(db_path=db)
        assert len(alerts) == 1
        assert 'JSearch' in alerts[0]
        assert '3 runs' in alerts[0]

    def test_a_source_is_named_once(self, tmp_path):
        """Stale and failing this run is still one line, not two."""
        db = _db(tmp_path)
        for _ in range(3):
            _run(db, ATS=5, Apify='RuntimeError: limit')
        assert len(health_alerts(db_path=db)) == 1

    def test_urls_are_stripped_from_errors(self, tmp_path):
        """A request error carries its URL, and a URL can carry an API key."""
        db = _db(tmp_path)
        _run(db, ATS=5, SerpAPI='HTTPError: 401 for https://serpapi.com/search?api_key=SECRET123')
        alert = health_alerts(db_path=db)[0]
        assert 'SECRET123' not in alert
        assert 'serpapi.com' not in alert


class TestHealthNote:
    def test_no_alerts_no_note(self):
        assert format_health_note([]) == ''

    def test_note_lists_each_alert_and_escapes_html(self):
        note = format_health_note(['Apify: failed <limit>', 'JSearch: no jobs in 3 runs'])
        assert 'Apify' in note and 'JSearch' in note
        assert '<limit>' not in note
        assert '&lt;limit&gt;' in note
