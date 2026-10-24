"""
Remote boards keep a job only when its title names a role the search is for.

Until 24.10 RemoteOK, Remotive, The Muse and We Work Remotely kept any job
whose advert contained one of fifteen CV skills as a plain substring. Those
skills include "Make" and "Git", which almost every advert contains, so
nothing was filtered. Jobicy searched by the tags Python and Claude API, and
Arbeitnow ignores its search word, so fifteen searches fetched the same feed.
The main digest filled with remote software and analytics roles.
"""

import pytest

import job_search_smart
from core.config import Config
from core.database import Database
from sources import free_boards
from conftest import FAKE_SKILLS
from test_volume import CV_SKILLS, Recorder, searcher_with


QUERIES = ['remote technical sales engineer', 'junior thermal engineer',
           'large language model', 'engineer', 'remote QA engineer']


# ── Role terms ────────────────────────────────────────────────────────────────

class TestRoleTerms:
    def test_a_query_gives_the_role_it_ends_with(self):
        roles = free_boards.role_terms(QUERIES)
        assert {'sales engineer', 'thermal engineer', 'qa engineer'} <= set(roles)

    def test_a_skill_query_gives_no_role(self):
        assert 'language model' not in free_boards.role_terms(QUERIES)

    def test_a_bare_role_word_is_not_a_role(self):
        # "engineer" alone would keep every software engineer on the board.
        assert 'engineer' not in free_boards.role_terms(QUERIES)

    def test_the_target_role_list_is_included(self):
        assert 'technical account' in free_boards.role_terms([])

    def test_title_screen_terms_are_included(self, monkeypatch):
        monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['Inženjer'])
        assert free_boards.title_names_role('Inzenjer prodaje', free_boards.role_terms([]))


class TestTitleNamesRole:
    ROLES = ['sales engineer', 'test engineer', 'r&d engineer', 'pre-sales']

    @pytest.mark.parametrize('title', [
        'Ubuntu Sales Engineer (Entry-Level)', 'SALES ENGINEER', 'Sales Engineers',
        'R&D Engineer', 'Pre-Sales Consultant'])
    def test_a_title_naming_the_role_is_kept(self, title):
        assert free_boards.title_names_role(title, self.ROLES)

    @pytest.mark.parametrize('title', [
        'Microservices Engineer', 'Contest Engineer', 'Data Analytics Engineer'])
    def test_a_title_naming_another_role_is_dropped(self, title):
        assert not free_boards.title_names_role(title, self.ROLES)

    def test_no_roles_keeps_nothing(self):
        assert not free_boards.title_names_role('Sales Engineer', [])


# ── Each board ────────────────────────────────────────────────────────────────

# The first job names the role only in its text and tags, the way "make" and
# "git" used to let everything through. Only the second may be kept.
TEXT_ONLY = {'title': 'Microservices Engineer', 'text': 'Make tools with our sales engineer.'}
TITLE = {'title': 'Sales Engineer', 'text': 'Quotes for valves.'}


def _serve(monkeypatch, payload):
    monkeypatch.setattr(free_boards, '_get', lambda url, params=None: payload)


class TestBoardsFilterOnTheTitle:
    def test_remoteok(self, monkeypatch):
        _serve(monkeypatch, [{'legal': 'notice'}] + [
            {'position': j['title'], 'company': 'Acme', 'description': j['text'],
             'url': f'https://remoteok.com/{i}', 'tags': ['sales engineer']}
            for i, j in enumerate((TEXT_ONLY, TITLE))])
        jobs = free_boards.search_remoteok(['sales engineer'])
        assert [j['title'] for j in jobs] == ['Sales Engineer']

    def test_remotive(self, monkeypatch):
        _serve(monkeypatch, {'jobs': [
            {'title': j['title'], 'company_name': 'Acme', 'description': j['text'],
             'url': f'https://remotive.com/{i}', 'tags': ['sales engineer']}
            for i, j in enumerate((TEXT_ONLY, TITLE))]})
        jobs = free_boards.search_remotive(['sales engineer'])
        assert [j['title'] for j in jobs] == ['Sales Engineer']

    def test_the_muse(self, monkeypatch):
        _serve(monkeypatch, {'results': [
            {'name': j['title'], 'contents': j['text'], 'company': {'name': 'Acme'},
             'refs': {'landing_page': f'https://themuse.com/{i}'},
             'locations': [{'name': 'Flexible / Remote'}]}
            for i, j in enumerate((TEXT_ONLY, TITLE))]})
        jobs = free_boards.search_the_muse(['sales engineer'], pages=1)
        assert [j['title'] for j in jobs] == ['Sales Engineer']

    def test_we_work_remotely(self, monkeypatch):
        items = ''.join(
            f'<item><title>Acme: {j["title"]}</title><description>{j["text"]}</description>'
            f'<link>https://weworkremotely.com/{i}</link></item>'
            for i, j in enumerate((TEXT_ONLY, TITLE)))
        response = type('Response', (), {
            'status_code': 200, 'text': f'<rss><channel>{items}</channel></rss>'})()
        monkeypatch.setattr(free_boards.requests, 'get', lambda *args, **kwargs: response)
        jobs = free_boards.search_weworkremotely(['sales engineer'])
        assert [j['title'] for j in jobs] == ['Sales Engineer']

    def test_arbeitnow(self, monkeypatch):
        _serve(monkeypatch, {'data': [
            {'title': j['title'], 'company_name': 'Acme', 'description': j['text'],
             'url': f'https://arbeitnow.com/{i}', 'remote': True}
            for i, j in enumerate((TEXT_ONLY, TITLE))]})
        jobs = free_boards.search_arbeitnow(['sales engineer'])
        assert [j['title'] for j in jobs] == ['Sales Engineer']


