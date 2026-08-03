"""
Free job search sources — no API keys required.
Replaces JSearch (paid/quota-limited) with:
  - RemoteOK          (remote only, no key)
  - Remotive          (remote only, no key)
  - Arbeitnow         (remote + EU, no key)
  - The Muse          (global, no key)
  - Jobicy            (remote only, no key)
  - We Work Remotely  (remote only, RSS, no key)
  - Himalayas         (remote only, no key)
  - Adzuna            (global, salary data, free API key required — ADZUNA_APP_ID + ADZUNA_APP_KEY)
  - Jooble            (global aggregator, free API key required — JOOBLE_API_KEY)
"""

import os
import xml.etree.ElementTree as ET
import requests
import time
from datetime import datetime
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
    RemoteOK public API — https://remoteok.com/api
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
    Remotive API — https://remotive.com/api/remote-jobs
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
    Arbeitnow public job board API — https://www.arbeitnow.com/api/job-board-api
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

def search_the_muse(keywords, pages=5):
    """
    The Muse public API — https://www.themuse.com/api/public/jobs
    No category filter (too restrictive); fetch recent jobs, filter client-side.
    No key required.
    """
    import re
    logger.info("[The Muse] Fetching recent jobs...")
    kw_lower = [k.lower() for k in keywords]
    jobs = []

    for page in range(1, pages + 1):
        data = _get(
            'https://www.themuse.com/api/public/jobs',
            params={'page': page, 'descending': 'true'}
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
    Jobicy remote jobs API — https://jobicy.com/api/v2/remote-jobs
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
    We Work Remotely RSS feed — https://weworkremotely.com/remote-jobs.rss
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
    Himalayas remote jobs API — https://himalayas.app/jobs/api
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
    Adzuna Jobs API — https://developer.adzuna.com/
    Free tier: 250 req/day. Has salary data. Requires ADZUNA_APP_ID + ADZUNA_APP_KEY in .env.
    country: 'gb' (UK), 'us', 'au', 'de', 'fr', 'ca', etc.
    """
    app_id  = os.getenv('ADZUNA_APP_ID', '')
    app_key = os.getenv('ADZUNA_APP_KEY', '')
    if not app_id or not app_key:
        logger.debug("[Adzuna] ADZUNA_APP_ID / ADZUNA_APP_KEY not set — skipping")
        return []

    logger.info(f"[Adzuna] Searching: {query!r} ({country.upper()})")
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    data = _get(url, params={
        'app_id':           app_id,
        'app_key':          app_key,
        'results_per_page': results_per_page,
        'what':             query,
        'where':            'remote',
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
    Jooble API — https://jooble.org/api/about
    Free tier, aggregates 140k+ job sources globally.
    Requires JOOBLE_API_KEY in .env — get free key at jooble.org/api/about.
    """
    api_key = os.getenv('JOOBLE_API_KEY', '')
    if not api_key:
        logger.debug("[Jooble] JOOBLE_API_KEY not set — skipping")
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
        logger.warning(f"[Jooble] Failed: {e}")
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

        # 1. RemoteOK — fetch once, filter client-side
        try:
            all_jobs.extend(search_remoteok(keywords))
            time.sleep(1)  # Polite rate limit
        except Exception as e:
            logger.warning(f"RemoteOK failed: {e}")

        # 2. Remotive — fetch all, filter by keywords client-side
        try:
            all_jobs.extend(search_remotive(keywords))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Remotive skipped: {e}")

        # 3. Arbeitnow — one request per query
        for q in queries:
            try:
                all_jobs.extend(search_arbeitnow(q, pages=2))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Arbeitnow skipped for '{q}': {e}")

        # 4. The Muse — fetch recent, filter by keywords client-side
        try:
            all_jobs.extend(search_the_muse(keywords, pages=5))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"The Muse skipped: {e}")

        # 5. Jobicy — top keywords as tags
        jobicy_tags = keywords[:5] if keywords else queries[:5]
        for tag in jobicy_tags:
            try:
                all_jobs.extend(search_jobicy(tag))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Jobicy skipped for '{tag}': {e}")

        # 6. We Work Remotely — fetch RSS once, filter client-side
        try:
            all_jobs.extend(search_weworkremotely(keywords))
            time.sleep(1)
        except Exception as e:
            logger.warning(f"WeWorkRemotely skipped: {e}")

        # 7. Himalayas — remote-only, no key, startup-heavy
        for q in queries[:3]:
            try:
                all_jobs.extend(search_himalayas(q))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Himalayas skipped for '{q}': {e}")

        # 9. Adzuna — salary data, top 3 queries to stay within 250 req/day limit
        for q in queries[:3]:
            try:
                all_jobs.extend(search_adzuna(q, country='gb'))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Adzuna skipped for '{q}': {e}")

        # 10. Jooble — global aggregator, top 3 queries
        for q in queries[:3]:
            try:
                all_jobs.extend(search_jooble(q))
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Jooble skipped for '{q}': {e}")

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
