"""
Tests for the never-resend guard.

A job reaches the chat at most once. The daily cleanup deletes rows older than
a week, and it used to delete the record that a job had been sent along with
them. A posting that was still advertised then came back under a new link and
was sent again. sent_history is the durable record that the cleanup never
touches, and an optional private list covers jobs handled outside the digest.
"""

import html
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_sender  # noqa: E402
from core.database import Database  # noqa: E402

REGION = 'Testland'


@pytest.fixture
def path(tmp_path, monkeypatch):
    db_path = str(tmp_path / 'history.db')
    monkeypatch.setattr(telegram_sender.Config, 'DATABASE_PATH', db_path)
    monkeypatch.setattr(telegram_sender.Config, 'REGIONAL_MATCH_TERMS', [REGION.lower()], raising=False)
    monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
    return db_path


@pytest.fixture
def db(path):
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


def add(db, title, company, link=None, score=80.0, source='Workday', location=None):
    return db.add_job(title, company, 'desc', link or f'https://example.com/{title}/{company}',
                      source=source, relevance_score=score,
                      location=location or f'City, {REGION}')


def rows(path, sql):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def selectable_ids():
    """Every id any of the three digests would pick today."""
    ids = set()
    for pick in (telegram_sender.get_unsent_jobs, telegram_sender.get_direct_jobs,
                 telegram_sender.get_regional_jobs):
        ids.update(job[0] for job in pick())
    return ids


def age_and_clean(db):
    """What the daily run does to a job first stored eight days ago."""
    db.connection.execute("UPDATE jobs SET extracted_date = datetime('now', '-8 days')")
    db.connection.commit()
    db.cleanup_old_entries(days=7)


class TestSentHistory:
    def test_marking_sent_writes_the_durable_record(self, db, path):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://example.com/jobs/1')
        telegram_sender.mark_jobs_sent([jid])
        assert rows(path, 'SELECT dedup_key, link FROM sent_history') == [
            ('sales engineer|flowco', 'https://example.com/jobs/1')]

    def test_cleanup_does_not_touch_the_durable_record(self, db, path):
        jid = add(db, 'Sales Engineer', 'Flowco')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        assert rows(path, 'SELECT job_id FROM telegram_sent_jobs') == []
        assert len(rows(path, 'SELECT dedup_key FROM sent_history')) == 1

    def test_repost_under_a_new_link_is_never_selected(self, db):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://boards.example/flowco/jobs/1')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        again = add(db, 'Sales Engineer', 'Flowco', link='https://boards.example/flowco/jobs/2')
        assert again in selectable_ids(), 'precondition: without the guard it would be sent'
        telegram_sender.suppress_repeats([])
        assert again not in selectable_ids()

    def test_same_link_under_a_new_title_is_never_selected(self, db):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://boards.example/flowco/jobs/1')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        again = add(db, 'Sales Engineer, Valves', 'Flowco', link='https://boards.example/flowco/jobs/1')
        telegram_sender.suppress_repeats([])
        assert again not in selectable_ids()

    def test_history_ignores_diacritics(self, db):
        jid = add(db, 'Inženjer prodaje', 'Firma d.o.o.', link='https://example.com/a')
        telegram_sender.mark_jobs_sent([jid])
        again = add(db, 'Inzenjer prodaje', 'Firma', link='https://example.com/b')
        telegram_sender.suppress_repeats([])
        assert again not in selectable_ids()

    def test_a_different_level_at_the_same_company_still_goes_out(self, db):
        """History is an exact key, never the loose exclusion rule, so a
        distinct opening at a company already sent is not hidden."""
        jid = add(db, 'Automation and Controls Engineer II', 'Orbitco', link='https://example.com/a')
        telegram_sender.mark_jobs_sent([jid])
        other = add(db, 'Senior Automation and Controls Engineer', 'Orbitco', link='https://example.com/b')
        telegram_sender.suppress_repeats([])
        assert other in selectable_ids()

    def test_a_link_with_tracking_parameters_is_the_same_link(self, db):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://boards.example/flowco/jobs/1')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        again = add(db, 'Valve Specialist', 'Flowco',
                    link='https://boards.example/flowco/jobs/1?utm_source=x')
        telegram_sender.suppress_repeats([])
        assert again not in selectable_ids()

    def test_titles_that_normalise_to_nothing_are_not_one_job(self, db):
        """"Intern - Sales" and "Internship (m/f/div.)" both lose every
        word to normalisation. Matched on that empty title, one send would
        hold back every such posting at the company for good."""
        jid = add(db, 'Intern - Sales', 'Bigco', link='https://example.com/a')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        other = add(db, 'Internship (m/f/div.)', 'Bigco', link='https://example.com/b')
        telegram_sender.suppress_repeats([])
        assert other in selectable_ids()

    def _send_and_age(self, db, title, link, location):
        jid = add(db, title, 'Bigco', link=link, location=location)
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)

    def test_the_same_title_at_another_site_still_goes_out(self, db):
        self._send_and_age(db, 'Sales Engineer - Germany', 'https://example.com/a',
                           'Stuttgart, Germany')
        other_site = add(db, 'Sales Engineer - Netherlands', 'Bigco',
                         link='https://example.com/b', location='Eindhoven, Netherlands')
        telegram_sender.suppress_repeats([])
        assert other_site in selectable_ids()

    def test_the_same_title_in_the_same_country_is_a_repeat(self, db):
        self._send_and_age(db, 'Sales Engineer - Germany', 'https://example.com/a',
                           'Stuttgart, Germany')
        same_site = add(db, 'Sales Engineer - Germany', 'Bigco',
                        link='https://example.com/c', location='Munich, Germany')
        telegram_sender.suppress_repeats([])
        assert same_site not in selectable_ids()

    def _send_long_ago(self, db, path):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://example.com/a')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE sent_history SET sent_date = datetime('now', '-60 days')")
            conn.commit()

    def test_a_title_sent_long_ago_may_go_out_again_under_a_new_link(self, db, path):
        self._send_long_ago(db, path)
        new_link = add(db, 'Sales Engineer', 'Flowco', link='https://example.com/b')
        telegram_sender.suppress_repeats([])
        assert new_link in selectable_ids()

    def test_a_link_already_sent_is_held_back_for_good(self, db, path):
        self._send_long_ago(db, path)
        old_link = add(db, 'Sales Engineer (m/w/d)', 'Flowco', link='https://example.com/a')
        telegram_sender.suppress_repeats([])
        assert old_link not in selectable_ids()

    def test_a_suppressed_repeat_is_logged_with_its_full_link(self, db, capsys):
        jid = add(db, 'Sales Engineer', 'Flowco', link='https://boards.example/flowco/jobs/1')
        telegram_sender.mark_jobs_sent([jid])
        age_and_clean(db)
        link = 'https://boards.example/flowco/jobs/2?gh_jid=123456789&office=remote-europe'
        again = add(db, 'Sales Engineer', 'Flowco', link=link)
        telegram_sender.suppress_repeats([])
        out = capsys.readouterr().out
        assert f'[repeat] id={again} | Sales Engineer | Flowco | {link}' in out


