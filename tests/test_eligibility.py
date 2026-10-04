"""
Work eligibility: who may legally take a job, and where it sits.

The candidate is a non-EU national. A job is worth sending only when he could
take it: it is in a country he can work in, or it offers visa sponsorship, or
it does not ask for local work authorisation at all.

Four leaks in the daily digest shaped these tests:

  - a refusal to sponsor ("we cannot provide sponsorship") read as an offer,
    because the offer phrase sits inside the refusal
  - a relocation package counted as visa sponsorship, so a US role that
    demands existing US work authorisation was kept and lifted
  - the bare word "global" in company boilerplate ("5 global offices") counted
    as worldwide eligibility and overrode every lock below it
  - "U.S. citizen" with dots, and UK clearance wording, slipped past

and one gap: there was no way to say "only these countries". The country
allow-list reads the location field, which some sources filled with a
lowercase country code or left without a country at all.

Every test passes its settings explicitly or pins them on Config, so the
result does not depend on a local .env.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.countries import (  # noqa: E402
    expand_codes, iso_name, location_countries, successfactors_location,
)
from core.job_filter import (  # noqa: E402
    SPONSORSHIP_BOOST,
    fails_eligibility,
    is_dealbreaker,
    is_export_controlled,
    is_geo_restricted,
    is_outside_allowed_countries,
    sponsorship_multiplier,
    sponsorship_stance,
)

HIS_ALLOWED = ['RS', 'BA', 'EU', 'EEA', 'GB', 'CH', 'EUROPE']
SPONSOR_ONLY = ['US']


def outside(location, description='', allowed=HIS_ALLOWED, sponsorship_only=SPONSOR_ONLY,
            text_ready=True):
    job = {'title': 'Sales Engineer', 'description': description, 'location': location}
    return is_outside_allowed_countries(job, allowed=allowed,
                                        sponsorship_only=sponsorship_only,
                                        text_ready=text_ready)


# ── Sponsorship stance ────────────────────────────────────────────────────────

class TestRefusalsAreNotOffers:
    REFUSALS = [
        'Unfortunately we cannot provide sponsorship for this role.',
        'We are unable to offer visa sponsorship.',
        'This role is not eligible for visa sponsorship.',
        'Future sponsorship for work authorization unavailable.',
        "We don’t sponsor visas for this position.",
        'We do not offer relocation, visa sponsorship or remote work.',
        'We are not able to sponsor at this time.',
        'This position is not eligible for sponsorship.',
        'Sponsorship: No',
    ]

    @pytest.mark.parametrize('text', REFUSALS)
    def test_refusal_is_read_as_a_refusal(self, text):
        assert sponsorship_stance(text) == 'refuse'

    @pytest.mark.parametrize('text', REFUSALS)
    @pytest.mark.parametrize('regions', [['ANY'], []])
    def test_refusal_drops_the_job_whatever_the_regions(self, text, regions):
        assert is_geo_restricted('Engineer', text, 'Cambridge, United Kingdom',
                                 eligible_regions=regions, sponsorship_only=[])

    @pytest.mark.parametrize('text', REFUSALS)
    def test_refusal_earns_no_boost(self, text):
        assert sponsorship_multiplier({'description': text}) == 1.0


class TestRealOffersStillWork:
    OFFERS = [
        'We offer visa sponsorship.',
        'Visa sponsorship available.',
        'We do sponsor visas.',
        'We sponsor H-1B visas for this team.',
        'Sponsorship is available for candidates who do not hold a permit.',
        # A negation about the candidate, not a refusal.
        'If you are not an EU citizen, we offer visa sponsorship.',
        'For candidates who do not hold an EU passport, we provide visa sponsorship.',
        'Candidates without a work permit: we sponsor visas.',
        'No visa sponsorship is needed for EU citizens, and we sponsor work permits '
        'for non-EU hires.',
        'If you do not hold a permit we will sponsor your visa.',
        # The lookback is a few words, so an early "no" in a long sentence
        # does not reach the offer at its end.
        'No relocation package is offered for this junior role but full visa '
        'sponsorship is provided.',
    ]

    @pytest.mark.parametrize('text', OFFERS)
    def test_offer_is_read_as_an_offer(self, text):
        assert sponsorship_stance(text) == 'offer'

    @pytest.mark.parametrize('text', OFFERS)
    def test_offer_is_boosted(self, text):
        assert sponsorship_multiplier({'description': text}) == SPONSORSHIP_BOOST

    def test_offer_keeps_a_role_with_a_uk_right_to_work_line(self):
        assert not is_geo_restricted(
            'Engineer', 'Right to work in the UK required. We offer Skilled Worker '
            'visa sponsorship for the right candidate.', 'London, United Kingdom',
            eligible_regions=[], sponsorship_only=[])

    @pytest.mark.parametrize('text', [
        'Will you now or in the future require visa sponsorship?',
        'Will you now or in the future require sponsorship for employment visa status',
    ])
    def test_application_question_is_neither(self, text):
        assert sponsorship_stance(text) is None

    def test_unrelated_sponsorship_is_neither(self):
        assert sponsorship_stance('We sponsor local football clubs and hackathons.') is None

    @pytest.mark.parametrize('text', [
        'You will report to the executive sponsor, not the line manager.',
        'Ensure no project proceeds without sponsor approval.',
    ])
    def test_a_sponsor_unrelated_to_visas_refuses_nothing(self, text):
        assert sponsorship_stance(text) is None
        assert not is_geo_restricted('Project Manager', text, 'Munich, Germany',
                                     eligible_regions=[], sponsorship_only=SPONSOR_ONLY)

    def test_list_wording_is_not_a_refusal(self):
        text = 'Benefits include, but are not limited to, visa sponsorship and a bonus.'
        assert sponsorship_stance(text) == 'offer'


class TestRelocationIsNotSponsorship:
    TEXT = ('Relocation assistance provided. Applicants must have current '
            'authorization to work in the United States.')

    def test_us_authorisation_line_drops_the_role(self):
        assert is_geo_restricted('Engineer', self.TEXT, 'Hybrid- Fremont, CA',
                                 eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY)

    def test_relocation_alone_earns_no_boost(self):
        assert sponsorship_multiplier({'description': self.TEXT}) == 1.0
        assert sponsorship_multiplier(
            {'description': 'Generous relocation package.'}) == 1.0

    def test_us_work_authorized_wording_is_caught(self):
        text = ('Indefinite U.S. work authorized individuals only. Future '
                'sponsorship for work authorization unavailable.')
        assert is_geo_restricted('Engineer', text, 'Remote',
                                 eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY)

    def test_authorisation_line_alone_is_caught_without_a_location(self):
        assert is_geo_restricted(
            'Engineer', 'You must be legally authorized to work in the U.S.', '',
            eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY)

    def test_authorisation_rule_is_off_without_the_setting(self):
        assert not is_geo_restricted('Engineer', self.TEXT, 'Hybrid- Fremont, CA',
                                     eligible_regions=['ANY'], sponsorship_only=[])

    def test_authorisation_rule_skipped_for_a_role_placed_in_an_allowed_country(self):
        assert not is_geo_restricted(
            'Engineer', 'Must be authorized to work in the United States or Germany.',
            'Munich, Germany', eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY,
            allowed=HIS_ALLOWED)


class TestBoilerplateDoesNotMaskALock:
    def test_global_offices_do_not_override_uk_right_to_work(self):
        text = ('Requirements Right to work in the UK. 350 employees across 5 '
                'global offices. Relocation assistance.')
        assert is_geo_restricted('Software Engineer', text, 'London, United Kingdom',
                                 eligible_regions=['ANY'], sponsorship_only=[])

    def test_worldwide_location_still_keeps_a_generic_role(self):
        assert not is_geo_restricted('Engineer', 'Must be legally authorized to work.',
                                     'Worldwide', eligible_regions=[], sponsorship_only=[])

    def test_remote_worldwide_text_still_keeps_a_generic_role(self):
        assert not is_geo_restricted(
            'Engineer', 'Fully remote worldwide. Must be legally authorized to work.', '',
            eligible_regions=[], sponsorship_only=[])

    def test_worldwide_customers_do_not_override_citizenship(self):
        assert is_geo_restricted('Engineer', 'Serving customers worldwide. US citizens only.',
                                 '', eligible_regions=['ANY'], sponsorship_only=[])

    def test_worldwide_customers_do_not_count_as_eligibility(self):
        assert is_geo_restricted(
            'Engineer', 'Serving customers worldwide. Must be legally authorized to work.',
            '', eligible_regions=[], sponsorship_only=[])

    def test_a_country_mention_does_not_override_a_lock(self):
        """A mention of the home market used to override every lock. The
        country allow-list on the location replaces that rule."""
        assert is_geo_restricted(
            'Engineer', 'US citizens only. Our support team in Serbia helps.', '',
            eligible_regions=['ANY'], sponsorship_only=[])

    @pytest.mark.parametrize('text, location', [
        ('Must be a US citizen. Work from anywhere.', 'Remote'),
        ('We cannot provide visa sponsorship.', 'Worldwide'),
        ('Right to work in the UK is required. International applicants welcome.', 'Remote'),
        ('This role is US only. Remote worldwide.', 'Remote'),
    ])
    def test_a_lock_beats_a_worldwide_signal(self, text, location):
        assert is_geo_restricted('Engineer', text, location,
                                 eligible_regions=['ANY'], sponsorship_only=[])

    @pytest.mark.parametrize('text', [
        'Contact us only via the careers form.',
        'Please reach out to us only if you have questions.',
    ])
    def test_the_pronoun_us_is_not_a_region_lock(self, text):
        assert not is_geo_restricted('Engineer', text, 'Berlin, Germany',
                                     eligible_regions=[], sponsorship_only=[])

    def test_us_only_is_matched_as_words(self):
        """'focus only' contains 'us only' as letters, not as words."""
        assert not is_geo_restricted(
            'Engineer', 'We focus only on industrial customers.', '',
            eligible_regions=[], sponsorship_only=[])
        assert is_geo_restricted('Engineer', 'This role is U.S. only.', '',
                                 eligible_regions=['ANY'], sponsorship_only=[])


class TestDottedCitizenship:
    @pytest.mark.parametrize('text', [
        'Must be a U.S. citizen.',
        'Due to contract terms, only U.S. citizens can be considered.',
        'U.S. citizenship required.',
        'US citizens only.',
    ])
    def test_citizenship_lock_drops(self, text):
        assert is_geo_restricted('Engineer', text, '', eligible_regions=['ANY'],
                                 sponsorship_only=[])

    def test_citizenship_lock_wins_over_an_offer(self):
        assert is_geo_restricted(
            'Engineer', 'We sponsor visas for most roles. Must be a U.S. citizen.', '',
            eligible_regions=['ANY'], sponsorship_only=[])

    def test_eeo_citizenship_status_is_not_a_lock(self):
        text = ('All qualified applicants will receive consideration without regard '
                'to race, religion, citizenship status or national origin.')
        assert not is_geo_restricted('Engineer', text, '', eligible_regions=[],
                                     sponsorship_only=[])

    def test_eeo_us_citizenship_status_is_not_a_lock(self):
        text = 'We hire regardless of race, religion or US citizenship status.'
        assert not is_geo_restricted('Engineer', text, '', eligible_regions=[],
                                     sponsorship_only=[])

    def test_verification_of_citizenship_or_work_authorization_is_not_a_lock(self):
        """The usual US legal line next to an H-1B offer. The few US jobs he
        wants are exactly these."""
        text = ('We sponsor H-1B visas. Employment is contingent on verification of '
                'US citizenship or work authorization, as required by law.')
        assert not is_geo_restricted('Engineer', text, 'Austin, TX',
                                     eligible_regions=[], sponsorship_only=SPONSOR_ONLY)

    def test_citizen_or_permanent_resident_is_still_a_lock(self):
        assert is_geo_restricted('Engineer', 'Must be a US citizen or permanent resident.',
                                 '', eligible_regions=['ANY'], sponsorship_only=[])

    def test_generic_authorization_for_another_country_is_dropped(self):
        assert is_geo_restricted('Engineer', 'You must have authorization to work in Canada.',
                                 'Remote', eligible_regions=[], sponsorship_only=[])

    def test_us_person_without_the_second_dot_is_export_controlled(self):
        assert is_export_controlled('Applicants must be a U.S person.')


class TestClearance:
    @pytest.mark.parametrize('text', [
        'Candidates must be eligible for SC clearance.',
        'DV clearance required.',
        'You must be eligible for security clearance.',
        'This role is subject to security vetting.',
    ])
    def test_clearance_is_a_dealbreaker(self, text):
        assert is_dealbreaker('Engineer', text)

    @pytest.mark.parametrize('text', [
        'Experience with customs clearance is a plus.',
        'Customs clearance required for shipments.',
    ])
    def test_customs_clearance_is_not(self, text):
        assert not is_dealbreaker('Logistics Engineer', text)

    def test_job_filter_method_uses_the_same_rule(self, job_filter):
        assert job_filter.is_negative_match('Engineer', 'DV clearance required.')
        assert not job_filter.is_negative_match('Engineer', 'Customs clearance required.')


# ── Country resolution ────────────────────────────────────────────────────────

class TestLocationCountries:
    @pytest.mark.parametrize('location, codes', [
        ('Kurli, MH, India', {'IN'}),
        ('Ho Chi Minh City, Vietnam', {'VN'}),
        ('Indianapolis, Indiana', {'US'}),
        ('Albuquerque, New Mexico', {'US'}),
        ('Santo Domingo, Dominican Republic', {'DO'}),
        ('Belfast, Northern Ireland', {'GB'}),
        ('Dublin, Ireland', {'IE'}),
        ('München, Deutschland', {'DE'}),
        ('Wien, Österreich', {'AT'}),
        ('Remote - US', {'US'}),
        ('Austin, TX', {'US'}),
        ('Remote (EU)', {'EU'}),
        ('European Union', {'EU'}),
        ('Remote - EMEA', {'EUROPE'}),
        ('Remote - APAC', {'OTHER_REGION'}),
        ('Toronto, Ontario, Canada', {'CA'}),
        ('Stuttgart, de', {'DE'}),
    ])
    def test_resolves(self, location, codes):
        assert location_countries(location) == codes

    @pytest.mark.parametrize('location', [
        '', None, 'Not stated', 'Remote', 'Hybrid', '2 Locations', 'Pimpri', 'Munich',
    ])
    def test_unplaced(self, location):
        assert location_countries(location) == set()

    def test_state_code_that_is_an_allowed_country_is_left_unresolved(self):
        assert location_countries('Wilmington, DE', protect={'DE'}) == set()
        assert location_countries('Wilmington, DE') == {'US'}

    def test_lowercase_state_code_is_not_a_state(self):
        assert location_countries('Austin, tx') == set()

    # Real SuccessFactors strings from the watchlist boards: "City, REGION,
    # CC, postcode". The region code is not a US state, and the ISO code is
    # the country.
    @pytest.mark.parametrize('location, codes', [
        ('Almere, FL, NL, 1327 AE', {'NL'}),
        ('Algete, MD, ES, 28110', {'ES'}),
        ('Truccazzano, MI, IT, 20060', {'IT'}),
        ('Pune, IN, 411013', {'IN'}),
        ('Haiyan, ZJ, CN, 314300', {'CN'}),
        ('Munich, BY, DE', {'DE'}),
        ('Almere, FL, Netherlands', {'NL'}),
        ('Austin, TX 78701', {'US'}),
        ('Houston, TX, US', {'US'}),
    ])
    def test_region_code_and_iso_code(self, location, codes):
        assert location_countries(location) == codes

    @pytest.mark.parametrize('raw, expected, codes', [
        ('Almere, FL, NL, 1327 AE', 'Almere, FL, Netherlands', {'NL'}),
        ('Algete, MD, ES, 28110', 'Algete, MD, Spain', {'ES'}),
        ('Truccazzano, MI, IT, 20060', 'Truccazzano, MI, Italy', {'IT'}),
        ('Pune, IN, 411013', 'Pune, India', {'IN'}),
        ('Burgstall, IT, 39014 (BZ)', 'Burgstall, Italy', {'IT'}),
        ('Haiyan, ZJ, CN, 314300', 'Haiyan, ZJ, China', {'CN'}),
    ])
    def test_successfactors_location(self, raw, expected, codes):
        assert successfactors_location(raw) == expected
        assert location_countries(expected) == codes

    def test_iso_name(self):
        assert iso_name('de') == 'Germany'
        assert iso_name('GB') == 'United Kingdom'
        assert iso_name('zz') == ''


class TestCountryAllowList:
    @pytest.mark.parametrize('location', [
        'Kurli, MH, India', 'Ho Chi Minh City, Vietnam', 'Toronto, Ontario, Canada',
        'Remote - APAC', 'North America',
    ])
    def test_outside_the_list_is_dropped(self, location):
        assert outside(location)

    @pytest.mark.parametrize('location', [
        'Stuttgart, BW, Germany', 'London, United Kingdom', 'Zurich, Switzerland',
        'Mostar, Bosnia and Herzegovina', 'Vranje, Serbia', 'Remote - EMEA', 'Europe',
        'San Francisco, CA | London, UK', 'Oslo, Norway', 'Remote (EU)',
    ])
    def test_inside_the_list_is_kept(self, location):
        assert not outside(location)

    @pytest.mark.parametrize('location', [
        '', 'Not stated', 'Remote', 'Hybrid', '2 Locations', 'Pimpri', 'Wilmington, DE',
        'Worldwide',
    ])
    def test_unplaced_locations_are_left_to_the_text_rules(self, location):
        assert not outside(location)

    @pytest.mark.parametrize('location', ['Itasca, IL, United States', 'Austin, TX'])
    def test_us_without_sponsorship_is_dropped(self, location):
        assert outside(location, 'Valve sizing and commissioning.')

    @pytest.mark.parametrize('location', ['Itasca, IL, United States', 'Austin, TX'])
    def test_us_with_sponsorship_is_kept(self, location):
        assert not outside(location, 'We sponsor H-1B visas.')

    def test_us_decision_waits_for_the_advert_text(self):
        """Before the description is fetched, a US title is not judged yet."""
        assert not outside('Itasca, IL, United States', '', text_ready=False)
        assert outside('Hanoi, Vietnam', '', text_ready=False)

    @pytest.mark.parametrize('location', [
        'US - Remote (Anywhere)', 'Anywhere in the United States', 'India - Anywhere',
    ])
    def test_a_worldwide_word_next_to_a_country_is_judged_on_the_country(self, location):
        assert outside(location, 'Valve sizing.')

    def test_work_from_anywhere_does_not_outweigh_us_authorization(self):
        text = ('Work from anywhere in the US. Applicants must have current '
                'authorization to work in the United States.')
        assert is_geo_restricted('Engineer', text, 'Remote', eligible_regions=['ANY'],
                                 sponsorship_only=SPONSOR_ONLY, allowed=HIS_ALLOWED)

    def test_anywhere_in_one_country_is_not_worldwide(self):
        assert is_geo_restricted('Engineer', 'Work from anywhere in the US.', 'Remote',
                                 eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY,
                                 allowed=HIS_ALLOWED)

    def test_remote_within_a_sponsorship_only_country_needs_an_offer(self):
        text = 'This role is fully remote within the United States.'
        assert is_geo_restricted('Engineer', text, 'Remote', eligible_regions=['ANY'],
                                 sponsorship_only=SPONSOR_ONLY, allowed=HIS_ALLOWED)
        assert not is_geo_restricted('Engineer', text + ' We sponsor H-1B visas.', 'Remote',
                                     eligible_regions=['ANY'], sponsorship_only=SPONSOR_ONLY,
                                     allowed=HIS_ALLOWED)
        assert not is_geo_restricted('Engineer', text, 'Remote', eligible_regions=['ANY'],
                                     sponsorship_only=[])

    def test_a_remote_job_open_worldwide_keeps_the_company_country(self):
        """Greenhouse and Ashby append the office country to a remote job."""
        text = 'Work from anywhere in the world.'
        assert not outside('Remote (United States)', text)
        assert not is_geo_restricted('Engineer', text, 'Remote (United States)',
                                     eligible_regions=[], sponsorship_only=SPONSOR_ONLY,
                                     allowed=HIS_ALLOWED)
        assert is_geo_restricted(
            'Engineer', text + ' Must have current authorization to work in the United States.',
            'Remote (United States)', eligible_regions=[], sponsorship_only=SPONSOR_ONLY,
            allowed=HIS_ALLOWED)

    def test_an_on_site_job_is_not_rescued_by_worldwide_wording(self):
        assert outside('Pune, India', 'We are hiring globally.')

    def test_eu_alone_covers_its_member_states(self):
        assert {'DE', 'AT'} <= expand_codes(['EU'])
        assert not outside('Munich, Germany', allowed=['EU'], sponsorship_only=[])
        assert outside('Pune, India', allowed=['EU'], sponsorship_only=[])

    def test_sponsorship_only_works_without_an_allow_list(self):
        assert outside('Austin, TX', 'Valve sizing.', allowed=[])
        assert not outside('Kurli, MH, India', 'Valve sizing.', allowed=[])

    def test_gates_are_off_by_default(self, monkeypatch):
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', [], raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', [], raising=False)
        job = {'title': 'Engineer', 'description': '', 'location': 'Kurli, MH, India'}
        assert not is_outside_allowed_countries(job)
        assert not is_outside_allowed_countries({**job, 'location': 'Austin, TX'})

    def test_settings_are_read_from_config(self, monkeypatch):
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', HIS_ALLOWED, raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', SPONSOR_ONLY, raising=False)
        job = {'title': 'Engineer', 'description': '', 'location': 'Kurli, MH, India'}
        assert is_outside_allowed_countries(job)


class TestConfigParsing:
    def test_lists_are_parsed_uppercase(self):
        from core.config import code_list
        assert code_list('rs, eu ,GB,') == ['RS', 'EU', 'GB']
        assert code_list('') == []
        assert code_list(None) == []

    def test_public_defaults_are_empty(self):
        """The public engine judges no country unless the user says so."""
        import os
        for key in ('ALLOWED_COUNTRIES', 'SPONSORSHIP_ONLY_COUNTRIES'):
            if not os.getenv(key):
                assert getattr(Config, key) == []


# ── filter_jobs ───────────────────────────────────────────────────────────────

class TestFilterJobsGate:
    DESC = 'Valve sizing, Kv calculation, P&ID and ATEX.'

    @pytest.fixture(autouse=True)
    def settings(self, monkeypatch):
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', HIS_ALLOWED, raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', SPONSOR_ONLY, raising=False)
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)

    def test_out_of_list_job_is_counted(self, job_filter, make_job):
        jobs = [make_job(description=self.DESC, location='Kurli, MH, India'),
                make_job(description=self.DESC, location='Stuttgart, BW, Germany',
                         company='Other')]
        kept = job_filter.filter_jobs(jobs, min_score=0)
        assert [j['location'] for j in kept] == ['Stuttgart, BW, Germany']
        assert job_filter.last_rejected['outside_allowed_countries'] == 1

    def test_hand_saved_job_bypasses_the_gate(self, job_filter, make_job):
        jobs = [make_job(description=self.DESC, location='Kurli, MH, India',
                         source='Gmail Draft')]
        assert len(job_filter.filter_jobs(jobs, min_score=0)) == 1

    def test_gate_runs_after_excluded_location(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', ['india'], raising=False)
        jobs = [make_job(description=self.DESC, location='Kurli, MH, India')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []
        assert job_filter.last_rejected['excluded_location'] == 1
        assert job_filter.last_rejected['outside_allowed_countries'] == 0

    def test_us_role_without_sponsorship_is_dropped(self, job_filter, make_job):
        jobs = [make_job(description=self.DESC, location='Itasca, IL, United States')]
        assert job_filter.filter_jobs(jobs, min_score=0) == []

    def test_us_role_with_sponsorship_is_kept(self, job_filter, make_job):
        jobs = [make_job(description=self.DESC + ' We sponsor H-1B visas.',
                         location='Itasca, IL, United States')]
        assert len(job_filter.filter_jobs(jobs, min_score=0)) == 1


class TestFailsEligibility:
    """One check for a stored row, reused by the digest just before sending."""

    def test_reasons(self, monkeypatch):
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', HIS_ALLOWED, raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', SPONSOR_ONLY, raising=False)
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)
        monkeypatch.setattr(Config, 'WORK_ELIGIBLE_REGIONS', ['ANY'], raising=False)
        assert fails_eligibility('Engineer', 'DV clearance required.', '') == 'dealbreaker'
        assert fails_eligibility('Engineer', 'Must be a U.S. Person.', '') == 'export_controlled'
        assert fails_eligibility('Engineer', 'US citizens only.', '') == 'geo_restricted'
        assert fails_eligibility('Engineer', 'Valve sizing.', 'Pune, India') == \
            'outside_allowed_countries'
        assert fails_eligibility('Engineer', 'Valve sizing.', 'Munich, Germany') is None
        assert fails_eligibility('Engineer', None, None) is None


# ── Sources keep the country they already have ────────────────────────────────

class TestSourcesKeepTheCountry:
    def test_smartrecruiters_uses_the_full_location(self, monkeypatch):
        from sources import ats
        page = {'content': [
            {'id': '1', 'name': 'Sales Engineer',
             'location': {'city': 'Stuttgart', 'country': 'de',
                          'fullLocation': 'Stuttgart, BW, Germany'}},
            {'id': '2', 'name': 'Process Engineer',
             'location': {'city': 'Stuttgart', 'country': 'de'}},
            {'id': '3', 'name': 'Buyer', 'location': {'country': 'in'}},
        ]}
        monkeypatch.setattr(ats, '_get_json', lambda url: page)
        jobs = ats.fetch_smartrecruiters('acme', 'Acme', max_jobs=10)
        assert [j['location'] for j in jobs] == [
            'Stuttgart, BW, Germany', 'Stuttgart, Germany', 'India']

    def test_greenhouse_adds_the_office_country(self, monkeypatch):
        from sources import ats
        data = {'jobs': [
            {'title': 'A', 'location': {'name': 'San Francisco'},
             'offices': [{'location': 'San Francisco, California, United States'}]},
            {'title': 'B', 'location': {'name': 'London, United Kingdom'},
             'offices': [{'location': 'London, United Kingdom'}]},
            {'title': 'C', 'location': {'name': 'Remote'}, 'offices': [{'location': None}]},
            {'title': 'D', 'location': {'name': 'Berlin; Munich'},
             'offices': [{'location': 'Berlin, Germany'}, {'location': 'München, Germany'}]},
            # The offices of a remote job say where the company sits.
            {'title': 'E', 'location': {'name': 'Remote'},
             'offices': [{'location': 'San Francisco, California, United States'}]},
        ]}
        monkeypatch.setattr(ats, '_get_json', lambda url: data)
        locations = [j['location'] for j in ats.fetch_greenhouse('acme', 'Acme')]
        assert locations == ['San Francisco (United States)', 'London, United Kingdom',
                             'Remote', 'Berlin; Munich (Germany)', 'Remote']

    def test_ashby_adds_the_address_country(self, monkeypatch):
        from sources import ats
        data = {'jobs': [
            {'title': 'A', 'location': 'Paris',
             'address': {'postalAddress': {'addressCountry': 'France'}},
             'secondaryLocations': [
                 {'location': 'Berlin', 'address': {'postalAddress': {'addressCountry': 'Germany'}}}]},
            {'title': 'B', 'location': 'Remote',
             'address': {'postalAddress': {'addressCountry': 'European Union'}}},
            {'title': 'C', 'location': 'Paris, France',
             'address': {'postalAddress': {'addressCountry': 'France'}}},
        ]}
        monkeypatch.setattr(ats, '_get_json', lambda url: data)
        jobs = ats.fetch_ashby('acme', 'Acme')
        assert jobs[0]['location'] == 'Paris (France, Germany)'
        assert location_countries(jobs[1]['location']) == {'EU'}
        assert jobs[2]['location'] == 'Paris, France'

    @pytest.mark.parametrize('raw, expected', [
        ('Vaasa, FI', 'Vaasa, Finland'),
        ('Heidenheim, BW (DE)', 'Heidenheim, BW (Germany)'),
        ('Houston, TX, US', 'Houston, TX, United States'),
        ('Remote', 'Remote'),
        ('Almere, FL, NL, 1327 AE', 'Almere, FL, Netherlands'),
        ('Burgstall, IT, 39014 (BZ)', 'Burgstall, Italy'),
    ])
    def test_successfactors_spells_out_the_country_code(self, raw, expected):
        import io
        from sources import ats
        feed = (
            '<?xml version="1.0" encoding="UTF-8" ?>'
            '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>'
            f'<item><title>Engineer</title><link>https://jobs.example.com/job/1/</link>'
            f'<description>x</description><g:location>{raw}</g:location></item>'
            '</channel></rss>').encode('utf-8')
        jobs = ats._sf_jobs_from_stream(io.BytesIO(feed), 'Acme', 'jobs.example.com', None)
        assert jobs[0]['location'] == expected

    def test_adzuna_adds_the_market_country(self, monkeypatch):
        from sources import free_boards
        monkeypatch.setenv('ADZUNA_APP_ID', 'id')
        monkeypatch.setenv('ADZUNA_APP_KEY', 'key')
        data = {'results': [
            {'title': 'Engineer', 'company': {'display_name': 'Acme'},
             'description': 'x', 'redirect_url': 'https://example.com/1',
             'location': {'display_name': 'Pune'}},
            {'title': 'Engineer 2', 'company': {'display_name': 'Acme'},
             'description': 'x', 'redirect_url': 'https://example.com/2', 'location': {}},
        ]}
        monkeypatch.setattr(free_boards, '_get', lambda url, params=None: data)
        jobs = free_boards.search_adzuna('engineer', country='in')
        assert jobs[0]['location'] == 'Pune, India'
        assert location_countries(jobs[1]['location']) == {'IN'}

    def test_smartrecruiters_detail_reads_additional_information(self, monkeypatch):
        from sources import ats
        detail = {'jobAd': {'sections': {
            'companyDescription': {'text': '<p>About us.</p>'},
            'jobDescription': {'text': '<p>Valve sizing.</p>'},
            'qualifications': {'text': '<p>Degree.</p>'},
            'additionalInformation': {'text': '<p>Indefinite U.S. work authorized '
                                              'individuals only.</p>'},
        }}}
        monkeypatch.setattr(ats, '_get_detail_json', lambda url: detail)
        text = ats._detail_smartrecruiters(
            {'link': 'https://jobs.smartrecruiters.com/acme/123'})
        assert 'Valve sizing.' in text
        assert 'work authorized individuals only' in text

    def test_workday_detail_adds_the_country(self, monkeypatch):
        from sources import ats
        detail = {'jobPostingInfo': {'jobDescription': '<p>Valve sizing.</p>',
                                     'country': {'descriptor': 'United States of America'}}}
        monkeypatch.setattr(ats, '_get_detail_json', lambda url: detail)
        job = {'link': 'https://acme.wd5.myworkdayjobs.com/External/job/1',
               'location': 'Itasca'}
        assert ats._detail_workday(job) == 'Valve sizing.'
        assert job['location'] == 'Itasca (United States of America)'
        assert location_countries(job['location']) == {'US'}

    def test_workday_detail_leaves_several_sites_unplaced(self, monkeypatch):
        """The detail names only the primary site's country, so a job at a
        German and a US site must not read as US only."""
        from sources import ats
        detail = {'jobPostingInfo': {'jobDescription': 'x',
                                     'country': {'descriptor': 'United States of America'}}}
        monkeypatch.setattr(ats, '_get_detail_json', lambda url: detail)
        job = {'link': 'https://acme.wd5.myworkdayjobs.com/External/job/1',
               'location': '2 Locations'}
        ats._detail_workday(job)
        assert job['location'] == '2 Locations'

    def test_workday_detail_does_not_repeat_a_known_country(self, monkeypatch):
        from sources import ats
        detail = {'jobPostingInfo': {'jobDescription': 'x',
                                     'country': {'descriptor': 'Germany'}}}
        monkeypatch.setattr(ats, '_get_detail_json', lambda url: detail)
        job = {'link': 'https://acme.wd5.myworkdayjobs.com/External/job/1',
               'location': 'Munich, Germany'}
        ats._detail_workday(job)
        assert job['location'] == 'Munich, Germany'


class TestAdzunaMarkets:
    QUERIES = [f'query {i}' for i in range(10)]

    def test_markets_outside_the_allow_list_are_skipped(self):
        from sources.free_boards import ADZUNA_CALL_BUDGET, adzuna_plan
        markets, per_market = adzuna_plan(self.QUERIES, HIS_ALLOWED, SPONSOR_ONLY)
        assert 'us' in markets and 'gb' in markets and 'de' in markets
        assert not {'in', 'au', 'br', 'mx', 'ca', 'sg', 'nz', 'za'} & set(markets)
        # The calls saved go to more queries in the markets left.
        assert per_market > 4
        assert len(markets) * per_market <= ADZUNA_CALL_BUDGET

    def test_us_only_with_sponsorship_only_us(self):
        from sources.free_boards import adzuna_plan
        markets, _ = adzuna_plan(self.QUERIES, HIS_ALLOWED, [])
        assert 'us' not in markets

    def test_no_allow_list_searches_every_market_as_before(self):
        from sources.free_boards import ADZUNA_MARKETS, adzuna_plan
        assert adzuna_plan(self.QUERIES, [], SPONSOR_ONLY) == (list(ADZUNA_MARKETS), 4)


# ── Description fetch budget ──────────────────────────────────────────────────

class TestPreEnrichment:
    def test_out_of_list_titles_are_not_fetched(self, monkeypatch):
        import job_search_smart
        from sources import ats
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', HIS_ALLOWED, raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', SPONSOR_ONLY, raising=False)
        fetched = []
        monkeypatch.setitem(ats.DETAIL_FETCHERS, 'SmartRecruiters',
                            lambda job: fetched.append(job['location']) or 'text')
        jobs = [
            {'title': 'Sales Engineer', 'location': 'Hanoi, Vietnam', 'description': '',
             'source': 'SmartRecruiters', 'link': 'https://jobs.smartrecruiters.com/a/1'},
            {'title': 'Sales Engineer', 'location': 'Itasca, IL, United States',
             'description': '', 'source': 'SmartRecruiters',
             'link': 'https://jobs.smartrecruiters.com/a/2'},
        ]
        ats.enrich_descriptions(
            jobs, should_fetch=lambda job: job_search_smart.worth_fetching(job, set()))
        assert fetched == ['Itasca, IL, United States']


# ── Rows already stored ───────────────────────────────────────────────────────

class TestStoredRowsAreRechecked:
    """A rule added today must also stop rows stored before it. The 7-day
    backlog kept sending export-controlled roles after the rule shipped."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import telegram_sender
        from core.database import Database
        path = str(tmp_path / 'eligibility.db')
        monkeypatch.setattr(Config, 'DATABASE_PATH', path)
        monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', HIS_ALLOWED, raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', SPONSOR_ONLY, raising=False)
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)
        database = Database(db_path=path)
        database.init_database()
        telegram_sender.init_telegram_tracking()
        yield database
        database.close()

    def add(self, db, title, description, location, source='Greenhouse'):
        return db.add_job(title, 'Co ' + title, description,
                          f'https://example.com/{title.replace(" ", "-")}',
                          source=source, relevance_score=80.0, location=location)

    def test_ineligible_rows_are_held_back(self, db):
        import telegram_sender
        bad = self.add(db, 'Test Engineer', 'Must be a U.S. Person under ITAR.', 'Remote')
        far = self.add(db, 'Plant Engineer', 'Valve sizing.', 'Pune, India')
        good = self.add(db, 'Valve Engineer', 'Valve sizing.', 'Munich, Germany')
        saved = self.add(db, 'Saved Engineer', 'DV clearance required.', 'Pune, India',
                         source='Gmail Draft')
        held = telegram_sender.suppress_ineligible()
        assert held == 2
        direct = telegram_sender.get_direct_jobs()
        main = telegram_sender.get_unsent_jobs()
        ids = {job[0] for job in direct} | {job[0] for job in main}
        assert bad not in ids and far not in ids
        assert good in ids and saved in ids
        assert all(len(job) == 8 for job in direct + main)

    def test_never_fatal(self, db, monkeypatch):
        import telegram_sender
        monkeypatch.setattr(Config, 'DATABASE_PATH', str(Path(db.db_path).parent / 'no' / 'x.db'))
        assert telegram_sender.suppress_ineligible() == 0


