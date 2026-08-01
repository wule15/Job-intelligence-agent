"""
Job search via DuckDuckGo web search.
Searches major job boards (Indeed, LinkedIn, Glassdoor, Wellfound) through DDG.
Free, no API key required.

Results are parsed from DDG snippets — less structured than JSearch,
but a solid fallback when paid quotas are exhausted.
"""

import re
import time
from datetime import datetime
from utils import setup_logging

logger = setup_logging('job_search_ddg')

# Job board sites to target
SITES = [
    'indeed.com',
    'linkedin.com/jobs',
    'glassdoor.com/job-listing',
    'wellfound.com/jobs',
    'jobs.lever.co',
    'boards.greenhouse.io',
]

# Title parse patterns — "Job Title at Company | Board" or "Job Title - Company"
TITLE_PATTERNS = [
    re.compile(r'^(.+?)\s+at\s+(.+?)\s*[|–\-]', re.IGNORECASE),  # "SE at Eaton |"
    re.compile(r'^(.+?)\s*[-–|]\s*(.+?)\s*[-–|]', re.IGNORECASE), # "SE - Eaton - remote"
    re.compile(r'^(.+?)\s+@\s+(.+?)\s*[|–\-]', re.IGNORECASE),    # "SE @ Eaton |"
]

# Words that indicate it's a job board result page, not a single job
SKIP_TITLES = ['jobs', 'search', 'results', 'opportunities', 'careers at', 'all jobs']


def _parse_title_company(raw_title: str) -> tuple[str, str]:
    """Extract (job_title, company) from a DDG result title."""
    # Strip site suffix e.g. "| Indeed.com" or "- Glassdoor"
    raw = re.sub(r'\s*[|–\-]\s*(Indeed|LinkedIn|Glassdoor|Wellfound|Lever|Greenhouse)[^|–\-]*$',
                 '', raw_title, flags=re.IGNORECASE).strip()

    for pat in TITLE_PATTERNS:
        m = pat.match(raw)
        if m:
            title   = m.group(1).strip().rstrip('-–').strip()
            company = m.group(2).strip().rstrip('-–').strip()
            # Sanity: title should be ≤ 10 words, not look like a site name
            if (len(title.split()) <= 10 and
                    not any(s in title.lower() for s in SKIP_TITLES) and
                    len(company) > 1):
                return title, company

    # Fallback: treat the cleaned raw title as the job title, company unknown
    if len(raw.split()) <= 8 and not any(s in raw.lower() for s in SKIP_TITLES):
        return raw, ''
    return '', ''


def search_ddg(query: str, max_results: int = 15) -> list[dict]:
    """
    Search DuckDuckGo for a job query across major job boards.
    Returns list of raw result dicts from ddgs.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        logger.warning("[DDG] ddgs package not installed — run: pip install ddgs")
        return []

    results = []
    site_query = ' OR '.join(f'site:{s}' for s in SITES[:4])
    full_query = f'{query} remote ({site_query})'

    try:
        hits = DDGS().text(full_query, max_results=max_results)
        if hits:
            results.extend(hits)
        time.sleep(0.3)
    except Exception as e:
        logger.debug(f"[DDG] Search failed for '{query}': {e}")

    return results


def parse_jobs(raw_results: list[dict], query: str) -> list[dict]:
    """Convert DDG text results into normalised job dicts."""
    jobs = []
    seen_links = set()

    for r in raw_results:
        href    = r.get('href', '') or ''
        title   = r.get('title', '') or ''
        snippet = r.get('body', '')  or ''

        if not href or href in seen_links:
            continue
        seen_links.add(href)

        # Skip pages that are clearly search/listing pages, not single jobs
        if any(s in href for s in ['/search?', '/jobs?q=', '/jobs/search', '/job-search']):
            continue
        if any(s in title.lower() for s in SKIP_TITLES):
            continue

        job_title, company = _parse_title_company(title)
        if not job_title:
            continue

        # Source label from URL
        source = 'DDG / Indeed'
        if 'linkedin.com' in href:  source = 'DDG / LinkedIn'
        elif 'glassdoor'  in href:  source = 'DDG / Glassdoor'
        elif 'wellfound'  in href:  source = 'DDG / Wellfound'
        elif 'lever.co'   in href:  source = 'DDG / Lever'
        elif 'greenhouse' in href:  source = 'DDG / Greenhouse'

        jobs.append({
            'title':          job_title,
            'company':        company,
            'description':    snippet,
            'link':           href,
            'salary':         None,
            'source':         source,
            'extracted_date': datetime.now().isoformat(),
        })

    return jobs


class DDGJobSearcher:
    """Search for jobs via DuckDuckGo."""

    def search(self, query: str, max_results: int = 15) -> list[dict]:
        logger.info(f"[DDG] Searching: {query!r}")
        raw = search_ddg(query, max_results=max_results)
        jobs = parse_jobs(raw, query)
        logger.debug(f"[DDG] '{query}' → {len(jobs)} jobs parsed")
        return jobs

    def search_all(self, queries: list[str]) -> list[dict]:
        """Run multiple queries and deduplicate by link."""
        all_jobs: list[dict] = []
        seen: set[str] = set()

        for query in queries:
            jobs = self.search(query)
            for job in jobs:
                link = job.get('link', '')
                if link not in seen:
                    seen.add(link)
                    all_jobs.append(job)
            time.sleep(0.5)

        logger.info(f"[DDG] Total unique jobs: {len(all_jobs)}")
        return all_jobs
