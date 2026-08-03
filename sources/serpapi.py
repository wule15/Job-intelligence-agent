"""
Google Jobs search via SerpAPI.
Free tier: 100 searches/month.

Sign up at https://serpapi.com → copy API key → add to .env:
  SERPAPI_KEY=your_key_here

Each call returns ~10 jobs with full descriptions, salary, apply links.
Covers all major job boards (LinkedIn, Indeed, Glassdoor, etc.) in one shot.
"""

import os
import requests
from core.utils import setup_logging
from core.config import Config

logger = setup_logging('sources.serpapi')

SERPAPI_URL = 'https://serpapi.com/search.json'


def search_google_jobs(query, location='Remote', num_results=10):
    """
    Search Google Jobs via SerpAPI.

    Args:
        query:       Search string e.g. "sales engineer"
        location:    Location filter (default "Remote")
        num_results: Approx results per call (SerpAPI returns ~10 per page)

    Returns:
        List of normalised job dicts.
    """
    api_key = os.getenv('SERPAPI_KEY') or getattr(Config, 'SERPAPI_KEY', None)
    if not api_key:
        logger.warning("[SerpAPI] SERPAPI_KEY not set — skipping Google Jobs")
        return []

    params = {
        'engine':    'google_jobs',
        'q':         f'{query} remote',
        'location':  location,
        'hl':        'en',
        'api_key':   api_key,
    }

    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=15)
        if resp.status_code == 401:
            logger.warning("[SerpAPI] Invalid API key")
            return []
        if resp.status_code == 429:
            logger.warning("[SerpAPI] Monthly quota exceeded")
            return []
        if resp.status_code != 200:
            logger.warning(f"[SerpAPI] HTTP {resp.status_code}")
            return []

        data = resp.json()
        raw_jobs = data.get('jobs_results', [])
        jobs = []

        for item in raw_jobs:
            # Salary
            salary = None
            detected = item.get('detected_extensions', {})
            if detected.get('salary'):
                salary = detected['salary']

            # Apply link — prefer direct link
            apply_link = ''
            for opt in item.get('apply_options', []):
                apply_link = opt.get('link', '')
                break
            if not apply_link:
                apply_link = item.get('share_link', '')

            # Description
            desc = item.get('description', '')
            highlights = item.get('job_highlights', [])
            if highlights:
                for h in highlights:
                    items_text = ' '.join(h.get('items', []))
                    desc += '\n' + items_text

            jobs.append({
                'title':       item.get('title', ''),
                'company':     item.get('company_name', ''),
                'description': desc[:2000],
                'link':        apply_link,
                'salary':      salary,
                'location':    item.get('location', 'Remote'),
                'source':      'Google Jobs',
            })

        logger.info(f"[SerpAPI] '{query}' → {len(jobs)} jobs")
        return jobs

    except requests.exceptions.Timeout:
        logger.warning(f"[SerpAPI] Timeout for '{query}'")
        return []
    except Exception as e:
        logger.warning(f"[SerpAPI] Error for '{query}': {e}")
        return []


class SerpAPIJobSearcher:
    """Search Google Jobs via SerpAPI across multiple queries."""

    def search_all(self, queries):
        """
        Run each query, deduplicate, return all jobs.
        Skips gracefully if API key missing or quota hit.
        """
        import time
        all_jobs = []
        seen = set()

        for q in queries:
            jobs = search_google_jobs(q)
            if not jobs and not os.getenv('SERPAPI_KEY'):
                break  # No key configured — stop trying
            for job in jobs:
                key = (job['title'].lower(), job['company'].lower())
                if key not in seen:
                    seen.add(key)
                    all_jobs.append(job)
            time.sleep(0.5)

        logger.info(f"[SerpAPI] Total unique: {len(all_jobs)}")
        return all_jobs


if __name__ == '__main__':
    searcher = SerpAPIJobSearcher()
    jobs = searcher.search_all(['sales engineer', 'mechanical engineer remote'])
    print(f"Found {len(jobs)} jobs")
    for j in jobs[:5]:
        print(f"  {j['title']} @ {j['company']} | {j.get('salary', 'N/A')}")
