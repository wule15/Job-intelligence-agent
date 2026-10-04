"""
Daily volume: a score bar that can be set, and early-career queries.

The digest used to send its slot count every day, 10 + 10 + up to 15,
because the score bar was a constant of 15 that almost every stored job
cleared. These tests pin down the changes:

  - The bars come from .env (DIGEST_MIN_SCORE, REGIONAL_MIN_SCORE), with the
    old value of 15 as the default. A typo falls back to the default instead
    of stopping the morning run.
  - Each digest reads its bar when it runs, not when the module is imported.
  - A day where nothing clears the bar sends no jobs, only one short note,
    so a quiet day can be told apart from a run that never happened.
  - Every sent job is logged with its score, so a bar can later be chosen
    from the run log without reading the database.
  - Entry-level software jobs from the home-market board stay at the
    regional bar, whatever it is set to.
  - An optional block of graduate, junior, trainee, associate and intern
    queries, with one of them rotated to the head of the list each day so
    it reaches the sources that only read the first few queries.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_search_smart  # noqa: E402
import telegram_sender  # noqa: E402
from core.config import Config, flag_setting, score_setting  # noqa: E402
from core.database import Database  # noqa: E402

from conftest import FAKE_SKILLS  # noqa: E402

EARLY_WORDS = re.compile(r'\b(graduate|junior|trainee|intern|associate)\b', re.I)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'volume.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


def add(db, title, company, score, source='Free', location='Remote, Europe'):
    return db.add_job(title, company, 'desc', f'https://example.com/{title}/{company}'.replace(' ', '-'),
                      source=source, relevance_score=score, location=location)


# ── Settings ────────────────────────────────────────────────────────────────

class TestScoreSetting:
    def test_unset_gives_the_default(self):
        assert score_setting(None, 15.0) == 15.0
        assert score_setting('', 15.0) == 15.0
        assert score_setting('   ', 15.0) == 15.0

    def test_a_number_is_read(self):
        assert score_setting('40', 15.0) == 40.0
        assert score_setting(' 27.5 ', 15.0) == 27.5

    def test_a_typo_falls_back_without_raising(self, capsys):
        assert score_setting('abc', 15.0, 'DIGEST_MIN_SCORE') == 15.0
        assert 'DIGEST_MIN_SCORE' in capsys.readouterr().out

    def test_out_of_range_is_clamped(self):
        assert score_setting('150', 15.0) == 100.0
        assert score_setting('-5', 15.0) == 0.0

    def test_regional_bar_defaults_to_the_main_bar(self):
        main_bar = score_setting('40', 15.0)
        assert score_setting(None, main_bar) == 40.0
        assert score_setting('12', main_bar) == 12.0

    def test_config_holds_floats_in_range(self):
        for value in (Config.DIGEST_MIN_SCORE, Config.REGIONAL_MIN_SCORE):
            assert isinstance(value, float) and 0.0 <= value <= 100.0

    @staticmethod
    def _load_config(monkeypatch, tmp_path, **env):
        """A fresh copy of core/config.py read with only `env` set, loaded
        under another name so the shared Config class is left alone. A
        temporary project root keeps a local .env out of it."""
        import importlib.util
        source = Path(__file__).resolve().parent.parent / 'core' / 'config.py'
        copy = tmp_path / 'config_probe.py'
        copy.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        for key in ('DIGEST_MIN_SCORE', 'REGIONAL_MIN_SCORE'):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        spec = importlib.util.spec_from_file_location('config_probe', copy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Config

    def test_the_default_bar_is_unchanged(self, monkeypatch, tmp_path):
        probe = self._load_config(monkeypatch, tmp_path)
        assert probe.DIGEST_MIN_SCORE == 15.0
        assert probe.REGIONAL_MIN_SCORE == 15.0

    def test_the_regional_bar_follows_the_main_bar(self, monkeypatch, tmp_path):
        probe = self._load_config(monkeypatch, tmp_path, DIGEST_MIN_SCORE='40')
        assert probe.DIGEST_MIN_SCORE == 40.0
        assert probe.REGIONAL_MIN_SCORE == 40.0


class TestFlagSetting:
    def test_on_values(self):
        for raw in ('1', 'true', 'TRUE', 'yes', 'on', ' On '):
            assert flag_setting(raw) is True, raw

    def test_everything_else_is_off(self):
        for raw in (None, '', '0', 'false', 'no', 'off', 'maybe'):
            assert flag_setting(raw) is False, raw


# ── The bar is read when each digest runs ───────────────────────────────────

class TestBarAtCallTime:
    def test_module_constant_mirrors_the_setting(self):
        assert telegram_sender.MIN_DIGEST_SCORE == Config.DIGEST_MIN_SCORE

    def test_main_digest(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        add(db, 'Below Bar', 'Co A', 35.0)
        add(db, 'Above Bar', 'Co B', 45.0)
        assert [j[1] for j in telegram_sender.get_unsent_jobs()] == ['Above Bar']

    def test_direct_digest(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        add(db, 'Below Bar', 'Co A', 35.0, source='Workday')
        add(db, 'Above Bar', 'Co B', 45.0, source='Workday')
        assert [j[1] for j in telegram_sender.get_direct_jobs()] == ['Above Bar']

    def test_regional_digest_uses_its_own_bar(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 20.0)
        monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['testland'])
        monkeypatch.setattr(Config, 'REGIONAL_JOB_LOCATIONS', [])
        add(db, 'Home Role', 'Local Co', 25.0, location='Testgrad, Testland')
        add(db, 'Too Weak', 'Other Co', 15.0, location='Testgrad, Testland')
        assert [j[1] for j in telegram_sender.get_regional_jobs()] == ['Home Role']

    def test_an_explicit_bar_still_wins(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        add(db, 'Mid', 'Co A', 30.0)
        assert [j[1] for j in telegram_sender.get_unsent_jobs(min_score=20)] == ['Mid']


# ── A full send step ────────────────────────────────────────────────────────

@pytest.fixture
def chat(monkeypatch):
    """Captures what main() would send, and keeps the run offline."""
    sent = []

    def fake_send(message):
        sent.append(message)
        return True

    monkeypatch.setattr(telegram_sender, 'send_telegram_message', fake_send)
    monkeypatch.setattr(telegram_sender, 'is_job_live', lambda *args, **kw: True)
    monkeypatch.setattr(telegram_sender, '_health_note', lambda: '')
    monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['testland'])
    monkeypatch.setattr(Config, 'REGIONAL_JOB_LOCATIONS', [])
    return sent


def sent_history_count(db):
    return db.connection.execute('SELECT COUNT(*) FROM sent_history').fetchone()[0]


class TestZeroDay:
    def test_nothing_above_the_bar_sends_no_jobs(self, db, chat, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 40.0)
        add(db, 'Board Role', 'Co A', 30.0, source='Workday')
        add(db, 'Aggregator Role', 'Co B', 35.0)
        add(db, 'Home Role', 'Co C', 25.0, location='Testgrad, Testland')
        telegram_sender.main()
        assert len(chat) == 1
        assert 'No job cleared the score bar' in chat[0]
        assert not any(t in chat[0] for t in ('Board Role', 'Aggregator Role', 'Home Role'))
        assert sent_history_count(db) == 0

    def test_a_day_with_jobs_sends_no_quiet_note(self, db, chat, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        add(db, 'Aggregator Role', 'Co B', 55.0)
        telegram_sender.main()
        assert chat and not any('No job cleared the score bar' in m for m in chat)

    def test_the_quiet_note_carries_the_health_note(self, db, chat, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(telegram_sender, '_health_note',
                            lambda: '\n⚠️ <b>Source health</b>\n• Apify down\n')
        telegram_sender.main()
        assert len(chat) == 1
        assert 'No job cleared the score bar' in chat[0] and 'Apify down' in chat[0]


class TestRunLogScores:
    def test_every_sent_line_carries_its_score(self, db, chat, monkeypatch, capsys):
        add(db, 'Board Role', 'Co A', 61.0, source='Workday')
        add(db, 'Aggregator Role', 'Co B', 52.0)
        add(db, 'Home Role', 'Co C', 33.0, location='Testgrad, Testland')
        telegram_sender.main()
        out = capsys.readouterr().out.splitlines()
        lines = [l for l in out if l.startswith(('[direct]', '[regional]', '[main]'))]
        assert len(lines) == 3
        scores = {l.split(' | ')[2]: l.split(' | ')[1] for l in lines}
        assert scores == {'Board Role': 'score=61', 'Aggregator Role': 'score=52',
                          'Home Role': 'score=33'}

    def test_the_bars_are_printed_once(self, db, chat, monkeypatch, capsys):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 30.0)
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 22.5)
        telegram_sender.main()
        bar_lines = [l for l in capsys.readouterr().out.splitlines()
                     if l.startswith('[*] Score bar')]
        assert bar_lines == ['[*] Score bar: main/direct 30, regional 22.5']


# ── Entry-level software keeps up with the regional bar ─────────────────────

def _infostud_job(title):
    return {'title': title, 'company': 'Acme', 'description': 'Short teaser.',
            'link': 'https://example.com/1', 'location': 'Testgrad, Testland',
            'source': 'Infostud'}


class TestEntrySoftwareFollowsTheBar:
    def test_floor_rises_with_the_regional_bar(self, job_filter, monkeypatch):
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 35.0)
        kept = job_filter.filter_jobs([_infostud_job('Junior Web Developer')], min_score=10)
        assert len(kept) == 1 and kept[0]['relevance_score'] >= 35

    def test_lifted_job_reaches_the_regional_digest(self, db, job_filter, monkeypatch):
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 35.0)
        monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['testland'])
        monkeypatch.setattr(Config, 'REGIONAL_JOB_LOCATIONS', [])
        job = job_filter.filter_jobs([_infostud_job('Junior Web Developer')], min_score=10)[0]
        add(db, job['title'], job['company'], job['relevance_score'],
            source='Infostud', location=job['location'])
        assert [j[1] for j in telegram_sender.get_regional_jobs()] == ['Junior Web Developer']

    def test_a_low_bar_keeps_the_old_floor(self, job_filter, monkeypatch):
        monkeypatch.setattr(Config, 'REGIONAL_MIN_SCORE', 5.0)
        kept = job_filter.filter_jobs([_infostud_job('Junior Web Developer')], min_score=10)
        assert kept[0]['relevance_score'] >= 15


# ── Early-career queries ────────────────────────────────────────────────────

CV_SKILLS = {
    'cvs': {
        'Engineer': {'skills': {'cfd simulation': 3, 'valve sizing': 3,
                                'technical sales': 2, 'process design': 2}},
    },
    'linkedin': {},
    'merged_skills': {},
}


def searcher_with(skills):
    searcher = job_search_smart.SmartJobSearcher.__new__(job_search_smart.SmartJobSearcher)
    searcher.skills_data = skills
    return searcher


def early(queries):
    return [q for q in queries if EARLY_WORDS.search(q)]


class TestEarlyCareerQueries:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', False)
        assert early(searcher_with(CV_SKILLS).build_search_queries(day=1)) == []

    def test_on_adds_the_engineering_and_sales_terms(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        queries = searcher_with(CV_SKILLS).build_search_queries(day=1)
        for term in ('graduate mechanical engineer', 'junior process engineer',
                     'junior CFD engineer', 'junior thermal engineer',
                     'junior sales engineer'):
            assert term in queries, term

    def test_sales_terms_need_sales_skills(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        no_sales = {'cvs': {'Engineer': {'skills': {'cfd simulation': 3, 'valve sizing': 3}}},
                    'linkedin': {}, 'merged_skills': {}}
        queries = early(searcher_with(no_sales).build_search_queries(day=1))
        assert queries and not any('sales' in q for q in queries)

    def test_thermal_and_cfd_terms_need_cfd_or_domain_skills(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        no_cfd = {'cvs': {'Engineer': {'skills': {'valve sizing': 3, 'technical sales': 2}}},
                  'linkedin': {}, 'merged_skills': {}}
        queries = early(searcher_with(no_cfd).build_search_queries(day=1))
        assert queries
        assert not any('thermal' in q or 'cfd' in q.lower() for q in queries)

    def test_shape_and_size(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        queries = early(searcher_with(CV_SKILLS).build_search_queries(day=1))
        assert 0 < len(queries) <= 15
        for q in queries:
            assert len(q.split()) >= 3, q
            assert not q.startswith('remote '), q

    def test_daily_rotation_puts_one_at_the_head(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        searcher = searcher_with(CV_SKILLS)
        day1 = searcher.build_search_queries(day=1)
        day2 = searcher.build_search_queries(day=2)
        assert EARLY_WORDS.search(day1[0]) and EARLY_WORDS.search(day2[0])
        assert day1[0] != day2[0]
        assert sorted(day1) == sorted(day2)

    def test_order_is_deterministic_and_longest_first(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        searcher = searcher_with(CV_SKILLS)
        first = searcher.build_search_queries(day=3)
        assert first == searcher.build_search_queries(day=3)
        rest = first[1:]
        assert rest == sorted(rest, key=lambda q: (-len(q), q))

    def test_flag_off_order_is_longest_first_with_ties_alphabetical(self, monkeypatch):
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', False)
        queries = searcher_with(CV_SKILLS).build_search_queries(day=3)
        assert queries == sorted(queries, key=lambda q: (-len(q), q))


class TestQueryTiers:
    def test_specific_tier_keeps_the_incoming_order(self):
        queries = ['junior sales engineer', 'remote business development representative',
                   'remote sales development representative', 'sales engineer', 'remote engineer']
        specific, broad = job_search_smart.split_query_tiers(queries)
        assert specific == queries[:3]
        assert broad == ['sales engineer', 'remote engineer']


class Recorder:
    """A source that records the queries it was given and returns nothing."""

    def __init__(self):
        self.queries = []

    def search_jobs(self, query, num_pages=2):
        self.queries.append(query)
        return []

    def search_all(self, queries=None, *args, **kwargs):
        if isinstance(queries, list):
            self.queries.extend(queries)
        return []

    def process_draft_jobs(self):
        return []


class TestPromotedQueryReachesCappedSources:
    def test_jsearch_and_linkedin_get_the_promoted_query_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Config, 'DATABASE_PATH', str(tmp_path / 'search.db'))
        monkeypatch.setattr(Config, 'EARLY_CAREER_QUERIES', True)
        monkeypatch.setattr(Config, 'JSEARCH_BUDGET', 5)
        searcher = searcher_with(CV_SKILLS)
        for name in ('ats', 'jsearch', 'free_search', 'linkedin', 'serpapi',
                     'apify', 'ddg', 'gmail'):
            setattr(searcher, name, Recorder())
        searcher.filter = job_search_smart.JobFilter()
        searcher.filter.skills_data = FAKE_SKILLS
        searcher.filter.all_skills = set()
        searcher.validator = job_search_smart.JobValidator()
        searcher.db = Database()
        searcher.db.init_database()

        head = searcher.build_search_queries()[0]
        searcher.search_all_sources()

        assert EARLY_WORDS.search(head)
        assert len(searcher.jsearch.queries) == 5
        assert searcher.jsearch.queries[0] == head
        assert searcher.linkedin.queries[0] == head
        assert searcher.free_search.queries[0] == head
