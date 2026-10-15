"""
Digest volume and ranking fixes of 16 October 2026.

Over 13 to 15 October the filters passed 227 jobs and the digest sent 50. The
company-board jobs the user wanted (two Danfoss key account roles at 36, a
Grundfos product owner role at 39) sat just under the bar of 40, while US
remote-board jobs scored 60 to 70. Only the top 20 of each day reached the run
log, so the rest could not be reviewed without opening the database. These
tests pin down the changes:

  - DIRECT_MIN_SCORE, a bar for the direct-company digest of its own. Blank
    follows DIGEST_MIN_SCORE when the digest runs.
  - MAIN_DIGEST_SIZE replaces the main digest's hard-coded 10 slots.
  - PRIORITY_COMPANIES and PRIORITY_COMPANY_BOOST lift named employers.
  - The run log lists every job that passed the filters, one line each.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_search_smart  # noqa: E402
import telegram_sender  # noqa: E402
from core.config import Config, count_setting, factor_setting  # noqa: E402
from core.database import Database  # noqa: E402
from core.job_filter import is_priority_company, priority_company_multiplier  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'fixes1610.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


@pytest.fixture
def chat(monkeypatch):
    """Captures what main() would send, and keeps the run offline."""
    sent = []
    monkeypatch.setattr(telegram_sender, 'send_telegram_message',
                        lambda message: sent.append(message) or True)
    monkeypatch.setattr(telegram_sender, 'is_job_live', lambda *args, **kw: True)
    monkeypatch.setattr(telegram_sender, '_health_note', lambda: '')
    return sent


def add(db, title, company, score, source='Free', location='Remote, Europe'):
    return db.add_job(title, company, 'desc', f'https://example.com/{title}/{company}'.replace(' ', '-'),
                      source=source, relevance_score=score, location=location)


# ── Settings ────────────────────────────────────────────────────────────────

class TestCountSetting:
    def test_unset_gives_the_default(self):
        assert count_setting(None, 10) == 10
        assert count_setting('  ', 10) == 10

    def test_a_number_is_read(self):
        assert count_setting('15', 10) == 15

    def test_a_typo_falls_back_without_raising(self, capsys):
        assert count_setting('ten', 10, 'MAIN_DIGEST_SIZE') == 10
        assert 'MAIN_DIGEST_SIZE' in capsys.readouterr().out

    def test_zero_is_raised_to_one(self):
        assert count_setting('0', 10) == 1


class TestFactorSetting:
    def test_unset_gives_the_default(self):
        assert factor_setting(None, 1.0) == 1.0

    def test_a_number_is_read(self):
        assert factor_setting('1.15', 1.0) == 1.15

    def test_it_can_lift_but_never_sink(self):
        assert factor_setting('0.5', 1.0) == 1.0
        assert factor_setting('3', 1.0) == 2.0

    def test_a_typo_falls_back_without_raising(self, capsys):
        assert factor_setting('high', 1.0, 'PRIORITY_COMPANY_BOOST') == 1.0
        assert 'PRIORITY_COMPANY_BOOST' in capsys.readouterr().out


class TestConfigProbe:
    @staticmethod
    def _load_config(monkeypatch, tmp_path, **env):
        """A fresh copy of core/config.py read with only `env` set, loaded under
        another name so the shared Config class is left alone."""
        import importlib.util
        source = Path(__file__).resolve().parent.parent / 'core' / 'config.py'
        copy = tmp_path / 'config_probe.py'
        copy.write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
        for key in ('DIGEST_MIN_SCORE', 'DIRECT_MIN_SCORE', 'MAIN_DIGEST_SIZE',
                    'PRIORITY_COMPANIES', 'PRIORITY_COMPANY_BOOST'):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        spec = importlib.util.spec_from_file_location('config_probe', copy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Config

    def test_defaults_change_nothing(self, monkeypatch, tmp_path):
        probe = self._load_config(monkeypatch, tmp_path)
        assert probe.DIRECT_MIN_SCORE is None
        assert probe.MAIN_DIGEST_SIZE == 10
        assert probe.PRIORITY_COMPANIES == []
        assert probe.PRIORITY_COMPANY_BOOST == 1.0

    def test_values_are_read(self, monkeypatch, tmp_path):
        probe = self._load_config(
            monkeypatch, tmp_path, DIRECT_MIN_SCORE='35', MAIN_DIGEST_SIZE='12',
            PRIORITY_COMPANIES='Grundfos, Atlas Copco', PRIORITY_COMPANY_BOOST='1.15')
        assert probe.DIRECT_MIN_SCORE == 35.0
        assert probe.MAIN_DIGEST_SIZE == 12
        assert probe.PRIORITY_COMPANIES == ['grundfos', 'atlas copco']
        assert probe.PRIORITY_COMPANY_BOOST == 1.15


# ── The direct digest's own bar ─────────────────────────────────────────────

class TestDirectBar:
    def test_blank_follows_the_main_bar_at_run_time(self, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        assert telegram_sender.direct_min_score() == 40.0

    def test_a_set_bar_wins(self, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'DIRECT_MIN_SCORE', 35.0)
        assert telegram_sender.direct_min_score() == 35.0

    def test_a_board_job_at_36_reaches_the_direct_digest(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'DIRECT_MIN_SCORE', 35.0)
        add(db, 'Key Account Manager', 'Danfoss', 36.0, source='SuccessFactors')
        add(db, 'Logistics Specialist', 'Danfoss', 30.0, source='SuccessFactors')
        assert [j[1] for j in telegram_sender.get_direct_jobs()] == ['Key Account Manager']

    def test_the_main_digest_keeps_its_own_bar(self, db, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'DIRECT_MIN_SCORE', 35.0)
        add(db, 'Remote Role', 'Board Co', 36.0)
        assert telegram_sender.get_unsent_jobs() == []

    def test_the_empty_note_names_the_direct_bar(self, db, chat, monkeypatch):
        monkeypatch.setattr(Config, 'DIGEST_MIN_SCORE', 40.0)
        monkeypatch.setattr(Config, 'DIRECT_MIN_SCORE', 35.0)
        telegram_sender.main()
        direct = [m for m in chat if 'Direct company openings' in m]
        assert direct and 'above score 35' in direct[0]
        assert any('Daily Job Digest' in m and 'above score 40' in m for m in chat)


# ── Main digest size ────────────────────────────────────────────────────────

class TestMainDigestSize:
    def _fill(self, db, count):
        for i in range(count):
            add(db, f'Role {i}', f'Company {i}', 60.0 - i)

    def _main_lines(self, capsys):
        return [l for l in capsys.readouterr().out.splitlines() if l.startswith('[main]')]

    def test_ten_by_default(self, db, chat, capsys):
        self._fill(db, 14)
        telegram_sender.main()
        assert len(self._main_lines(capsys)) == 10

    def test_the_setting_is_used(self, db, chat, monkeypatch, capsys):
        monkeypatch.setattr(Config, 'MAIN_DIGEST_SIZE', 12)
        self._fill(db, 14)
        telegram_sender.main()
        assert len(self._main_lines(capsys)) == 12


# ── Priority employers ──────────────────────────────────────────────────────

class TestPriorityCompany:
    def test_matches_whole_words_in_the_company_name(self):
        names = ['atlas copco', 'grundfos']
        assert is_priority_company('Atlas Copco Group', names)
        assert is_priority_company('GRUNDFOS Srbija d.o.o.', names)
        assert not is_priority_company('Copco Atlas Ltd', names)
        assert not is_priority_company('Grundfossil Energy', names)

    def test_nothing_is_lifted_by_default(self, make_job):
        assert priority_company_multiplier(make_job(company='Grundfos')) == 1.0

    def test_a_named_employer_gets_the_boost(self, make_job, monkeypatch):
        monkeypatch.setattr(Config, 'PRIORITY_COMPANIES', ['grundfos'])
        monkeypatch.setattr(Config, 'PRIORITY_COMPANY_BOOST', 1.15)
        assert priority_company_multiplier(make_job(company='Grundfos')) == 1.15
        assert priority_company_multiplier(make_job(company='Acme')) == 1.0

    def test_the_boost_moves_the_stored_score(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(Config, 'PRIORITY_COMPANIES', ['atlas copco'])
        monkeypatch.setattr(Config, 'PRIORITY_COMPANY_BOOST', 1.2)
        text = 'Technical sales of fluid systems, valve sizing and commissioning.'
        jobs = [make_job(title='Sales Engineer', description=text, company=c,
                         link=f'https://example.com/{i}', location='Antwerp, Belgium')
                for i, c in enumerate(('Atlas Copco Group', 'Other Industrial'))]
        kept = {j['company']: j['relevance_score'] for j in job_filter.filter_jobs(jobs, min_score=0)}
        assert kept['Atlas Copco Group'] > kept['Other Industrial']


# ── Every passing job in the run log ────────────────────────────────────────

class TestPassedLog:
    def test_line_format(self):
        line = job_search_smart.passed_line({
            'relevance_score': 39, 'source': 'SuccessFactors', 'title': 'Product Owner',
            'company': 'Grundfos', 'location': 'Bjerringbro, Denmark',
            'link': 'https://example.com/po'})
        assert line == ('[passed] score=39 | SuccessFactors | Product Owner | Grundfos'
                        ' | Bjerringbro, Denmark | https://example.com/po')

    def test_blanks_and_line_breaks_stay_on_one_line(self):
        line = job_search_smart.passed_line({'title': 'Sales\nEngineer', 'company': 'Acme'})
        assert '\n' not in line
        assert line == '[passed] score=0 | Unknown | Sales Engineer | Acme | - | -'

    def test_every_job_is_listed_not_only_the_top_20(self, capsys):
        jobs = [{'title': f'Role {i}', 'company': 'Acme', 'relevance_score': 60 - i,
                 'link': f'https://example.com/{i}'} for i in range(25)]
        searcher = job_search_smart.SmartJobSearcher.__new__(job_search_smart.SmartJobSearcher)
        searcher.display_results(jobs)
        out = capsys.readouterr().out
        passed = [l for l in out.splitlines() if l.startswith('[passed]')]
        assert len(passed) == 25
        assert 'ALL 25 JOBS THAT PASSED THE FILTERS' in out
        assert passed[0].startswith('[passed] score=60 ') and passed[-1].startswith('[passed] score=36 ')