class TestTwoWrites:
    def test_a_failed_history_write_still_marks_the_job_sent(self, db, path, capsys):
        """If sent_history cannot be written, the job must still count as sent
        today, or the main digest would send it again in the same run."""
        jid = add(db, 'Sales Engineer', 'Flowco')
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('DROP TABLE sent_history')
            conn.commit()
        assert telegram_sender.mark_jobs_sent([jid]) is True
        assert rows(path, 'SELECT job_id FROM telegram_sent_jobs') == [(jid,)]
        assert jid not in selectable_ids()
        assert 'send history' in capsys.readouterr().out

    def test_the_repeat_check_is_never_fatal(self, db, path):
        add(db, 'Sales Engineer', 'Flowco')
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('DROP TABLE sent_history')
            conn.commit()
        assert telegram_sender.suppress_repeats([('Flowco', 'Sales Engineer')]) == 0


class TestExclusions:
    ENTRIES = [('Primer firma', 'Inzenjer podrske')]

    def test_an_excluded_job_is_never_selected(self, db, capsys):
        jid = add(db, 'Inženjer podrške (m/ž)', 'Primer firma d.o.o.', link='https://example.com/x')
        telegram_sender.suppress_repeats(self.ENTRIES)
        assert jid not in selectable_ids()
        assert '[excluded] id=' in capsys.readouterr().out

    def test_an_excluded_job_is_not_recorded_as_sent(self, db, path):
        add(db, 'Inženjer podrške (m/ž)', 'Primer firma d.o.o.')
        telegram_sender.suppress_repeats(self.ENTRIES)
        assert rows(path, 'SELECT * FROM sent_history') == []

    def test_other_jobs_are_left_alone(self, db):
        keep = add(db, 'Inženjer podrške', 'Druga firma')
        telegram_sender.suppress_repeats(self.ENTRIES)
        assert keep in selectable_ids()


