"""
Job search via Apify actors.
Uses pre-built actors for LinkedIn Jobs and Indeed, full descriptions,
salaries, apply links. Far more structured than DDG snippets.

Actors used:
  - bebity/linkedin-jobs-scraper   (LinkedIn Jobs)
  - misceres/indeed-scraper        (Indeed)
  - apify/website-content-crawler  (career page enrichment)

Free tier: ~$5 compute units/month. LinkedIn search ~0.25 CU per run.
"""

import os
import time
from datetime import datetime
from core.utils import setup_logging

logger = setup_logging('sources.apify')


def _client():
    from apify_client import ApifyClient
    key = os.getenv('APIFY_API_KEY', '')
    if not key:
        raise ValueError("APIFY_API_KEY not set in .env")
    return ApifyClient(key)


def _salary_str(job: dict) -> str | None:
    lo = job.get('salaryMin') or job.get('salary_min')
    hi = job.get('salaryMax') or job.get('salary_max')
    curr = job.get('salaryCurrency') or '$'
    if lo and hi:
        return f"{curr}{int(lo):,} – {curr}{int(hi):,}"
    if lo:
        return f"{curr}{int(lo):,}+"
    return job.get('salary') or None


# ── LinkedIn Jobs ─────────────────────────────────────────────────────────────

def search_linkedin(queries: list[str], max_per_query: int = 25) -> list[dict]:
    """
    Run LinkedIn Jobs scraper for each query.
    Actor: bebity/linkedin-jobs-scraper
    """
    try:
        client = _client()
    except ValueError as e:
        logger.warning(f"[Apify] {e}")
        return []

    jobs = []
    for query in queries:
        logger.info(f"[Apify/LinkedIn] Searching: {query!r}")
        try:
            # timeout_secs caps how long we wait for the actor. Without it a
            # runaway actor blocks the whole daily run, which is exactly how the
            # Indeed scraper used to stall for 30+ minutes.
            run = client.actor("bebity/linkedin-jobs-scraper").call(run_input={
                "queries": [query],
                "locationFilter": "Remote",
                "datePostedFilter": "past-week",
                "count": max_per_query,
            }, timeout_secs=120)
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                try:
                    jobs.append({
                        "title":          item.get("title", ""),
                        "company":        item.get("companyName", ""),
                        "description":    item.get("description", ""),
                        "link":           item.get("jobUrl") or item.get("applyUrl", ""),
                        "salary":         _salary_str(item),
                        "source":         "Apify / LinkedIn",
                        "extracted_date": datetime.now().isoformat(),
                    })
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[Apify/LinkedIn] Failed for '{query}': {e}")
        time.sleep(0.5)

    logger.info(f"[Apify/LinkedIn] {len(jobs)} jobs found")
    return jobs


# ── Indeed ────────────────────────────────────────────────────────────────────

def search_indeed(queries: list[str], max_per_query: int = 25) -> list[dict]:
    """
    Run Indeed scraper for each query.
    Actor: misceres/indeed-scraper
    """
    try:
        client = _client()
    except ValueError as e:
        logger.warning(f"[Apify] {e}")
        return []

    jobs = []
    for query in queries:
        logger.info(f"[Apify/Indeed] Searching: {query!r}")
        try:
            run = client.actor("misceres/indeed-scraper").call(run_input={
                "position": query,
                "location": "Remote",
                "maxItems": max_per_query,
                "parseJobDetail": True,
            }, timeout_secs=120)
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                try:
                    jobs.append({
                        "title":          item.get("positionName", ""),
                        "company":        item.get("company", ""),
                        "description":    item.get("description", ""),
                        "link":           item.get("url", ""),
                        "salary":         item.get("salary") or None,
                        "source":         "Apify / Indeed",
                        "extracted_date": datetime.now().isoformat(),
                    })
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"[Apify/Indeed] Failed for '{query}': {e}")
        time.sleep(0.5)

    logger.info(f"[Apify/Indeed] {len(jobs)} jobs found")
    return jobs


# ── Career page enrichment ────────────────────────────────────────────────────

def scrape_job_page(url: str) -> str | None:
    """
    Use Apify's website-content-crawler to get a job description from
    any ATS page, including JS-heavy ones like Workday.
    Returns plain text or None.
    """
    try:
        client = _client()
        run = client.actor("apify/website-content-crawler").call(run_input={
            "startUrls": [{"url": url}],
            "maxCrawlPages": 1,
            "crawlerType": "cheerio",   # fast, no JS; swap to "playwright" for Workday
        })
        for item in client.dataset(run["defaultDatasetId"]).iterate_items():
            text = item.get("text") or item.get("markdown") or ""
            if len(text) > 150:
                return text[:3000]
    except Exception as e:
        logger.debug(f"[Apify/crawl] Failed for {url}: {e}")
    return None


# ── Combined searcher class ───────────────────────────────────────────────────

class ApifyJobSearcher:

    def search_all(self, queries: list[str]) -> list[dict]:
        """
        Run LinkedIn with specific queries + Indeed once with a broad query.
        LinkedIn: $1/1,000 (specific skill-based queries for quality)
        Indeed: $6/1,000 (single broad query for diversity, one call per session)
        """
        all_jobs: list[dict] = []
        seen: set[str] = set()

        # 1. LinkedIn, all specific queries (top 3)
        logger.info(f"[Apify/LinkedIn] Searching {len(queries[:3])} specific queries...")
        for job in search_linkedin(queries[:3]):
            link = job.get("link", "")
            key  = link or f"{job['title']}|{job['company']}"
            if key not in seen:
                seen.add(key)
                all_jobs.append(job)

        # 2. Indeed via Apify is DISABLED on purpose. The misceres/indeed-scraper
        #    ignores maxItems and crawls the whole location (4,000+ pages), which
        #    stalled the daily run for 30+ minutes and burned the entire monthly
        #    Apify free credit in a single call. Its output is also the lowest
        #    value in the digest: Indeed is demoted, and the ATS boards plus the
        #    free aggregators already cover this ground. Re-enable only behind an
        #    actor that honours a hard page cap. search_indeed() is kept for that.

        logger.info(f"[Apify] {len(all_jobs)} unique jobs total (Indeed disabled)")
        return all_jobs
