"""
Adverts written in a language the candidate cannot read.

A measurement on a live batch found two adverts in the digest that he could
not read at all: a design engineer advert written in Slovak under English
section headings, and a test automation advert written in French. Neither
names a language requirement, so the required-language rule had nothing to
match. The advert's own language is now read from its common words.

The rule is conservative. It judges only a body long enough to read, only
when one language clearly outweighs every other one, English included, and
only for the languages the user lists. Serbian, Croatian and Bosnian adverts
always pass, and so does German unless it is listed. Every test pins the
settings it reads, so the result does not depend on a local .env.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.job_filter import fails_advert_language  # noqa: E402
from core.multilingual import advert_language  # noqa: E402

# Every language the detector knows except English, German and the South
# Slavic ones, which is the kind of list a user who reads those would set.
UNREAD = ['slovak', 'czech', 'polish', 'slovenian', 'french', 'dutch', 'italian',
          'spanish', 'portuguese', 'hungarian', 'romanian', 'turkish', 'danish',
          'swedish', 'korean', 'arabic', 'japanese', 'chinese', 'greek']

# The Slovak advert from the measurement, with the employer's name removed.
SLOVAK = (
    "The Impact You'll Make Staňte sa súčasťou R&D tímu spoločnosti a podieľajte "
    "sa na vývoji a zlepšovaní hydraulických rozvádzačov, ktoré nachádzajú "
    "uplatnenie v stavebných, poľnohospodárskych a ďalších pracovných strojoch po "
    "celom svete. Ak ste konštruktér/konštruktérka na začiatku svojej kariéry, "
    "ponúkneme vám priestor rozvíjať technické znalosti pri podpore sériovej výroby "
    "a získavať skúsenosti v medzinárodnom prostredí. Ak už máte skúsenosti s "
    "vývojom produktov, budete mať príležitosť podieľať sa na tvorbe nových riešení "
    "a inováciách v oblasti mobilnej hydrauliky. Vaša práca bude mať reálny dopad "
    "na kvalitu, spoľahlivosť a budúcnosť našich produktov. Zároveň budete "
    "spolupracovať s kolegami z rôznych odborností a krajín, čo vám umožní neustále "
    "sa učiť a profesijne rásť. What You'll Be Doing Ak vás baví podieľať sa na "
    "zlepšovaní a podpore existujúcich produktov: Tvorba výkresovej dokumentácie a "
    "technických podkladov v prostredí 2D/3D CAD. Práca v systémoch PLM a SAP, "
    "vrátane vytvárania objednávok na prototypové a PPAP súčiastky. Spolupráca pri "
    "zmenových konaniach existujúcich produktov a komunikácia s dodávateľmi. "
    "Poskytovanie technickej podpory pre sériovú výrobu."
)

FRENCH = (
    "Description du poste : Au sein de notre équipe qualité, vous serez en charge "
    "de l'automatisation des tests de nos applications. Vous participerez à la "
    "conception des plans de test, au développement des scripts automatisés et à "
    "l'analyse des résultats. Profil recherché : vous êtes diplômé d'une école "
    "d'ingénieur et vous avez une première expérience dans l'automatisation des "
    "tests. La maîtrise de Python et des outils d'intégration continue est "
    "indispensable. Nous vous offrons un poste en CDI, des locaux modernes et une "
    "équipe dynamique. Le poste est basé en Île-de-France avec deux jours de "
    "télétravail par semaine."
)

SERBIAN = (
    "Opis posla: Tražimo inženjera prodaje koji će raditi sa ključnim kupcima u "
    "industriji. Kandidat će biti odgovoran za pripremu ponuda, tehničku podršku "
    "kupcima i praćenje tržišta. Potrebno je da imate završen mašinski fakultet i "
    "najmanje dve godine iskustva u prodaji industrijske opreme. Nudimo "
    "stimulativnu zaradu, rad u stabilnoj kompaniji i mogućnost stručnog "
    "usavršavanja. Ukoliko ste zainteresovani, pošaljite nam svoju biografiju do "
    "kraja meseca. Samo kandidati koji uđu u uži izbor biće kontaktirani."
)

CROATIAN = (
    "Opis radnog mjesta: Tražimo inženjera za prodaju koji će surađivati s "
    "ključnim kupcima. Odgovornosti uključuju izradu ponuda, tehničku podršku i "
    "praćenje tržišta. Očekujemo završen strojarski fakultet te najmanje dvije "
    "godine iskustva u prodaji industrijske opreme. Nudimo stimulativnu plaću, rad "
    "u stabilnoj tvrtki i mogućnost stručnog usavršavanja. Ako ste zainteresirani, "
    "pošaljite nam životopis do kraja mjeseca. Kontaktirat ćemo samo kandidate "
    "koji uđu u uži izbor."
)

BOSNIAN = (
    "Opis poslova: Tražimo inženjera prodaje koji će raditi sa ključnim kupcima "
    "na području cijele zemlje. Kandidat će biti zadužen za pripremu ponuda i "
    "tehničku podršku kupcima, kao i za praćenje konkurencije. Uslovi: završen "
    "mašinski fakultet, najmanje dvije godine iskustva u prodaji i vozačka "
    "dozvola B kategorije. Nudimo platu u skladu sa iskustvom i rad u mladom "
    "kolektivu. Ukoliko ispunjavate uslove, prijavu pošaljite putem portala do "
    "kraja mjeseca."
)

GERMAN = (
    "Ihre Aufgaben: Sie unterstützen unser Team bei der Strömungssimulation von "
    "hydraulischen Komponenten. Sie erstellen Rechenmodelle, werten die Ergebnisse "
    "aus und dokumentieren sie für die Konstruktion. Ihr Profil: Sie studieren "
    "Maschinenbau oder Verfahrenstechnik und haben erste Erfahrungen mit CFD. Gute "
    "Englischkenntnisse runden Ihr Profil ab. Wir bieten eine spannende Aufgabe in "
    "einem internationalen Umfeld, flexible Arbeitszeiten und eine faire Vergütung."
)

ENGLISH_WITH_A_FRENCH_LINE = (
    "We are looking for a sales engineer to join our team in France. You will "
    "work with industrial customers on valve sizing and selection, prepare "
    "technical offers and support the sales team from enquiry to order. You have "
    "a degree in mechanical engineering and two years of experience in technical "
    "sales. We offer a permanent contract and a company car. Le poste est basé à "
    "Lyon avec des déplacements chez nos clients."
)

SERBIAN_CYRILLIC = (
    "Тражимо инжењера продаје који ће радити са кључним купцима у индустрији. "
    "Кандидат ће бити одговоран за припрему понуда и техничку подршку купцима. "
    "Нудимо стимулативну зараду и рад у стабилној компанији. Уколико сте "
    "заинтересовани, пошаљите нам своју биографију до краја месеца."
)

KOREAN = (
    "우리는 산업용 밸브 영업 엔지니어를 찾고 있습니다. 고객과 협력하여 기술 제안서를 "
    "작성하고 프로젝트를 관리합니다. 기계공학 학위와 영업 경험이 필요합니다. "
    "경쟁력 있는 급여와 교육 기회를 제공합니다."
)


class TestAdvertLanguage:
    def test_the_measured_adverts(self):
        assert advert_language(SLOVAK) == 'slovak'
        assert advert_language(FRENCH) == 'french'

    def test_south_slavic_adverts_read_as_serbian(self):
        for text in (SERBIAN, CROATIAN, BOSNIAN):
            assert advert_language(text) == 'serbian', text[:40]

    def test_other_languages(self):
        assert advert_language(GERMAN) == 'german'
        assert advert_language(KOREAN) == 'korean'

    def test_english_with_a_line_of_another_language_is_english(self):
        assert advert_language(ENGLISH_WITH_A_FRENCH_LINE) == 'english'

    def test_unsure_is_none(self):
        # Too short to read, and Cyrillic, which several languages share.
        assert advert_language('Staňte sa súčasťou nášho tímu v Bratislave.') is None
        assert advert_language(SERBIAN_CYRILLIC) is None
        assert advert_language('') is None
        assert advert_language(None) is None


class TestFailsAdvertLanguage:
    def test_listed_languages_fail(self):
        assert fails_advert_language(SLOVAK, UNREAD)
        assert fails_advert_language(FRENCH, UNREAD)
        assert fails_advert_language(KOREAN, UNREAD)

    def test_what_he_reads_passes(self):
        for text in (SERBIAN, CROATIAN, BOSNIAN, SERBIAN_CYRILLIC, GERMAN,
                     ENGLISH_WITH_A_FRENCH_LINE):
            assert not fails_advert_language(text, UNREAD), text[:40]

    def test_off_when_no_language_is_listed(self, monkeypatch):
        monkeypatch.setattr(Config, 'UNREADABLE_ADVERT_LANGUAGES', [], raising=False)
        assert not fails_advert_language(SLOVAK)

    def test_a_listed_south_slavic_alias_is_honoured(self):
        # Which languages count is the user's choice, aliases included.
        assert fails_advert_language(CROATIAN, ['croatian'])

    def test_english_as_the_working_language_rescues_it(self):
        text = FRENCH + " La langue de travail est l'anglais. Working language is English."
        assert not fails_advert_language(text, UNREAD)


class TestTheFilterDropsThem:
    @pytest.fixture(autouse=True)
    def settings(self, monkeypatch):
        monkeypatch.setattr(Config, 'UNREADABLE_ADVERT_LANGUAGES', UNREAD, raising=False)
        monkeypatch.setattr(Config, 'NON_FLUENT_LANGUAGES', [], raising=False)
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', [], raising=False)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', [], raising=False)

    def test_filter(self, job_filter, make_job, monkeypatch):
        monkeypatch.setattr(job_filter, 'score_job_with_cv',
                            lambda *a, **k: (58.0, 'Sales_Engineer'))
        jobs = [make_job('Design Engineer', SLOVAK, company='A', source='SuccessFactors'),
                make_job('Test Automation Engineer', FRENCH, company='B'),
                make_job('Sales Engineer', SERBIAN, company='C', source='Infostud'),
                make_job('CFD Intern', GERMAN, company='D')]
        kept = job_filter.filter_jobs(jobs, min_score=10)
        assert sorted(job['company'] for job in kept) == ['C', 'D']
        assert job_filter.last_rejected['advert_language'] == 2

    def test_stored_rows_are_rechecked(self, tmp_path, monkeypatch):
        import telegram_sender
        from core.database import Database
        path = str(tmp_path / 'language.db')
        monkeypatch.setattr(Config, 'DATABASE_PATH', path)
        monkeypatch.setattr(Config, 'DIGEST_EXCLUDE_FILE', '', raising=False)
        monkeypatch.setattr(Config, 'EXCLUDED_LOCATIONS', [], raising=False)
        database = Database(db_path=path)
        database.init_database()
        telegram_sender.init_telegram_tracking()
        try:
            slovak = database.add_job('Design Engineer', 'A', SLOVAK, 'https://example.com/1',
                                      source='SuccessFactors', relevance_score=80.0,
                                      location='Remote, Europe')
            serbian = database.add_job('Sales Engineer', 'B', SERBIAN, 'https://example.com/2',
                                       source='Infostud', relevance_score=80.0,
                                       location='Remote, Europe')
            assert telegram_sender.suppress_ineligible() == 1
            main = {job[0] for job in telegram_sender.get_unsent_jobs()}
            assert slovak not in main and serbian in main
        finally:
            database.close()
