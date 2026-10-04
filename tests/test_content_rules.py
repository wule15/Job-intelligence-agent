"""
Rules that read the advert text: the required-years gate, the
electrical-only degree rule, the "excellence" language level and one
posting printed in two languages. Each case is modelled on advert wording,
or is the near miss that must stay.
"""

import pytest

from core.config import Config, years_setting
from core.job_filter import (
    content_drop_reason, requires_electrical_degree, requires_too_many_years,
    requires_unspoken_language,
)
from core.job_normalize import find_near_duplicates, find_same_postings, posting_reference


# ── Required years ────────────────────────────────────────────────────────────

def test_years_gate_off_by_default():
    assert not requires_too_many_years('At least five years of experience in mechanical design.')


def test_years_above_limit_drop():
    text = 'You bring at least five years of experience in mechanical design.'
    assert requires_too_many_years(text, limit=3)
    assert requires_too_many_years('At least 4 years of experience.', limit=3)


def test_years_at_limit_kept():
    assert not requires_too_many_years('Minimum 3 years of experience in the same field.', limit=3)


def test_range_counts_as_lower_bound():
    assert not requires_too_many_years('2-4 years of professional engineering experience', limit=3)
    assert not requires_too_many_years('Experience: 3\u20145 years in sales', limit=3)


@pytest.mark.parametrize('text', [
    '5 years of experience is a plus.',
    'Preferably 5 years of experience in sales.',
    '5 years of experience in pumps is preferable.',
    '5 years of experience in the valve industry is an asset.',
    '5 years of experience would be beneficial.',
    'Pozeljno iskustvo od 5 godina.',
    'Prednost: iskustvo od 5 godina u prodaji.',
    'Iskustvo od 5 godina predstavlja prednost.',
])
def test_preferred_years_never_drop(text):
    assert not requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    'Minimum 5 years of experience in sales, knowledge of SAP is a plus.',
    'Minimum 4 years of experience, 6 years preferred.',
    '5 years of experience required; 8 years preferred.',
])
def test_plus_in_another_clause_does_not_hide_a_requirement(text):
    assert requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    'Up to 5 years of experience in mechanical design.',
    'Graduates with up to 5 years of experience are welcome.',
    'Less than 5 years of experience.',
    'Maximum 5 years of professional experience.',
    'No more than 4 years of experience.',
    'Radno iskustvo do 5 godina.',
    'Iskustvo: do 5 godina.',
    'Iskustvo: najviše 5 godina.',
])
def test_upper_limit_is_not_a_requirement(text):
    assert not requires_too_many_years(text, limit=3)


def test_serbian_years_read():
    assert requires_too_many_years('Minimum 5 godina relevantnog iskustva', limit=3)
    assert requires_too_many_years('5 ili više godina iskustva u prodaji', limit=3)
    assert requires_too_many_years('Minimum 4 i vise godina radnog iskustva', limit=3)
    assert not requires_too_many_years('Od 1-3 godine radnog iskustva', limit=3)


def test_serbian_range_counts_as_lower_bound():
    assert not requires_too_many_years('3 do 5 godina iskustva', limit=3)
    assert not requires_too_many_years('Od 3 do 5 godina radnog iskustva', limit=3)


@pytest.mark.parametrize('text', [
    '5 or more years of experience in sales.',
    'Five (5) years of experience in pump design.',
    'Minimum 5-year experience in the field.',
    'Mindestens 5 Jahre Berufserfahrung im Vertrieb.',
    'Berufserfahrung von mindestens 5 Jahren.',
])
def test_other_ways_of_writing_a_requirement(text):
    assert requires_too_many_years(text, limit=3)


def test_company_history_with_a_company_word_ignored():
    text = 'Zahvaljujući tome i iskustvu dužem od 10 godina, kreiramo rešenja.'
    assert not requires_too_many_years(text, limit=3)
    assert not requires_too_many_years('We have 12 years of experience in pumps.', limit=3)


def test_figures_above_fifteen_years_ignored():
    assert not requires_too_many_years('Over 50 years of experience in valve design.', limit=3)


