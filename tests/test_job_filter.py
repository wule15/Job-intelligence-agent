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
    is_blocked_source,
    is_geo_restricted,
    is_non_english_title,
    is_us_located,
    us_location_multiplier,
    US_LOCATION_PENALTY,
    TITLE_BOOST_MULTIPLIER,
    scam_risk,
    JobFilter,
)


class TestScamRisk:
    """A softer layer than the hard block: suspicious jobs are flagged and
    heavily downranked, not deleted. Must work on title/company/link alone so
    the digest can recompute the flag."""

    def test_off_platform_apply_is_risky(self):
        assert scam_risk('Data Entry Clerk', 'Apply via WhatsApp to +1 555 0100')
        assert scam_risk('Remote Assistant, text us to apply')

    def test_pay_to_work_is_risky(self):
        assert scam_risk('Warehouse role', 'A refundable deposit is required to start')
        assert scam_risk('Agent', 'Pay a fee to secure onboarding')

    def test_reshipping_front_is_risky(self):
        assert scam_risk('Package Forwarding Coordinator (work from home)')

    def test_free_paas_apply_link_is_risky(self):
        assert scam_risk('Sales Rep', link='https://vacancy.up.railway.app/apply')

    def test_flag_works_on_title_and_company_only(self):
        # The digest recomputes with no description; the signal must survive.
        assert scam_risk('Recruiter', company='Reach me on WhatsApp Ltd',
                         link='https://indeed.com/x')

    def test_a_normal_job_is_not_flagged(self):
        assert not scam_risk('Sales Engineer', 'Kv valve sizing and flow control',
                             'Flowserve', 'https://boards.greenhouse.io/x/jobs/1')

    def test_scam_risk_downranks_but_does_not_drop(self):
        jf = JobFilter()
        jobs = [{
            'title': 'Remote Data Entry, text us to apply',
            'company': 'QuickHire', 'source': 'Apify / Indeed',
            'description': 'Earn from home. Text us to apply now.',
            'link': 'https://indeed.com/viewjob?jk=1',
        }]
        out = jf.filter_jobs(jobs, min_score=0)
        assert len(out) == 1, 'a suspected scam is downranked, not dropped'
        assert out[0].get('scam_risk') is True


class TestUSDownrank:
    """US-located jobs with no worldwide/EU signal are penalised, not dropped.
    A worldwide-remote signal or a European location cancels the penalty."""

    def test_city_state_is_us(self):
        assert is_us_located('Austin, TX')

    def test_spelled_out_country_is_us(self):
        assert is_us_located('Remote, United States')

    def test_european_location_is_not_us(self):
        assert not is_us_located('Berlin, Germany')

    def test_us_job_is_penalised(self):
        assert us_location_multiplier({'location': 'Austin, TX'}) == US_LOCATION_PENALTY

    def test_us_but_worldwide_is_not_penalised(self):
        assert us_location_multiplier(
            {'location': 'Austin, TX', 'description': 'Fully remote worldwide.'}) == 1.0

    def test_european_job_is_not_penalised(self):
        assert us_location_multiplier({'location': 'Berlin, Germany'}) == 1.0


