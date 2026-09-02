"""
Free job search sources, no API keys required.
Replaces JSearch (paid/quota-limited) with:
  - RemoteOK          (remote only, no key)
  - Remotive          (remote only, no key)
  - Arbeitnow         (remote + EU, no key)
  - The Muse          (global, no key)
  - Jobicy            (remote only, no key)
  - We Work Remotely  (remote only, RSS, no key)
  - Himalayas         (remote only, no key)
- Adzuna (global, salary data, free API key required, ADZUNA_APP_ID + ADZUNA_APP_KEY)
- Jooble (global aggregator, free API key required, JOOBLE_API_KEY)
"""

import json
import os
import re
import xml.etree.ElementTree as ET
import requests
import time
from datetime import datetime
from urllib.parse import quote
from core.utils import setup_logging

logger = setup_logging('sources.free_boards')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; JobDigest/1.0)'
}
TIMEOUT = 15


def _get(url, params=None):
    """GET with timeout + error handling. Returns parsed JSON or None."""
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"HTTP {r.status_code} from {url}")
    except Exception as e:
        logger.warning(f"Request failed {url}: {e}")
    return None


# ── normaliser ────────────────────────────────────────────────────────────────

def _job(title, company, description, link, salary=None, location='Remote', source='Free'):
    return {
        'title': (title or '').strip(),
        'company': (company or '').strip(),
        'description': (description or '').strip(),
        'link': (link or '').strip(),
        'salary': salary,
        'location': location or 'Remote',
        'source': source,
    }


# ── RemoteOK ──────────────────────────────────────────────────────────────────

def search_remoteok(keywords):
    """
    RemoteOK public API, https://remoteok.com/api
    Returns all remote jobs; we filter by keyword client-side.
    Free, no key, ~1 req/s polite limit.
    """
    logger.info("[RemoteOK] Fetching jobs...")
    data = _get('https://remoteok.com/api')
    if not data:
        return []

    jobs = []
    kw_lower = [k.lower() for k in keywords]

    for item in data:
        if not isinstance(item, dict) or 'position' not in item:
            continue

        title = item.get('position', '')
        desc = item.get('description', '') or ''
        tags = ' '.join(item.get('tags', []))
        text = (title + ' ' + desc + ' ' + tags).lower()

        if not any(k in text for k in kw_lower):
            continue

        jobs.append(_job(
            title=title,
            company=item.get('company', ''),
            description=desc[:2000],
            link=item.get('url', ''),
            source='RemoteOK',
        ))

    logger.info(f"[RemoteOK] Matched {len(jobs)} jobs")
    return jobs


# ── Remotive ──────────────────────────────────────────────────────────────────

def search_remotive(keywords):
    """
    Remotive API, https://remotive.com/api/remote-jobs
    Does NOT support multi-word queries well; fetch all and filter client-side.
    Free, no key.
    """
    logger.info("[Remotive] Fetching all remote jobs...")
    data = _get('https://remotive.com/api/remote-jobs', params={'limit': 100})
    if not data:
        return []

    kw_lower = [k.lower() for k in keywords]
    jobs = []
    for item in data.get('jobs', []):
        title = item.get('title', '')
        desc = item.get('description', '') or ''
        tags = ' '.join(item.get('tags', []) if isinstance(item.get('tags'), list) else [])
        text = (title + ' ' + desc + ' ' + tags).lower()
        if not any(k in text for k in kw_lower):
            continue
        salary = item.get('salary') or None
        jobs.append(_job(
            title=title,
            company=item.get('company_name', ''),
            description=desc[:2000],
            link=item.get('url', ''),
            salary=str(salary) if salary else None,
            source='Remotive',
        ))

    logger.info(f"[Remotive] Matched {len(jobs)} jobs")
    return jobs


# ── Arbeitnow ─────────────────────────────────────────────────────────────────

