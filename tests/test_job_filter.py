"""
Tests for scoring and filtering.

filter_jobs is the only rejection path in the pipeline. These tests exist so
that stays true, and so a change to the keyword lists cannot quietly start
throwing away everything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_filter import (  # noqa: E402
    is_geo_restricted,
    is_non_english_title,
    TITLE_BOOST_MULTIPLIER,
)


class TestScoring:
    def test_no_text_at_all_scores_zero(self, job_filter):
        score, cv = job_filter.score_job_with_cv('', '', '')
        assert score == 0
        assert cv is None

    def test_title_only_still_scores(self, job_filter):
        """
        LinkedIn guest cards and the SmartRecruiters list endpoint return a
        title and no description. Returning 0 for those discarded every job
        from two working sources.
        """
        score, cv = job_filter.score_job_with_cv(
            'Valve Sizing Engineer', '', 'Acme Fluid Systems')
        assert score > 0
        assert cv is not None

    def test_description_scores_higher_than_title_alone(self, job_filter):
        """A title-only match is weaker evidence and should rank lower."""
        title_only, _ = job_filter.score_job_with_cv('Sales Engineer', '', 'Acme')
        with_desc, _ = job_filter.score_job_with_cv(
            'Sales Engineer',
            'Valve sizing, Kv calculation, P&ID, ATEX and commissioning.',
            'Acme')
        assert with_desc > title_only

    def test_matching_skills_raise_the_score(self, job_filter):
        low, _ = job_filter.score_job_with_cv(
            'Analyst', 'Spreadsheets and reporting.', 'Acme')
        high, _ = job_filter.score_job_with_cv(
            'Analyst', 'Valve sizing, Kv calculation, P&ID and ATEX work.', 'Acme')
        assert high > low

    def test_best_cv_is_the_one_that_matches(self, job_filter):
        _, cv = job_filter.score_job_with_cv(
            'Writer', 'Technical writing, SEO and documentation.', 'Acme')
        assert cv == 'Technical_Writer'

        _, cv = job_filter.score_job_with_cv(
            'Engineer', 'Valve sizing, Kv calculation and commissioning.', 'Acme')
        assert cv == 'Sales_Engineer'

    def test_title_boost_applies(self, job_filter):
        """
        A target role in the title scores higher than a neutral one.

        Only the direction is asserted, not the exact ratio. score_job_with_cv
        scores the title as part of the job text, so the two calls do not
        share a base score and the multipliers do not compose cleanly.
        """
        desc = 'Valve sizing, Kv calculation and P&ID work.'
        plain, _ = job_filter.score_job_with_cv('Analyst', desc, 'Acme')
        boosted, _ = job_filter.score_job_with_cv('Sales Engineer', desc, 'Acme')
        assert boosted > plain
        assert boosted <= 100

    def test_title_boost_multiplier_is_a_boost(self):
        """Guard against the multiplier being set to 1.0 or below."""
        assert TITLE_BOOST_MULTIPLIER > 1.0

    def test_score_never_exceeds_100(self, job_filter):
        desc = ' '.join(job_filter.all_skills) + ' industrial valve fluid'
        score, _ = job_filter.score_job_with_cv('Sales Engineer', desc, 'Acme')
        assert score <= 100


class TestGeoRestriction:
    def test_us_only_is_blocked(self):
        assert is_geo_restricted('Engineer', 'Must be authorized to work in the US.', '')

    def test_allow_phrase_overrides_the_block(self):
        assert not is_geo_restricted(
            'Engineer', 'US preferred, but open to candidates across EMEA.', '')

    def test_unrestricted_job_passes(self):
        assert not is_geo_restricted('Engineer', 'Fully remote role.', '')

    def test_empty_input_passes(self):
        assert not is_geo_restricted('', '', '')


class TestNonEnglishTitles:
    def test_german_title_is_flagged(self):
        assert is_non_english_title('Vertriebsingenieur (m/w/d)')

    def test_english_title_is_not_flagged(self):
        assert not is_non_english_title('Sales Engineer')

    def test_empty_title_is_not_flagged(self):
        assert not is_non_english_title('')

    def test_marker_must_start_a_word(self):
        """Whole word matching, so ordinary English is not caught."""
        assert not is_non_english_title('Senior Content Lead')


class TestFilterJobs:
    """filter_jobs is the single rejection path. Prove each rule fires."""

    def test_dealbreaker_is_rejected(self, job_filter, make_job):
        jobs = [make_job(description='Active security clearance required. Valve sizing.')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_geo_restricted_is_rejected(self, job_filter, make_job):
        jobs = [make_job(description='Must be authorized to work in the US. Valve sizing.')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_non_english_title_is_rejected(self, job_filter, make_job):
        jobs = [make_job(title='Vertriebsingenieur',
                         description='Valve sizing and Kv calculation.')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_below_min_score_is_rejected(self, job_filter, make_job):
        jobs = [make_job(description='Completely unrelated role about catering.')]
        assert job_filter.filter_jobs(jobs, min_score=50) == []

    def test_good_job_survives(self, job_filter, make_job):
        jobs = [make_job(description='Valve sizing, Kv calculation, P&ID and ATEX.')]
        kept = job_filter.filter_jobs(jobs, min_score=0)
        assert len(kept) == 1
        assert kept[0]['relevance_score'] > 0
        assert kept[0]['best_cv'] == 'Sales_Engineer'

    def test_gmail_draft_bypasses_every_rule(self, job_filter, make_job):
        """A job you saved by hand is one you already chose."""
        jobs = [make_job(
            title='Vertriebsingenieur',
            description='Must be authorized to work in the US. Security clearance required.',
            source='Gmail Draft',
        )]
        kept = job_filter.filter_jobs(jobs, min_score=90)
        assert len(kept) == 1, "manually saved jobs must never be filtered out"

    def test_results_are_sorted_by_score(self, job_filter, make_job):
        jobs = [
            make_job(title='Analyst', description='Some valve sizing.'),
            make_job(title='Sales Engineer',
                     description='Valve sizing, Kv calculation, P&ID, ATEX, commissioning.'),
        ]
        kept = job_filter.filter_jobs(jobs, min_score=0)
        scores = [j['relevance_score'] for j in kept]
        assert scores == sorted(scores, reverse=True)

    def test_empty_input_returns_empty(self, job_filter):
        assert job_filter.filter_jobs([], min_score=0) == []

    def test_filter_does_not_discard_everything(self, job_filter, make_job):
        """
        Blunt guard against a keyword list change that rejects the world.
        This is the class of failure the link validator had.
        """
        jobs = [
            make_job(title='Sales Engineer',
                     description='Valve sizing, Kv calculation, P&ID, fluid systems.')
            for _ in range(20)
        ]
        kept = job_filter.filter_jobs(jobs, min_score=0)
        assert len(kept) == 20
