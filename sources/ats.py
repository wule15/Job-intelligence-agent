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
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.config import Config
from core.countries import is_remote_place, iso_name, successfactors_location, with_countries
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

# Detail (per-job) fetches get their own non-retrying session and a short
# timeout. Retries multiply the cost of one hung endpoint, and enrichment is
# best-effort, so a slow detail page fails fast rather than blocking the run.
# This is the ATS-side version of the timeout that was added to the Apify call.
DETAIL_TIMEOUT = (4, 8)      # (connect, read) seconds
DETAIL_BUDGET_SECS = 90      # cumulative wall-clock ceiling for the whole enrich pass
detail_session = build_session(attempts=0, user_agent=HEADERS['User-Agent'])

GREENHOUSE_URL = 'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true'
LEVER_URL = 'https://api.lever.co/v0/postings/{slug}?mode=json'
ASHBY_URL = 'https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true'
SMARTRECRUITERS_URL = 'https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit={limit}&offset={offset}'
WORKDAY_URL = 'https://{tenant}.wd{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs'
SUCCESSFACTORS_URL = 'https://{host}/sitemap.xml'

# The Google Jobs RSS namespace SuccessFactors uses for its structured fields.
SF_GOOGLE_NS = '{http://base.google.com/ns/1.0}'

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


def _greenhouse_location(item):
    """
    The location name, with the office countries it lacks appended.

    Greenhouse's location name is free text set by the employer, often a bare
    city ("San Francisco"). The office records carry the country as the last
    part of their location ("San Francisco, California, United States"), so
    that is added in brackets for the country allow-list to read.

    Not for a remote job: the offices say where the company sits, not where
    a remote hire may live, and "Remote (United States)" would be read as a
    US job.
    """
    name = (item.get('location') or {}).get('name') or 'Not stated'
    if is_remote_place(name):
        return name
    countries = []
    for office in item.get('offices') or ():
        office_location = (office or {}).get('location') or ''
        if office_location.strip():
            countries.append(office_location.rsplit(',', 1)[-1].strip())
    return with_countries(name, countries)


