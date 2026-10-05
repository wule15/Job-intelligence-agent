"""
Cover letters: every letter belongs to a stored job, and none is paid for twice.

A letter costs one Claude API call. It is filed under the id of the job's row
in the database, the same id the dashboard and the cleanup read. Before this
was pinned down, the --search path filed every letter under no id at all: the
search results carry no database id, the insert failed quietly, and the next
run paid for the same letters again under a file named cover_letter_None_.

No test here calls the API. The generator is a fake that counts its calls.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_search_smart  # noqa: E402
import validate_system  # noqa: E402
import write_cover_letters  # noqa: E402
from core.config import Config  # noqa: E402
from core.database import Database  # noqa: E402


class FakeGenerator:
    """Stands in for CoverLetterGenerator and counts the paid calls."""

    calls = []

    def __init__(self):
        pass

    def generate_cover_letter(self, title, company, description,
                              selected_cv=None, cv_name_hint=None):
        FakeGenerator.calls.append((title, company))
        return f"Dear Hiring Manager,\nA letter for {title} at {company}."

    def format_cover_letter(self, title, company, text):
        return text


@pytest.fixture(autouse=True)
def fresh_counter():
    FakeGenerator.calls = []


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / 'letters.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    monkeypatch.setattr(Config, 'OUTPUT_DIR', tmp_path / 'output')
    monkeypatch.setattr(Config, 'TELEGRAM_BOT_TOKEN', '', raising=False)
    monkeypatch.setattr(Config, 'TELEGRAM_CHAT_ID', '', raising=False)
    monkeypatch.setattr(write_cover_letters.time, 'sleep', lambda s: None)
    return path


@pytest.fixture
def db(db_path):
    database = Database(db_path=db_path)
    yield database
    database.close()


def letter_files():
    folder = Path(Config.OUTPUT_DIR) / 'docx cover letters'
    return sorted(p.name for p in folder.glob('*.docx')) if folder.exists() else []


def letter_rows(path):
    with Database(db_path=path) as database:
        rows = database.connection.execute(
            'SELECT job_id FROM cover_letters_sent ORDER BY job_id').fetchall()
        return [row[0] for row in rows]


def stored(db, title, company, score=50.0):
    return db.add_job(title, company, f'{title} at {company}.',
                      f'https://example.com/{title}/{company}',
                      relevance_score=score)


def as_letter_job(job_id, title, company):
    return {'id': job_id, 'title': title, 'company': company,
            'description': 'desc', 'link': 'https://example.com', 'best_cv': None}


class TestWriteLetter:
    def test_letter_is_filed_under_the_stored_job_id(self, db):
        job_id = stored(db, 'Sales Engineer', 'Valveco')

        path = write_cover_letters.write_letter(
            FakeGenerator(), db, as_letter_job(job_id, 'Sales Engineer', 'Valveco'))

        assert path.name.startswith(f'cover_letter_{job_id}_')
        assert db.job_has_cover_letter(job_id)

    def test_a_second_run_does_not_pay_for_the_same_letter(self, db):
        job_id = stored(db, 'Sales Engineer', 'Valveco')
        job = as_letter_job(job_id, 'Sales Engineer', 'Valveco')

        first = write_cover_letters.write_letter(FakeGenerator(), db, job)
        second = write_cover_letters.write_letter(FakeGenerator(), db, job)

        assert first is not None
        assert second is None
        assert len(FakeGenerator.calls) == 1
        assert len(letter_files()) == 1

    def test_a_job_with_no_id_is_never_written(self, db, db_path):
        path = write_cover_letters.write_letter(
            FakeGenerator(), db, as_letter_job(None, 'Sales Engineer', 'Valveco'))

        assert path is None
        assert FakeGenerator.calls == []
        assert letter_files() == []
        assert letter_rows(db_path) == []

    def test_an_id_that_is_not_a_stored_job_is_never_written(self, db, db_path):
        stored(db, 'Sales Engineer', 'Valveco')

        path = write_cover_letters.write_letter(
            FakeGenerator(), db, as_letter_job(999, 'Sales Engineer', 'Valveco'))

        assert path is None
        assert FakeGenerator.calls == []
        assert letter_rows(db_path) == []

    def test_a_title_with_characters_windows_refuses_still_saves(self, db):
        # Windows either refuses these characters, so the save failed and the
        # next run paid again, or reads a colon as a hidden side stream, so
        # the letter landed in an empty-looking file cut off at the colon.
        title = 'Engineer: Valves / Pumps? "Remote" <EU> | *'
        job_id = stored(db, title, 'Valveco')

        path = write_cover_letters.write_letter(
            FakeGenerator(), db, as_letter_job(job_id, title, 'Valveco'))

        assert path is not None
        assert not set('<>:"/\\|?*') & set(path.name)
        assert path.name in letter_files()
        assert path.stat().st_size > 0
        assert db.job_has_cover_letter(job_id)


class TestSearchResults:
    def test_results_resolve_to_their_stored_ids(self, db):
        first = stored(db, 'Sales Engineer', 'Valveco')
        second = stored(db, 'Application Engineer', 'Pumpco')
        found = [
            {'title': 'Sales Engineer', 'company': 'Valveco', 'description': 'a'},
            {'title': 'Application Engineer', 'company': 'Pumpco', 'description': 'b'},
        ]

        jobs = write_cover_letters.letter_jobs_from_results(db, found, limit=5)

        assert [job['id'] for job in jobs] == [first, second]

    def test_an_id_the_board_sent_is_not_taken_as_the_job_id(self, db):
        # Some boards send their own posting id. It must never stand in for
        # the database id, or a letter lands on a different job.
        other = stored(db, 'Mechanical Engineer', 'Gearco')
        mine = stored(db, 'Sales Engineer', 'Valveco')
        found = [{'id': other, 'title': 'Sales Engineer', 'company': 'Valveco'}]

        jobs = write_cover_letters.letter_jobs_from_results(db, found, limit=5)

        assert [job['id'] for job in jobs] == [mine]

    def test_a_result_that_was_not_stored_is_skipped(self, db):
        found = [{'title': 'Sales Engineer', 'company': 'Valveco'}]

        assert write_cover_letters.letter_jobs_from_results(db, found, limit=5) == []

    def test_a_result_that_already_has_a_letter_is_skipped(self, db):
        lettered = stored(db, 'Sales Engineer', 'Valveco')
        fresh = stored(db, 'Application Engineer', 'Pumpco')
        db.add_cover_letter(lettered, 'Sales Engineer', 'Valveco', 'auto', 'text')
        found = [
            {'title': 'Sales Engineer', 'company': 'Valveco'},
            {'title': 'Application Engineer', 'company': 'Pumpco'},
        ]

        jobs = write_cover_letters.letter_jobs_from_results(db, found, limit=5)

        assert [job['id'] for job in jobs] == [fresh]

    def test_the_same_job_twice_in_the_results_is_written_once(self, db):
        job_id = stored(db, 'Sales Engineer', 'Valveco')
        found = [
            {'title': 'Sales Engineer', 'company': 'Valveco'},
            {'title': 'Sales Engineer (Remote)', 'company': 'Valveco Inc'},
        ]

        jobs = write_cover_letters.letter_jobs_from_results(db, found, limit=5)

        assert [job['id'] for job in jobs] == [job_id]

    def test_the_limit_counts_jobs_that_will_be_written(self, db):
        lettered = stored(db, 'Sales Engineer', 'Valveco')
        db.add_cover_letter(lettered, 'Sales Engineer', 'Valveco', 'auto', 'text')
        fresh = stored(db, 'Application Engineer', 'Pumpco')
        found = [
            {'title': 'Sales Engineer', 'company': 'Valveco'},
            {'title': 'Application Engineer', 'company': 'Pumpco'},
        ]

        jobs = write_cover_letters.letter_jobs_from_results(db, found, limit=1)

        assert [job['id'] for job in jobs] == [fresh]


class FakeSearcher:
    """Stores its results the way the real search does, and returns them
    without a database id, as the real search does."""

    RESULTS = [
        {'title': 'Sales Engineer', 'company': 'Valveco', 'description': 'a',
         'link': 'https://example.com/1', 'relevance_score': 60.0},
        {'title': 'Application Engineer', 'company': 'Pumpco', 'description': 'b',
         'link': 'https://example.com/2', 'relevance_score': 55.0},
    ]

    def search_all_sources(self):
        database = Database()
        for job in self.RESULTS:
            database.add_job(job['title'], job['company'], job['description'],
                             job['link'], relevance_score=job['relevance_score'])
        return [dict(job) for job in self.RESULTS]


class TestRerunningTheScripts:
    def run_writer(self, monkeypatch, *args):
        monkeypatch.setattr(write_cover_letters, 'CoverLetterGenerator', FakeGenerator)
        monkeypatch.setattr(sys, 'argv', ['write_cover_letters.py', *args])
        return write_cover_letters.main()

    def test_default_path_rerun_writes_nothing_new(self, db_path, monkeypatch):
        with Database(db_path=db_path) as database:
            ids = [stored(database, 'Sales Engineer', 'Valveco'),
                   stored(database, 'Application Engineer', 'Pumpco')]

        self.run_writer(monkeypatch)
        self.run_writer(monkeypatch)

        assert len(FakeGenerator.calls) == 2
        assert letter_rows(db_path) == sorted(ids)
        assert len(letter_files()) == 2

    def test_search_path_files_letters_under_real_ids_and_rerun_writes_nothing(
            self, db_path, monkeypatch):
        monkeypatch.setattr(job_search_smart, 'SmartJobSearcher', FakeSearcher)

        self.run_writer(monkeypatch, '--search')
        self.run_writer(monkeypatch, '--search')

        assert len(FakeGenerator.calls) == 2
        rows = letter_rows(db_path)
        assert None not in rows and len(rows) == 2
        assert not any('None' in name for name in letter_files())
        assert len(letter_files()) == 2

    def test_validate_system_files_under_real_ids_and_never_repeats(
            self, db_path, monkeypatch):
        monkeypatch.setattr(validate_system, 'SmartJobSearcher', FakeSearcher)
        monkeypatch.setattr(validate_system, 'CoverLetterGenerator', FakeGenerator)

        assert validate_system.main() == 0
        assert validate_system.main() == 0

        assert len(FakeGenerator.calls) == 2
        rows = letter_rows(db_path)
        assert None not in rows and len(rows) == 2
        assert len(letter_files()) == 2
