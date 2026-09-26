"""
Tests for the ranking adjustments added 2026-09-26.

Five rules, each tested on its own:
  - export-controlled roles are rejected, since a non-US national can never
    take them, while ordinary US roles are still kept
  - required years of experience lower a job's rank but never drop it
  - the title boost confirms a real description match and never creates one
  - two aggregators that crowd the digest with off-target roles rank lower
  - roles at pump, valve and flow-equipment companies are lifted and marked
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_filter import (  # noqa: E402
    NEGATIVE_KEYWORDS,
    is_export_controlled,
    required_experience,
    experience_multiplier,
    source_priority_multiplier,
    LOW_PRIORITY_SOURCE_MULTIPLIER,
    is_flow_equipment_role,
    flow_equipment_multiplier,
    FLOW_EQUIPMENT_BOOST,
    JobFilter,
)


# ── Export control ────────────────────────────────────────────────────────────

class TestExportControl:
    @pytest.mark.parametrize('text', [
        'This position requires access to ITAR controlled data.',
        'Applicants must be a U.S. person as defined by federal law.',
        'Candidates must be US persons.',
        'Work is subject to the Export Administration Regulations.',
        'Access to EAR-controlled technology is required.',
        'Role is subject to the EAR and related rules.',
        'This role involves export controlled information.',
    ])
    def test_explicit_exclusions_are_detected(self, text):
        assert is_export_controlled(text)

    @pytest.mark.parametrize('text', [
        'Sales engineer for our Houston office. Valve sizing experience.',
        'Great ear for customer needs and a year of experience.',
        'Contact us personally at the plant for a tour.',
        'Our focus person on site will train you.',
        'We sell to customers across the US.',
    ])
    def test_ordinary_text_is_not_flagged(self, text):
        assert not is_export_controlled(text)

    def test_export_controlled_job_is_rejected(self, job_filter, make_job):
        jobs = [make_job(description='Valve sizing. Must be a U.S. person under ITAR.')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_plain_us_job_is_kept(self, job_filter, make_job):
        jobs = [make_job(location='Houston, TX',
                         description='Valve sizing and Kv calculation for US plants.')]
        assert len(job_filter.filter_jobs(jobs, min_score=0)) == 1


# ── Years of experience ───────────────────────────────────────────────────────

class TestRequiredExperience:
    def test_years_are_no_longer_dealbreakers(self):
        for phrase in ('15+ years', '20+ years', '15 years of experience',
                       '20 years of experience'):
            assert phrase not in NEGATIVE_KEYWORDS

    @pytest.mark.parametrize('text,years', [
        ('Minimum 5 years of experience in sales.', 5),
        ('5+ years experience in valve sizing.', 5),
        ('At least three years of relevant experience.', 3),
        ('3-5 years of experience with P&ID.', 3),
        ('Experience: 7 years in process engineering.', 7),
        ('2 years of experience in B2B sales.', 2),
    ])
    def test_parses_the_requirement(self, text, years):
        found, _ = required_experience(text)
        assert found == years

    def test_takes_the_largest_requirement(self):
        text = '2+ years of CRM experience. 6+ years of engineering experience.'
        found, _ = required_experience(text)
        assert found == 6

    @pytest.mark.parametrize('text', [
        'Founded 20 years ago, we have grown across Europe.',
        'We have over 30 years of experience in the valve industry.',
        'Our company has 25 years of experience serving customers.',
        'Hands-on experience with pumps and valves.',
    ])
    def test_ignores_numbers_that_are_not_a_requirement(self, text):
        found, _ = required_experience(text)
        assert found is None

    def test_detects_preferred(self):
        found, preferred = required_experience(
            '5 years of experience is preferred but not required.')
        assert found == 5 and preferred

    @pytest.mark.parametrize('years,expected', [
        (None, 1.0), (1, 1.0), (2, 1.0), (3, 0.95), (4, 0.95),
        (5, 0.85), (7, 0.85), (8, 0.75), (15, 0.75),
    ])
    def test_tiers(self, years, expected):
        text = '' if years is None else f'{years}+ years of experience required.'
        assert experience_multiplier({'description': text}) == pytest.approx(expected)

    def test_preferred_is_halfway_back_toward_one(self):
        required = experience_multiplier(
            {'description': '5+ years of experience required.'})
        preferred = experience_multiplier(
            {'description': '5+ years of experience would be a plus.'})
        assert preferred == pytest.approx(1 - (1 - required) / 2)

    def test_senior_job_is_ranked_lower_but_not_dropped(self, monkeypatch):
        jf = JobFilter()
        monkeypatch.setattr(jf, 'score_job_with_cv', lambda *a, **k: (60.0, 'cv'))
        base = 'Remote valve sizing role.'
        jobs = [
            {'title': 'Sales Engineer', 'company': 'Senior', 'location': 'Remote',
             'source': 'Google Jobs',
             'description': base + ' 20+ years of experience required.'},
            {'title': 'Sales Engineer', 'company': 'Junior', 'location': 'Remote',
             'source': 'Google Jobs', 'description': base},
        ]
        out = jf.filter_jobs(jobs, min_score=0)
        assert [j['company'] for j in out] == ['Junior', 'Senior']


# ── Title boost confirms, never creates ───────────────────────────────────────

class TestTitleBoostGating:
    def test_title_alone_does_not_boost_an_off_target_description(self, job_filter):
        """A target-role title over a description the CV barely matches is a
        title trap, e.g. an Application Engineer post about circuit boards."""
        desc = ('Design PCB layouts and embedded firmware for consumer devices. '
                'Commissioning of test rigs.')
        plain, _ = job_filter.score_job_with_cv('Analyst', desc, 'Acme')
        titled, _ = job_filter.score_job_with_cv('Application Engineer', desc, 'Acme')
        assert titled == plain

    def test_title_still_boosts_a_real_match(self, job_filter):
        desc = 'Valve sizing, Kv calculation and P&ID work.'
        plain, _ = job_filter.score_job_with_cv('Analyst', desc, 'Acme')
        titled, _ = job_filter.score_job_with_cv('Sales Engineer', desc, 'Acme')
        assert titled > plain

    def test_title_only_sources_keep_their_boost(self, job_filter):
        """Some sources return a title and no description. With nothing else to
        read, the title is all the evidence there is, so it still counts."""
        plain, _ = job_filter.score_job_with_cv('Valve Sizing Analyst', '', 'Acme')
        titled, _ = job_filter.score_job_with_cv(
            'Valve Sizing Sales Engineer', '', 'Acme')
        assert titled > plain


# ── Source priority ───────────────────────────────────────────────────────────

class TestSourcePriority:
    @pytest.mark.parametrize('source', ['WeWorkRemotely', 'Apify / Indeed', 'Indeed'])
    def test_crowding_sources_rank_lower(self, source):
        assert source_priority_multiplier({'source': source}) == LOW_PRIORITY_SOURCE_MULTIPLIER

    @pytest.mark.parametrize('source', ['Google Jobs', 'Infostud', 'Greenhouse', ''])
    def test_other_sources_are_neutral(self, source):
        assert source_priority_multiplier({'source': source}) == 1.0

    def test_multiplier_is_a_mild_demotion(self):
        assert 0.8 <= LOW_PRIORITY_SOURCE_MULTIPLIER < 1.0


# ── Flow-equipment roles ──────────────────────────────────────────────────────

class TestFlowEquipmentRoles:
    @pytest.mark.parametrize('company', ['Grundfos', 'Danfoss A/S', 'WILO SE',
                                         'KSB SE & Co. KGaA', 'Xylem', 'SAMSON AG'])
    def test_known_makers_are_recognised(self, company):
        assert is_flow_equipment_role('Mechanical Engineer', '', company)

    def test_description_signals_are_recognised(self):
        desc = ('You will prepare quotations and technical offers for pumps and '
                'valves, and mentor junior engineers.')
        assert is_flow_equipment_role('Engineer', desc, 'Acme')

    def test_passing_mention_is_not_enough(self):
        desc = 'Our office has a coffee machine and a water valve in the kitchen.'
        assert not is_flow_equipment_role('Office Manager', desc, 'Acme')

    def test_word_boundary_on_company_names(self):
        assert not is_flow_equipment_role('Clerk', '', 'Wilson Logistics')

    def test_multiplier(self):
        assert flow_equipment_multiplier(
            {'title': 'Engineer', 'company': 'Grundfos'}) == FLOW_EQUIPMENT_BOOST
        assert flow_equipment_multiplier(
            {'title': 'Engineer', 'company': 'Acme'}) == 1.0

    def test_boost_is_small(self):
        assert 1.0 < FLOW_EQUIPMENT_BOOST <= 1.15


# ── Digest rendering ──────────────────────────────────────────────────────────

class TestDigestSide:
    def test_weworkremotely_only_fills_leftover_slots(self):
        import telegram_sender
        assert telegram_sender.is_low_priority_source('WeWorkRemotely')
        assert telegram_sender.is_low_priority_source('Apify / Indeed')
        assert not telegram_sender.is_low_priority_source('Infostud')

    def test_flow_equipment_role_is_marked(self):
        import telegram_sender
        out = telegram_sender._render_job_lines([
            (1, 'Mechanical Engineer', 'Grundfos', 80, 'https://x', None, 'Test', 0),
            (2, 'Office Manager', 'Acme', 70, 'https://y', None, 'Test', 0),
        ])
        first, second = out.split('\n\n')[:2]
        assert telegram_sender.FLOW_EQUIPMENT_MARKER in first
        assert telegram_sender.FLOW_EQUIPMENT_MARKER not in second