def search_arbeitnow(query, pages=3):
    """
    Arbeitnow public job board API, https://www.arbeitnow.com/api/job-board-api
    Supports ?search= and ?page=. Free, no key.
    """
    logger.info(f"[Arbeitnow] Searching: {query}")
    jobs = []

    for page in range(1, pages + 1):
        data = _get(
            'https://www.arbeitnow.com/api/job-board-api',
            params={'search': query, 'page': page}
        )
        if not data or not data.get('data'):
            break

        for item in data['data']:
            # Only remote or internationally applicable
            if not item.get('remote') and item.get('location', '').lower() not in ('', 'remote', 'worldwide'):
                continue

            jobs.append(_job(
                title=item.get('title', ''),
                company=item.get('company_name', ''),
                description=item.get('description', '')[:2000],
                link=item.get('url', ''),
                location='Remote' if item.get('remote') else item.get('location', ''),
                source='Arbeitnow',
            ))

        if len(data['data']) < 10:
            break  # Last page

    logger.info(f"[Arbeitnow] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── The Muse ──────────────────────────────────────────────────────────────────

# The Muse expects this exact string. Asking for remote jobs server-side is
# the difference between 20 usable listings a page and roughly one in a
# hundred, because the unfiltered feed is overwhelmingly on-site.
MUSE_REMOTE_LOCATION = 'Flexible / Remote'


def search_the_muse(keywords, pages=3):
    """
    The Muse public API, https://www.themuse.com/api/public/jobs
    No key required.

    Filter by location server-side. Without it the endpoint returns every
    job The Muse lists, over 20,000 pages of mostly on-site roles, and the
    client-side remote check then discards about 99 percent of what was
    fetched. Measured on the live API: five unfiltered pages yielded one
    remote job, and one filtered page yields twenty.

    The client-side remote check below is kept as a safety net. It is
    redundant while the parameter works and costs nothing, and it means a
    silent change to the API cannot start letting on-site jobs through.
    """
    import re
    logger.info("[The Muse] Fetching remote jobs...")
    kw_lower = [k.lower() for k in keywords]
    jobs = []

    for page in range(1, pages + 1):
        data = _get(
            'https://www.themuse.com/api/public/jobs',
            params={
                'page': page,
                'descending': 'true',
                'location': MUSE_REMOTE_LOCATION,
            }
        )
        if not data or not data.get('results'):
            break

        for item in data['results']:
            locations = item.get('locations', [])
            loc_names = [l.get('name', '') for l in locations]
            is_remote = (
                not locations or
                any('remote' in l.lower() or 'flexible' in l.lower() or 'anywhere' in l.lower()
                    for l in loc_names)
            )
            if not is_remote:
                continue

            title = item.get('name', '')
            desc_raw = item.get('contents', '')
            desc = re.sub(r'<[^>]+>', ' ', desc_raw)
            text = (title + ' ' + desc).lower()

            if not any(k in text for k in kw_lower):
                continue

            jobs.append(_job(
                title=title,
                company=item.get('company', {}).get('name', ''),
                description=desc[:2000],
                link=item.get('refs', {}).get('landing_page', ''),
                location=', '.join(loc_names) or 'Remote',
                source='The Muse',
            ))

    logger.info(f"[The Muse] Matched {len(jobs)} jobs")
    return jobs


# ── Jobicy ────────────────────────────────────────────────────────────────────

def search_jobicy(tag, count=50):
    """
    Jobicy remote jobs API, https://jobicy.com/api/v2/remote-jobs
    Free, no key, supports ?tag= and ?industry=.

    Do not send geo=worldwide. The API validates geo against a list of real
    regions (usa, europe, uk, emea, apac) and answers 400 to anything else,
    so geo=worldwide made every call fail and this source returned nothing on
    every run. Omitting geo is what actually means worldwide. There is a test
    asserting the rejected values do not come back.
    """
    logger.info(f"[Jobicy] Searching tag: {tag}")
    data = _get(
        'https://jobicy.com/api/v2/remote-jobs',
        params={'count': count, 'tag': tag}
    )
    if not data:
        return []

    jobs = []
    for item in data.get('jobs', []):
        salary = item.get('annualSalaryMin')
        salary_max = item.get('annualSalaryMax')
        salary_str = None
        if salary and salary_max:
            currency = item.get('salaryCurrency', 'USD')
            salary_str = f"{currency} {salary:,} – {salary_max:,}"
        elif salary:
            salary_str = f"{item.get('salaryCurrency', 'USD')} {salary:,}+"

        jobs.append(_job(
            title=item.get('jobTitle', ''),
            company=item.get('companyName', ''),
            description=item.get('jobDescription', '')[:2000],
            link=item.get('url', ''),
            salary=salary_str,
            location=item.get('jobGeo', 'Worldwide'),
            source='Jobicy',
        ))

    logger.info(f"[Jobicy] Found {len(jobs)} jobs for tag '{tag}'")
    return jobs


# ── We Work Remotely ─────────────────────────────────────────────────────────

def search_weworkremotely(keywords):
    """
    We Work Remotely RSS feed, https://weworkremotely.com/remote-jobs.rss
    Free, no key. High-quality curated remote listings.
    Client-side keyword filtering.
    """
    logger.info("[WeWorkRemotely] Fetching RSS feed...")
    try:
        r = requests.get('https://weworkremotely.com/remote-jobs.rss', headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            logger.warning(f"[WeWorkRemotely] HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.text)
    except Exception as e:
        logger.warning(f"[WeWorkRemotely] Failed: {e}")
        return []

    kw_lower = [k.lower() for k in keywords]
    jobs = []

    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        desc  = (item.findtext('description') or '').strip()
        link  = (item.findtext('link') or '').strip()
        # WWR titles are formatted as "Company: Job Title"
        if ':' in title:
            company, job_title = title.split(':', 1)
            company = company.strip()
            job_title = job_title.strip()
        else:
            company, job_title = '', title

        text = (job_title + ' ' + desc).lower()
        if not any(k in text for k in kw_lower):
            continue

        jobs.append(_job(
            title=job_title,
            company=company,
            description=desc[:2000],
            link=link,
            source='WeWorkRemotely',
        ))

    logger.info(f"[WeWorkRemotely] Matched {len(jobs)} jobs")
    return jobs


# ── Himalayas ─────────────────────────────────────────────────────────────────

def search_himalayas(query, limit=50):
    """
    Himalayas remote jobs API, https://himalayas.app/jobs/api
    100% remote-only listings. Free, no key. Startup-heavy, equity info in some listings.
    Supports ?q= for keyword search and ?limit= for result count.
    """
    logger.info(f"[Himalayas] Searching: {query!r}")
    data = _get('https://himalayas.app/jobs/api', params={'q': query, 'limit': limit})
    if not data:
        return []

    jobs = []
    for item in data.get('jobs', []):
        salary_min = item.get('salaryMin')
        salary_max = item.get('salaryMax')
        salary_str = None
        if salary_min and salary_max:
            currency = item.get('salaryCurrency', 'USD')
            salary_str = f"{currency} {int(salary_min):,} – {int(salary_max):,}"
        elif salary_min:
            salary_str = f"{item.get('salaryCurrency', 'USD')} {int(salary_min):,}+"

        jobs.append(_job(
            title=item.get('title', ''),
            company=item.get('company', {}).get('name', '') if isinstance(item.get('company'), dict) else item.get('company', ''),
            description=item.get('description', '')[:2000],
            link=item.get('url', '') or item.get('applicationLink', ''),
            salary=salary_str,
            location='Remote',  # Himalayas is remote-only
            source='Himalayas',
        ))

    logger.info(f"[Himalayas] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Adzuna ────────────────────────────────────────────────────────────────────

def search_adzuna(query, country='gb', results_per_page=20):
    """
    Adzuna Jobs API, https://developer.adzuna.com/
    Free tier: 250 req/day. Has salary data. Requires ADZUNA_APP_ID + ADZUNA_APP_KEY in .env.
    country: 'gb' (UK), 'us', 'au', 'de', 'fr', 'ca', etc.
    """
    app_id  = os.getenv('ADZUNA_APP_ID', '')
    app_key = os.getenv('ADZUNA_APP_KEY', '')
    if not app_id or not app_key:
        logger.debug("[Adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set, skipping")
        return []

    logger.info(f"[Adzuna] Searching: {query!r} ({country.upper()})")
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    # No 'where=remote' filter: EU industrial roles are mostly on-site or hybrid,
    # and the digest wants both (remote is preferred later by a downrank, not a
    # hard filter). Searching country-wide is what surfaces the real inventory,
    # e.g. ~900 German sales-engineer roles rather than the handful tagged remote.
    data = _get(url, params={
        'app_id':           app_id,
        'app_key':          app_key,
        'results_per_page': results_per_page,
        'what':             query,
        'content-type':     'application/json',
    })
    if not data:
        return []

    jobs = []
    for item in data.get('results', []):
        salary_min = item.get('salary_min')
        salary_max = item.get('salary_max')
        salary_str = None
        if salary_min and salary_max:
            salary_str = f"£{int(salary_min):,} – £{int(salary_max):,}" if country == 'gb' else f"${int(salary_min):,} – ${int(salary_max):,}"
        elif salary_min:
            salary_str = f"£{int(salary_min):,}+" if country == 'gb' else f"${int(salary_min):,}+"

        jobs.append(_job(
            title=item.get('title', ''),
            company=item.get('company', {}).get('display_name', ''),
            description=item.get('description', '')[:2000],
            link=item.get('redirect_url', ''),
            salary=salary_str,
            location=item.get('location', {}).get('display_name', 'Remote'),
            source='Adzuna',
        ))

    logger.info(f"[Adzuna] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Jooble ────────────────────────────────────────────────────────────────────

def search_jooble(query, location='remote'):
    """
    Jooble API, https://jooble.org/api/about
    Free tier, aggregates 140k+ job sources globally.
    Requires JOOBLE_API_KEY in .env, get free key at jooble.org/api/about.
    """
    api_key = os.getenv('JOOBLE_API_KEY', '')
    if not api_key:
        logger.debug("[Jooble] JOOBLE_API_KEY not set, skipping")
        return []

    logger.info(f"[Jooble] Searching: {query!r}")
    try:
        r = requests.post(
            f"https://jooble.org/api/{api_key}",
            json={'keywords': query, 'location': location, 'resultonpage': 20},
            headers={**HEADERS, 'Content-Type': 'application/json'},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning(f"[Jooble] HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        # Log the error type only. The Jooble API key sits in the request URL
        # path, and a requests exception repr can embed the full URL, so logging
        # the whole exception could leak the key into a log file.
        logger.warning(f"[Jooble] Failed: {type(e).__name__}")
        return []

    jobs = []
    for item in data.get('jobs', []):
        salary = item.get('salary') or None
        jobs.append(_job(
            title=item.get('title', ''),
            company=item.get('company', ''),
            description=item.get('snippet', '')[:2000],
            link=item.get('link', ''),
            salary=str(salary) if salary else None,
            location=item.get('location', 'Remote'),
            source='Jooble',
        ))

    logger.info(f"[Jooble] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Infostud (poslovi.infostud.com), Serbia ───────────────────────────────────

INFOSTUD_SEARCH = "https://poslovi.infostud.com/oglasi-za-posao-{q}"


def _extract_next_data(html):
    """Return the Next.js __NEXT_DATA__ JSON from a page as a dict, or None.

    Next.js ships the page's data in a <script id="__NEXT_DATA__"> tag. Reading
    that is the app's own data contract and far more stable than scraping HTML
    classes that change with every redesign.
    """
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _parse_infostud_jobs(data):
    """Turn a parsed Infostud __NEXT_DATA__ dict into job dicts. Pure and
    offline-testable, so the network fetch and the parsing can be tested apart.

    Every Infostud result is in Serbia, so the location is stamped ", Serbia".
    That way the regional digest matches it on the country term regardless of
    which city (Vranje, Subotica, ...) the listing names.
    """
    try:
        primary = data['props']['pageProps']['initialSearchResults']['jobs']['primary']
    except (KeyError, TypeError):
        return []
    jobs = []
    for it in primary or []:
        title = (it.get('title') or '').strip()
        if not title:
            continue
        city = (it.get('location') or '').strip()
        location = f"{city}, Serbia" if city else "Serbia"
        # Some fields (jobSummary) come back as nested objects, not strings.
        # Take the snippet only when it is plainly a string, else leave empty.
        desc = it.get('textAdSnippet')
        if not isinstance(desc, str):
            desc = ''
        salary = it.get('salary')
        if not isinstance(salary, str):
            salary = None
        jobs.append(_job(
            title=title,
            company=(it.get('companyName') or '').strip(),
            description=desc,
            link=it.get('url') or '',
            salary=salary,
            location=location,
            source='Infostud'))
    return jobs


def search_infostud(query, pages=1):
    """
    Infostud (poslovi.infostud.com), Serbia's largest job board. No public API,
    so we read the Next.js __NEXT_DATA__ JSON the listing page ships. This also
    covers HelloWorld.rs IT listings, since both are Infostud-group sites and
    HelloWorld jobs surface in Infostud results, so a separate HelloWorld
    scraper would be redundant. Free, no key.
    """
    jobs = []
    q = quote(query.strip().replace(' ', '-'))
    logger.info(f"[Infostud] Searching: {query!r}")
    for page in range(1, pages + 1):
        url = INFOSTUD_SEARCH.format(q=q)
        if page > 1:
            url += f"?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                logger.warning(f"[Infostud] HTTP {r.status_code} for '{query}'")
                break
        except Exception as e:
            logger.warning(f"[Infostud] request failed for '{query}': {type(e).__name__}")
            break
        page_jobs = _parse_infostud_jobs(_extract_next_data(r.text) or {})
        if not page_jobs:
            break
        jobs.extend(page_jobs)
        time.sleep(0.5)
    logger.info(f"[Infostud] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Bundesagentur für Arbeit (German Federal Employment Agency) ────────────────

def search_bundesagentur(query, size=25):
    """
    German Federal Employment Agency "Jobsuche" API. Free, no signup; a static
    key identifies the mobile app. The largest EU source for industrial and
    engineering roles (Vertriebsingenieur, Anwendungstechniker, Verfahrensingenieur).

    NOTE: the endpoint appears IP-restricted to German/EU addresses. It could not
    be verified from the build environment (HTTP 403 "no match"), so confirm it
    returns data once running on the live deployment. Fails soft (returns []).
    """
    logger.info(f"[Bundesagentur] Searching: {query!r}")
    try:
        r = requests.get(
            "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/app/jobs",
            params={'was': query, 'size': size, 'page': 1},
            headers={
                'X-API-Key': 'jobboerse-jobsuche',
                'User-Agent': 'Jobsuche/2.9.2 (de.arbeitsagentur.jobboerse; iOS 15.1) Alamofire/5.4.4',
                'Accept': 'application/json',
            },
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning(f"[Bundesagentur] HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        logger.warning(f"[Bundesagentur] skipped: {type(e).__name__}")
        return []

    jobs = []
    for o in data.get('stellenangebote', []):
        loc = o.get('arbeitsort', {}) or {}
        refnr = o.get('refnr', '')
        link = o.get('externeUrl') or (
            f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}" if refnr else '')
        jobs.append(_job(
            title=o.get('titel', '') or o.get('beruf', ''),
            company=o.get('arbeitgeber', ''),
            description=o.get('beruf', '') or '',
            link=link,
            location=', '.join(x for x in (loc.get('ort'), loc.get('region')) if x) or 'Deutschland',
            source='Bundesagentur',
        ))
    logger.info(f"[Bundesagentur] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Reed.co.uk ────────────────────────────────────────────────────────────────

def search_reed(query, location='Remote', results=20):
    """
    Reed.co.uk Jobseeker API, https://www.reed.co.uk/developers
    Free API key, HTTP Basic (key as username, blank password). Deep UK
    engineering and technical-sales inventory. Needs REED_API_KEY in .env.
    """
    api_key = os.getenv('REED_API_KEY', '')
    if not api_key:
        logger.debug("[Reed] REED_API_KEY not set, skipping (free key at reed.co.uk/developers)")
        return []
    logger.info(f"[Reed] Searching: {query!r}")
    try:
        r = requests.get(
            "https://www.reed.co.uk/api/1.0/search",
            params={'keywords': query, 'locationName': location, 'resultsToTake': results},
            auth=(api_key, ''),
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning(f"[Reed] HTTP {r.status_code}")
            return []
        data = r.json()
    except Exception as e:
        logger.warning(f"[Reed] skipped: {type(e).__name__}")
        return []

    jobs = []
    for item in data.get('results', []):
        smin, smax = item.get('minimumSalary'), item.get('maximumSalary')
        salary = (f"£{int(smin):,} – £{int(smax):,}" if smin and smax
                  else (f"£{int(smin):,}+" if smin else None))
        jobs.append(_job(
            title=item.get('jobTitle', ''),
            company=item.get('employerName', ''),
            description=(item.get('jobDescription', '') or '')[:2000],
            link=item.get('jobUrl', ''),
            salary=salary,
            location=item.get('locationName', 'UK'),
            source='Reed',
        ))
    logger.info(f"[Reed] Found {len(jobs)} jobs for '{query}'")
    return jobs


# ── Orchestrator ──────────────────────────────────────────────────────────────

class FreeJobSearcher:
    """
    Searches all free sources in one call.
    Accepts a list of search queries and a keyword list for client-side filtering.
    """

    def search_all(self, queries, keywords=None):
        """
        Args:
            queries:  list of query strings (e.g. ["remote engineer", "sales engineer"])
            keywords: list of keywords for client-side filtering on sources that
                      don't support server-side search (e.g. RemoteOK)
        Returns:
            Deduplicated list of job dicts
        """
        keywords = keywords or queries
        all_jobs = []

        # 1. RemoteOK, fetch once, filter client-side
        try:
            all_jobs.extend(search_remoteok(keywords))
            time.sleep(1)  # Polite rate limit
        except Exception as e:
            logger.warning(f"RemoteOK failed: {e}")

        # 2. Remotive, fetch all, filter by keywords client-side
        try:
            all_jobs.extend(search_remotive(keywords))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Remotive skipped: {e}")

        # 3. Arbeitnow, one request per query
        for q in queries:
            try:
                all_jobs.extend(search_arbeitnow(q, pages=2))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Arbeitnow skipped for '{q}': {e}")

        # 4. The Muse, fetch recent, filter by keywords client-side
        try:
            all_jobs.extend(search_the_muse(keywords, pages=5))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"The Muse skipped: {e}")

        # 5. Jobicy, top keywords as tags
        jobicy_tags = keywords[:5] if keywords else queries[:5]
        for tag in jobicy_tags:
            try:
                all_jobs.extend(search_jobicy(tag))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Jobicy skipped for '{tag}': {e}")

        # 6. We Work Remotely, fetch RSS once, filter client-side
        try:
            all_jobs.extend(search_weworkremotely(keywords))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"WeWorkRemotely skipped: {e}")

        # 7. Himalayas, remote-only, no key, startup-heavy
        for q in queries[:3]:
            try:
                all_jobs.extend(search_himalayas(q))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Himalayas skipped for '{q}': {e}")

        # 9. Adzuna, GLOBAL. Every country Adzuna covers (Europe, US, Canada,
        # Australia, India, Singapore, NZ, South Africa, Brazil, Mexico). The
        # work-eligibility filter downstream keeps only roles the user can take
        # (visa obtainable via sponsorship, or no permit needed), so a wide net is
        # correct. Adzuna does NOT cover the UAE/Gulf; that needs a separate source.
        # Free tier 250 req/day; 19 countries x 4 queries = 76, inside it.
        for country in ('gb', 'de', 'nl', 'at', 'pl', 'fr', 'it', 'es', 'be', 'ch',
                        'us', 'ca', 'au', 'in', 'sg', 'nz', 'za', 'mx', 'br'):
            for q in queries[:4]:
                try:
                    all_jobs.extend(search_adzuna(q, country=country))
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Adzuna skipped for '{q}' ({country}): {e}")

        # 10. Jooble, global aggregator, top 3 queries (defaults to remote)
        for q in queries[:3]:
            try:
                all_jobs.extend(search_jooble(q))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Jooble skipped for '{q}': {e}")

        # 10b. Gulf coverage via Jooble, since Adzuna has no UAE/Gulf. Use the
        # COUNTRY name ("United Arab Emirates", not "Dubai"), city names
        #      return nothing. The work-eligibility filter keeps only the roles
        #      the user can actually take (visa obtainable or no permit).
        for loc in ('United Arab Emirates', 'Saudi Arabia', 'Qatar'):
            for q in queries[:3]:
                try:
                    all_jobs.extend(search_jooble(q, location=loc))
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Jooble {loc} skipped for '{q}': {e}")

        # 10c. Regional coverage via Jooble, driven by the user's private config
        #      (REGIONAL_JOB_LOCATIONS in .env, empty by default so the public
        #      engine adds nothing here). Country names work best in Jooble
        #      ("Serbia", not "Beograd"). This is what surfaces the home market,
        #      e.g. the Balkans, that Adzuna does not cover. The work-eligibility
        #      filter keeps these (Serbia/Bosnia/Montenegro are no-permit).
        from core.config import Config as _RegionCfg
        for loc in _RegionCfg.REGIONAL_JOB_LOCATIONS:
            for q in queries[:4]:
                try:
                    all_jobs.extend(search_jooble(q, location=loc))
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Jooble regional {loc} skipped for '{q}': {e}")

        # 10d. Dedicated regional job boards, driven by REGIONAL_BOARDS in the
        #      user's .env (empty by default, so the public engine adds nothing).
        #      These read a local board's own structured data, for coverage the
        #      global aggregators under-serve. Add a board by writing a search_*
        #      function and registering it in BOARD_DISPATCH.
        BOARD_DISPATCH = {'infostud': search_infostud}
        for name in _RegionCfg.REGIONAL_BOARDS:
            fn = BOARD_DISPATCH.get(name.strip().lower())
            if not fn:
                logger.warning(f"Unknown regional board '{name}', skipping")
                continue
            for q in queries[:4]:
                try:
                    all_jobs.extend(fn(q))
                    time.sleep(0.3)
                except Exception as e:
                    logger.warning(f"Board {name} skipped for '{q}': {e}")

        # 11. Bundesagentur, German Federal Employment Agency. German role terms
        #     find the most; the user's English queries also return results.
        de_terms = list(queries[:2]) + ['Vertriebsingenieur', 'Anwendungstechniker',
                                        'Verfahrensingenieur']
        for q in de_terms:
            try:
                all_jobs.extend(search_bundesagentur(q))
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Bundesagentur skipped for '{q}': {e}")

        # 12. Reed, UK engineering / technical-sales depth. Needs REED_API_KEY;
        #     skips silently without one.
        for q in queries[:3]:
            try:
                all_jobs.extend(search_reed(q))
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Reed skipped for '{q}': {e}")

        # Deduplicate by (title, company)
        seen = set()
        unique = []
        for job in all_jobs:
            key = (job['title'].lower(), job['company'].lower())
            if key not in seen and job['title'] and job['company']:
                seen.add(key)
                unique.append(job)

        logger.info(f"[FreeJobSearcher] Total unique jobs: {len(unique)} from {len(all_jobs)} raw")
        return unique


if __name__ == '__main__':
    searcher = FreeJobSearcher()
    jobs = searcher.search_all(
        queries=['remote engineer', 'sales engineer', 'remote technical'],
        keywords=['engineer', 'sales', 'technical', 'remote']
    )
    print(f"\nFound {len(jobs)} jobs:")
    for j in jobs[:10]:
        print(f"  [{j['source']}] {j['title']} @ {j['company']}")
