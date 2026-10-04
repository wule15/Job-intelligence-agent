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


@pytest.fixture(autouse=True)
def public_default_settings(monkeypatch):
    """Every test starts from the public defaults, not the user's settings.

    core/config.py reads core/.env when it is imported, so in a deployment
    folder the user's own countries, languages and score bars would leak in
    and decide what a test sees. A test that needs a setting sets it itself.
    """
    from core.config import Config
    defaults = {
        'ALLOWED_COUNTRIES': [], 'SPONSORSHIP_ONLY_COUNTRIES': [],
        'WORK_ELIGIBLE_REGIONS': [], 'EXCLUDED_LOCATIONS': [],
        'NON_FLUENT_LANGUAGES': [], 'UNREADABLE_ADVERT_LANGUAGES': [],
        'EXCLUDED_TITLE_TERMS': [], 'DROP_LOCAL_TRADE_TITLES': False,
        'MAX_REQUIRED_YEARS': None, 'DROP_ELECTRICAL_ONLY_DEGREE': False,
        'NON_EUROPE_PREFERENCE': 1.0, 'DIGEST_EXCLUDE_FILE': '',
        'DIGEST_MIN_SCORE': 15.0, 'REGIONAL_MIN_SCORE': 15.0,
        'EARLY_CAREER_QUERIES': False, 'DIGEST_LABEL': '',
        'REGIONAL_DIGEST_LABEL': 'Regional jobs', 'REGIONAL_JOB_LOCATIONS': [],
        'REGIONAL_MATCH_TERMS': [], 'REGIONAL_BOARDS': [], 'INFOSTUD_CITIES': [],
        'REGIONAL_QUERIES': [], 'TITLE_SCREEN_TERMS': [], 'SERPAPI_QUERIES': [],
    }
    for name, value in defaults.items():
        monkeypatch.setattr(Config, name, value, raising=False)
    # The sender keeps a module copy of the bar, read once at import.
    import telegram_sender
    monkeypatch.setattr(telegram_sender, 'MIN_DIGEST_SCORE', 15.0, raising=False)


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
