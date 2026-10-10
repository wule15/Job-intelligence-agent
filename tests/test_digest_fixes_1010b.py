"""
Three more leaks from the 10 October digests: a US-only remote job labelled
"Anywhere" by Google Jobs, an advert published with its template unfilled,
and "sichere Deutschkenntnisse" not read as a German requirement.
"""

from core.config import Config
from core.countries import remote_countries
from core.job_filter import (
    content_drop_reason, fails_eligibility, is_outside_allowed_countries,
    is_template_advert, requires_unspoken_language,
)

US_LINK = ('https://www.remoterocketship.com/us/company/ku-dk/jobs/'
           'robotic-automation-engineer-i-united-states-remote')


def _europe(monkeypatch):
    monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', ['RS', 'EU', 'EEA', 'GB', 'CH', 'EUROPE'])
    monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', ['US'])


# ── Remote in one named country ───────────────────────────────────────────────

def test_remote_country_read_from_link_and_text():
    assert remote_countries('', US_LINK) == {'US'}
    assert remote_countries('United States – Remote') == {'US'}
    assert remote_countries('Location: Remote (UK)') == {'GB'}
    assert remote_countries('A remote-first team', 'https://x.com/jobs/sales-remote') == set()


def test_anywhere_job_remote_in_us_only_dropped(monkeypatch):
    _europe(monkeypatch)
    job = {'title': 'Robotic Automation Engineer I', 'description': 'Lab automation.',
           'location': 'Anywhere', 'link': US_LINK}
    assert is_outside_allowed_countries(job)
    assert fails_eligibility(job['title'], job['description'], 'Anywhere', US_LINK) \
        == 'outside_allowed_countries'


def test_us_remote_with_sponsorship_kept(monkeypatch):
    _europe(monkeypatch)
    job = {'title': 'X', 'location': 'Remote',
           'description': 'United States – Remote. We offer visa sponsorship.'}
    assert not is_outside_allowed_countries(job)


def test_remote_in_allowed_or_unnamed_country_kept(monkeypatch):
    _europe(monkeypatch)
    for description in ('Remote - Germany', 'Fully remote team',
                        'United States – Remote. Work from anywhere in the world.'):
        job = {'title': 'X', 'description': description, 'location': 'Anywhere'}
        assert not is_outside_allowed_countries(job), description


# ── Unfinished template ───────────────────────────────────────────────────────

TEMPLATE = ('[Jobtitel] im Vertrieb. [Aufgabe #1, max. 3-5 Bulletpoints] '
            'Starke Kommunikation in [Sprache, Format oder Kontext, z. B. Englisch].')


def test_unfilled_template_dropped():
    assert is_template_advert(TEMPLATE)
    assert is_template_advert('Lorem ipsum dolor sit amet')
    assert content_drop_reason('External Sales', TEMPLATE) == 'template_advert'


def test_ordinary_brackets_kept():
    assert not is_template_advert('Sales Engineer (m/w/d) [EN], e.g. pumps and valves')
    assert not is_template_advert('Tasks: [1] quote pumps, [2] visit plants')


# ── "Sicher" as a German level ────────────────────────────────────────────────

def test_sichere_deutschkenntnisse_drops():
    for text in ('Mit sicheren Deutsch- und Englischkenntnissen bewegst Du Dich souveraen.',
                 'Sichere Deutschkenntnisse in Wort und Schrift',
                 'Deutsch sicher in Wort und Schrift'):
        assert requires_unspoken_language(text, ['german']), text


def test_sicher_elsewhere_kept():
    for text in ('Gute Deutschkenntnisse', 'Sicheres Auftreten, gute Deutschkenntnisse',
                 'Arbeitssicherheit in Deutschland', 'Sicherer Umgang mit MS Office, Deutsch B1',
                 'Sichere Englischkenntnisse, Deutsch von Vorteil'):
        assert not requires_unspoken_language(text, ['german']), text
