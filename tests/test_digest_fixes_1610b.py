"""
US-only remote jobs from the remote boards, 16 October 2026.

Four of the 47 jobs sent on 16.10 could only be taken from the United States:
KPI Solutions and Dreem Health from RemoteOK, GigaKOM from jobmesh and 6sense
from We Work Remotely. Their feeds said "Remote" or nothing, and each page's
schema.org JobPosting data said where the hire must live. The fix of 13.10
covered Google Jobs and JSearch only. These tests pin down the changes:

  - RemoteOK keeps the API's location field, and We Work Remotely keeps the
    feed's country, as "Remote, <place>", so the allow-list can judge them.
  - The page check reads applicantLocationRequirements from a remote job's
    page. A job limited to countries the user cannot take is not sent, and is
    held back for good, which also covers the rows stored before this fix.

No test makes a network call.
"""

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telegram_sender  # noqa: E402
from core.config import Config  # noqa: E402
from core.countries import applicant_countries  # noqa: E402
from core.database import Database  # noqa: E402
from core.job_filter import fails_eligibility, is_applicant_restricted  # noqa: E402
from sources import free_boards  # noqa: E402

ALLOWED = ['RS', 'BA', 'EU', 'EEA', 'GB', 'CH', 'EUROPE']


def page(requirements, kind='TELECOMMUTE', extra=''):
    """A job page with one JobPosting block, the shape RemoteOK serves."""
    return ('<html><head><script type="application/ld+json">'
            '{"@context": "http://schema.org", "@type": "JobPosting", '
            f'"title": "Role", "jobLocationType": "{kind}", '
            f'"applicantLocationRequirements": {requirements}{extra}}}'
            '</script></head><body>Apply now</body></html>')


US_ONLY = page('[{"@type": "Country", "name": "United States"}]')


# ── Reading the page ─────────────────────────────────────────────────────────

class TestApplicantCountries:
    def test_a_country_name(self):
        assert applicant_countries(US_ONLY) == {'US'}

    def test_iso_codes_as_we_work_remotely_writes_them(self):
        assert applicant_countries(page('[{"@type": "Country", "name": "US"}]')) == {'US'}
        assert applicant_countries(page('{"@type": "Country", "name": "GB"}')) == {'GB'}

    def test_a_raw_line_break_in_a_string_is_read(self):
        """We Work Remotely leaves line breaks inside its JSON strings."""
        html = page('[{"@type": "Country", "name": "US"}]',
                    extra=', "description": "Line one\nline two"')
        assert applicant_countries(html) == {'US'}

    def test_a_long_list_is_kept_whole(self):
        names = ', '.join(f'{{"@type": "Country", "name": "{c}"}}' for c in ('US', 'RS', 'DE'))
        assert applicant_countries(page(f'[{names}]')) == {'US', 'RS', 'DE'}

    def test_anywhere_sets_no_limit(self):
        assert applicant_countries(page('[{"@type": "Country", "name": "Anywhere"}]')) == set()

    def test_an_entry_it_cannot_place_sets_no_limit(self):
        names = '[{"@type": "Country", "name": "US"}, {"@type": "Country", "name": "Narnia"}]'
        assert applicant_countries(page(names)) == set()

    def test_a_job_that_is_not_remote_is_not_read(self):
        """Infostud fills the field with the office's country on an on-site
        job, and an on-site US role may still offer a visa."""
        assert applicant_countries(page('{"@type": "Country", "name": "US"}', kind='')) == set()

    def test_a_graph_block_is_searched(self):
        html = ('<script type="application/ld+json">{"@graph": [{"@type": "WebPage"}, '
                '{"@type": "JobPosting", "jobLocationType": "TELECOMMUTE", '
                '"applicantLocationRequirements": {"name": "United States"}}]}</script>')
        assert applicant_countries(html) == {'US'}

    def test_no_block_or_broken_json_sets_no_limit(self):
        assert applicant_countries('<html>Apply now</html>') == set()
        assert applicant_countries('<script type="application/ld+json">{oops</script>') == set()
        assert applicant_countries(None) == set()


class TestIsApplicantRestricted:
    def test_a_country_outside_the_list(self):
        assert is_applicant_restricted({'US'}, ALLOWED)

    def test_an_allowed_country(self):
        assert not is_applicant_restricted({'GB'}, ALLOWED)
        assert not is_applicant_restricted({'US', 'PL'}, ALLOWED)

    def test_a_sponsorship_only_country_still_counts_as_outside(self, monkeypatch):
        """The hire must already live there. No visa moves the user."""
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', ['US'])
        assert is_applicant_restricted({'US'}, ALLOWED)

    def test_an_unknown_limit_is_never_restricted(self):
        assert not is_applicant_restricted(set(), ALLOWED)
        assert not is_applicant_restricted(None, ALLOWED)

    def test_inert_in_the_public_default(self):
        assert not is_applicant_restricted({'US'})


# ── The feeds ────────────────────────────────────────────────────────────────

class TestRemoteAt:
    def test_a_place_goes_next_to_remote(self):
        assert free_boards.remote_at('United States') == 'Remote, United States'
        assert free_boards.remote_at('Germany') == 'Remote, Germany'

    def test_blank_stays_remote(self):
        assert free_boards.remote_at('') == 'Remote'
        assert free_boards.remote_at(None) == 'Remote'

    def test_a_place_that_says_remote_is_kept(self):
        assert free_boards.remote_at('Remote - US') == 'Remote - US'
        assert free_boards.remote_at('Worldwide') == 'Worldwide'


