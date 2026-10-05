"""
The local dashboard: a neutral mark, and no letter left on a deleted job.

Every test runs against a temporary database. The dashboard module reads the
database path from Config each time it connects, so pointing Config at a
temporary file before the import keeps the real database closed.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config  # noqa: E402
from core.database import Database  # noqa: E402

TEMPLATE = ROOT / 'templates' / 'dashboard.html'


@pytest.fixture
def dashboard_client(tmp_path, monkeypatch):
    path = str(tmp_path / 'dashboard.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    database = Database(db_path=path)
    database.init_database()

    import dashboard  # first import runs its migrations on the path set above
    dashboard.run_migrations()
    dashboard.app.config['TESTING'] = True
    with dashboard.app.test_client() as client:
        yield client, database
    database.close()


class TestNeutralMark:
    """The public template carries no one's initials or name. The page is
    the same for whoever clones the repository."""

    def test_page_title_carries_no_initials(self):
        html = TEMPLATE.read_text(encoding='utf-8')
        title = re.search(r'<title>(.*?)</title>', html, re.S).group(1).strip()

        assert title == 'Job Dashboard'

    def test_mark_is_an_icon_with_no_letters(self):
        html = TEMPLATE.read_text(encoding='utf-8')
        mark = re.search(r'<div class="app-mark"[^>]*>(.*?)</div>', html, re.S)

        assert mark, 'the sidebar mark is missing'
        assert re.search(r'<i class="fa-', mark.group(1))
        assert re.sub(r'<[^>]+>', '', mark.group(1)).strip() == ''

    def test_no_initials_class_or_comment_is_left(self):
        html = TEMPLATE.read_text(encoding='utf-8')

        assert 'nv-logo' not in html
        assert not re.search(r'\bNV\b', html)

    def test_rendered_page_shows_the_neutral_mark(self, dashboard_client):
        client, database = dashboard_client
        database.add_job('Sales Engineer', 'Valveco', 'desc', 'https://example.com/1',
                         relevance_score=60.0)

        page = client.get('/').get_data(as_text=True)

        assert '<title>Job Dashboard</title>' in page
        assert 'class="app-mark"' in page
        assert not re.search(r'\bNV\b', page)


class TestDeletingAJob:
    def test_its_letter_record_goes_with_it(self, dashboard_client):
        client, database = dashboard_client
        job_id = database.add_job('Sales Engineer', 'Valveco', 'desc',
                                  'https://example.com/1')
        database.add_cover_letter(job_id, 'Sales Engineer', 'Valveco', 'auto', 'text')

        response = client.post('/api/job-delete', json={'job_id': job_id})

        assert response.get_json() == {'success': True}
        assert not database.job_has_cover_letter(job_id)

    def test_a_new_job_given_the_freed_id_has_no_letter(self, dashboard_client):
        # SQLite hands the highest deleted id to the next new row. A letter
        # left behind would then show on, and block a letter for, a job it
        # was never written for.
        client, database = dashboard_client
        old_id = database.add_job('Sales Engineer', 'Valveco', 'desc',
                                  'https://example.com/1')
        database.add_cover_letter(old_id, 'Sales Engineer', 'Valveco', 'auto', 'text')
        client.post('/api/job-delete', json={'job_id': old_id})

        new_id = database.add_job('Application Engineer', 'Pumpco', 'desc',
                                  'https://example.com/2')

        assert new_id == old_id
        assert not database.job_has_cover_letter(new_id)
        assert database.get_jobs_without_cover_letters(limit=5)[0][0] == new_id
