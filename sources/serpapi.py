"""
Google Jobs search via SerpAPI.
Free tier: 250 searches/month (as of 2026).

Sign up at https://serpapi.com, copy the API key, add it to .env:
  SERPAPI_KEY=your_key_here

Each call returns ~10 jobs with full descriptions, salary, and apply links.

Google Jobs results are localised per market. A bare query defaults to a
remote/US context and returns little or nothing for a specific country, so to
get real national inventory each market must be queried with its own
gl + google_domain + hl. SerpApi only accepts a fixed allowlist of gl codes,
so MARKETS below lists the country markets verified to return results; codes
outside the allowlist (e.g. se, no, fi, lu, si, rs, ba) are not reachable
through this source and are left to Jooble, Adzuna and Infostud instead.
"""

import os
import re
import time
import requests
from core.utils import setup_logging
from core.config import Config

logger = setup_logging('sources.serpapi')

SERPAPI_URL = 'https://serpapi.com/search.json'

# Country markets verified (2026-09) to return Google Jobs results via SerpApi.
# Each entry carries the gl + google_domain + hl that market needs; without them
# Google returns nothing for the country. Add a market only after confirming
# SerpApi accepts its gl code and the query returns jobs.
MARKETS = {
    'de': {'location': 'Germany',        'gl': 'de', 'google_domain': 'google.de',    'hl': 'de'},
    'at': {'location': 'Austria',        'gl': 'at', 'google_domain': 'google.at',    'hl': 'de'},
    'fr': {'location': 'France',         'gl': 'fr', 'google_domain': 'google.fr',    'hl': 'fr'},
    'nl': {'location': 'Netherlands',    'gl': 'nl', 'google_domain': 'google.nl',    'hl': 'nl'},
    'be': {'location': 'Belgium',        'gl': 'be', 'google_domain': 'google.be',    'hl': 'nl'},
    'dk': {'location': 'Denmark',        'gl': 'dk', 'google_domain': 'google.dk',    'hl': 'da'},
    'gb': {'location': 'United Kingdom', 'gl': 'uk', 'google_domain': 'google.co.uk', 'hl': 'en'},
    'us': {'location': 'United States',  'gl': 'us', 'google_domain': 'google.com',   'hl': 'en'},
}


def _normalise(raw_jobs, fallback_location='Remote'):
    """Turn SerpApi google_jobs items into the agent's job dict shape."""
    jobs = []
    for item in raw_jobs:
        salary = item.get('detected_extensions', {}).get('salary')

        apply_link = ''
        for opt in item.get('apply_options', []):
            apply_link = opt.get('link', '')
            break
        if not apply_link:
            apply_link = item.get('share_link', '')

        desc = item.get('description', '')
        for h in item.get('job_highlights', []):
            desc += '\n' + ' '.join(h.get('items', []))

        jobs.append({
            'title':       item.get('title', ''),
            'company':     item.get('company_name', ''),
            'description': desc[:2000],
            'link':        apply_link,
            'salary':      salary,
            'location':    item.get('location', fallback_location),
            'source':      'Google Jobs',
        })
    return jobs


def search_google_jobs(query, market=None, num_results=10):
    """
    Search Google Jobs via SerpAPI for one query in one market.

    Args:
        query:  Search string, e.g. "process engineer".
        market: A dict from MARKETS (location/gl/google_domain/hl) to target a
                country, or None for a remote/global pass. With a market the
                query is used as-is and localised to that country; without one
                the query is biased to remote roles.

    Returns:
        A list of normalised job dicts. Empty on any error, quota hit, or
        missing key, so a failure never ends the run.
    """
    api_key = os.getenv('SERPAPI_KEY') or getattr(Config, 'SERPAPI_KEY', None)
    if not api_key:
        logger.warning("[SerpAPI] SERPAPI_KEY not set, skipping Google Jobs")
        return []

    # The query builder prepends "remote " to every query. That fights an
    # on-site country search (a market pass for "remote sales engineer" in
    # Germany returns nothing), and it is redundant for the remote pass, where
    # ltype=1 already does the filtering. Strip it in both cases.
    query = re.sub(r'^\s*remote\s+', '', query, flags=re.IGNORECASE).strip() or query

    params = {'engine': 'google_jobs', 'api_key': api_key}
    if market:
        params['q'] = query
        params['location'] = market['location']
        params['gl'] = market['gl']
        params['google_domain'] = market['google_domain']
        params['hl'] = market['hl']
        label = f"{query!r} @ {market['location']}"
    else:
        # Fully-remote-anywhere pass. Google Jobs rejects location='Remote', so
        # use ltype=1 (its work-from-home filter) with no location; results come
        # back as "Anywhere". The downstream work-eligibility filter then keeps
        # only the ones open worldwide / no-permit / with sponsorship.
        params['q'] = query
        params['ltype'] = '1'
        params['hl'] = 'en'
        label = f"{query!r} @ remote (anywhere)"

    try:
        resp = requests.get(SERPAPI_URL, params=params, timeout=20)
        if resp.status_code == 401:
            logger.warning("[SerpAPI] Invalid API key")
            return []
        if resp.status_code == 429:
            logger.warning("[SerpAPI] Monthly quota exceeded")
            return []
        if resp.status_code != 200:
            logger.warning(f"[SerpAPI] HTTP {resp.status_code} for {label}")
            return []

        data = resp.json()
        # SerpApi reports an unsupported location/gl in the JSON body with a 200,
        # so surface it rather than silently returning nothing.
        if data.get('error'):
            logger.warning(f"[SerpAPI] {label}: {data['error']}")
            return []

        jobs = _normalise(data.get('jobs_results', []),
                          fallback_location=market['location'] if market else 'Remote')
        logger.info(f"[SerpAPI] {label} -> {len(jobs)} jobs")
        return jobs

    except requests.exceptions.Timeout:
        logger.warning(f"[SerpAPI] Timeout for {label}")
        return []
    except Exception as e:
        logger.warning(f"[SerpAPI] Error for {label}: {e}")
        return []


