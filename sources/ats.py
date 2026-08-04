"""
Company careers pages, read directly from their applicant tracking system.

Greenhouse, Lever and Ashby all expose an unauthenticated JSON endpoint per
company board. No key, no quota, no rate limit worth worrying about, and the
listing is the company's own posting rather than an aggregator's copy of it.

This is the highest signal source in the project. Aggregators tell you what
is on the market. This tells you what a specific company you want to work for
is hiring for today, with the full description attached.

Companies are listed in config/companies.json. Copy
config/companies.example.json and edit it. Finding a slug takes one look at
the careers page URL:

    boards.greenhouse.io/acmefluid          -> greenhouse, "acmefluid"
    jobs.lever.co/acmefluid                 -> lever,      "acmefluid"
    jobs.ashbyhq.com/acmefluid              -> ashby,      "acmefluid"
"""

import json
from pathlib import Path

from core.config import Config
from core.http_client import build_session
from core.job_normalize import canonical_url
from core.utils import setup_logging

logger = setup_logging('sources.ats')

TIMEOUT = 12
HEADERS = {'User-Agent': 'job-intelligence-agent (+https://github.com/)'}

# One session for the whole module, so retries and connection pooling apply
# to every board. Twenty boards on one run means a lot of reconnecting
# otherwise.
session = build_session(user_agent=HEADERS['User-Agent'])

GREENHOUSE_URL = 'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true'
LEVER_URL = 'https://api.lever.co/v0/postings/{slug}?mode=json'
ASHBY_URL = 'https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true'
SMARTRECRUITERS_URL = 'https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}&offset={offset}'
WORKDAY_URL = 'https://{tenant}.wd{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs'

# Page size for the vendors that paginate.
PAGE_SIZE = 100


def _strip_html(text):
    """
    Reduce a description to plain text for keyword scoring.

    Unescape before stripping, and unescape again after. The order matters
    and getting it wrong is silent.

    Greenhouse returns its content HTML-escaped, so the raw string holds
    "&lt;h2&gt;" rather than "<h2>". Stripping first finds no tags to strip,
    and the later unescape then turns the entities into live markup, so the
    description arrives full of tags and attributes. Those feed straight into
    keyword scoring, where a class name or a data attribute containing a
    skill term inflates the score of a job that never mentioned it.

    The second unescape catches entities that were double encoded, which
    several boards do.
    """
    if not text:
        return ''
    import html
    import re
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _get_json(url):
    response = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    if response.status_code == 404:
        raise LookupError('board not found, check the slug')
    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')
    return response.json()


def fetch_greenhouse(slug, company_name=None, max_jobs=None):
    data = _get_json(GREENHOUSE_URL.format(slug=slug))
    jobs = []
    for item in (data.get('jobs', [])[:max_jobs] if max_jobs else data.get('jobs', [])):
        jobs.append({
            'title': item.get('title', ''),
            'company': company_name or slug,
            'description': _strip_html(item.get('content', '')),
            'link': canonical_url(item.get('absolute_url', '')),
            'location': (item.get('location') or {}).get('name', 'Not stated'),
            'salary': None,
            'source': 'Greenhouse',
            'date_posted': item.get('updated_at'),
        })
    return jobs


def fetch_lever(slug, company_name=None, max_jobs=None):
    data = _get_json(LEVER_URL.format(slug=slug))
    jobs = []
    for item in (data[:max_jobs] if max_jobs else data):
        categories = item.get('categories') or {}
        jobs.append({
            'title': item.get('text', ''),
            'company': company_name or slug,
            'description': _strip_html(item.get('descriptionPlain') or item.get('description', '')),
            'link': canonical_url(item.get('hostedUrl', '')),
            'location': categories.get('location', 'Not stated'),
            'salary': categories.get('commitment'),
            'source': 'Lever',
            'date_posted': None,
        })
    return jobs


def fetch_ashby(slug, company_name=None, max_jobs=None):
    data = _get_json(ASHBY_URL.format(slug=slug))
    jobs = []
    for item in (data.get('jobs', [])[:max_jobs] if max_jobs else data.get('jobs', [])):
        jobs.append({
            'title': item.get('title', ''),
            'company': company_name or slug,
            'description': _strip_html(item.get('descriptionPlain') or ''),
            'link': canonical_url(item.get('jobUrl', '')),
            'location': item.get('location', 'Not stated'),
            'salary': (item.get('compensation') or {}).get('summary'),
            'source': 'Ashby',
            'date_posted': item.get('publishedAt'),
        })
    return jobs