class TestRemoteOk:
    def test_the_api_location_is_kept(self, monkeypatch):
        monkeypatch.setattr(free_boards, '_get', lambda url, params=None: [
            {'legal': 'notice'},
            {'position': 'Automation Specialist', 'company': 'Dreem Health',
             'description': 'Automation work.', 'url': 'https://remoteok.com/1',
             'location': 'United States'},
            {'position': 'Automation Lead', 'company': 'Open Co',
             'description': 'Automation work.', 'url': 'https://remoteok.com/2',
             'location': ''},
        ])
        jobs = free_boards.search_remoteok(['automation'])
        assert [j['location'] for j in jobs] == ['Remote, United States', 'Remote']

    def test_a_us_location_is_now_dropped(self, monkeypatch):
        monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', ALLOWED)
        monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', ['US'])
        assert fails_eligibility('Automation Specialist', 'Automation work.',
                                 'Remote, United States') == 'outside_allowed_countries'
        assert fails_eligibility('Automation Specialist', 'Automation work.', 'Remote') is None


class TestWeWorkRemotely:
    RSS = ('<rss><channel>'
           '<item><title>6sense: IT Support Specialist</title><description>IT support</description>'
           '<link>https://weworkremotely.com/1</link><region>Anywhere in the World</region>'
           '<country>\U0001F1FA\U0001F1F8 United States of America</country></item>'
           '<item><title>Huntress: Support Engineer</title><description>IT support</description>'
           '<link>https://weworkremotely.com/2</link><region>Anywhere in the World</region>'
           '<country></country></item>'
           '</channel></rss>')

    def test_the_country_is_kept(self, monkeypatch):
        response = type('Response', (), {'status_code': 200, 'text': self.RSS})()
        monkeypatch.setattr(free_boards.requests, 'get', lambda *args, **kwargs: response)
        jobs = free_boards.search_weworkremotely(['support'])
        assert [j['location'] for j in jobs] == ['Remote, United States of America', 'Remote']


# ── The digest ───────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'fixes1610b.db')
    monkeypatch.setattr(Config, 'DATABASE_PATH', path)
    database = Database(db_path=path)
    database.init_database()
    telegram_sender.init_telegram_tracking()
    yield database
    database.close()


@pytest.fixture
def pages(monkeypatch):
    """Serves a page per link instead of the network. Unlisted links get an
    ordinary page with no JobPosting block."""
    served = {}

    def fake_get(url, **kwargs):
        text = served.get(url, '<html>Apply now</html>')
        return type('Response', (), {'status_code': 200, 'text': text, 'headers': {}})()

    monkeypatch.setattr(telegram_sender.requests, 'get', fake_get)
    monkeypatch.setattr(telegram_sender, '_is_internal_url', lambda url: False)
    monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', ALLOWED)
    telegram_sender.check_link_live.cache_clear()
    telegram_sender.reset_liveness()
    yield served
    telegram_sender.check_link_live.cache_clear()
    telegram_sender.reset_liveness()


def add(db, title, company, score, source='RemoteOK'):
    link = f'https://example.com/{title}/{company}'.replace(' ', '-')
    return db.add_job(title, company, 'desc', link, source=source,
                      relevance_score=score, location='Remote'), link


def held_back():
    with closing(sqlite3.connect(Config.DATABASE_PATH)) as conn:
        return {row[0] for row in conn.execute('SELECT job_id FROM telegram_sent_jobs')}


class TestDigest:
    def test_a_us_only_page_is_not_live(self, pages):
        pages['https://remoteok.com/us'] = US_ONLY
        assert telegram_sender.is_job_live('https://remoteok.com/us', 'RemoteOK') is False
        assert telegram_sender.liveness_summary()['restricted'] == ['https://remoteok.com/us']
        assert telegram_sender.liveness_summary()['closed_page'] == []

    def test_an_allowed_or_unstated_page_is_live(self, pages):
        pages['https://remoteok.com/uk'] = page('[{"@type": "Country", "name": "GB"}]')
        assert telegram_sender.is_job_live('https://remoteok.com/uk', 'RemoteOK') is True
        assert telegram_sender.is_job_live('https://remoteok.com/plain', 'RemoteOK') is True
        assert telegram_sender.liveness_summary()['restricted'] == []

    def test_a_stored_us_job_gives_its_slot_away_and_is_held_back(self, db, pages, monkeypatch,
                                                                  capsys):
        us_id, us_link = add(db, 'Deployment Engineer', 'KPI Solutions', 80.0)
        open_id, _ = add(db, 'Automation Engineer', 'Open Co', 60.0)
        pages[us_link] = US_ONLY
        sent = []
        monkeypatch.setattr(telegram_sender, 'send_telegram_message',
                            lambda message: sent.append(message) or True)
        monkeypatch.setattr(telegram_sender, '_health_note', lambda: '')

        telegram_sender.main()

        chat = '\n'.join(sent)
        assert 'Automation Engineer' in chat and 'Deployment Engineer' not in chat
        assert us_id in held_back() and open_id in held_back()
        out = capsys.readouterr().out
        assert (f'[ineligible:applicant_countries US] id={us_id} | Deployment Engineer | '
                f'KPI Solutions | {us_link}') in out
        assert '1 held back as limited to other countries' in out

    def test_held_back_rows_are_kept_not_deleted(self, db, pages):
        us_id, us_link = add(db, 'Deployment Engineer', 'KPI Solutions', 80.0)
        pages[us_link] = US_ONLY
        assert telegram_sender.is_job_live(us_link, 'RemoteOK') is False
        assert telegram_sender.hold_back_restricted([us_link]) == 1
        with closing(sqlite3.connect(Config.DATABASE_PATH)) as conn:
            assert conn.execute('SELECT id FROM jobs WHERE id = ?', (us_id,)).fetchall()
        assert telegram_sender.hold_back_restricted([us_link]) == 0