class TestArbeitnowFetch:
    def test_no_search_word_is_sent(self, monkeypatch):
        # The API ignores it: three different words returned the same 300 jobs.
        sent = []

        def get(url, params=None):
            sent.append(params)
            return {'data': [{'title': 'Sales Engineer', 'company_name': 'Acme',
                              'url': f'https://arbeitnow.com/{len(sent)}', 'remote': True}] * 10}
        monkeypatch.setattr(free_boards, '_get', get)
        free_boards.search_arbeitnow(['sales engineer'], pages=2)
        assert sent == [{'page': 1}, {'page': 2}]


# ── One run of every free board ───────────────────────────────────────────────

@pytest.fixture
def boards(monkeypatch):
    """Every search function in free_boards replaced by a recorder. A board's
    jobs are set in `serves`; its calls land in `calls`."""
    calls = {}
    serves = {}
    names = ('remoteok', 'remotive', 'arbeitnow', 'the_muse', 'jobicy',
             'weworkremotely', 'himalayas', 'adzuna', 'jooble', 'bundesagentur', 'reed')

    def recorder(name):
        def search(*args, **kwargs):
            calls.setdefault(name, []).append(args)
            return [dict(job) for job in serves.get(name, [])]
        return search

    for name in names:
        monkeypatch.setattr(free_boards, f'search_{name}', recorder(name))
    monkeypatch.setattr(free_boards, '_run_regional_boards', lambda *args, **kwargs: [])
    monkeypatch.setattr(free_boards.time, 'sleep', lambda seconds: None)
    return calls, serves


def _job(title, source):
    return {'title': title, 'company': 'Acme', 'description': '', 'link': f'https://x/{title}',
            'salary': None, 'location': 'Remote', 'source': source}


class TestSearchAll:
    QUERIES = ['remote sales engineer', 'sales engineer', 'remote test engineer',
               'test engineer', 'large language model']

    def test_arbeitnow_is_fetched_once(self, boards):
        calls, _ = boards
        free_boards.FreeJobSearcher().search_all(self.QUERIES)
        assert len(calls['arbeitnow']) == 1

    def test_the_title_filter_gets_role_terms_not_cv_skills(self, boards):
        calls, _ = boards
        free_boards.FreeJobSearcher().search_all(self.QUERIES)
        assert calls['remoteok'][0][0] == free_boards.role_terms(self.QUERIES)

    def test_jobicy_tags_are_the_queries_without_remote(self, boards):
        calls, _ = boards
        free_boards.FreeJobSearcher().search_all(self.QUERIES)
        assert [args[0] for args in calls['jobicy']] == [
            'sales engineer', 'test engineer', 'large language model']

    @pytest.mark.parametrize('board, source', [('jobicy', 'Jobicy'), ('himalayas', 'Himalayas')])
    def test_searched_boards_are_filtered_on_the_title_too(self, boards, board, source):
        _, serves = boards
        serves[board] = [_job('Sales Engineer', source), _job('Microservices Engineer', source)]
        jobs = free_boards.FreeJobSearcher().search_all(self.QUERIES)
        assert [j['title'] for j in jobs if j['source'] == source] == ['Sales Engineer']

    def test_country_boards_are_not_title_filtered(self, boards, monkeypatch):
        # Adzuna searches one country at a time and its jobs are judged later
        # by the full filter. Only the remote boards take the title rule.
        _, serves = boards
        monkeypatch.setattr(free_boards, 'adzuna_plan', lambda *args: (['gb'], 1))
        serves['adzuna'] = [_job('Microservices Engineer', 'Adzuna')]
        jobs = free_boards.FreeJobSearcher().search_all(self.QUERIES)
        assert [j['title'] for j in jobs if j['source'] == 'Adzuna'] == ['Microservices Engineer']


# ── The search run ────────────────────────────────────────────────────────────

class KeywordRecorder(Recorder):
    def search_all(self, queries=None, *args, **kwargs):
        self.kwargs = kwargs
        return super().search_all(queries, *args, **kwargs)


def test_cv_skills_no_longer_reach_the_free_boards(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'DATABASE_PATH', str(tmp_path / 'search.db'))
    searcher = searcher_with(CV_SKILLS)
    for name in ('ats', 'jsearch', 'linkedin', 'serpapi', 'apify', 'ddg', 'gmail'):
        setattr(searcher, name, Recorder())
    searcher.free_search = KeywordRecorder()
    searcher.filter = job_search_smart.JobFilter()
    searcher.filter.skills_data = FAKE_SKILLS
    searcher.filter.all_skills = set()
    searcher.validator = job_search_smart.JobValidator()
    searcher.db = Database()
    searcher.db.init_database()

    searcher.search_all_sources()

    assert searcher.free_search.queries
    assert searcher.free_search.kwargs == {}
