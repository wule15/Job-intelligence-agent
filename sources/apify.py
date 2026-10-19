"""
Job search via Apify actors.
Uses pre-built actors for LinkedIn Jobs and Indeed, full descriptions,
salaries, apply links. Far more structured than DDG snippets.

Actors used:
  - bebity/linkedin-jobs-scraper   (LinkedIn Jobs)
  - misceres/indeed-scraper        (Indeed)
  - apify/website-content-crawler  (career page enrichment)

Paid per result on Apify's free plan ($5 a month, hard stop). See Budget below.
"""

import os
import time
from datetime import datetime
from decimal import Decimal

from core.countries import iso_name, with_countries
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


# ── Budget ────────────────────────────────────────────────────────────────────
# Both actors are paid per result and the account is on Apify's free plan,
# which stops at $5 a month (it never bills). A normal run costs about $0.13:
# LinkedIn 3 searches x 15 jobs at about $0.0015 a job, Indeed 1 search x 10
# jobs at $0.006 a job. APIFY_MONTHLY_CAP_USD stops calling Apify a little
# before the plan's cap, so the source goes quiet for the last days of a
# cycle instead of failing mid-run. Every call also carries its own hard cost
# limit (max_total_charge_usd), so one call can never run away.
LINKEDIN_ROWS = 15
LINKEDIN_CALL_LIMIT_USD = Decimal('0.05')
INDEED_ROWS = 10
INDEED_CALL_LIMIT_USD = Decimal('0.08')
# LinkedIn experience levels: 1 internship, 2 entry, 3 associate, 4 mid-senior,
# 5 director. Entry and associate match the roles the digest is for.
LINKEDIN_LEVELS = ["2", "3"]


def _monthly_cap():
    try:
        return float(os.getenv('APIFY_MONTHLY_CAP_USD', '4.5'))
    except ValueError:
        return 4.5


def budget_left(client) -> bool:
    """False once this cycle's Apify usage reaches APIFY_MONTHLY_CAP_USD.
    An unreadable usage figure counts as budget left: the per-call cost
    limits still apply."""
    try:
        current = client.user().limits().get('current', {})
        used = float(current.get('monthlyUsageUsd') or 0)
    except Exception as e:
        logger.debug(f"[Apify] usage check failed: {type(e).__name__}")
        return True
    if used >= _monthly_cap():
        logger.warning(f"[Apify] ${used:.2f} used this cycle, cap ${_monthly_cap():.2f}: "
                       f"skipping Apify until the cycle resets")
        return False
    return True


def _indeed_country(day=None) -> str:
    """One Indeed country a day, in turn, from APIFY_INDEED_COUNTRIES."""
    codes = [c.strip().upper() for c in
             os.getenv('APIFY_INDEED_COUNTRIES', 'GB,IE,NL,DE,AT,BE,CH').split(',') if c.strip()]
    if not codes:
        return 'GB'
    day = day if day is not None else datetime.now().timetuple().tm_yday
    return codes[day % len(codes)]


# ── LinkedIn Jobs ─────────────────────────────────────────────────────────────

def search_linkedin(queries: list[str], client=None) -> list[dict]:
    """
    Run the LinkedIn Jobs scraper once per query. Actor: bebity/linkedin-jobs-scraper.

    The actor's input changed: it used to take queries, locationFilter,
    datePostedFilter and count, and now takes titles, locations, rows and
    publishedAt. The old names were silently ignored, so every call returned
    the actor's default listing (US restaurant and retail jobs) instead of
    the search. rows must be set: 0 means every page of results.
    """
    if client is None:
        try:
            client = _client()
        except ValueError as e:
            logger.warning(f"[Apify] {e}")
            return []
    location = os.getenv('APIFY_LINKEDIN_LOCATION') or 'European Union'

    jobs = []
    for query in queries:
        logger.info(f"[Apify/LinkedIn] Searching: {query!r} in {location}")
        try:
            run = client.actor("bebity/linkedin-jobs-scraper").call(run_input={
                "titles": [query],
                "locations": [location],
                "rows": LINKEDIN_ROWS,
                "publishedAt": "r604800",       # past week
                "experienceLevels": LINKEDIN_LEVELS,
            }, max_items=LINKEDIN_ROWS, max_total_charge_usd=LINKEDIN_CALL_LIMIT_USD,
               timeout_secs=120)
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                try:
                    jobs.append({
                        "title":          item.get("title", ""),
                        "company":        item.get("companyName", ""),
                        "description":    item.get("description", ""),
                        "link":           item.get("jobUrl") or item.get("applyUrl", ""),
                        "location":       item.get("location", ""),
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

def search_indeed(queries: list[str], country=None, client=None) -> list[dict]:
    """
    Run the Indeed scraper once per query in one country. Actor: misceres/indeed-scraper.

    It used to search the US site (the actor's default country) with two
    fixed queries, "remote engineer" and "content strategy", and dropped the
    location the actor returns, so US jobs reached the digest with no place
    the country filter could read. It now searches one European Indeed site a
    day with the CV's own query, and keeps the location, tagged with the
    country searched. maxItems and parseJobDetail no longer exist in the
    actor's input; maxItemsPerSearch is the cap.
    """
    if client is None:
        try:
            client = _client()
        except ValueError as e:
            logger.warning(f"[Apify] {e}")
            return []
    country = country or _indeed_country()

    jobs = []
    for query in queries:
        logger.info(f"[Apify/Indeed] Searching: {query!r} in {country}")
        try:
            run = client.actor("misceres/indeed-scraper").call(run_input={
                "position": query,
                "country": country,
                "maxItemsPerSearch": INDEED_ROWS,
            }, max_items=INDEED_ROWS, max_total_charge_usd=INDEED_CALL_LIMIT_USD,
               timeout_secs=120)
            for item in client.dataset(run["defaultDatasetId"]).iterate_items():
                try:
                    jobs.append({
                        "title":          item.get("positionName", ""),
                        "company":        item.get("company", ""),
                        "description":    item.get("description", ""),
                        "link":           item.get("url", ""),
                        "location":       with_countries(item.get("location", ""), [iso_name(country)]),
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
        LinkedIn with the top three CV queries, then Indeed with the top one,
        both under the monthly budget check. Duplicates by link are dropped.
        """
        try:
            client = _client()
        except ValueError as e:
            logger.warning(f"[Apify] {e}")
            return []
        if not budget_left(client):
            return []

        all_jobs: list[dict] = []
        seen: set[str] = set()

        def keep(found):
            for job in found:
                link = job.get("link", "")
                key = link or f"{job['title']}|{job['company']}"
                if key not in seen:
                    seen.add(key)
                    all_jobs.append(job)

        logger.info(f"[Apify/LinkedIn] Searching {len(queries[:3])} specific queries...")
        keep(search_linkedin(queries[:3], client=client))
        logger.info("[Apify/Indeed] Searching the top query...")
        keep(search_indeed(queries[:1], client=client))

        logger.info(f"[Apify] {len(all_jobs)} unique jobs total")
        return all_jobs
