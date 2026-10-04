"""
Tests for deduplication normalisation.

The old dedup compared raw strings, so the same posting arriving from three
sources with three tracking URLs and three decorated titles counted as three
jobs. These tests pin the normalisation rules, because they are the part
most likely to need tuning later and the part where a careless change
silently merges two genuinely different jobs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.job_normalize import (  # noqa: E402
    canonical_url,
    dedup_key,
    find_near_duplicates,
    history_key,
    matches_exclusion,
    normalize_company,
    normalize_title,
    parse_exclusions,
    title_similarity,
)


class TestCanonicalUrl:
    def test_strips_utm_parameters(self):
        assert canonical_url('https://example.com/j/1?utm_source=li&utm_campaign=x') \
            == 'https://example.com/j/1'

    def test_keeps_meaningful_parameters(self):
        assert canonical_url('https://example.com/jobs?id=7') == 'https://example.com/jobs?id=7'

    def test_strips_fragment(self):
        assert canonical_url('https://example.com/j/1#apply') == 'https://example.com/j/1'

    def test_lowercases_host_only(self):
        """Host is case insensitive. Path is not, some boards use mixed case ids."""
        assert canonical_url('https://WWW.Example.COM/Jobs/AbC') == 'https://www.example.com/Jobs/AbC'

    def test_drops_trailing_slash(self):
        assert canonical_url('https://example.com/jobs/1/') == 'https://example.com/jobs/1'

    def test_parameter_order_does_not_matter(self):
        a = canonical_url('https://example.com/j?b=2&a=1')
        b = canonical_url('https://example.com/j?a=1&b=2')
        assert a == b

    def test_linkedin_tracking_is_stripped(self):
        raw = 'https://www.linkedin.com/jobs/view/12345?trk=public_jobs&trackingId=abc%3D'
        assert canonical_url(raw) == 'https://www.linkedin.com/jobs/view/12345'

    def test_empty_url_is_safe(self):
        assert canonical_url('') == ''
        assert canonical_url(None) == ''

    def test_malformed_url_does_not_raise(self):
        assert canonical_url('not a url') is not None


class TestNormalizeTitle:
    @pytest.mark.parametrize('raw,expected', [
        ('Sales Engineer', 'sales engineer'),
        ('Sales Engineer (Remote)', 'sales engineer'),
        ('Sales Engineer (m/w/d)', 'sales engineer'),
        ('Senior Sales Engineer (Remote, m/w/d) - Full-time', 'senior sales engineer'),
        ('Sales Engineer [Urgent]', 'sales engineer'),
        ('Sales Engineer - Berlin', 'sales engineer'),
        ('  Sales   Engineer  ', 'sales engineer'),
    ])
    def test_decoration_is_removed(self, raw, expected):
        assert normalize_title(raw) == expected

    def test_seniority_is_kept(self):
        """Senior and junior are different jobs. Do not strip them."""
        assert normalize_title('Senior Sales Engineer') != normalize_title('Sales Engineer')

    def test_empty_is_safe(self):
        assert normalize_title('') == ''
        assert normalize_title(None) == ''


class TestNormalizeCompany:
    @pytest.mark.parametrize('raw,expected', [
        ('Acme', 'acme'),
        ('Acme Inc', 'acme'),
        ('Acme Inc.', 'acme'),
        ('Acme Fluid Systems B.V.', 'acme fluid systems'),
        ('Acme GmbH', 'acme'),
        ('Acme d.o.o.', 'acme'),
        ('Globex Group Ltd', 'globex'),
    ])
    def test_legal_suffixes_are_stripped(self, raw, expected):
        assert normalize_company(raw) == expected

    def test_different_companies_stay_different(self):
        assert normalize_company('Acme') != normalize_company('Globex')

    def test_empty_is_safe(self):
        assert normalize_company('') == ''
        assert normalize_company(None) == ''


class TestDedupKey:
    def test_decorated_variants_share_a_key(self):
        assert dedup_key('Sales Engineer (Remote)', 'Acme Inc') == dedup_key('Sales Engineer', 'Acme')

    def test_different_titles_do_not_share_a_key(self):
        assert dedup_key('Sales Engineer', 'Acme') != dedup_key('Content Writer', 'Acme')

    def test_same_title_different_company_does_not_share_a_key(self):
        assert dedup_key('Sales Engineer', 'Acme') != dedup_key('Sales Engineer', 'Globex')

    def test_seniority_does_not_collapse(self):
        """The dangerous direction: merging two real jobs into one."""
        assert dedup_key('Senior Sales Engineer', 'Acme') != dedup_key('Sales Engineer', 'Acme')


class TestNearDuplicates:
    def test_word_order_is_caught(self):
        assert title_similarity('Sales Engineer Industrial', 'Industrial Sales Engineer') == 1.0

    def test_unrelated_titles_score_low(self):
        assert title_similarity('Sales Engineer', 'Payroll Administrator') < 0.3

    def test_near_duplicate_is_reported(self):
        jobs = [
            {'title': 'Sales Engineer', 'company': 'Acme'},
            {'title': 'Sales Engineer (Remote)', 'company': 'Acme Inc'},
        ]
        assert find_near_duplicates(jobs) == {1}

    def test_first_occurrence_is_kept(self):
        jobs = [
            {'title': 'Sales Engineer', 'company': 'Acme'},
            {'title': 'Sales Engineer (Remote)', 'company': 'Acme'},
            {'title': 'Sales Engineer (m/w/d)', 'company': 'Acme'},
        ]
        assert find_near_duplicates(jobs) == {1, 2}

    def test_same_title_at_different_companies_is_not_a_duplicate(self):
        jobs = [
            {'title': 'Sales Engineer', 'company': 'Acme'},
            {'title': 'Sales Engineer', 'company': 'Globex'},
        ]
        assert find_near_duplicates(jobs) == set()

    def test_distinct_roles_are_not_merged(self):
        jobs = [
            {'title': 'Sales Engineer', 'company': 'Acme'},
            {'title': 'Content Writer', 'company': 'Acme'},
        ]
        assert find_near_duplicates(jobs) == set()

    def test_empty_list_is_safe(self):
        assert find_near_duplicates([]) == set()


class TestHistoryKey:
    """The key the never-resend record compares on."""

    def test_ignores_diacritics(self):
        assert history_key('Inženjer prodaje', 'Firma d.o.o.') == history_key('Inzenjer prodaje', 'Firma')

    def test_keeps_seniority_apart(self):
        assert history_key('Automation and Controls Engineer II', 'Orbitco')             != history_key('Senior Automation and Controls Engineer', 'Orbitco')

    def test_dedup_key_itself_is_unchanged(self):
        """The stored, uniquely indexed key must not fold accents. Changing it
        would make the index migration merge or delete existing rows."""
        assert dedup_key('Inženjer prodaje', 'Firma') == 'inženjer prodaje|firma'


class TestParseExclusions:
    def test_skips_blank_and_comment_lines(self):
        text = '# company | title\n\nAcme | Sales Engineer\n   \n# Globex | Writer\n'
        assert parse_exclusions(text) == [('Acme', 'Sales Engineer')]

    def test_splits_on_the_first_bar_only(self):
        text = 'Acme | Lead Product Engineer -> CTO | Python'
        assert parse_exclusions(text) == [('Acme', 'Lead Product Engineer -> CTO | Python')]

    def test_drops_lines_with_an_empty_side(self):
        assert parse_exclusions('Acme |\n| Sales Engineer\nno bar at all\n') == []

    def test_empty_text_is_safe(self):
        assert parse_exclusions('') == []
        assert parse_exclusions(None) == []


class TestMatchesExclusion:
    """The loose match for jobs handled outside the digest. Entries are typed
    by hand from an applications log, so they rarely match the advert exactly."""

    def test_trailing_qualifier_and_legal_suffix(self):
        entries = [('Primer firma', 'Inzenjer podrske')]
        assert matches_exclusion('Inženjer podrške - Junior', 'Primer firma d.o.o.', entries)

    def test_entry_with_a_reference_number(self):
        entries = [('Uzorak Search', 'Menadžer nabavke, ref. 1234')]
        assert matches_exclusion('Menadžer nabavke', 'Uzorak Search Executive Search d.o.o.', entries)

    def test_entry_company_with_a_country_word(self):
        entries = [('Fabrika Primer', 'Inženjer kvaliteta')]
        assert matches_exclusion('Inženjer kvaliteta', 'Fabrika Primer d.o.o.', entries)
        assert matches_exclusion('Inženjer kvaliteta', 'Fabrika', entries)

    def test_accents_in_the_company_are_ignored(self):
        entries = [('Masinogradnja Primer', 'Inzenjer kvaliteta')]
        assert matches_exclusion('Inženjer kvaliteta', 'Mašinogradnja Primer d.o.o.', entries)

    def test_same_title_at_another_company_is_not_matched(self):
        entries = [('Primer firma', 'Inzenjer podrske')]
        assert not matches_exclusion('Inženjer podrške', 'Druga firma', entries)

    def test_one_word_entry_does_not_swallow_longer_titles(self):
        entries = [('Acme', 'Engineer')]
        assert not matches_exclusion('Sales Engineer', 'Acme', entries)
        assert matches_exclusion('Engineer', 'Acme', entries)

    def test_a_generic_advert_is_not_hidden_by_a_longer_entry(self):
        """The advert's words all inside the entry no longer match: that hid
        every later generic posting at a watchlist company."""
        assert not matches_exclusion('Mechanical Engineer', 'Nordco',
                                     [('Nordco', 'Experienced mechanical engineer')])
        assert not matches_exclusion('Automation Engineer', 'Globex',
                                     [('Globex', 'Robotics and Automation Engineer (Lines)')])

    def test_a_two_word_entry_matches_only_the_same_title(self):
        entries = [('Globex Corporation', 'Quality Engineer')]
        assert matches_exclusion('Quality Engineer', 'Globex', entries)
        assert not matches_exclusion('Supplier Quality Engineer', 'Globex', entries)

    def test_a_longer_entry_allows_one_extra_word(self):
        entries = [('Acme', 'Pump Application Specialist')]
        assert matches_exclusion('Pump Application Specialist Water', 'Acme', entries)
        assert not matches_exclusion('Pump Application Specialist Team Lead', 'Acme', entries)

    def test_different_level_is_not_matched(self):
        entries = [('Orbitco', 'Automation and Controls Engineer II')]
        assert not matches_exclusion('Senior Automation and Controls Engineer', 'Orbitco', entries)

    def test_company_prefix_must_end_on_a_word(self):
        entries = [('Acme', 'Sales Engineer')]
        assert not matches_exclusion('Sales Engineer', 'Acmetronics', entries)

    def test_empty_inputs_are_safe(self):
        assert not matches_exclusion('Sales Engineer', 'Acme', [])
        assert not matches_exclusion('', '', [('Acme', 'Sales Engineer')])