class TestBackfill:
    """The first run after this change copies what was already sent, so the
    last week of sends is protected from day one."""

    def _old_database(self, path):
        database = Database(db_path=path)
        database.init_database()
        ids = [add(database, f'Role {i}', f'Company {i}') for i in range(3)]
        # The tracking table as it was before sent_history existed.
        database.connection.execute(
            'CREATE TABLE telegram_sent_jobs (job_id INTEGER PRIMARY KEY, '
            'sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
        database.connection.executemany(
            'INSERT INTO telegram_sent_jobs (job_id) VALUES (?)', [(i,) for i in ids[:2]])
        database.connection.commit()
        return database, ids

    def test_backfill_copies_past_sends(self, path):
        database, ids = self._old_database(path)
        try:
            telegram_sender.init_telegram_tracking()
            keys = {k for (k,) in rows(path, 'SELECT dedup_key FROM sent_history')}
            assert keys == {'role 0|company 0', 'role 1|company 1'}
        finally:
            database.close()

    def test_backfill_runs_once(self, path):
        database, ids = self._old_database(path)
        try:
            telegram_sender.init_telegram_tracking()
            # A later suppression writes telegram_sent_jobs, which must never
            # be read back as a send.
            database.connection.execute('INSERT INTO telegram_sent_jobs (job_id) VALUES (?)', (ids[2],))
            database.connection.commit()
            telegram_sender.init_telegram_tracking()
            assert len(rows(path, 'SELECT dedup_key FROM sent_history')) == 2
        finally:
            database.close()

    def test_fresh_database_is_safe(self, path):
        telegram_sender.init_telegram_tracking()
        assert rows(path, 'SELECT * FROM sent_history') == []


class TestLoadExclusions:
    def test_empty_setting_reads_nothing(self, path, capsys):
        assert telegram_sender.load_exclusions() == []
        assert capsys.readouterr().out == ''

    def test_missing_file_warns_once(self, path, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(tmp_path / 'nope.txt'))
        assert telegram_sender.load_exclusions() == []
        assert len(capsys.readouterr().out.strip().splitlines()) == 1

    def test_unreadable_file_warns_once(self, path, tmp_path, monkeypatch, capsys):
        # A directory exists but cannot be read as a file.
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(tmp_path))
        assert telegram_sender.load_exclusions() == []
        assert len(capsys.readouterr().out.strip().splitlines()) == 1

    def test_byte_order_mark_is_tolerated(self, path, tmp_path, monkeypatch):
        f = tmp_path / 'exclude.txt'
        f.write_text('Primer firma | Inzenjer podrske\n', encoding='utf-8-sig')
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(f))
        assert telegram_sender.load_exclusions() == [('Primer firma', 'Inzenjer podrske')]

    def test_relative_path_resolves_against_the_project_root(self, path, tmp_path, monkeypatch):
        (tmp_path / 'data').mkdir()
        (tmp_path / 'data' / 'exclude.txt').write_text('Acme | Sales Engineer\n', encoding='utf-8')
        monkeypatch.setattr(telegram_sender, 'PROJECT_ROOT', tmp_path)
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', 'data/exclude.txt')
        assert telegram_sender.load_exclusions() == [('Acme', 'Sales Engineer')]


class TelegramCapture:
    def __init__(self, status=200):
        self.status, self.messages = status, []

    def __call__(self, url, data=None, timeout=None):
        self.messages.append(data['text'])
        return type('Response', (), {'status_code': self.status})()


@pytest.fixture
def sending(db, monkeypatch):
    monkeypatch.setattr(telegram_sender.Config, 'TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setattr(telegram_sender.Config, 'TELEGRAM_CHAT_ID', '1')
    monkeypatch.setattr(telegram_sender, 'check_link_live', lambda url, timeout=6: True)
    monkeypatch.setattr(telegram_sender, 'posting_is_open',
                        lambda link, source, company='': True)
    return db


class TestMainStillSends:
    def _run(self, monkeypatch, status=200):
        telegram = TelegramCapture(status)
        monkeypatch.setattr(telegram_sender.requests, 'post', telegram)
        telegram_sender.main()
        return '\n'.join(telegram.messages)

    def test_missing_exclusion_file(self, sending, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(tmp_path / 'nope.txt'))
        add(sending, 'Sales Engineer', 'Flowco')
        assert 'Sales Engineer' in self._run(monkeypatch)
        warnings = [l for l in capsys.readouterr().out.splitlines() if 'exclusion' in l.lower()]
        assert len(warnings) <= 1

    def test_unreadable_exclusion_file(self, sending, tmp_path, monkeypatch):
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(tmp_path))
        add(sending, 'Sales Engineer', 'Flowco')
        assert 'Sales Engineer' in self._run(monkeypatch)

    def test_a_crash_in_the_parser(self, sending, tmp_path, monkeypatch):
        f = tmp_path / 'exclude.txt'
        f.write_text('Acme | Writer\n', encoding='utf-8')
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(f))

        def boom(text):
            raise RuntimeError('parser bug')
        monkeypatch.setattr(telegram_sender, 'parse_exclusions', boom)
        add(sending, 'Sales Engineer', 'Flowco')
        assert 'Sales Engineer' in self._run(monkeypatch)

    def test_a_crash_in_the_repeat_check(self, sending, monkeypatch, capsys):
        def boom(url):
            raise RuntimeError('normaliser bug')
        monkeypatch.setattr(telegram_sender, 'canonical_url', boom)
        add(sending, 'Sales Engineer', 'Flowco')
        assert 'Sales Engineer' in self._run(monkeypatch)
        assert 'Could not check for repeats' in capsys.readouterr().out

    def test_a_failed_send_writes_neither_table(self, sending, path, monkeypatch):
        add(sending, 'Sales Engineer', 'Flowco')
        self._run(monkeypatch, status=500)
        assert rows(path, 'SELECT * FROM telegram_sent_jobs') == []
        assert rows(path, 'SELECT * FROM sent_history') == []