# ── Round two: leaks found by replaying a day's raw batch ─────────────────────
# The strings below are copied from public job-board adverts.

ZURICH_FOOTER = (
    'Please note: Zurich does not accept unsolicited CVs from agencies. Preferred '
    'vendors should use our Recruiting Agency Portal. Location(s): AM - Schaumburg, '
    'AM - Chicago Remote Working: Hybrid Schedule: Full Time Employment Sponsorship '
    'Offered: No Linkedin Recruiter Tag: #LI-AK1 LI-ASSOCIATE #LI-HYBRID')

ANTHROPIC_VISA_LINE = (
    'Location-based hybrid policy: Currently, we expect all staff to be in one of our '
    'offices at least 25% of the time. However, some roles may require more time in '
    'our offices. Visa sponsorship: We do sponsor visas! However, we aren\'t able to '
    'successfully sponsor visas for every role and every candidate. But if we make you '
    'an offer, we will make every reasonable effort to get you a visa, and we retain '
    'an immigration lawyer to help with this.')


class TestFieldStyleSponsorshipAnswers:
    """Some boards end the advert with a form: "Employment Sponsorship
    Offered: No". The words "sponsorship offered" read as an offer, so US
    postings that refuse sponsorship passed the US rule."""

    @pytest.mark.parametrize('text', [
        ZURICH_FOOTER,
        'Remote Working: Hybrid Schedule: Full Time Employment Sponsorship: No '
        'LinkedIn Recruiter Tag: #LI-',
        'Visa sponsorship available: No',
        'Sponsorship offered - No',
        'Visa sponsorship provided? No.',
    ])
    def test_a_no_answer_is_a_refusal(self, text):
        assert sponsorship_stance(text) == 'refuse'

    @pytest.mark.parametrize('text', [
        'Employment Sponsorship Offered: Yes',
        'Visa sponsorship available: Yes',
    ])
    def test_a_yes_answer_is_an_offer(self, text):
        assert sponsorship_stance(text) == 'offer'

    def test_the_us_posting_is_dropped(self):
        assert outside('Schaumburg, IL, United States', ZURICH_FOOTER)
        assert is_geo_restricted('Integrated Communications Specialist', ZURICH_FOOTER,
                                 'Schaumburg, IL, United States', eligible_regions=['ANY'],
                                 sponsorship_only=SPONSOR_ONLY, allowed=HIS_ALLOWED)


