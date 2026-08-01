"""
Tests for storage and deduplication.

Every test uses a temporary database file. The real job_digest.db is never
opened.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(db_path=str(tmp_path / 'test.db'))
    database.init_database()
    yield database
    database.close()


class TestDeduplication:
    def test_new_job_is_inserted(self, db):
        job_id = db.add_job('Sales Engineer', 'Acme', 'desc',
                            'https://example.com/1', source='Test')
        assert job_id is not None

    def test_same_title_and_company_does_not_duplicate(self, db):
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1')
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/2')

        cursor = db.connection.cursor()
        cursor.execute(
            'SELECT COUNT(*) FROM jobs WHERE job_title=? AND company=?',
            ('Sales Engineer', 'Acme'))
        assert cursor.fetchone()[0] == 1

    def test_repeat_increments_seen_count(self, db):
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1')
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/2')

        cursor = db.connection.cursor()
        cursor.execute(
            'SELECT seen_count FROM jobs WHERE job_title=? AND company=?',
            ('Sales Engineer', 'Acme'))
        assert cursor.fetchone()[0] == 2

    def test_higher_score_wins_on_repeat(self, db):
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1',
                   relevance_score=20.0, best_cv='CV_A')
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/2',
                   relevance_score=55.0, best_cv='CV_B')

        cursor = db.connection.cursor()
        cursor.execute(
            'SELECT relevance_score, best_cv FROM jobs WHERE job_title=?',
            ('Sales Engineer',))
        score, cv = cursor.fetchone()
        assert score == 55.0
        assert cv == 'CV_B'

    def test_lower_score_does_not_overwrite(self, db):
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1',
                   relevance_score=55.0, best_cv='CV_B')
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/2',
                   relevance_score=20.0, best_cv='CV_A')

        cursor = db.connection.cursor()
        cursor.execute(
            'SELECT relevance_score, best_cv FROM jobs WHERE job_title=?',
            ('Sales Engineer',))
        score, cv = cursor.fetchone()
        assert score == 55.0
        assert cv == 'CV_B'

    def test_different_company_is_a_different_job(self, db):
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1')
        db.add_job('Sales Engineer', 'Globex', 'desc', 'https://example.com/2')

        cursor = db.connection.cursor()
        cursor.execute('SELECT COUNT(*) FROM jobs')
        assert cursor.fetchone()[0] == 2

    def test_decorated_title_is_the_same_job(self, db):
        """
        Storage dedups on the normalised key, not the raw strings.

        This test previously documented the opposite as a known limitation.
        The unique index now sits on dedup_key, so these collapse to one row.
        """
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1')
        db.add_job('Sales Engineer (Remote)', 'Acme Inc', 'desc', 'https://example.com/2')

        cursor = db.connection.cursor()
        cursor.execute('SELECT COUNT(*) FROM jobs')
        assert cursor.fetchone()[0] == 1

        cursor.execute('SELECT seen_count FROM jobs')
        assert cursor.fetchone()[0] == 2

    def test_seniority_is_not_collapsed(self, db):
        """The dangerous direction. These are two real, different jobs."""
        db.add_job('Sales Engineer', 'Acme', 'desc', 'https://example.com/1')
        db.add_job('Senior Sales Engineer', 'Acme', 'desc', 'https://example.com/2')

        cursor = db.connection.cursor()
        cursor.execute('SELECT COUNT(*) FROM jobs')
        assert cursor.fetchone()[0] == 2


class TestLinkTracking:
    def test_unseen_link_reports_false(self, db):
        assert db.is_job_link_seen('https://example.com/never') is False

    def test_marked_link_reports_true(self, db):
        db.mark_job_link_seen('https://example.com/1', job_id=None)
        assert db.is_job_link_seen('https://example.com/1') is True

    def test_marking_twice_does_not_raise(self, db):
        db.mark_job_link_seen('https://example.com/1')
        db.mark_job_link_seen('https://example.com/1')
        assert db.is_job_link_seen('https://example.com/1') is True
