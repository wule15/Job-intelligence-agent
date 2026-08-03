#!/usr/bin/env python3
"""
Enrich email-tracked jobs with real job descriptions via DuckDuckGo web search.

For each email-origin job that has a generic/missing description, this script:
  1. Searches DDG for "job_title company_name job requirements responsibilities"
  2. Aggregates top result snippets into a description proxy
  3. Updates description and relevance_score in the DB

Free, no API key required.

Usage:
    python enrich_email_jobs.py
    python enrich_email_jobs.py --days 30   # only jobs added in last 30 days
    python enrich_email_jobs.py --rescore   # rescore ALL email jobs, even already-scored ones
"""

import sqlite3
import time
import re
import argparse
import requests
from bs4 import BeautifulSoup
from core.config import Config
from core.job_filter import JobFilter


_SCRAPE_SKIP = re.compile(
    r'myworkday\.com|workday\.com|linkedin\.com|taleo\.net|csod\.com|icims\.com',
    re.IGNORECASE,
)

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

_SELECTORS = [
    '.posting-body',
    '.job__description',
    '#content .section',
    '.ashby-job-posting-description',
    '[data-ui="job-description"]',
    '.job-description',
    '.description',
    '[class*="jobDescription"]',
    '[class*="job-detail"]',
    '[class*="posting-body"]',
    'main article',
    'main',
    'article',
]


def _scrape(url: str) -> str | None:
    """Fetch a job URL and return description text, or None."""
    if not url or _SCRAPE_SKIP.search(url):
        return None
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'noscript']):
            tag.decompose()
        for sel in _SELECTORS:
            elem = soup.select_one(sel)
            if elem:
                text = re.sub(r'\s+', ' ', elem.get_text(separator=' ', strip=True))
                if len(text) > 150:
                    return text[:3000]
    except Exception:
        pass
    return None


def _ddg_search(title: str, company: str) -> str | None:
    """
    Search DuckDuckGo for the job posting and return aggregated snippet text.
    Returns None if no useful results found.
    """
    try:
        from ddgs import DDGS
        query = f'"{title}" "{company}" job requirements responsibilities'
        results = DDGS().text(query, max_results=5)
        if not results:
            results = DDGS().text(f"{title} {company} job description", max_results=5)
        if not results:
            return None
        snippets = ' '.join(r.get('body', '') for r in results if r.get('body'))
        return snippets[:3000] if snippets.strip() else None
    except Exception as e:
        print(f"  [!] DDG search error: {e}")
        return None


def run(days_back: int | None = None, rescore: bool = False):
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    age_clause = ''
    params: list = []
    if days_back:
        age_clause = "AND extracted_date >= datetime('now', ?)"
        params.append(f'-{days_back} days')

    score_clause = '' if rescore else 'AND (relevance_score IS NULL OR relevance_score = 0)'

    cur.execute(f"""
        SELECT id, job_title, company, description, relevance_score, link
        FROM jobs
        WHERE source LIKE 'Email%'
          AND (
              description IS NULL
              OR LOWER(TRIM(description)) LIKE 'applied via email tracker%'
          )
          {score_clause}
          {age_clause}
        ORDER BY id DESC
    """, params)
    jobs = cur.fetchall()

    if not jobs:
        print("[*] No email jobs need enrichment.")
        conn.close()
        return

    scorer = JobFilter()
    print(f"[*] Enriching {len(jobs)} email-origin job(s)...\n")

    enriched = 0
    title_only = 0
    not_found = 0

    for job in jobs:
        jid     = job['id']
        title   = job['job_title']
        company = job['company']
        link    = job['link']

        print(f"  {title} @ {company} ...", end=' ', flush=True)

        # 1. Scrape the URL from the email (most accurate)
        description = _scrape(link) if link else None
        source_label = 'email link' if description else None

        # 1b. Apify crawler for JS-heavy pages plain requests can't handle
        if not description and link:
            try:
                from job_search_apify import scrape_job_page
                description = scrape_job_page(link)
                if description:
                    source_label = 'Apify crawl'
            except Exception:
                pass

        # 2. DDG fallback
        if not description:
            description = _ddg_search(title, company)
            if description:
                source_label = 'web search'
            time.sleep(0.8)

        if description:
            score, best_cv = scorer.score_job_with_cv(title, description, company)
            cur.execute("""
                UPDATE jobs
                SET description     = ?,
                    relevance_score = ?,
                    best_cv         = COALESCE(NULLIF(best_cv, ''), ?)
                WHERE id = ?
            """, (description[:2000], score, best_cv, jid))
            conn.commit()
            print(f"OK  [{score:.1f}% match, {source_label}]")
            enriched += 1
        else:
            # Fallback: score by title+company only
            score, best_cv = scorer.score_job_with_cv(title, title, company)
            if score > 0:
                cur.execute("""
                    UPDATE jobs
                    SET relevance_score = ?,
                        best_cv = COALESCE(NULLIF(best_cv, ''), ?)
                    WHERE id = ? AND (relevance_score IS NULL OR relevance_score = 0)
                """, (score, best_cv, jid))
                conn.commit()
                print(f"title-only [{score:.1f}% match]")
            else:
                print("no score")
            title_only += 1

    conn.close()
    print(f"\n[+] Done:")
    print(f"    {enriched} enriched via web search")
    print(f"    {title_only} scored by title only (DDG returned nothing)")
    if title_only:
        print(f"    Note: older postings may no longer appear in web search results")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Enrich email-tracked jobs with real descriptions')
    parser.add_argument('--days',    type=int, default=None, help='Only jobs added in last N days')
    parser.add_argument('--rescore', action='store_true',    help='Rescore all email jobs, even already-scored ones')
    args = parser.parse_args()

    print("=" * 60)
    print("EMAIL JOB ENRICHMENT (DDG web search)")
    print("=" * 60)
    run(days_back=args.days, rescore=args.rescore)