class TestOfferWithACaveat:
    """"We do sponsor visas! However, we aren't able to sponsor for every
    role" is an offer with a caveat, not a refusal."""

    def test_caveat_is_read_as_an_offer(self):
        assert sponsorship_stance(ANTHROPIC_VISA_LINE) == 'offer'

    def test_the_role_is_kept_and_lifted(self):
        assert not outside('San Francisco, CA', ANTHROPIC_VISA_LINE)
        assert not is_geo_restricted('Account Executive', ANTHROPIC_VISA_LINE,
                                     'Dublin, IE (IE)', eligible_regions=[],
                                     sponsorship_only=SPONSOR_ONLY, allowed=HIS_ALLOWED)
        assert sponsorship_multiplier({'description': ANTHROPIC_VISA_LINE}) == \
            SPONSORSHIP_BOOST

    @pytest.mark.parametrize('text', [
        'Not every role is eligible for visa sponsorship.',
        'We are unable to sponsor visas for any role.',
        'We cannot sponsor visas for this role.',
    ])
    def test_a_caveat_alone_or_a_flat_refusal_is_not_an_offer(self, text):
        assert sponsorship_stance(text) != 'offer'

    def test_a_caveat_without_an_offer_is_neither(self):
        text = "We aren't able to sponsor visas for every role and every candidate."
        assert sponsorship_stance(text) is None