def fetch_smartrecruiters(slug, company_name=None, max_jobs=300):
    """
    SmartRecruiters. This is what most large European industrials use.

    Paginated, and some boards are very large: Bosch alone lists over 4,000
    postings. max_jobs caps the pull so one employer cannot flood a digest.
    """
    jobs = []
    offset = 0

    while len(jobs) < max_jobs:
        page = _get_json(SMARTRECRUITERS_URL.format(
            slug=slug, limit=min(PAGE_SIZE, max_jobs - len(jobs)), offset=offset))
        content = page.get('content', [])
        if not content:
            break

        for item in content:
            location = item.get('location') or {}
            city = location.get('city') or ''
            country = location.get('country') or ''
            jobs.append({
                'title': item.get('name', ''),
                'company': company_name or slug,
                # The list endpoint carries no description. The scorer falls
                # back to the title, which is weaker but not nothing.
                'description': (item.get('jobAd') or {}).get('sections', {}).get(
                    'jobDescription', {}).get('text', '') if item.get('jobAd') else '',
                'link': canonical_url(
                    f"https://jobs.smartrecruiters.com/{slug}/{item.get('id', '')}"),
                'location': ', '.join(p for p in (city, country) if p) or 'Not stated',
                'salary': None,
                'source': 'SmartRecruiters',
                'date_posted': item.get('releasedDate'),
            })

        offset += len(content)
        if len(content) < PAGE_SIZE:
            break

    return jobs


