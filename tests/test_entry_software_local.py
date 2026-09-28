"""
Entry-level software jobs from the home-market board are kept.

The candidate can apply to junior and mid-level software and QA roles on the
local board, but they score low against an engineering and sales CV, so the
pure-programming rule and the score cutoff dropped every one of them. Senior
roles stay filtered, and the same roles from global boards are unaffected, so
the main digest does not fill up with junior dev jobs from everywhere.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_filter import (  # noqa: E402
    ENTRY_SOFTWARE_FLOOR,
    is_entry_software_title,
)


def _job(title, source='Infostud', description='Short teaser.'):
    return {'title': title, 'company': 'Acme', 'description': description,
            'link': 'https://example.com/1', 'location': 'Serbia', 'source': source}


class TestEntrySoftwareTitle:
    def test_junior_and_unranked_dev_titles_count(self):
        for title in ('Junior Web Developer', 'Front-End Developer',
                      'QA Engineer', 'Manual Tester', 'Software Developer',
                      'Junior Java programer', 'Medior Python Developer'):
            assert is_entry_software_title(title), title

    def test_senior_titles_do_not_count(self):
        for title in ('Senior Software Engineer', 'Lead Frontend Developer',
                      'Principal Engineer, Software', 'Software Architect',
                      'Head of QA', 'Sr. Backend Developer',
                      'Staff Software Engineer'):
            assert not is_entry_software_title(title), title

    def test_non_software_titles_do_not_count(self):
        for title in ('Sales Manager', 'Maintenance Engineer', 'BIM Modeler'):
            assert not is_entry_software_title(title), title


class TestEntrySoftwareKept:
    def test_local_junior_dev_is_kept_at_the_digest_floor(self, job_filter):
        kept = job_filter.filter_jobs([_job('Junior Web Developer')], min_score=10)
        assert len(kept) == 1
        assert kept[0]['relevance_score'] >= ENTRY_SOFTWARE_FLOOR

    def test_local_senior_dev_is_still_filtered(self, job_filter):
        kept = job_filter.filter_jobs([_job('Senior Software Engineer')], min_score=10)
        assert kept == []

    def test_same_role_from_a_global_board_is_unchanged(self, job_filter):
        kept = job_filter.filter_jobs(
            [_job('Junior Web Developer', source='Remotive')], min_score=10)
        assert kept == []

    def test_dealbreakers_still_apply(self, job_filter):
        job = _job('Junior Web Developer',
                   description='US citizens only, ITAR controlled.')
        assert job_filter.filter_jobs([job], min_score=10) == []
