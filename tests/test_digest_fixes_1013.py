"""
13 October: about 8 of 10 main-digest jobs on 12.10 were US-only. Google Jobs
searches the United States when given no country and labels a remote job
there "Anywhere", which read as worldwide. JSearch stored the city alone, so a
remote US job had no location at all. And the cloud, which has no Gmail login,
logged a Gmail error on every run.
"""

from core.config import Config
from core.countries import remote_countries
from core.job_filter import fails_eligibility
from sources import serpapi
from sources.gmail_drafts import GmailDraftJobExtractor
from sources.jsearch import jsearch_location


def _europe(monkeypatch):
    monkeypatch.setattr(Config, 'ALLOWED_COUNTRIES', ['RS', 'BA', 'EU', 'EEA', 'GB', 'CH', 'EUROPE'])
    monkeypatch.setattr(Config, 'SPONSORSHIP_ONLY_COUNTRIES', ['US'])


def _google_item(location):
    return {'title': 'Field Service Engineer I', 'company_name': 'Marel',
            'description': 'Service food processing equipment.', 'location': location}


# ── Google Jobs: the searched country is added to a location naming none ─────

def test_remote_pass_anywhere_is_tagged_us_and_dropped(monkeypatch):
    _europe(monkeypatch)
    job = serpapi._normalise([_google_item('Anywhere')], 'Remote', country='United States')[0]
    assert job['location'] == 'Anywhere (United States)'
    assert fails_eligibility(job['title'], job['description'], job['location']) \
        == 'outside_allowed_countries'


def test_market_pass_keeps_its_own_country(monkeypatch):
    _europe(monkeypatch)
    items = [_google_item('Beliebiger Ort'), _google_item('Chemnitz'), _google_item('France')]
    jobs = serpapi._normalise(items, '', country='Germany')
    assert [j['location'] for j in jobs] == ['Beliebiger Ort (Germany)', 'Chemnitz (Germany)', 'France']
    assert all(fails_eligibility(j['title'], j['description'], j['location']) is None for j in jobs)


def test_worldwide_remote_job_from_the_us_pass_is_kept(monkeypatch):
    _europe(monkeypatch)
    item = _google_item('Anywhere')
    item['description'] = 'Fully remote, we hire globally and you can work from anywhere in the world.'
    job = serpapi._normalise([item], 'Remote', country='United States')[0]
    assert fails_eligibility(job['title'], job['description'], job['location']) is None


def test_remote_pass_searches_the_us_explicitly(monkeypatch):
    sent = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {'jobs_results': [_google_item('Anywhere')]}

    def fake_get(url, params, timeout):
        sent.update(params)
        return Response()

    monkeypatch.setenv('SERPAPI_KEY', 'test')
    monkeypatch.setattr(serpapi.requests, 'get', fake_get)
    jobs = serpapi.search_google_jobs('field service engineer')
    assert sent['gl'] == 'us'
    assert jobs[0]['location'] == 'Anywhere (United States)'


# ── JSearch: city, state and country, not the city alone ─────────────────────

def test_jsearch_location_carries_the_country():
    assert jsearch_location({'job_city': None, 'job_state': None, 'job_country': 'US',
                             'job_is_remote': True}) == 'Remote (United States)'
    assert jsearch_location({'job_city': 'Palo Alto', 'job_state': 'CA', 'job_country': 'US',
                             'job_is_remote': False}) == 'Palo Alto, CA'
    assert jsearch_location({'job_city': 'Berlin', 'job_state': 'Berlin', 'job_country': 'DE',
                             'job_is_remote': False}) == 'Berlin, Berlin (Germany)'
    assert jsearch_location({}) == ''


def test_remote_us_jsearch_job_is_dropped(monkeypatch):
    _europe(monkeypatch)
    location = jsearch_location({'job_country': 'US', 'job_is_remote': True})
    assert fails_eligibility('Solutions Engineer', 'Join our team.', location) \
        == 'outside_allowed_countries'


# ── Rows already stored as "Anywhere" or with no location ─────────────────────

JOBGETHER = ('This position is posted by Jobgether on behalf of a partner company. '
             'The offer is available from: United States, Indiana.')
JOBLEADS = ('https://www.jobleads.com/us/job/remote-hydro-mechanical-engineer-turbine-design-'
            'commissioning--united-states--e9d88990daad150424ae677da465d29f3')


def test_jobgether_available_from_line():
    assert remote_countries(JOBGETHER) == {'US'}
    assert remote_countries('The offer is available from: Germany, Austria.') == {'DE', 'AT'}
    assert remote_countries('Available from January, apply now.') == set()


def test_country_spelled_out_in_the_link_path():
    assert remote_countries('', JOBLEADS) == {'US'}
    # A two-letter code in the path is not read, it often names a site language.
    assert remote_countries('', 'https://www.jobs.abbott/us/en/job/31157878/Field-Service-Engineer') == set()


def test_stored_us_rows_are_dropped_at_send_time(monkeypatch):
    _europe(monkeypatch)
    assert fails_eligibility('Inside Sales Representative', JOBGETHER, 'Anywhere') \
        == 'outside_allowed_countries'
    assert fails_eligibility('Remote Hydro Mechanical Engineer', 'Design turbines.', '', JOBLEADS) \
        == 'outside_allowed_countries'


# ── Gmail drafts without a login ──────────────────────────────────────────────

def test_gmail_skipped_quietly_without_a_login(monkeypatch, caplog):
    monkeypatch.setattr(Config, 'GMAIL_USER', None)
    monkeypatch.setattr(Config, 'GMAIL_APP_PASSWORD', None)
    extractor = GmailDraftJobExtractor.__new__(GmailDraftJobExtractor)
    with caplog.at_level('INFO'):
        assert extractor.connect_gmail() is None
    assert not [r for r in caplog.records if r.levelname == 'ERROR']
