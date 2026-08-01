"""
LinkedIn job search via the public guest endpoint.

Replaces the RSS connector, which stopped working. LinkedIn now returns an
HTML login page for those feed URLs, which is why the old code logged
"[LinkedIn RSS] XML parse error" four times a run and returned nothing.

This uses the endpoint LinkedIn's own job search page calls for pagination:

    /jobs-guest/jobs/api/seeMoreJobPostings/search

It needs no authentication and returns an HTML fragment of <li> cards. There
is no documented API and no contract, so this connector is written to fail
quietly and report itself rather than to be relied on. It is rate limited
aggressively; the delay between pages is not optional.
"""

import time
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from http_client import build_session
from job_normalize import canonical_url
from utils import setup_logging

logger = setup_logging('job_search_linkedin')

SEARCH_URL = 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}

TIMEOUT = 12
PAGE_SIZE = 25
DELAY_BETWEEN_REQUESTS = 2.0

# f_TPR=r604800 restricts to the last 7 days. f_WT=2 is remote.
DEFAULT_FILTERS = {'f_TPR': 'r604800', 'f_WT': '2'}


class LinkedInJobSearcher:
    """Search LinkedIn through the unauthenticated guest endpoint."""

    def __init__(self, location='European Union', remote_only=True):
        # LinkedIn rate limits this endpoint aggressively. A retrying session
        # with backoff is the difference between a throttled query returning
        # nothing and returning results a few seconds later.
        self.session = build_session()
        self.session.headers.update(HEADERS)
        self.location = location
        self.remote_only = remote_only

    def _fetch_page(self, query, start):
        params = {'keywords': query, 'location': self.location, 'start': start}
        params.update(DEFAULT_FILTERS)
        if not self.remote_only:
            params.pop('f_WT', None)

        url = f"{SEARCH_URL}?{urlencode(params)}"
        response = self.session.get(url, timeout=TIMEOUT)

        if response.status_code == 429:
            raise RuntimeError('LinkedIn rate limited the request (429)')
        if response.status_code != 200:
            raise RuntimeError(f'LinkedIn returned HTTP {response.status_code}')

        return response.text

    @staticmethod
    def _parse_cards(html, query):
        """
        Turn the HTML fragment into job dicts.

        The fragment is a bare list of <li> elements. LinkedIn changes these
        class names periodically, so every field is looked up defensively and
        a card that yields no title is skipped rather than half stored.
        """
        soup = BeautifulSoup(html, 'html.parser')
        jobs = []

        for card in soup.find_all('li'):
            title_el = card.find(['h3', 'span'], class_=lambda c: bool(c) and 'title' in c.lower())
            company_el = card.find(['h4', 'a'], class_=lambda c: bool(c) and 'subtitle' in c.lower())
            link_el = card.find('a', href=True)
            location_el = card.find(class_=lambda c: bool(c) and 'location' in c.lower())
            date_el = card.find('time')

            title = title_el.get_text(strip=True) if title_el else ''
            if not title:
                continue

            link = canonical_url(link_el['href']) if link_el else ''

            jobs.append({
                'title': title,
                'company': company_el.get_text(strip=True) if company_el else 'Unknown',
                'description': '',  # guest cards carry no description
                'link': link,
                'location': location_el.get_text(strip=True) if location_el else 'Remote',
                'salary': None,
                'source': 'LinkedIn',
                'date_posted': date_el.get('datetime') if date_el else None,
                'search_query': query,
            })

        return jobs

    def search_jobs(self, query, pages=2):
        """
        Search one query. Returns a list of job dicts, possibly empty.

        Stops early on the first page that yields nothing, which is how the
        endpoint signals the end of results.
        """
        collected = []

        for page in range(pages):
            start = page * PAGE_SIZE
            try:
                html = self._fetch_page(query, start)
            except Exception as e:
                logger.warning(f"[LinkedIn] {query!r} page {page + 1}: {type(e).__name__}: {e}")
                break

            page_jobs = self._parse_cards(html, query)
            if not page_jobs:
                break

            collected.extend(page_jobs)
            time.sleep(DELAY_BETWEEN_REQUESTS)

        logger.info(f"[LinkedIn] {query!r}: {len(collected)} jobs")
        return collected

    def search_all(self, queries, pages=2):
        """Search several queries and deduplicate by canonical link."""
        seen_links = set()
        results = []

        for query in queries:
            for job in self.search_jobs(query, pages=pages):
                link = job.get('link')
                if link and link in seen_links:
                    continue
                if link:
                    seen_links.add(link)
                results.append(job)

        logger.info(f"[LinkedIn] {len(results)} unique jobs from {len(queries)} queries")
        return results


if __name__ == '__main__':
    searcher = LinkedInJobSearcher()
    for job in searcher.search_all(['remote sales engineer'], pages=1)[:5]:
        print(f"{job['title']} @ {job['company']}")
        print(f"  {job['link']}")
