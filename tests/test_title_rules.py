"""
Title and language rules: jobs the candidate would never apply to.

Ten days of digests (245 jobs, about 3 percent useful) showed four kinds of
job getting through that he rules out on sight:

  - senior titles (Senior, Sr, Lead, Staff, Principal, Head of, Director, VP,
    Chief). Plain "Manager" titles are wanted and stay.
  - pure software development titles the old substring list missed (Java,
    C++, Ruby, front-end with a hyphen, DevOps, SRE, database administrator),
    and dev titles the list did catch but kept, because a common word such as
    "cloud" or "platform" in the advert lifted the score over the keep bar
  - trade titles from the home-market board: technicians, electricians and
    civil engineering roles, written with or without diacritics
  - adverts that require a language at a level he does not have (fluent,
    C1, "sehr gute Deutschkenntnisse", "verhandlungssicher")

Entry-level software and QA jobs from the home-market board are still kept,
unless the title is senior. Every test pins the settings it reads, so the
result does not depend on a local .env.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.job_filter import (  # noqa: E402
    ENTRY_SOFTWARE_FLOOR,
    is_local_trade_title,
    is_pure_dev_title,
    is_senior_title,
    requires_unspoken_language,
    title_drop_reason,
)

HIS_LANGUAGES = ['german', 'dutch', 'french', 'turkish', 'korean', 'arabic']


def _score(value):
    """Stand-in for score_job_with_cv, so a test controls the score."""
    return lambda *args, **kwargs: (value, 'Sales_Engineer')


# ── Senior titles ─────────────────────────────────────────────────────────────

class TestSeniorTitle:
    SENIOR = (
        'Senior Sales Engineer', 'Sr. Application Engineer', 'Sr Process Engineer',
        'Lead Engineer', 'Team Lead Sales', 'Staff Engineer',
        'Principal Consultant', 'Head of Sales', 'Sales Director',
        'Director of Engineering', 'VP Sales', 'SVP Operations',
        'Vice President Sales', 'Chief Engineer', 'SENIOR MECHANICAL ENGINEER',
        'Lead-Engineer Commissioning', 'Sales Team Leader',
    )
    NOT_SENIOR = (
        'Sales Manager', 'Account Manager', 'Project Manager',
        'Lead Generation Specialist', 'Lead Qualification Specialist',
        'Lead Gen Specialist', 'Leading Sales Engineer', 'Staffing Coordinator',
        'Head Office Sales Engineer', 'Junior Sales Engineer',
        'Graduate Process Engineer', 'Masinski inzenjer', '',
        # A graduate scheme, and a senior word at the end of a longer word.
        'Graduate Engineer - Staff Development Programme', 'Waitstaff Supervisor',
    )

    def test_senior_titles(self):
        for title in self.SENIOR:
            assert is_senior_title(title), title

    def test_titles_that_are_not_senior(self):
        for title in self.NOT_SENIOR:
            assert not is_senior_title(title), title

    def test_none_is_safe(self):
        assert not is_senior_title(None)


class TestSeniorTitlesAreDropped:
    def test_dropped_whatever_the_score(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(90.0))
        kept = job_filter.filter_jobs(
            [make_job('Senior Sales Engineer'), make_job('Sales Manager', company='Other')],
            min_score=10)
        assert [job['title'] for job in kept] == ['Sales Manager']
        assert job_filter.last_rejected['senior_title'] == 1

    def test_hand_saved_senior_job_is_kept(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(90.0))
        kept = job_filter.filter_jobs(
            [make_job('Senior Sales Engineer', source='Gmail Draft')], min_score=10)
        assert len(kept) == 1


# ── Software titles ───────────────────────────────────────────────────────────

class TestPureDevTitle:
    DEV = (
        'Java Engineer', 'C++ Developer', 'C++ Engineer', 'Ruby on Rails Engineer',
        'Backend Engineer', 'Back End Developer', 'Front-End Developer',
        'Frontend Engineer', 'Full-Stack Developer', 'Fullstack Engineer',
        'DevOps Specialist', 'DevSecOps Engineer', 'SRE', 'Site Reliability Engineer',
        'Database Administrator', 'DBA', 'Web Developer', '.NET Developer',
        'ASP.NET Developer', 'C# Developer', 'Software Engineer', 'Programer',
        'Python Developer', 'Platform Engineer', 'Firmware Engineer',
        'Backend Software Engineer', 'Software Development Engineer',
        'Mobile App Developer',
        # Missed until a measurement on a live batch, October 2026.
        'Engineering Manager', 'Data Scientist', 'Machine Learning Engineer',
        'Forward Deployed Engineer', 'Data Engineer', 'Cloud Engineer',
        'Security Engineer', 'Developer Advocate',
        # The same families written other ways.
        'Software Engineering Manager', 'Engineering Manager, Platform',
        'ML Engineer', 'MLOps Engineer', 'Forward Deployed Software Engineer',
        'Big Data Engineer', 'Cloud Infrastructure Engineer',
        'Cybersecurity Engineer', 'Cyber Security Engineer',
        'Application Security Engineer', 'Developer Relations Engineer',
        'Data Science Intern',
    )
    NOT_DEV = (
        'QA Automation Engineer', 'Software Engineer in Test',
        'Test Automation Engineer', 'Software Engineer AI Integrations',
        'AI Implementation Engineer', 'Business Developer', 'Sales Engineer',
        'Solutions Engineer', 'Application Engineer', 'Automation Engineer',
        'PLC Programmer', 'Mobile Service Engineer',
        'Go-To-Market Engineer', 'Field Service Engineer', '',
        # Automotive platform engineering and his CFD track.
        'Vehicle Platform Engineer', 'CFD Software Engineer',
        'Simulation Software Developer',
        # Engineering managers of a discipline he works in, and the
        # wanted titles next to the new software families.
        'Sales Engineering Manager', 'Manufacturing Engineering Manager',
        'Process Engineering Manager', 'Engineering Project Manager',
        'Cloud Sales Engineer', 'Cloud Solutions Engineer', 'Data Center Engineer',
        'Functional Safety Engineer', 'Security Test Engineer',
        'Forward Deployed Engineer, AI Implementation',
        'Machine Learning Test Automation Engineer',
    )

    def test_dev_titles(self):
        for title in self.DEV:
            assert is_pure_dev_title(title), title

    def test_titles_that_are_not_pure_dev(self):
        for title in self.NOT_DEV:
            assert not is_pure_dev_title(title), title

    def test_method_name_kept(self, job_filter):
        assert job_filter.is_dev_titled('Java Engineer')
        assert not job_filter.is_dev_titled('Sales Engineer')


class TestHardwareEngineeringManagersAreKept:
    """Engineering manager titles at pump, valve, compressor and other
    equipment makers in the measured batch name their discipline after the
    words, or in a word that is not just before them. Only a discipline
    written directly before "Engineering Manager" used to keep the title, so
    all of these were dropped as software roles."""
    HARDWARE = (
        'Engineering Manager, Mission Mechanical',
        'Engineering Manager, Mission Electrical',
        'Electronics Engineering Manager',
        'Hardware Engineering Manager',
        'Sustaining Engineering Manager',
        'Engineering Manager, Water Safety',
        'Engineering Manager Drivetrain, Function & Driver Environment',
        'Engineering Manager, Continuous Improvement (Lean) Manager',
        'Engineering Manager, Avionics',
        'Launch and Recovery Engineering Manager',
        'APAC Regional Engineering Manager',
        'Engineering Manager, Coastal Branch',
        'Engineering Manager, Project Delivery',
        'Engineering Manager - Valves and Actuators',
        'Engineering Manager, Pumps',
        'Engineering Manager, Compressor Packages',
        'Thermal Engineering Manager',
        'Controls Engineering Manager',
        'Reliability Engineering Manager',
        'Engineering Manager, HVAC Systems',
    )
    SOFTWARE = (
        'Engineering Manager, Billing', 'Engineering Manager - Observability',
        'Engineering Manager, Cloud Infrastructure', 'AI Engineering Manager',
        'Data Engineering Manager', 'Analytics Engineering Manager, Data Platform',
        'Engineering Manager, Design Systems', 'Engineering Manager, Data Quality',
        'Engineering Manager, Applications', 'Engineering Manager, Identity Services',
        'Golang Engineering Manager, Commercial Systems',
    )

    def test_hardware_managers_are_kept(self):
        for title in self.HARDWARE:
            assert not is_pure_dev_title(title), title

    def test_software_managers_are_still_dropped(self):
        for title in self.SOFTWARE:
            assert is_pure_dev_title(title), title


class TestDevTitlesAreDropped:
    def test_boosted_score_no_longer_keeps_a_dev_title(self, job_filter, make_job,
                                                       monkeypatch):
        # The sector boost lifted a one-skill match on "cloud platform" past
        # the old keep bar of 20. A dev title is now dropped on the title.
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(58.0))
        job = make_job('Backend Engineer', description='Build our cloud platform.')
        assert job_filter.filter_jobs([job], min_score=10) == []
        assert job_filter.last_rejected['pure_programming'] == 1

    def test_global_board_front_end_is_dropped(self, job_filter, make_job):
        job = make_job('Front-End Developer', source='Remotive')
        assert job_filter.filter_jobs([job], min_score=10) == []
        assert job_filter.last_rejected['pure_programming'] == 1


class TestEntrySoftwareStaysOnTheLocalBoard:
    def _job(self, make_job, title):
        return make_job(title, description='Short teaser.', source='Infostud',
                        location='Serbia')

    def test_junior_and_unranked_dev_titles_are_kept(self, job_filter, make_job):
        for title in ('Junior Java Developer', 'Front-End Developer'):
            kept = job_filter.filter_jobs([self._job(make_job, title)], min_score=10)
            assert len(kept) == 1, title
            assert kept[0]['relevance_score'] >= ENTRY_SOFTWARE_FLOOR

    def test_new_software_families_from_global_boards_are_dropped(self):
        for title in ('Data Scientist', 'Cloud Engineer', 'Developer Advocate'):
            assert title_drop_reason(title, 'Greenhouse') == 'pure_programming', title

    def test_entry_software_exemption_still_applies(self):
        # The existing exemption covers titles naming software, developer,
        # QA or testing. A local-board junior software title stays kept.
        assert title_drop_reason('Junior Software Engineer, Data', 'Infostud') is None

    def test_senior_local_qa_is_dropped_as_senior(self, job_filter, make_job):
        kept = job_filter.filter_jobs([self._job(make_job, 'Senior QA Engineer')],
                                      min_score=10)
        assert kept == []
        assert job_filter.last_rejected['senior_title'] == 1


# ── Trade titles from the home-market board ───────────────────────────────────

class TestLocalTradeTitle:
    """His decision: technicians and electricians are dropped whatever else
    the title says. Mechanical, process, maintenance and the rest are fields
    he wants, not words that rescue a technician title."""
    DROPPED = (
        'Elektrotehničar', 'Elektrotehnicar', 'Elektricar', 'Električar',
        'Elektroinstalater', 'Građevinski inženjer', 'Gradjevinski inzenjer',
        'Gradevinski tehnicar', 'TEHNIČAR', 'TEHNICAR',
        'Električar održavanja', 'Elektrotehničar održavanja',
        'Elektroinstalater - automatizacija', 'Mašinski tehničar',
        'Tehničar održavanja', 'Procesni tehničar', 'Termotehničar',
        'Diplomirani inženjer građevinarstva', 'Inženjer građevine',
    )
    KEPT = (
        'Mašinski inženjer', 'Masinski inzenjer', 'Procesni inzenjer',
        'Inzenjer odrzavanja', 'Tehnička podrška', 'Inzenjer automatizacije',
        'Inspektor kvaliteta',
        'Komercijalista, prodaja građevinskog materijala', 'Inženjer prodaje', '',
    )

    def test_trade_titles(self):
        for title in self.DROPPED:
            assert is_local_trade_title(title), title

    def test_wanted_local_titles(self):
        for title in self.KEPT:
            assert not is_local_trade_title(title), title

    def test_filter_drops_them(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(Config, 'DROP_LOCAL_TRADE_TITLES', True, raising=False)
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(58.0))
        jobs = [make_job('Elektroinstalater', source='Infostud', location='Serbia'),
                make_job('Mašinski inženjer', source='Infostud', location='Serbia',
                         company='Other')]
        kept = job_filter.filter_jobs(jobs, min_score=10)
        assert [job['title'] for job in kept] == ['Mašinski inženjer']
        assert job_filter.last_rejected['local_trade_title'] == 1

    def test_off_unless_the_setting_is_on(self, job_filter, make_job, monkeypatch):
        """Which trades to rule out is personal, so the public engine keeps them."""
        monkeypatch.setattr(Config, 'DROP_LOCAL_TRADE_TITLES', False, raising=False)
        assert title_drop_reason('Elektricar', 'Infostud') is None
        assert title_drop_reason('Facility Technician', 'SuccessFactors') is None


class TestEnglishAndGermanTradeTitle:
    """The same trades written in English or German, on any source. A
    company careers board lists its technician jobs in English ("Facility
    Technician" at a plant in the home market) and its German sites list
    "Elektroniker" roles; both passed the local rule. A title that also says
    engineer is kept, so "Technician / Engineer" is not lost."""
    DROPPED = (
        'Facility Technician',
        'Elektroniker für Automatisierungstechnik (m/w/d)',
        'Elektroniker / Elektriker (m/w/d) für Elektromontage',
        'Elektriker / Elektroniker (m/w/d) Instandhaltung Motoren',
        'Field Service Technician', 'Maintenance Technicians', 'Electrician',
        'Servicetechniker (m/w/d)', 'Engineering Technician',
    )
    KEPT = (
        'Field Service Engineer', 'Technical Sales Engineer', 'Technical Support',
        'Service Technician / Engineer', 'Ingenieur oder Techniker (m/w/d)',
        'Technology Engineer', 'Electrical Engineer', 'Elektroingenieur (m/w/d)',
        'Mechanical Engineer', '',
        # Application and process engineering in German, see the glossary.
        'Anwendungstechniker (m/w/d)', 'Verfahrenstechniker (m/w/d)',
        'Applikationstechniker (m/w/d)',
        # Technical sales and design work that a German title can name
        # "Techniker": the glossary reads "Vertrieb" as sales.
        'Vertriebstechniker (m/w/d)', 'Techniker im Vertrieb (m/w/d)',
        'Konstrukteur / Techniker Maschinenbau (m/w/d)', 'Sales Technician',
    )

    @pytest.fixture(autouse=True)
    def rule_on(self, monkeypatch):
        monkeypatch.setattr(Config, 'DROP_LOCAL_TRADE_TITLES', True, raising=False)

    def test_trade_titles_on_any_source(self):
        for title in self.DROPPED:
            for source in ('SuccessFactors', 'Greenhouse', 'Infostud'):
                assert title_drop_reason(title, source) == 'local_trade_title', (title, source)

    def test_engineer_titles_are_kept(self):
        for title in self.KEPT:
            assert title_drop_reason(title, 'SuccessFactors') is None, title

    def test_filter_drops_them(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(58.0))
        jobs = [make_job('Facility Technician', source='SuccessFactors'),
                make_job('Field Service Engineer', source='SuccessFactors',
                         company='Other')]
        kept = job_filter.filter_jobs(jobs, min_score=10)
        assert [job['title'] for job in kept] == ['Field Service Engineer']
        assert job_filter.last_rejected['local_trade_title'] == 1


class TestTitleDropReason:
    def test_reasons(self, monkeypatch):
        monkeypatch.setattr(Config, 'DROP_LOCAL_TRADE_TITLES', True, raising=False)
        assert title_drop_reason('Senior Sales Engineer') == 'senior_title'
        assert title_drop_reason('Senior Java Developer', 'Infostud') == 'senior_title'
        assert title_drop_reason('Elektricar', 'Infostud') == 'local_trade_title'
        assert title_drop_reason('Java Engineer', 'Remotive') == 'pure_programming'
        assert title_drop_reason('Java Developer', 'Infostud') is None
        assert title_drop_reason('Sales Engineer') is None
        assert title_drop_reason(None) is None

    def test_doomed_titles_do_not_spend_the_fetch_budget(self):
        import job_search_smart
        skills = {'valve sizing', 'technical sales'}
        assert job_search_smart.worth_fetching(
            {'title': 'Technical Sales Engineer', 'location': ''}, skills)
        assert not job_search_smart.worth_fetching(
            {'title': 'Senior Technical Sales Engineer', 'location': ''}, skills)


# ── Required languages ────────────────────────────────────────────────────────

class TestRequiredLanguage:
    REQUIRED = (
        'Fluent German is required.',
        'You are fluent in English and German.',
        'Sehr gute Deutsch- und Englischkenntnisse',
        'Verhandlungssichere Deutschkenntnisse',
        'Deutsch- und Englischkenntnisse verhandlungssicher',
        'Fließend Deutsch in Wort und Schrift',
        'Fliessend Deutsch',
        'German (C1)',
        'German at C1 level',
        'C1 German',
        'Native German speaker',
        'Deutsch auf C1-Niveau',
        'Français courant',
        'Vloeiend Nederlands',
        'Native Arabic speaker',
        'Fluent Korean',
        'Tečno znanje nemačkog jezika',
        'Tecno znanje nemackog jezika',
        'Nemački jezik - C1',
        'Excellent command of the German language',
        'Excellent written and spoken German',
        'Odlično znanje engleskog i nemačkog jezika',
        'Requirements:\n- Fluent German\n- 3 years in sales',
        'Ausgezeichnete Deutschkenntnisse',
        'German native speaker',
        'German language skills at C1 level',
        'Deutschkenntnisse (mind. C1)',
        'Deutsch mindestens C1',
        'German language skills (at least C1)',
        'Deutsch als Muttersprache',
        'Akici Turkce bilgisi',
        # From a presales advert in the measurement, October 2026.
        'Experience working with customers in the DACH region Proficient German speaker',
        'Proficiency in German',
        'Highly proficient in German and English',
        # A plus marker elsewhere in the sentence does not hide the demand.
        'Fluent German, English is a plus',
        # Nor does one in another sentence or bullet.
        'Fluent German required. Experience with SAP is a plus.',
        'Requirements:\n- Verhandlungssicheres Deutsch\n- Python is a plus',
    )
    NOT_REQUIRED = (
        'German is a plus.',
        'Fluent German is a plus.',
        'Gute Deutschkenntnisse',
        'German B1 or higher',
        'Fluent English; basic German',
        'Fluent English and basic German',
        'German B2/C1',
        'Basic proficiency in German',
        'Limited proficiency in German',
        'Proficiency in German at B1 level',
        'Proficient English speaker, German B2',
        'Excellent English, German B1',
        'German, fluent English',
        'Fluent in English, Germany-based role',
        'Deutschkenntnisse von Vorteil',
        'Fluent English, German nice to have',
        'Poznavanje nemačkog jezika je prednost',
        'Fluent Spanish',
        'We serve German customers.',
        'Non-native German speakers are welcome.',
        'C1 driving licence and German customers',
        'Gute bis sehr gute Deutschkenntnisse',
        'Experience with C2 systems, German customers',
        '',
        # B1 and B2, written with a preposition, skill words or a comma.
        'Fluent English and German at B2 level',
        'Fließende Englischkenntnisse, Deutschkenntnisse auf B2-Niveau',
        'Fluent English and German language skills (B2)',
        'Fluent English and German, B2 minimum',
        'Fluent English and German at least B1',
        'Very good English and German at B1 level',
        'Excellent English, German speaking',
        'B2/C1 German',
        # The language as an adjective, or the team, not the candidate.
        'We build excellent German-engineered pumps',
        'Join a native German team',
        'Our team speaks German, English and French fluently',
        # English accepted instead.
        'Deutsch C1 oder Englisch C1',
        # A high level named only as a plus, in the advert's own language.
        'Sehr gute Deutschkenntnisse von Vorteil',
        'Fliessend Deutsch wunschenswert',
        'Vloeiend Nederlands is een pre',
        'Francais courant est un atout',
        'Odlicno znanje nemackog jezika je prednost',
    )

    def test_required_levels(self):
        for text in self.REQUIRED:
            assert requires_unspoken_language(text, HIS_LANGUAGES), text

    def test_levels_he_has_or_a_plus(self):
        for text in self.NOT_REQUIRED:
            assert not requires_unspoken_language(text, HIS_LANGUAGES), text

    def test_none_is_safe(self):
        assert not requires_unspoken_language(None, HIS_LANGUAGES)

    def test_empty_setting_never_drops(self, monkeypatch):
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', [], raising=False)
        assert not requires_unspoken_language('Fluent German is required.')

    def test_setting_is_read_when_no_list_is_passed(self, monkeypatch):
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', ['german'], raising=False)
        assert requires_unspoken_language('Fluent German is required.')
        assert not requires_unspoken_language('Fluent Dutch is required.')

    def test_unlisted_language_matches_only_its_own_name(self):
        assert requires_unspoken_language('Fluent Polish required.', ['polish'])
        assert not requires_unspoken_language('Fluent German required.', ['polish'])

    def test_filter_drops_the_advert(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', HIS_LANGUAGES, raising=False)
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(70.0))
        jobs = [make_job(description='Valve sizing. Sehr gute Deutschkenntnisse.'),
                make_job(description='Valve sizing. Gute Deutschkenntnisse.',
                         company='Other')]
        kept = job_filter.filter_jobs(jobs, min_score=10)
        assert [job['company'] for job in kept] == ['Other']
        assert job_filter.last_rejected['language_required'] == 1

    def test_filter_keeps_it_when_the_setting_is_empty(self, job_filter, make_job,
                                                       monkeypatch):
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', [], raising=False)
        monkeypatch.setattr(job_filter, 'score_job_with_cv', _score(70.0))
        job = make_job(description='Valve sizing. Sehr gute Deutschkenntnisse.')
        assert len(job_filter.filter_jobs([job], min_score=10)) == 1


# ── Rows stored before the rules existed ──────────────────────────────────────

class TestStoredRowsAreRechecked:
    """Jobs are stored for a week. A row stored before these rules must not be
    sent after them."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        import telegram_sender
        from core.database import Database
        path = str(tmp_path / 'titles.db')
        monkeypatch.setattr(Config, 'DATABASE_PATH', path)
        monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', [], raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', [], raising=False)
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', HIS_LANGUAGES, raising=False)
        monkeypatch.setattr(Config, 'DROP_LOCAL_TRADE_TITLES', True, raising=False)
        monkeypatch.setattr(Config, 'REGIONAL_MATCH_TERMS', ['serbia'], raising=False)
        database = Database(db_path=path)
        database.init_database()
        telegram_sender.init_telegram_tracking()
        yield database
        database.close()

    def add(self, db, title, source, location='Remote, Europe', description='Valve sizing.'):
        return db.add_job(title, 'Co ' + title, description,
                          f'https://example.com/{title.replace(" ", "-")}',
                          source=source, relevance_score=80.0, location=location)

    def test_rows_failing_the_new_rules_are_held_back(self, db):
        import telegram_sender
        trade = self.add(db, 'Elektroinstalater', 'Infostud', location='Serbia')
        entry = self.add(db, 'Junior Java Developer', 'Infostud', location='Serbia')
        senior = self.add(db, 'Senior Sales Engineer', 'Remotive')
        staff = self.add(db, 'Staff Software Engineer', 'Greenhouse')
        german = self.add(db, 'Sales Engineer', 'Greenhouse',
                          description='Valve sizing. Verhandlungssichere Deutschkenntnisse.')
        good = self.add(db, 'Valve Sales Engineer', 'Greenhouse')
        saved = self.add(db, 'Senior Saved Engineer', 'Gmail Draft')

        assert telegram_sender.suppress_ineligible() == 4

        regional = {job[0] for job in telegram_sender.get_regional_jobs()}
        direct = {job[0] for job in telegram_sender.get_direct_jobs()}
        main = {job[0] for job in telegram_sender.get_unsent_jobs()}
        assert trade not in regional and entry in regional
        assert staff not in direct and german not in direct and good in direct
        assert senior not in main and saved in main
