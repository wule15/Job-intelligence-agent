"""
LinkedIn job search via public RSS feeds.
No API key required. Fetches remote jobs filtered by keyword.

LinkedIn RSS URL format:
  https://www.linkedin.com/jobs/search/?keywords=QUERY&location=Worldwide&f_WT=2&f_TPR=r86400&format=rss
  f_WT=2  = remote only
  f_TPR=r86400 = posted in last 24h (86400 seconds)
"""

import requests
import xml.etree.ElementTree as ET
import re
from core.utils import setup_logging
from datetime import datetime

logger = setup_logging('job_search_linkedin_rss')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    'Accept-Language': 'en-US,en;q=0.9',
}


def _clean(text):
    """Strip HTML tags and extra whitespace."""
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def search_linkedin_rss(query, max_results=25):
    """
    Fetch jobs from LinkedIn public RSS feed.

    Args:
        query:       Search string e.g. "sales engineer"
        max_results: Cap on jobs returned per query

    Returns:
        List of job dicts normalised to the standard schema.
    """
    url = (
        'https://www.linkedin.com/jobs/search/?'
        f'keywords={requests.utils.quote(query)}'
        '&location=Worldwide'
        '&f_WT=2'          # Remote only
        '&f_TPR=r604800'   # Posted in last 7 days
        '&format=rss'
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"[LinkedIn RSS] HTTP {resp.status_code} for '{query}'")
            return []

        # Parse RSS
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            # LinkedIn sometimes returns HTML when blocked — detect & skip
            if b'<html' in resp.content[:200].lower():
                logger.warning("[LinkedIn RSS] Got HTML instead of RSS — may be rate-limited")
            else:
                logger.warning("[LinkedIn RSS] XML parse error")
            return []

        ns = {'content': 'http://purl.org/rss/1.0/modules/content/'}
        jobs = []

        for item in root.findall('.//item')[:max_results]:
            title_el   = item.find('title')
            link_el    = item.find('link')
            desc_el    = item.find('description')
            content_el = item.find('content:encoded', ns)

            title = _clean(title_el.text if title_el is not None else '')
            link  = (link_el.text or '').strip() if link_el is not None else ''
            desc  = _clean(
                content_el.text if content_el is not None
                else (desc_el.text if desc_el is not None else '')
            )

            # LinkedIn RSS titles often contain company: "Job Title at Company"
            company = ''
            if ' at ' in title:
                parts = title.rsplit(' at ', 1)
                title   = parts[0].strip()
                company = parts[1].strip()

            if not title:
                continue

            jobs.append({
                'title':       title,
                'company':     company,
                'description': desc[:2000],
                'link':        link,
                'salary':      None,
                'location':    'Remote',
                'source':      'LinkedIn',
            })

        logger.info(f"[LinkedIn RSS] '{query}' → {len(jobs)} jobs")
        return jobs

    except requests.exceptions.Timeout:
        logger.warning(f"[LinkedIn RSS] Timeout for '{query}'")
        return []
    except Exception as e:
        logger.warning(f"[LinkedIn RSS] Error for '{query}': {e}")
        return []


class LinkedInRSSSearcher:
    """Search LinkedIn via RSS across multiple queries."""

    DEFAULT_QUERIES = [
        'sales engineer remote',
        'technical sales remote',
        'mechanical engineer remote',
        'process engineer remote',
        'content strategist remote',
        'technical writer remote',
        'AI engineer remote',
        'solutions engineer remote',
        'automation engineer remote',
    ]

    def search_all(self, queries=None):
        """
        Search all queries, deduplicate by (title, company).
        Returns flat list of job dicts.
        """
        import time
        queries = queries or self.DEFAULT_QUERIES
        all_jobs = []
        seen = set()

        for q in queries:
            jobs = search_linkedin_rss(q)
            for job in jobs:
                key = (job['title'].lower(), job['company'].lower())
                if key not in seen:
                    seen.add(key)
                    all_jobs.append(job)
            time.sleep(1.5)  # Polite rate limit

        logger.info(f"[LinkedIn RSS] Total unique: {len(all_jobs)}")
        return all_jobs


if __name__ == '__main__':
    searcher = LinkedInRSSSearcher()
    jobs = searcher.search_all(['sales engineer remote', 'mechanical engineer remote'])
    print(f"Found {len(jobs)} jobs")
    for j in jobs[:5]:
        print(f"  {j['title']} @ {j['company']} — {j['link'][:60]}")