def fetch_greenhouse(slug, company_name=None, max_jobs=None):
    data = _get_json(GREENHOUSE_URL.format(slug=slug))
    jobs = []
    for item in (data.get('jobs', [])[:max_jobs] if max_jobs else data.get('jobs', [])):
        jobs.append({
            'title': item.get('title', ''),
            'company': company_name or slug,
            'description': _strip_html(item.get('content', '')),
            'link': canonical_url(item.get('absolute_url', '')),
            'location': _greenhouse_location(item),
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


def _ashby_country(record):
    address = ((record or {}).get('address') or {}).get('postalAddress') or {}
    return address.get('addressCountry') or ''


def _ashby_location(item):
    """The location name, with the address countries of the primary and
    secondary locations appended when the name lacks them."""
    countries = [_ashby_country(item)]
    countries += [_ashby_country(extra) for extra in item.get('secondaryLocations') or ()
                  if isinstance(extra, dict)]
    return with_countries(item.get('location') or 'Not stated', countries)


def fetch_ashby(slug, company_name=None, max_jobs=None):
    data = _get_json(ASHBY_URL.format(slug=slug))
    jobs = []
    for item in (data.get('jobs', [])[:max_jobs] if max_jobs else data.get('jobs', [])):
        jobs.append({
            'title': item.get('title', ''),
            'company': company_name or slug,
            'description': _strip_html(item.get('descriptionPlain') or ''),
            'link': canonical_url(item.get('jobUrl', '')),
            'location': _ashby_location(item),
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
            # fullLocation reads "Kurli, MH, India". Without it, the city and
            # the country's name: the bare lowercase code this used to keep
            # ("Pune, in", "Stuttgart, de") read as US state codes downstream
            # and hid the country from the location rules.
            city = location.get('city') or ''
            country = location.get('country') or ''
            country_name = iso_name(country) or country
            place = (location.get('fullLocation')
                     or ', '.join(p for p in (city, country_name) if p)
                     or 'Not stated')
            jobs.append({
                'title': item.get('name', ''),
                'company': company_name or slug,
                # The list endpoint carries no description. The scorer falls
                # back to the title, which is weaker but not nothing.
                'description': (item.get('jobAd') or {}).get('sections', {}).get(
                    'jobDescription', {}).get('text', '') if item.get('jobAd') else '',
                'link': canonical_url(
                    f"https://jobs.smartrecruiters.com/{slug}/{item.get('id', '')}"),
                'location': place,
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


def _sf_jobs_from_stream(fileobj, company_name, slug, max_jobs):
    """
    Parse a SuccessFactors /sitemap.xml RSS feed into job dicts.

    Split out from the fetch so it can be tested on a fixed feed with no
    network. Streams with iterparse and stops at max_jobs, so a large feed
    (SAP's runs ~1000 jobs and 15 MB) is neither fully buffered nor fully
    downloaded once the cap is reached. Each item carries its full description,
    so no follow-up detail fetch is needed.
    """
    import xml.etree.ElementTree as ET

    def local(tag):
        return tag.rsplit('}', 1)[-1]

    jobs = []
    for _event, elem in ET.iterparse(fileobj, events=('end',)):
        if local(elem.tag) != 'item':
            continue
        title = (elem.findtext('title') or '').strip()
        link = (elem.findtext('link') or '').strip()
        # The feed writes "City, REGION, CC, postcode". The postcode is
        # removed and the ISO code spelled out, so the location rules read
        # the country and not the region code.
        location = successfactors_location(
            elem.findtext(SF_GOOGLE_NS + 'location')
            or elem.findtext('location') or 'Not stated')
        jobs.append({
            'title': title,
            'company': company_name or slug,
            'description': _strip_html(elem.findtext('description') or ''),
            'link': canonical_url(link),
            'location': location,
            'salary': None,
            'source': 'SuccessFactors',
            # The feed carries an expiration date, not a post date, so there is
            # no reliable posted date to record.
            'date_posted': None,
        })
        elem.clear()
        if max_jobs and len(jobs) >= max_jobs:
            break
    return jobs


def fetch_successfactors(slug, company_name=None, max_jobs=300):
    """
    SAP SuccessFactors career sites (Career Site Builder).

    SuccessFactors has no clean public jobs JSON: the OData surface and the
    /services/recruiting endpoint are OAuth gated (401). What Career Site
    Builder does publish, unauthenticated, is a Google Jobs RSS feed at
    /sitemap.xml. This reads that.

    slug is the career-site host, not a company token, e.g. "jobs.sap.com".
    A site that is not Career Site Builder, or has the feed disabled, returns
    no feed and is reported by name like any other failing board.
    """
    response = session.get(
        SUCCESSFACTORS_URL.format(host=slug),
        headers=HEADERS, timeout=TIMEOUT, stream=True)
    if response.status_code == 404:
        raise LookupError('no /sitemap.xml feed, not a Career Site Builder host')
    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')

    # Ask urllib3 to gunzip transparently so iterparse sees XML, not gzip bytes.
    response.raw.decode_content = True
    try:
        return _sf_jobs_from_stream(response.raw, company_name, slug, max_jobs)
    finally:
        response.close()


FETCHERS = {
    'greenhouse': fetch_greenhouse,
    'lever': fetch_lever,
    'ashby': fetch_ashby,
    'smartrecruiters': fetch_smartrecruiters,
    'workday': fetch_workday,
    'successfactors': fetch_successfactors,
}

# Sources whose list endpoint returns titles without descriptions. Scoring a
# title against a CV is close to meaningless, so these need a second request
# per job to be comparable with sources that return full text.
NEEDS_DETAIL_FETCH = {'SmartRecruiters', 'Workday', 'Infostud'}

# Sources whose description is only a teaser. Fetched even when the teaser is
# there, because a two-line snippet scores almost as badly as a bare title.
# Infostud's search results carry about 270 characters, and on that almost
# every Infostud job fell under the score cutoff.
SNIPPET_ONLY_SOURCES = {'Infostud'}

# The teaser-only sources get their own fetch budget, spent first. They used
# to share the 60 fetches with the company boards and came last in the list,
# and live runs had 121 to 148 jobs waiting, so the local board's adverts
# kept their teaser, failed the score, and the regional digest fell from 15
# jobs to 2. One day's local search passes about 60 adverts through the title
# screen, and an advert page takes about half a second, so 100 fetches in 60
# seconds covers a full day with room to spare. The whole enrichment pass
# ends within SNIPPET_BUDGET_SECS plus DETAIL_BUDGET_SECS, plus at most one
# request timeout for each.
SNIPPET_MAX_FETCHES = 100
SNIPPET_BUDGET_SECS = 60


def _get_detail_json(url):
    """One detail fetch: short timeout, no retries, JSON back."""
    response = detail_session.get(
        url, headers={**HEADERS, 'Accept': 'application/json'}, timeout=DETAIL_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')
    return response.json()


def _workday_cxs_url(link):
    """
    Turn a Workday human posting URL into its JSON (cxs) endpoint.

        https://TENANT.wdN.myworkdayjobs.com/SITE/externalPath
        https://TENANT.wdN.myworkdayjobs.com/wday/cxs/TENANT/SITE/externalPath

    The human URL returns JSON without a jobPostingInfo body, so the old code
    fetched it, read nothing, and every Workday job stayed title-only. The cxs
    path is the one that carries the description.
    """
    parts = urlparse(link)
    tenant = parts.netloc.split('.')[0]
    return f'https://{parts.netloc}/wday/cxs/{tenant}{parts.path}'


def _detail_smartrecruiters(job):
    """Full posting text for one SmartRecruiters job."""
    posting_id = job['link'].rstrip('/').rsplit('/', 1)[-1]
    slug = job['link'].split('/')[-2]
    data = _get_detail_json(
        f'https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}')

    sections = (data.get('jobAd') or {}).get('sections') or {}
    # additionalInformation is where employers put work-authorization lines
    # ("Indefinite U.S. work authorized individuals only"), so it is read too.
    parts = [
        _strip_html((sections.get(key) or {}).get('text', ''))
        for key in ('jobDescription', 'qualifications', 'additionalInformation',
                    'companyDescription')
    ]
    return ' '.join(part for part in parts if part)


def _detail_workday(job):
    """
    Full posting text for one Workday job, from the cxs JSON endpoint.

    The list endpoint gives only a city or "2 Locations". The same detail
    response names the country, so it is added to a city here, at no extra
    request. Not to "2 Locations": the detail names only the primary site's
    country, and a job at a German and a US site would read as US only.
    """
    data = _get_detail_json(_workday_cxs_url(job['link']))
    info = data.get('jobPostingInfo') or {}
    country = (info.get('country') or {}).get('descriptor') or ''
    multi_site = re.fullmatch(r'\d+\s+locations?', (job.get('location') or '').strip(), re.I)
    if country and not multi_site:
        job['location'] = with_countries(job.get('location') or '', [country])
    return _strip_html(info.get('jobDescription', ''))


def _detail_infostud(job):
    """Full advert text for one Infostud job, from the page's Next.js data."""
    from sources.free_boards import fetch_infostud_detail
    return fetch_infostud_detail(job['link'], session=detail_session, timeout=DETAIL_TIMEOUT)


DETAIL_FETCHERS = {
    'SmartRecruiters': _detail_smartrecruiters,
    'Workday': _detail_workday,
    'Infostud': _detail_infostud,
}


def enrich_descriptions(jobs, should_fetch, max_fetches=60, max_snippet_fetches=None):
    """
    Fetch full descriptions for jobs whose source returned titles only, or
    only a teaser.

    Two budgets, each a count and a time limit. The teaser-only sources
    (SNIPPET_ONLY_SOURCES) are fetched first, under SNIPPET_MAX_FETCHES and
    SNIPPET_BUDGET_SECS. The title-only company boards follow, under
    max_fetches and DETAIL_BUDGET_SECS. Neither can spend the other's.

    Args:
        jobs: list of job dicts
        should_fetch: callable(job) -> bool, the screen deciding which jobs
            are worth a request. Keeping this out of the connector means the
            scoring logic owns the decision, not the fetcher.
        max_fetches: hard ceiling on requests to the title-only sources per
            run. Reported when hit, never silently applied.
        max_snippet_fetches: the same for the teaser-only sources. None means
            SNIPPET_MAX_FETCHES, read now.

    Returns:
        (enriched_count, skipped_over_budget), both summed over the two budgets
    """
    if max_snippet_fetches is None:
        max_snippet_fetches = SNIPPET_MAX_FETCHES
    candidates = [
        job for job in jobs
        if job.get('source') in NEEDS_DETAIL_FETCH
        and (job.get('source') in SNIPPET_ONLY_SOURCES
             or not (job.get('description') or '').strip())
        and job.get('link')
        and should_fetch(job)
    ]
    snippets = [job for job in candidates if job['source'] in SNIPPET_ONLY_SOURCES]
    title_only = [job for job in candidates if job['source'] not in SNIPPET_ONLY_SOURCES]

    enriched, over_budget = 0, 0
    for group, limit, seconds, label in (
            (snippets, max_snippet_fetches, SNIPPET_BUDGET_SECS, 'teaser-only'),
            (title_only, max_fetches, DETAIL_BUDGET_SECS, 'title-only')):
        done, skipped = _fetch_details(group, limit, seconds, label)
        enriched += done
        over_budget += skipped

    logger.info(
        f"[ATS] Fetched descriptions for {enriched} of {len(candidates)} screened jobs"
    )
    return enriched, over_budget


def _fetch_details(candidates, max_fetches, budget_secs, label):
    """
    Fetch descriptions for one group of candidates, under its own count and
    its own wall-clock limit. Returns (enriched_count, skipped_over_budget).
    """
    over_budget = max(0, len(candidates) - max_fetches)
    if over_budget:
        logger.warning(
            f"[ATS] {len(candidates)} {label} jobs passed the title screen but the "
            f"budget is {max_fetches}. Skipping {over_budget}, they keep the text "
            f"their source gave."
        )

    enriched = 0
    # A cumulative wall-clock ceiling so a run of slow detail pages cannot drag
    # the whole daily run out. This is the same lesson as the Apify stall: cap
    # the time, not just the count.
    deadline = time.monotonic() + budget_secs
    budget = candidates[:max_fetches]
    for i, job in enumerate(budget):
        if time.monotonic() > deadline:
            logger.warning(
                f"[ATS] {label} enrichment hit its {budget_secs}s budget after {i} "
                f"fetches; {len(budget) - i} jobs keep the text their source gave."
            )
            break
        try:
            description = DETAIL_FETCHERS[job['source']](job)
        except Exception as e:
            logger.debug(f"[ATS] detail fetch failed for {job.get('title')!r}: {type(e).__name__}")
            continue

        if description:
            job['description'] = description
            enriched += 1
    return enriched, over_budget


# ── Is a stored posting still open? ──────────────────────────────────────────
# The digest sends jobs stored up to a week ago, so a posting can close between
# the day its board was read and the day it is sent. Each vendor's own API
# says whether one posting is still listed, which is more reliable than the
# public page: a closed Greenhouse job redirects to the board with HTTP 200,
# and an Ashby page answers 200 even for an id that never existed.
#
# posting_is_open answers False only on proof that the posting is closed,
# None when it cannot tell (the caller then falls back to the page check), and
# True otherwise. A timeout, a rate limit or a server error is not proof, so
# it counts as open: sending one closed job costs less than losing an open one.
# Requests go through detail_session, no retries and a short timeout, and only
# to the vendors' fixed API hosts or a Workday tenant host.
_ASHBY_BOARDS = {}           # slug -> set of open posting ids, None if unreadable
_GREENHOUSE_SLUGS = None     # company name, lower case -> board slug
_UNKNOWN = object()          # cache marker for "the board answered nothing usable"

_GREENHOUSE_HOSTS = ('boards.greenhouse.io', 'job-boards.greenhouse.io')
_CLOSED_STATUSES = (404, 410)


def reset_liveness_caches():
    """Forget the boards and the company list read so far. Called once per run."""
    global _GREENHOUSE_SLUGS
    _ASHBY_BOARDS.clear()
    _GREENHOUSE_SLUGS = None


def _greenhouse_slug_for(company):
    """The board slug configured for a Greenhouse company, by name."""
    global _GREENHOUSE_SLUGS
    if _GREENHOUSE_SLUGS is None:
        _GREENHOUSE_SLUGS = {
            (c.get('name') or c['slug']).strip().lower(): c['slug']
            for c in ATSJobSearcher().load_companies() if c.get('ats') == 'greenhouse'
        }
    return _GREENHOUSE_SLUGS.get((company or '').strip().lower())


def _status_check(url):
    """GET url and read the status: False on 404 or 410, True otherwise."""
    response = detail_session.get(
        url, headers={**HEADERS, 'Accept': 'application/json'}, timeout=DETAIL_TIMEOUT)
    return response.status_code not in _CLOSED_STATUSES


def _greenhouse_open(parts, company):
    # gh_jid is always Greenhouse's own id. A number in the path of a custom
    # careers domain is read only when there is no gh_jid.
    job_id = (parse_qs(parts.query).get('gh_jid') or [''])[0]
    if not job_id:
        match = re.search(r'/jobs/(\d+)', parts.path)
        job_id = match.group(1) if match else ''
    slug = None
    if parts.netloc in _GREENHOUSE_HOSTS:
        slug = parts.path.strip('/').split('/')[0] or None
    slug = slug or _greenhouse_slug_for(company)
    if not (slug and job_id.isdigit()):
        return None
    return _status_check(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}')


def _ashby_open(parts):
    path = parts.path.strip('/').split('/')
    if parts.netloc != 'jobs.ashbyhq.com' or len(path) < 2:
        return None
    slug, job_id = path[0], path[1]
    if slug not in _ASHBY_BOARDS:
        # One board read per run answers every Ashby job at that company.
        _ASHBY_BOARDS[slug] = None
        try:
            response = detail_session.get(
                ASHBY_URL.format(slug=slug), headers=HEADERS, timeout=DETAIL_TIMEOUT)
            jobs = response.json().get('jobs') if response.status_code == 200 else None
            if isinstance(jobs, list):
                # An empty list may be a glitch, and acting on it would drop
                # every job at the company, so it proves nothing.
                ids = set()
                for item in jobs:
                    ids.add(str(item.get('id') or ''))
                    ids.add(urlparse(item.get('jobUrl') or '').path.rstrip('/').rsplit('/', 1)[-1])
                ids.discard('')
                _ASHBY_BOARDS[slug] = ids or _UNKNOWN
        except Exception as e:
            logger.debug(f"[ATS] Ashby board {slug} unreadable: {type(e).__name__}")
    ids = _ASHBY_BOARDS[slug]
    if ids is None:
        return True
    if ids is _UNKNOWN:
        return None
    return job_id in ids


def _lever_open(parts):
    path = parts.path.strip('/').split('/')
    if parts.netloc != 'jobs.lever.co' or len(path) < 2:
        return None
    return _status_check(f'https://api.lever.co/v0/postings/{path[0]}/{path[1]}')


def _smartrecruiters_open(parts):
    path = parts.path.strip('/').split('/')
    if parts.netloc != 'jobs.smartrecruiters.com' or len(path) < 2:
        return None
    response = detail_session.get(
        f'https://api.smartrecruiters.com/v1/companies/{path[0]}/postings/{path[1]}',
        headers={**HEADERS, 'Accept': 'application/json'}, timeout=DETAIL_TIMEOUT)
    if response.status_code in _CLOSED_STATUSES:
        return False
    # The API answers 400 for an id it does not recognise, which is not the
    # same as a posting that closed.
    if response.status_code == 400:
        return None
    if response.status_code == 200:
        return response.json().get('active') is not False
    return True


def _workday_open(link, parts):
    if not parts.netloc.endswith('.myworkdayjobs.com'):
        return None
    return _status_check(_workday_cxs_url(link))


def posting_is_open(link, source, company=''):
    """
    Whether a company-board posting is still listed by its vendor.

    Returns False only on proof it is closed, None when the vendor or the
    link gives no way to tell, and True otherwise (any error included).
    """
    parts = urlparse(link or '')
    try:
        if source == 'Greenhouse':
            return _greenhouse_open(parts, company)
        if source == 'Ashby':
            return _ashby_open(parts)
        if source == 'Lever':
            return _lever_open(parts)
        if source == 'SmartRecruiters':
            return _smartrecruiters_open(parts)
        if source == 'Workday':
            return _workday_open(link, parts)
        # SuccessFactors has no per-posting endpoint without a login.
        return None
    except Exception as e:
        logger.debug(f"[ATS] liveness check failed for {link}: {type(e).__name__}")
        return True


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
