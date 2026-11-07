"""
Weekly sending. With DIGEST_SCHEDULE=weekly the search still runs every day
and stores what it finds, but Telegram gets nothing on most days: on
WEEKLY_DIGEST_DAY one message carries the WEEKLY_DIGEST_SIZE best unsent jobs
of the week, ranked across every source. Daily stays the default.
"""

from datetime import datetime

import pytest

import telegram_sender
from core.config import Config, weekday_setting

MONDAY = datetime(2026, 11, 9, 9, 20)
TUESDAY = datetime(2026, 11, 10, 9, 20)


def _job(n, score=50.0):
    return (n, f'Sales Engineer {n}', f'Company {n}', score,
            f'https://example.com/jobs/{n}', '', 'Workday', 0)


# ── Settings ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, day', [
    ('monday', 0), ('Friday', 4), ('sun', 6), ('3', 3), ('', 0), (None, 0)])
def test_weekday_setting_reads_names_and_numbers(raw, day):
    assert weekday_setting(raw, 0) == day


def test_weekday_setting_falls_back_on_nonsense():
    assert weekday_setting('someday', 0) == 0


# ── The weekly message ───────────────────────────────────────────────────────

def test_weekly_message_numbers_every_job():
    messages, ids = telegram_sender.format_weekly_digest([_job(n) for n in range(1, 21)])
    text = ''.join(messages)
    assert ids == list(range(1, 21))
    assert 'Weekly Job Digest' in messages[0]
    assert '<b>20. Sales Engineer 20</b>' in text


def test_weekly_message_splits_under_the_telegram_limit():
    long_title = [(n, 'Senior Valve Applications Sales Engineer ' * 4, 'Company', 60.0,
                   'https://example.com/' + 'x' * 150, 'cv', 'Workday', 0) for n in range(1, 21)]
    messages, ids = telegram_sender.format_weekly_digest(long_title)
    assert len(messages) > 1
    assert all(len(message) <= telegram_sender.TELEGRAM_MESSAGE_LIMIT for message in messages)
    assert ids == list(range(1, 21))


# ── main() ───────────────────────────────────────────────────────────────────

@pytest.fixture
def quiet(monkeypatch):
    """main() with the database and network stubbed; records what is sent."""
    sent, marked, asked = [], [], {}

    def unsent(limit=None, **kwargs):
        asked['limit'] = limit
        return [_job(n) for n in range(1, (limit or 10) + 1)]

    for name, value in {
        'init_telegram_tracking': lambda: None, 'load_exclusions': lambda: [],
        'suppress_repeats': lambda ex: 0, 'suppress_ineligible': lambda: 0,
        'reset_liveness': lambda: None, '_report_liveness': lambda: None,
        'get_direct_jobs': lambda **kw: [], 'get_regional_jobs': lambda **kw: [],
        'get_unsent_jobs': unsent, '_health_note': lambda: '',
        '_regional_configured': lambda: False,
        'mark_jobs_sent': lambda ids: marked.append(list(ids)) or True,
        'send_telegram_message': lambda message: sent.append(message) or True,
    }.items():
        monkeypatch.setattr(telegram_sender, name, value)
    monkeypatch.setattr(Config, 'DIGEST_SCHEDULE', 'weekly', raising=False)
    monkeypatch.setattr(Config, 'WEEKLY_DIGEST_DAY', 0, raising=False)
    monkeypatch.setattr(Config, 'WEEKLY_DIGEST_SIZE', 20, raising=False)
    return sent, marked, asked


def test_weekly_mode_sends_nothing_on_other_days(quiet, monkeypatch):
    sent, marked, _ = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: TUESDAY)
    assert telegram_sender.main() == 0
    assert sent == [] and marked == []


def test_weekly_mode_sends_the_best_twenty_on_its_day(quiet, monkeypatch):
    sent, marked, asked = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    assert telegram_sender.main() == 0
    assert asked['limit'] == 20
    assert sent and 'Weekly Job Digest' in sent[0]
    assert sum(marked, []) == list(range(1, 21))


def test_weekly_mode_failed_send_marks_nothing_and_exits_1(quiet, monkeypatch):
    sent, marked, _ = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    monkeypatch.setattr(telegram_sender, 'send_telegram_message', lambda message: False)
    assert telegram_sender.main() == 1
    assert marked == []


def test_weekly_mode_with_nothing_to_send_says_so(quiet, monkeypatch):
    sent, _, _ = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    monkeypatch.setattr(telegram_sender, 'get_unsent_jobs', lambda **kw: [])
    assert telegram_sender.main() == 0
    assert len(sent) == 1 and 'Weekly Job Digest' in sent[0]


def test_weekly_domestic_message_goes_first_with_five_jobs(quiet, monkeypatch):
    sent, marked, _ = quiet
    asked = {}

    def regional(limit=None, **kwargs):
        asked['limit'] = limit
        return [_job(n, 30.0) for n in range(101, 101 + limit)]
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    monkeypatch.setattr(telegram_sender, '_regional_configured', lambda: True)
    monkeypatch.setattr(telegram_sender, 'get_regional_jobs', regional)
    monkeypatch.setattr(Config, 'WEEKLY_REGIONAL_SIZE', 5, raising=False)

    assert telegram_sender.main() == 0
    assert asked['limit'] == 5
    assert 'Sales Engineer 101' in sent[0] and 'Weekly Job Digest' in sent[1]
    assert marked[0] == list(range(101, 106))


def test_weekly_domestic_message_skipped_without_a_region(quiet, monkeypatch):
    sent, _, _ = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    called = []
    monkeypatch.setattr(telegram_sender, 'get_regional_jobs', lambda **kw: called.append(1) or [])
    telegram_sender.main()
    assert called == [] and all('Weekly Job Digest' in message for message in sent)


def test_weekly_domestic_message_says_so_when_empty(quiet, monkeypatch):
    sent, _, _ = quiet
    monkeypatch.setattr(telegram_sender, '_now', lambda: MONDAY)
    monkeypatch.setattr(telegram_sender, '_regional_configured', lambda: True)
    assert telegram_sender.main() == 0
    assert len(sent) == 2 and 'Weekly Job Digest' in sent[1]


def test_daily_is_still_the_default(quiet, monkeypatch):
    sent, _, _ = quiet
    monkeypatch.setattr(Config, 'DIGEST_SCHEDULE', 'daily')
    monkeypatch.setattr(telegram_sender, '_now', lambda: TUESDAY)
    telegram_sender.main()
    assert any('Daily Job Digest' in message for message in sent)