@pytest.mark.parametrize('text', [
    'With over 10 years of experience, Acme is a leader in pumps.',
    'Sa više od 10 godina iskustva, naša firma je lider na tržištu.',
    'Firma sa 10 godina iskustva u oblasti pumpi.',
])
def test_company_describing_itself_ignored(text):
    assert not requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    'With 5+ years of experience, you will lead the service team.',
    'Za našu kompaniju tražimo inženjera sa 5 godina iskustva.',
    'U našem timu tražimo kandidata sa najmanje 5 godina iskustva.',
    'Join the company as a design engineer: 5+ years of experience required.',
])
def test_company_word_near_a_requirement_does_not_hide_it(text):
    assert requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    'Experience with SAP. Contract for 4 years.',
    'Experience required. Salary review after 5 years.',
    'Relevant experience is a plus.\nWe offer a 5 year contract.',
    'Experience with ISO 9001. Warranty 5 years.',
    'A 4 year engineering degree and 2 years of experience.',
    "Bachelor's degree (4 years) and experience with CAD.",
    'Fluent English. 4 years of university studies, relevant experience required.',
    'You have a 4 year college degree. You preferably have experience with a CRM system.',
    'You have a 4 year college degree You preferably have experience with a CRM system',
    'Your programme will typically last between two and four years, combining '
    'structured learning with hands-on experience.',
])
def test_years_that_are_not_experience_ignored(text):
    assert not requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    "Bachelor's degree and 2+ years of relevant experience, or 4+ years.",
    'Minimum 3 years of experience in pump assembly or testing, or 5 years of '
    'experience in a similar position.',
    "Bachelor's with 5 years of experience or Master's with 3 years.",
])
def test_route_within_the_limit_keeps_the_job(text):
    assert not requires_too_many_years(text, limit=3)


@pytest.mark.parametrize('text', [
    '5 years experience in global B2B sales and 2 years experience with HVAC distributors.',
    '5 years experience in sales or marketing, 2 years experience with distributors.',
])
def test_separate_requirements_are_not_alternatives(text):
    assert requires_too_many_years(text, limit=3)


def test_decimal_years_not_read_as_their_last_digit():
    assert not requires_too_many_years('2.5 years of experience in sales', limit=3)
    assert not requires_too_many_years('2,5 godine radnog iskustva', limit=3)


def test_heading_line_read_with_the_line_under_it():
    assert requires_too_many_years('Experience:\n- 5 years in pump sales', limit=3)
    assert not requires_too_many_years('Nice to have:\n- 5 years of experience in pumps', limit=3)


def test_years_setting():
    assert years_setting('') is None
    assert years_setting(None) is None
    assert years_setting(' 3 ') == 3
    assert years_setting('-2') == 0
    assert years_setting('abc', 'MAX_REQUIRED_YEARS') is None


def test_years_setting_read_from_config(monkeypatch):
    monkeypatch.setattr(Config, 'MAX_REQUIRED_YEARS', 3)
    assert content_drop_reason('Engineer', 'At least 5 years of design experience.') == 'experience_required'


def test_years_reason_comes_first(monkeypatch):
    monkeypatch.setattr(Config, 'MAX_REQUIRED_YEARS', 3)
    monkeypatch.setattr(Config, 'DROP_ELECTRICAL_ONLY_DEGREE', True)
    text = 'BSc in Electrical Engineering.\nAt least 5 years of design experience.'
    assert content_drop_reason('Engineer', text) == 'experience_required'


# ── Electrical-only degree ────────────────────────────────────────────────────

def _on(monkeypatch):
    monkeypatch.setattr(Config, 'DROP_ELECTRICAL_ONLY_DEGREE', True)


def test_electrical_rule_off_by_default():
    assert not requires_electrical_degree('BSc/MSc in Electrical / Electronics Engineering.')


@pytest.mark.parametrize('text', [
    'BSc/MSc in Electrical / Electronics Engineering.',
    "Diploma or Bachelor's degree in Electrical Engineering, Electronics "
    'Technology, or equivalent technical qualification.',
    'A 4-year degree in Electrical Engineering or equivalent engineering education.',
    'Diplomirani inzenjer elektrotehnike',
    'Zavrsen Elektrotehnicki fakultet',
    'B.Sc. in Electrical Engineering',
    'M.Sc. in Electrical Engineering',
    'B.Sc. or M.Sc. in Electrical Engineering',
    'Electrical engineering degree required.',
    "Bachelor's degree in Engineering (Electrical).",
    'Bachelor of Engineering in Electrical and Electronic Systems.',
    'Abgeschlossenes Studium der Elektrotechnik.',
    'Diplom-Ingenieur Elektrotechnik.',
    'Degree in Power Engineering or Electrical Engineering.',
    'Degree in Electrical Engineering and strong computer skills.',
    'Degree in Electrical Engineering, experience in the chemical industry.',
])
def test_electrical_only_degrees_drop(monkeypatch, text):
    _on(monkeypatch)
    assert requires_electrical_degree(text)


def test_degree_read_one_clause_at_a_time(monkeypatch):
    _on(monkeypatch)
    assert requires_electrical_degree('We build mechanical presses.\nDegree in Electrical Engineering.')