def resolve_markets(countries):
    """Map configured country codes to MARKETS entries.

    An unknown or unsupported code (one SerpApi's gl allowlist rejects) is
    skipped with a warning, not fatal, so a typo cannot kill the pass.
    """
    markets = []
    for cc in countries or []:
        m = MARKETS.get(cc.strip().lower())
        if m:
            markets.append(m)
        else:
            logger.warning(f"[SerpAPI] No supported market for '{cc}', skipping")
    return markets


class SerpAPIJobSearcher:
    """Search Google Jobs via SerpAPI across queries and country markets."""

    def search_all(self, queries, countries=None, budget=6, rotate=0):
        """
        Run queries against the configured country markets, breadth-first (every
        market gets the top query before any market gets a second), capped at
        `budget` total SerpApi searches to protect the monthly quota.

        Args:
            queries:   Search strings to run.
            countries: Targets to query (see MARKETS). The special code "remote"
                       (or "anywhere") runs a fully-remote-anywhere pass; real
                       country codes run localised market passes. Empty or None
                       runs a single remote pass over the queries.
            budget:    Hard cap on the number of SerpApi searches this call
                       makes. None or 0 means no cap.
            rotate:    Offset to rotate the query list by (pass the day-of-year).
                       Because the budget usually only affords the first query
                       across all targets, rotating daily means each market is
                       covered by a different query on successive days, so the
                       whole curated list gets used over time instead of only
                       its first entry.

        Returns:
            Deduplicated job dicts. Stops early if no API key is configured.
        """
        queries = list(queries)
        if queries and rotate:
            r = rotate % len(queries)
            queries = queries[r:] + queries[:r]
        codes = [c.strip().lower() for c in (countries or [])]
        remote_wanted = any(c in ('remote', 'anywhere', 'worldwide') for c in codes)
        market_codes = [c for c in codes if c not in ('remote', 'anywhere', 'worldwide')]
        markets = resolve_markets(market_codes)

        # Targets to query, remote (None) first so the budget never drops it.
        # With nothing configured, default to a single remote pass.
        targets = ([None] if remote_wanted else []) + markets
        if not targets:
            targets = [None]

        # Build the (query, target) task list. Breadth-first across targets so a
        # small budget still spans every target rather than exhausting on one.
        tasks = []
        for q in queries:
            for t in targets:
                tasks.append((q, t))

        if budget and budget > 0:
            dropped = len(tasks) - budget
            tasks = tasks[:budget]
            if dropped > 0:
                logger.info(f"[SerpAPI] Budget {budget}: running {len(tasks)} "
                            f"searches, {dropped} query/market pairs skipped")

        all_jobs = []
        seen = set()
        for q, m in tasks:
            jobs = search_google_jobs(q, market=m)
            if not jobs and not os.getenv('SERPAPI_KEY'):
                break  # no key configured, stop wasting iterations
            for job in jobs:
                key = (job['title'].lower(), job['company'].lower())
                if key not in seen and job['title'] and job['company']:
                    seen.add(key)
                    all_jobs.append(job)
            time.sleep(0.5)

        logger.info(f"[SerpAPI] Total unique across {len(tasks)} searches: {len(all_jobs)}")
        return all_jobs


if __name__ == '__main__':
    searcher = SerpAPIJobSearcher()
    jobs = searcher.search_all(['sales engineer', 'process engineer'],
                               countries=['de', 'at'], budget=4)
    print(f"Found {len(jobs)} jobs")
    for j in jobs[:5]:
        print(f"  {j['title']} @ {j['company']} | {j.get('location')}")