class TestLocationsPlacedByShapeOrPlace:
    """Location formats the country module could not place, so the allow-list
    never judged them, and an Australian or Saudi role reached the digest."""

    @pytest.mark.parametrize('location, codes', [
        # SuccessFactors with a state and postcode after the ISO code.
        ('Mulgrave, AU, VIC 3170', {'AU'}),
        ('Minneapolis, MN, US, MN 55447', {'US'}),
        # A city with a numeric region code: placed by the city's name.
        ('King Abdullah Economic City, 02 (02)', {'SA'}),
        # A city with an office code.
        ('San Francisco - SF9', {'US'}),
        # A region abbreviation for the Americas.
        ('Remote, AMER', {'OTHER_REGION'}),
        # Shapes that must not change.
        ('Austin, TX 78701', {'US'}),
        ('Pune, IN, 411013', {'IN'}),
        ('Almere, FL, NL, 1327 AE', {'NL'}),
    ])
    def test_resolves(self, location, codes):
        assert location_countries(location) == codes

    @pytest.mark.parametrize('location', [
        'Mulgrave, AU, VIC 3170', 'King Abdullah Economic City, 02 (02)', 'Remote, AMER',
    ])
    def test_outside_the_list(self, location):
        assert outside(location, 'Valve sizing.')

    def test_us_city_needs_a_sponsorship_offer(self):
        assert outside('San Francisco - SF9', 'Valve sizing.')
        assert not outside('San Francisco - SF9', 'We sponsor H-1B visas.')