@pytest.mark.parametrize('text', [
    'IV-VII stepen strucne spreme tehnickog smera (elektrotehnika, masinstvo ili srodne oblasti)',
    'Tehnicki fakultet (mehatronika), elektrotehnicki ili masinski fakultet',
    'Elektrotehnicki ili masinski fakultet',
    'Diplomirani inzenjer elektrotehnike ili srodne oblasti',
    "Bachelor's degree in Electrical or Mechanical Engineering.",
    "Bachelor's degree in electrical engineering or a related field.",
    'Degree in Electrical, Chemical or Process Engineering.',
    "Bachelor's degree in Electrical Engineering or Computer Science.",
    'Degree in Electrical, Industrial or Energy Engineering.',
    'Degree in Electrical Engineering or other engineering discipline.',
    'Degree in Electrical Engineering or a comparable technical degree.',
    'Degree in Electrical Engineering or another technical discipline.',
    'Degree in Electrical or other engineering field.',
    'Elektrotehnicki fakultet ili slicno.',
    'Elektrotehnicki fakultet ili drugi tehnicki fakultet.',
])
def test_second_discipline_kept(monkeypatch, text):
    _on(monkeypatch)
    assert not requires_electrical_degree(text)


@pytest.mark.parametrize('text', [
    'Degree in Electrical Eng. or Mechanical Eng.',
    'Degree in Electrical Engineering (e.g. Power Systems), or mechanical.',
    'Degree in Electrical Engineering; Mechanical Engineering also considered.',
    "Bachelor's degree in Electrical Engineering.\nMechanical engineers are also welcome.",
    'BSc in Mechanical Engineering. MSc in Electrical Engineering is a plus.',
])
def test_mechanical_alternative_in_another_clause_kept(monkeypatch, text):
    _on(monkeypatch)
    assert not requires_electrical_degree(text)


@pytest.mark.parametrize('text', [
    'Degree in Electrical Engineering is preferred.',
    'A degree in electrical engineering would be an advantage.',
    'Engineering degree, electrical background preferred.',
])
def test_preferred_electrical_degree_kept(monkeypatch, text):
    _on(monkeypatch)
    assert not requires_electrical_degree(text)


@pytest.mark.parametrize('text', [
    "Bachelor's degree in engineering and knowledge of electrical systems.",
    'You master electrical schematics and P&IDs.',
    'Master electrical drawings and wiring diagrams.',
    'A 360-degree view of electrical systems.',
])
def test_electrical_named_without_an_electrical_degree_kept(monkeypatch, text):
    _on(monkeypatch)
    assert not requires_electrical_degree(text)


def test_electrical_reason_name(monkeypatch):
    _on(monkeypatch)
    assert content_drop_reason('Bid Engineer', 'BSc/MSc in Electrical Engineering.') == 'electrical_degree'


# ── The filter and the digest's recheck both apply them ───────────────────────

def _content_rules_on(monkeypatch):
    monkeypatch.setattr(Config, 'MAX_REQUIRED_YEARS', 3)
    monkeypatch.setattr(Config, 'DROP_ELECTRICAL_ONLY_DEGREE', True)


def test_filter_applies_both_rules(job_filter, make_job, monkeypatch):
    _content_rules_on(monkeypatch)
    monkeypatch.setattr(job_filter, 'score_job_with_cv', lambda *a, **k: (58.0, 'Sales_Engineer'))
    jobs = [make_job('Design Engineer', 'At least 5 years of experience in pump design.', company='A'),
            make_job('Test Engineer', 'BSc in Electrical Engineering.', company='B'),
            make_job('Sales Engineer', 'Two years of experience in valve sales.', company='C')]
    kept = job_filter.filter_jobs(jobs, min_score=10)
    assert [job['company'] for job in kept] == ['C']
    assert job_filter.last_rejected['experience_required'] == 1
    assert job_filter.last_rejected['electrical_degree'] == 1


