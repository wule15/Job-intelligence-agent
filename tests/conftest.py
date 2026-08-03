"""
Shared fixtures.

Every test runs against temporary files. Nothing here reads the real
database, the real CVs or the real .env.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_filter import JobFilter  # noqa: E402


# A small skills profile standing in for the extracted CV cache.
# Two CVs so the per-CV scoring has something to choose between.
FAKE_SKILLS = {
    'cvs': {
        'Sales_Engineer': {
            'skills': {
                'valve sizing': 3, 'kv calculation': 3, 'p&id': 2,
                'atex': 2, 'pneumatic actuators': 2, 'technical sales': 3,
                'fluid systems': 3, 'commissioning': 2,
            },
        },
        'Technical_Writer': {
            'skills': {
                'technical writing': 4, 'seo': 3, 'documentation': 3,
                'content strategy': 3, 'b2b content': 2,
            },
        },
    },
    'linkedin': {},
    'merged_skills': {},
}


@pytest.fixture
def job_filter():
    """A JobFilter with a known skills profile, not the real CV cache."""
    jf = JobFilter()
    jf.skills_data = FAKE_SKILLS
    jf.all_skills = {
        s.lower()
        for cv in FAKE_SKILLS['cvs'].values()
        for s in cv['skills']
    }
    return jf


@pytest.fixture
def make_job():
    """Build a job dict with sensible defaults."""
    def _make(title='Sales Engineer', description='', company='Acme', **kw):
        job = {
            'title': title,
            'description': description,
            'company': company,
            'link': 'https://example.com/jobs/1',
            'source': 'Test',
        }
        job.update(kw)
        return job
    return _make