class TestPlaceholderLocations:
    """Danfoss writes the template text "City-State-Country" instead of a
    place. A placeholder places nothing, so the countries the advert itself
    names decide: only disallowed ones count against it."""

    PLACEHOLDER = 'City-State-Country, City-State-Country, City-State-Country'
    CANADA_US = ("You'll play a hands-on role in helping customers across Canada get the "
                 'most from their AC drive systems. The US base salary range for this '
                 'full-time position is $65,000 -$100,000 + bonus + benefits.')

    def test_disallowed_countries_in_the_advert_drop_it(self):
        assert outside(self.PLACEHOLDER, self.CANADA_US)

    def test_a_sponsorship_offer_still_counts(self):
        assert outside(self.PLACEHOLDER, 'The US base salary range is $65,000.')
        assert not outside(self.PLACEHOLDER,
                           'The US base salary range is $65,000. We sponsor H-1B visas.')

    def test_an_allowed_country_in_the_advert_keeps_it(self):
        assert not outside(self.PLACEHOLDER, 'Join our team in Nordborg, Denmark. '
                                             'Customers across Canada and the US.')

    def test_an_advert_naming_no_country_is_left_to_the_text_rules(self):
        assert not outside(self.PLACEHOLDER, "Bachelor's degree in Mechanical Engineering. "
                                             '2 years in a global organization.')

    def test_only_placeholders_read_the_advert(self):
        """A real place, even an unplaced one, is not second-guessed by
        country names in the advert, which are often customers or offices."""
        assert not outside('Munich', 'Customers across Canada and the US.')
        assert not outside('Remote', 'Customers across Canada and the US.')
        assert not outside('', 'Customers across Canada and the US.')