def fetch_workday(slug, company_name=None, max_jobs=300, wd='5', site='External'):
    """
    Workday. Covers most of the large industrial and defence employers that
    are on none of the other vendors.

    Unlike the others this needs three values, not one, and they come from
    the careers page URL:

        https://TENANT.wdN.myworkdayjobs.com/SITE
                ^^^^^^   ^                  ^^^^

    So https://wattswater.wd5.myworkdayjobs.com/External gives
        "slug": "wattswater", "wd": "5", "site": "External"

    A wrong site returns HTTP 422 and a wrong tenant returns 401. Both are
    reported by name rather than silently swallowed.
    """
    url = WORKDAY_URL.format(tenant=slug, wd=wd, site=site)
    jobs = []
    offset = 0
    # Workday reports the result count on the first page only. Every later
    # page returns total: 0, so this has to be captured once and kept.
    # Comparing offset against the per-page value stopped every board after
    # two pages, which looked like a small employer rather than a bug.
    total = None

    while len(jobs) < max_jobs:
        response = session.post(
            url,
            json={'appliedFacets': {}, 'limit': 20, 'offset': offset, 'searchText': ''},
            headers={**HEADERS, 'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=TIMEOUT,
        )
        if response.status_code == 401:
            raise LookupError(f'Workday tenant {slug!r} rejected the request, check the tenant name')
        if response.status_code == 422:
            raise LookupError(f'Workday site {site!r} not found for tenant {slug!r}')
        if response.status_code != 200:
            raise RuntimeError(f'HTTP {response.status_code}')

        data = response.json()
        if total is None:
            total = data.get('total') or 0

        postings = data.get('jobPostings', [])
        if not postings:
            break

        for item in postings:
            path = item.get('externalPath', '')
            jobs.append({
                'title': item.get('title', ''),
                'company': company_name or slug,
                'description': item.get('bulletFields') and ' '.join(item['bulletFields']) or '',
                'link': canonical_url(f'https://{slug}.wd{wd}.myworkdayjobs.com/{site}{path}'),
                'location': item.get('locationsText', 'Not stated'),
                'salary': None,
                'source': 'Workday',
                'date_posted': item.get('postedOn'),
            })

        offset += len(postings)
        if total and offset >= total:
            break

    return jobs


FETCHERS = {
    'greenhouse': fetch_greenhouse,
    'lever': fetch_lever,
    'ashby': fetch_ashby,
    'smartrecruiters': fetch_smartrecruiters,
    'workday': fetch_workday,
}

# Sources whose list endpoint returns titles without descriptions. Scoring a
# title against a CV is close to meaningless, so these need a second request
# per job to be comparable with sources that return full text.
NEEDS_DETAIL_FETCH = {'SmartRecruiters', 'Workday'}


def _detail_smartrecruiters(job):
    """Full posting text for one SmartRecruiters job."""
    posting_id = job['link'].rstrip('/').rsplit('/', 1)[-1]
    slug = job['link'].split('/')[-2]
    data = _get_json(f'https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}')

    sections = (data.get('jobAd') or {}).get('sections') or {}
    parts = [
        _strip_html((sections.get(key) or {}).get('text', ''))
        for key in ('jobDescription', 'qualifications', 'companyDescription')
    ]
    return ' '.join(part for part in parts if part)


def _detail_workday(job):
    """Full posting text for one Workday job."""
    response = session.get(
        job['link'],
        headers={**HEADERS, 'Accept': 'application/json'},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')

    info = response.json().get('jobPostingInfo') or {}
    return _strip_html(info.get('jobDescription', ''))


DETAIL_FETCHERS = {
    'SmartRecruiters': _detail_smartrecruiters,
    'Workday': _detail_workday,
}


def enrich_descriptions(jobs, should_fetch, max_fetches=60):
    """
    Fetch full descriptions for jobs whose source returned titles only.

    Args:
        jobs: list of job dicts
        should_fetch: callable(job) -> bool, the screen deciding which jobs
            are worth a request. Keeping this out of the connector means the
            scoring logic owns the decision, not the fetcher.
        max_fetches: hard ceiling on requests per run. Reported when hit,
            never silently applied.

    Returns:
        (enriched_count, skipped_over_budget)
    """
    candidates = [
        job for job in jobs
        if job.get('source') in NEEDS_DETAIL_FETCH
        and not (job.get('description') or '').strip()
        and job.get('link')
        and should_fetch(job)
    ]

    over_budget = max(0, len(candidates) - max_fetches)
    if over_budget:
        logger.warning(
            f"[ATS] {len(candidates)} jobs passed the title screen but the budget is "
            f"{max_fetches}. Skipping {over_budget}, they keep title-only scoring."
        )

    enriched = 0
    for job in candidates[:max_fetches]:
        try:
            description = DETAIL_FETCHERS[job['source']](job)
        except Exception as e:
            logger.debug(f"[ATS] detail fetch failed for {job.get('title')!r}: {type(e).__name__}")
            continue

        if description:
            job['description'] = description
            enriched += 1

    logger.info(
        f"[ATS] Fetched descriptions for {enriched} of {len(candidates)} screened jobs"
    )
    return enriched, over_budget


class ATSJobSearcher:
    """Read job listings straight from company careers pages."""

    def __init__(self, config_path=None):
        self.config_path = Path(config_path or (Path(Config.CONFIG_DIR) / 'companies.json'))

    def load_companies(self):
        """
        Load the company list.

        Returns an empty list and says so if the file is absent. A missing
        config is a normal state on a fresh clone, not an error.
        """
        if not self.config_path.exists():
            logger.info(
                f"[ATS] No company list at {self.config_path}. "
                "Copy config/companies.example.json to companies.json to enable this source."
            )
            return []

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"[ATS] Could not read {self.config_path}: {e}")
            return []

        companies = data.get('companies', data if isinstance(data, list) else [])
        valid = [c for c in companies if c.get('ats') in FETCHERS and c.get('slug')]

        skipped = len(companies) - len(valid)
        if skipped:
            logger.warning(f"[ATS] Skipped {skipped} entries with an unknown ats or no slug")

        return valid

    def search_all(self, queries=None):
        """
        Fetch every configured company board.

        `queries` is accepted for interface symmetry with the other sources
        and deliberately ignored. An ATS board is small enough to take whole
        and let the scorer decide, which is more reliable than guessing
        each vendor's search syntax.
        """
        companies = self.load_companies()
        if not companies:
            return []

        all_jobs = []
        failed = []

        for company in companies:
            ats = company['ats']
            slug = company['slug']
            name = company.get('name', slug)

            # Workday needs two extra values from the careers page URL.
            extra = {k: company[k] for k in ('wd', 'site') if k in company}
            if 'max_jobs' in company:
                extra['max_jobs'] = company['max_jobs']

            try:
                jobs = FETCHERS[ats](slug, company_name=name, **extra)
                all_jobs.extend(jobs)
                logger.info(f"[ATS] {name} ({ats}): {len(jobs)} jobs")
            except Exception as e:
                failed.append(f"{name} ({ats}): {type(e).__name__}")
                logger.warning(f"[ATS] {name} ({ats}) failed: {type(e).__name__}: {e}")

        if failed:
            logger.warning(f"[ATS] {len(failed)} of {len(companies)} boards failed: {'; '.join(failed)}")

        logger.info(f"[ATS] {len(all_jobs)} jobs from {len(companies) - len(failed)} boards")
        return all_jobs


if __name__ == '__main__':
    for job in ATSJobSearcher().search_all()[:10]:
        print(f"{job['source']:11} {job['company']:24} {job['title']}")