class TestBlockedSource:
    """Fake or malicious boards must be dropped, whichever field names them
    and whether they appear as a display name, a slug or a domain."""

    def test_display_name_in_company_is_blocked(self):
        assert is_blocked_source({'company': 'Vacancy Global Pro'})

    def test_hyphenated_slug_is_blocked(self):
        assert is_blocked_source({'source': 'remote-zest-jobs'})

    def test_domain_in_link_is_blocked(self):
        assert is_blocked_source({'link': 'https://remoteclickjobs.com/apply/42'})

    def test_legitimate_job_passes(self):
        assert not is_blocked_source({
            'company': 'Emerson', 'source': 'Greenhouse',
            'link': 'https://boards.greenhouse.io/emerson/jobs/1',
        })

    def test_empty_job_passes(self):
        assert not is_blocked_source({})


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
    def test_us_auth_is_blocked_under_strict_default(self):
        """With no eligible regions (the public default), a role requiring US
        work authorization and offering no sponsorship is dropped."""
        assert is_geo_restricted(
            'Engineer', 'Must be authorized to work in the US.', '',
            eligible_regions=[])

    def test_allow_phrase_overrides_the_block(self):
        assert not is_geo_restricted(
            'Engineer', 'US preferred, but open to candidates across EMEA.', '')

    def test_unrestricted_job_passes(self):
        assert not is_geo_restricted('Engineer', 'Fully remote role.', '')

    def test_empty_input_passes(self):
        assert not is_geo_restricted('', '', '')

    # The EU loosening is opt-in per user, via eligible_regions (read from
    # WORK_ELIGIBLE_REGIONS in .env). These pass it explicitly so the tests are
    # deterministic regardless of the local .env.
    EU = ['EU', 'EEA']

    def test_eu_role_with_generic_auth_requirement_is_kept(self):
        """An EU-eligible user: an EU-located role that only asks for generic
        work authorization is takeable on a permit / Blue Card, so it is kept."""
        assert not is_geo_restricted(
            'Valve Engineer',
            'Must be authorized to work in Germany. Valve sizing and commissioning.',
            'Frankfurt, Germany', eligible_regions=self.EU)

    def test_eu_role_valid_work_permit_boilerplate_is_kept(self):
        assert not is_geo_restricted(
            'Process Engineer', 'A valid work permit is required for this role.',
            'Copenhagen, Denmark', eligible_regions=self.EU)

    def test_eu_role_blocked_when_not_eligible(self):
        """The generic public default: with no eligible regions configured, the
        same EU role is dropped. This is what keeps the shared engine correct
        for a user who cannot work in the EU."""
        assert is_geo_restricted(
            'Valve Engineer', 'Must be authorized to work in Germany.',
            'Frankfurt, Germany', eligible_regions=[])

    def test_us_role_generic_auth_still_blocked(self):
        """Outside the eligible region the same generic requirement drops it,
        even for an EU-eligible user."""
        assert is_geo_restricted(
            'Engineer', 'Must have the right to work in the United States.',
            'Austin, TX', eligible_regions=self.EU)

    def test_any_region_keeps_generic_auth_anywhere(self):
        """A user willing to pursue authorization anywhere ('ANY') keeps a
        generic-authorization role wherever it sits, US included."""
        assert not is_geo_restricted(
            'Engineer', 'Must be authorized to work in the United States.',
            'Austin, TX', eligible_regions=['ANY'])

    def test_any_region_still_drops_citizenship_lock(self):
        """'ANY' is not a magic pass: a hard citizenship lock still drops,
        because it can never be satisfied."""
        assert is_geo_restricted(
            'Engineer', 'US citizens only.', 'Austin, TX', eligible_regions=['ANY'])

    def test_eu_citizenship_only_still_blocked(self):
        """Citizenship is not a permit: an EU-nationals-only lock still drops,
        even for an EU-located role and an EU-eligible user."""
        assert is_geo_restricted(
            'Engineer', 'Open to EU nationals only.', 'Berlin, Germany',
            eligible_regions=self.EU)

    def test_generic_auth_no_location_stays_blocked(self):
        """With no location to place it in the EU, a bare authorization
        requirement is still treated as a block."""
        assert is_geo_restricted(
            'Engineer', 'Must hold a valid work permit for this position.', '',
            eligible_regions=self.EU)


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
        # A hard citizenship lock always drops, independent of the configured
        # eligible regions, so this exercises the rejection path deterministically.
        jobs = [make_job(description='US citizens only. Valve sizing.')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_blocked_source_is_rejected(self, job_filter, make_job):
        """Even a perfectly matching job is dropped if it comes from a fake board."""
        jobs = [make_job(
            description='Valve sizing, Kv calculation, P&ID and ATEX.',
            company='Remote Zest Jobs',
        )]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_us_job_is_downranked_not_dropped(self, job_filter, make_job):
        """A US-located job is kept but scores below its identical EU twin."""
        desc = 'Valve sizing, Kv calculation, P&ID and ATEX.'
        jobs = [
            make_job(description=desc, location='Austin, TX'),
            make_job(description=desc, location='Rotterdam, Netherlands'),
        ]
        kept = job_filter.filter_jobs(jobs, min_score=0)
        assert len(kept) == 2, "the US job must be kept, only downranked"
        assert kept[0]['location'] == 'Rotterdam, Netherlands'
        assert kept[0]['relevance_score'] > kept[1]['relevance_score']

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