def test_stored_rows_are_rechecked(tmp_path, monkeypatch):
    import telegram_sender
    from core.database import Database
    _content_rules_on(monkeypatch)
    path = str(tmp_path / 'content.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    try:
        years = database.add_job('Design Engineer', 'A', 'At least 5 years of experience in pump design.',
                                 'https://example.com/1', source='Test', relevance_score=80.0,
                                 location='Remote, Europe')
        degree = database.add_job('Test Engineer', 'B', 'BSc in Electrical Engineering.',
                                  'https://example.com/2', source='Test', relevance_score=80.0,
                                  location='Remote, Europe')
        fits = database.add_job('Sales Engineer', 'C', 'Two years of experience in valve sales.',
                                'https://example.com/3', source='Test', relevance_score=80.0,
                                location='Remote, Europe')
        assert telegram_sender.suppress_ineligible() == 2
        main = {job[0] for job in telegram_sender.get_unsent_jobs()}
        assert years not in main and degree not in main and fits in main
    finally:
        database.close()


# ── Language level written as a noun ──────────────────────────────────────────

def test_excellence_reads_as_high_level():
    text = 'Excellence knowledge of Dutch and English (written and spoken) is a requirement'
    assert requires_unspoken_language(text, ['dutch'])


@pytest.mark.parametrize('text, language', [
    ('Excellence in customer service. Dutch is a plus.', 'dutch'),
    ('Drive operational excellence in Dutch operations.', 'dutch'),
    ('Drive operational excellence in German production plants.', 'german'),
    ('We combine Swiss precision and German excellence.', 'german'),
    ('Pursuing excellence in French and German projects.', 'french'),
])
def test_excellence_elsewhere_does_not_drop(text, language):
    assert not requires_unspoken_language(text, [language])


# ── One posting in two languages ──────────────────────────────────────────────

@pytest.mark.parametrize('text, ref', [
    ('Job ID 12345 | Hybrid', '12345'),
    ('Req ID: 67890 Job Location: Remote', '67890'),
    ('Job ID: 2026-1234', '2026-1234'),
    ('Req ID: JR-2026-0001', 'jr-2026-0001'),
    ('Requisition number REQ-2026-00042', 'req-2026-00042'),
    ('Stellennummer: 2026/0412', '2026/0412'),
    ('Job ID 12 is too short to be a posting number', ''),
    ('No reference in this advert.', ''),
])
def test_posting_reference_read(text, ref):
    assert posting_reference(text) == ref


def test_same_posting_two_languages_is_one_job():
    jobs = [
        {'title': 'Praktikum Stroemungssimulation', 'company': 'Acme',
         'description': 'Job ID 12345 | Standort Hybrid'},
        {'title': 'Intern Flow simulation / CFD', 'company': 'Acme',
         'description': 'Job ID 12345 | Location Hybrid'},
    ]
    assert find_same_postings(jobs) == {1}


@pytest.mark.parametrize('first, second', [
    ('Job ID 2026-1234', 'Job ID 2026-5678'),
    ('Req ID: JR-2026-0001', 'Req ID: JR-2026-0002'),
])
def test_year_numbered_postings_are_different_jobs(first, second):
    jobs = [{'title': 'Sales Engineer', 'company': 'Acme', 'description': first},
            {'title': 'Test Engineer', 'company': 'Acme', 'description': second}]
    assert find_same_postings(jobs) == set()


def test_same_number_at_other_company_kept():
    jobs = [
        {'title': 'Sales Engineer', 'company': 'Acme', 'description': 'Job ID 12345'},
        {'title': 'Test Engineer', 'company': 'Globex', 'description': 'Job ID 12345'},
    ]
    assert find_same_postings(jobs) == set()


def test_near_duplicate_pass_ignores_posting_numbers():
    # The pass before the filter must not compare numbers: it would keep
    # whichever copy came first, readable or not.
    jobs = [{'title': 'CFD Praktikum', 'company': 'Acme', 'description': 'Job ID 4711'},
            {'title': 'Intern Flow simulation', 'company': 'Acme', 'description': 'Job ID 4711'}]
    assert find_near_duplicates(jobs) == set()


def test_readable_copy_survives_when_the_other_language_comes_first(job_filter, make_job,
                                                                     monkeypatch):
    # The German copy arrives first and fails the language rule. The English
    # copy of the same posting must still reach the output.
    monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', ['german'])
    monkeypatch.setattr(job_filter, 'score_job_with_cv', lambda *a, **k: (58.0, 'Sales_Engineer'))
    jobs = [make_job('CFD Intern', 'Stellen-ID 4711. Sehr gute Deutschkenntnisse.',
                     link='https://example.com/de'),
            make_job('CFD Intern (m/f/d)', 'Job ID 4711. English is the working language.',
                     link='https://example.com/en')]
    kept = job_filter.filter_jobs(jobs, min_score=10)
    assert [job['link'] for job in kept] == ['https://example.com/en']


def test_best_scoring_copy_kept_when_both_pass(job_filter, make_job, monkeypatch):
    # Both copies pass every rule. The English one scores higher and is kept,
    # although it arrives second.
    monkeypatch.setattr(job_filter, 'score_job_with_cv',
                        lambda title, description, company: (
                            50.0 if 'English' in description else 30.0, 'Sales_Engineer'))
    jobs = [make_job('CFD Intern', 'Job ID 4711. Deutsch.', link='https://example.com/de'),
            make_job('CFD Intern (m/f/d)', 'Job ID 4711. English.', link='https://example.com/en')]
    kept = job_filter.filter_jobs(jobs, min_score=10)
    assert [job['link'] for job in kept] == ['https://example.com/en']
    assert job_filter.last_rejected['same_posting'] == 1
