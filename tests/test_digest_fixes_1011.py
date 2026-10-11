"""
Three leaks from the 11 October digests: a US job kept because a refusal to
consider candidates who need sponsorship read as an offer, "Fluency in English
and in Swedish or Danish" not read as a requirement, and LinkedIn cards scored
with no advert text at all.
"""

from sources import ats
from sources.linkedin import fetch_linkedin_detail
from core.job_filter import is_outside_allowed_countries, requires_unspoken_language, sponsorship_stance

YIPIT = ('Please note that for this position, we are not able to consider candidates '
         'who currently or in the future will require visa sponsorship.')


# ── Refusing candidates who need sponsorship ──────────────────────────────────

def test_refusal_to_consider_candidates_needing_sponsorship():
    assert sponsorship_stance(YIPIT) == 'refuse'
    assert sponsorship_stance('We are unable to hire applicants who need visa sponsorship.') == 'refuse'


def test_offers_still_read_as_offers():
    assert sponsorship_stance('We offer visa sponsorship for this role.') == 'offer'
    assert sponsorship_stance('Candidates who need visa sponsorship are welcome, we provide it.') == 'offer'


def test_us_job_refusing_sponsorship_is_dropped():
    job = {'title': 'Data Product Analyst', 'description': YIPIT, 'location': 'USA'}
    assert is_outside_allowed_countries(
        job, allowed=['RS', 'EU', 'EEA', 'GB', 'CH', 'EUROPE'], sponsorship_only=['US'])


# ── Two languages on top of English ───────────────────────────────────────────

LANGUAGES = ['swedish', 'danish', 'german', 'french']


def test_second_language_after_english_and_in():
    assert requires_unspoken_language(
        'Fluency in English and in Swedish or Danish is essential.', LANGUAGES)
    assert requires_unspoken_language(
        'Fluency in English and Swedish or Danish is essential.', LANGUAGES)
    assert requires_unspoken_language('Fluent in English and in German.', LANGUAGES)


def test_language_as_alternative_to_english_still_kept():
    assert not requires_unspoken_language('Fluent German or English.', LANGUAGES)
    assert not requires_unspoken_language('Fluent English or German required.', LANGUAGES)


def test_required_language_beside_an_alternative_is_still_read():
    assert requires_unspoken_language('Fluent German and English or French.', LANGUAGES)


# ── LinkedIn advert text ──────────────────────────────────────────────────────

class _Response:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


class _Session:
    def __init__(self, response):
        self.response = response
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.response


def test_linkedin_detail_reads_posting_text():
    html = ('<div class="show-more-less-html__markup"><p>Requirements</p>'
            '<ul><li>Fluent Italian</li><li>3 years in sales</li></ul></div>')
    session = _Session(_Response(200, html))
    link = 'https://it.linkedin.com/jobs/view/graduate-sales-engineer-at-emerson-4471880660'
    text = fetch_linkedin_detail(link, session=session, timeout=5)
    assert session.urls == ['https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4471880660']
    assert text.splitlines() == ['Requirements', 'Fluent Italian', '3 years in sales']


def test_linkedin_detail_without_job_id_makes_no_request():
    session = _Session(_Response(200, ''))
    assert fetch_linkedin_detail('https://www.linkedin.com/jobs', session=session, timeout=5) == ''
    assert session.urls == []


def test_linkedin_fetched_after_company_boards(monkeypatch):
    order = []
    monkeypatch.setitem(ats.DETAIL_FETCHERS, 'LinkedIn', lambda job: order.append('LinkedIn') or 'x')
    monkeypatch.setitem(ats.DETAIL_FETCHERS, 'Workday', lambda job: order.append('Workday') or 'x')
    jobs = [
        {'source': 'LinkedIn', 'link': 'https://www.linkedin.com/jobs/view/a-1234567890', 'description': ''},
        {'source': 'Workday', 'link': 'https://x.wd3.myworkdayjobs.com/s/job/a', 'description': ''},
    ]
    ats.enrich_descriptions(jobs, should_fetch=lambda job: True, max_fetches=2)
    assert order == ['Workday', 'LinkedIn']