class TestDailyRunHoldsBack:
    """The daily run itself must apply the exclusion list and recheck stored
    rows. Calling the two functions directly proves the rules; these prove
    main() still calls them before any digest is chosen."""

    def _run(self, monkeypatch):
        telegram = TelegramCapture()
        monkeypatch.setattr(telegram_sender.requests, 'post', telegram)
        telegram_sender.main()
        return '\n'.join(telegram.messages)

    def test_the_exclusion_file_is_applied(self, sending, tmp_path, monkeypatch, capsys):
        f = tmp_path / 'exclude.txt'
        f.write_text('Primer firma | Inzenjer podrske\n', encoding='utf-8')
        monkeypatch.setattr(telegram_sender.Config, 'DIGEST_EXCLUDE_FILE', str(f))
        add(sending, 'Inženjer podrške', 'Primer firma d.o.o.')
        add(sending, 'Sales Engineer', 'Flowco')
        chat = self._run(monkeypatch)
        assert 'Sales Engineer' in chat
        assert 'podrške' not in chat
        assert '[excluded] id=' in capsys.readouterr().out

    def test_stored_rows_are_rechecked(self, sending, monkeypatch, capsys):
        sending.add_job('Valve Engineer', 'Defenseco', 'Must be a U.S. Person under ITAR.',
                        'https://example.com/itar', source='Workday', relevance_score=90.0,
                        location=f'City, {REGION}')
        add(sending, 'Senior Sales Engineer', 'Bigco')
        add(sending, 'Sales Engineer', 'Flowco')
        chat = self._run(monkeypatch)
        assert 'Sales Engineer' in chat
        assert 'Valve Engineer' not in chat and 'Senior Sales Engineer' not in chat
        out = capsys.readouterr().out
        assert '[ineligible:export_controlled]' in out
        assert '[ineligible:senior_title]' in out


class TestQuietDayNote:
    """The 'nothing cleared the bar' note goes out only when all three
    digests are empty."""

    def _run(self, monkeypatch):
        telegram = TelegramCapture()
        monkeypatch.setattr(telegram_sender.requests, 'post', telegram)
        telegram_sender.main()
        return '\n'.join(telegram.messages)

    def test_a_day_with_only_a_direct_job(self, sending, monkeypatch):
        add(sending, 'Valve Engineer', 'Flowco', source='Workday', location='Munich, Germany')
        chat = self._run(monkeypatch)
        assert 'Direct company openings' in chat and 'Valve Engineer' in chat
        assert 'No job cleared the score bar' not in chat

    def test_a_day_with_only_a_regional_job(self, sending, monkeypatch):
        add(sending, 'Valve Engineer', 'Flowco', source='Infostud')
        chat = self._run(monkeypatch)
        assert 'Valve Engineer' in chat and 'from your region' in chat
        assert 'No job cleared the score bar' not in chat


class TestRender:
    def test_html_in_a_title_is_escaped_and_the_link_kept_whole(self):
        link = ("https://jobs.example.com/acme/744000153210880-very-long-posting-slug"
                "?gh_jid=5248196007&lang=en&note=it's\"quoted\"")
        out = telegram_sender._render_job_lines([
            (1, 'R&D <Lead>', 'A&B Pumps', 80, link, None, 'SmartRecruiters', 0)])
        assert 'R&amp;D &lt;Lead&gt;' in out
        assert '<Lead>' not in out
        assert 'A&amp;B Pumps' in out
        assert f'href="{html.escape(link, quote=True)}"' in out


class TestRunLogLinks:
    def test_display_results_prints_the_full_link(self, capsys):
        import job_search_smart
        link = 'https://jobs.smartrecruiters.com/AcmeGroup/744000153210880-sales-engineer-flow-control'
        searcher = job_search_smart.SmartJobSearcher.__new__(job_search_smart.SmartJobSearcher)
        searcher.display_results([{'title': 'Sales Engineer', 'company': 'Acme', 'link': link}])
        out = capsys.readouterr().out
        assert f'Link: {link}\n' in out
        assert '...' not in out
