#!/usr/bin/env python3
"""Send job digest summary to Telegram - only NEW jobs (no duplicates)."""

import sqlite3
from collections import defaultdict
from datetime import datetime
from functools import lru_cache

import requests

from core.config import Config
from core.job_filter import ALWAYS_INCLUDE_SOURCES, scam_risk
from core.utils import format_cv_label

# ── Digest composition ───────────────────────────────────────────────────────
# Slots are a ceiling, not a floor. A job only occupies one if it clears
# MIN_DIGEST_SCORE against the CV profile. An unfilled slot means nothing
# qualified, and the run says so rather than padding with weak matches.
DIGEST_SIZE = 15
ATS_SLOTS = 5
AGGREGATOR_SLOTS = 5
MAX_PER_COMPANY = 2

# The separate direct-from-company digest. Kept small: it is a shortlist of
# the highest-signal jobs, not a second full feed.
DIRECT_DIGEST_SIZE = 10

# Higher than the storage cutoff. Everything above MIN_RELEVANCE_SCORE is
# worth keeping and reviewing on the dashboard; only the stronger matches are
# worth interrupting your day for.
MIN_DIGEST_SCORE = 25

# Company careers boards, read from their applicant tracking system. Held to
# their own quota because they are the highest signal source and would
# otherwise be crowded out by aggregator volume.
ATS_SOURCES = {'Greenhouse', 'Lever', 'Ashby', 'SmartRecruiters', 'Workday', 'SuccessFactors'}

# Aggregators that repeatedly serve expired or low-signal listings (Indeed via
# Apify or the plain adapter, and JSearch). They are still searched, but in the
# digest they only fill slots left over after ATS boards and the quality
# aggregators, so they can never dominate a day again. Matched as a substring
# so 'Apify / Indeed', 'Indeed' and 'JSearch' all qualify.
LOW_PRIORITY_SOURCE_MARKERS = ('indeed', 'jsearch')

# Phrases a careers page or aggregator shows once a posting is gone. Presence
# of any one marks the link expired.
EXPIRED_PAGE_MARKERS = (
    'no longer accepting applications', 'no longer available',
    'this job has expired', 'job posting has expired', 'posting has expired',
    'position has been filled', 'this position is no longer',
    'posting is no longer active', 'job is no longer available',
    'this job is no longer', 'nicht mehr verfügbar',
)


def is_low_priority_source(source):
    """True for the demoted aggregators (Indeed / JSearch)."""
    s = (source or '').lower()
    return any(marker in s for marker in LOW_PRIORITY_SOURCE_MARKERS)


@lru_cache(maxsize=2048)
def check_link_live(url, timeout=6):
    """
    Best-effort check that a job link still points at a live posting.

    Returns False ONLY when the posting is clearly gone: an HTTP 404 or 410, or
    a 200 page whose text says the role is filled or expired. Anything
    ambiguous, a timeout, a bot block, or any other non-200, returns True.
    Dropping a real job over a transient error is worse than letting one stale
    link through, so the check only removes what it can prove is dead.

    What it catches: a real HTTP 404 or 410 (RemoteOK and The Muse do this),
    and a 200 page carrying explicit expired text. What it does NOT catch: a
    soft 404 that answers 200 and quietly serves an index page (Jobicy does
    this), and a board that bot-blocks the request (Indeed answers 200 with a
    block page). Both read as live here. Keeping Indeed and JSearch off the
    digest is the job of the source demotion, not this check; this check is the
    net for the boards that fail honestly. Cached per run so the same URL is
    fetched at most once.
    """
    if not url:
        return True
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={'User-Agent': 'Mozilla/5.0 (job-digest liveness check)'})
    except Exception:
        return True
    if resp.status_code in (404, 410):
        return False
    if resp.status_code != 200:
        return True
    return not any(marker in resp.text.lower() for marker in EXPIRED_PAGE_MARKERS)


def init_telegram_tracking():
    """Create table to track which jobs have been sent via Telegram."""
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_sent_jobs (
                job_id INTEGER PRIMARY KEY,
                sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[!] Error initializing tracking: {e}")

def get_unsent_jobs(limit=DIGEST_SIZE, min_score=MIN_DIGEST_SCORE, is_live=None):
    """
    Choose the day's digest: guaranteed slots, every slot earned on merit.

    The old logic sorted by score and applied a per-source cap. With company
    careers boards added, one large employer can list 200 roles and will win
    that comparison every day, so the digest became one company plus noise.

    Quotas fix the mix. The score gate keeps them honest: a quota is a
    ceiling, never a floor. If only two company-board jobs clear the bar, you
    get two, not two plus three bad ones. An empty slot is information.

    Order of selection:
      1. Jobs you saved by hand, always, ignoring both quota and score
      2. Company careers boards, best first, capped per company
      3. Quality aggregators, best first, capped per company
      4. Wildcard from ATS + quality aggregators
      5. Last resort: the demoted aggregators (Indeed / JSearch), only if the
         digest is still short

    Indeed and JSearch are held back to step 5 so they can never crowd out the
    better sources. That is what stops a repeat of the all-Indeed digest.

    is_live: optional callable(url) -> bool. When given, a non-ATS candidate
    whose link it rejects is skipped as expired. ATS jobs bypass it: a company
    board only lists open roles, and bot blocks would false-drop them. Left
    None (the default) there is no network call, which keeps the unit tests
    offline.

    Returns a list of (id, title, company, score, link, best_cv, source) tuples.
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, job_title, company, relevance_score, link, best_cv, source
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT 400
        """)
        all_jobs = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[!] Error fetching jobs: {type(e).__name__}: {e}")
        return []

    if not all_jobs:
        return []

    selected = []
    chosen_ids = set()
    # Counted across every pass, not per pass. Tracking it per pass let one
    # employer take its cap in the board quota and its cap again in the
    # wildcard, which is the concentration this function exists to prevent.
    company_counts = defaultdict(int)

    def take(candidates, slots, per_company):
        """Fill up to `slots` from `candidates`, respecting the company cap.

        An aggregator candidate is dropped when the liveness check says its
        link is expired. ATS jobs and hand-saved jobs skip that check: ATS
        boards only list open roles, and a job you saved by hand you keep even
        if its link has since gone stale.
        """
        taken = 0
        for job in candidates:
            if taken >= slots or len(selected) >= limit:
                break
            if job[0] in chosen_ids:
                continue
            company = (job[2] or 'Unknown').strip().lower()
            if company_counts[company] >= per_company:
                continue
            liveness_exempt = job[6] in ATS_SOURCES or job[6] in ALWAYS_INCLUDE_SOURCES
            if is_live and not liveness_exempt and not is_live(job[4]):
                continue
            selected.append(job)
            chosen_ids.add(job[0])
            company_counts[company] += 1
            taken += 1
        return taken

    # 1. Manually saved jobs bypass everything. You already chose these.
    take([j for j in all_jobs if j[6] in ALWAYS_INCLUDE_SOURCES],
         slots=limit, per_company=limit)

    # Everything else must clear the score bar to be eligible at all.
    qualified = [j for j in all_jobs if (j[3] or 0) >= min_score]

    ats = [j for j in qualified if j[6] in ATS_SOURCES]
    # The demoted aggregators are held out of the main passes entirely; they
    # only get a look in the final last-resort fill below.
    low_priority = [j for j in qualified if is_low_priority_source(j[6])]
    quality_agg = [j for j in qualified
                   if j[6] not in ATS_SOURCES and not is_low_priority_source(j[6])]

    quota_filled = {
        'ATS boards': take(ats, ATS_SLOTS, MAX_PER_COMPANY),
        'aggregators': take(quality_agg, AGGREGATOR_SLOTS, MAX_PER_COMPANY),
    }

    # 4. Wildcard: best of what is left, but still only from the preferred
    #    sources (ATS + quality aggregators). This absorbs an unfilled quota
    #    without reaching for the demoted sources.
    quota_filled['wildcard'] = take(ats + quality_agg, limit - len(selected), MAX_PER_COMPANY)

    # 5. Last resort: the demoted Indeed / JSearch listings, only if the digest
    #    is still short. On a healthy day with enough ATS and quality jobs this
    #    takes nothing.
    quota_filled['last_resort'] = take(low_priority, limit - len(selected), MAX_PER_COMPANY)

    shortfall = {
        name: expected - filled
        for name, filled, expected in (
            ('ATS boards', quota_filled['ATS boards'], ATS_SLOTS),
            ('aggregators', quota_filled['aggregators'], AGGREGATOR_SLOTS),
        )
        if filled < expected
    }
    if shortfall:
        # Either nothing cleared the score bar, or the per-company cap bit.
        # Both are worth seeing: a chronically short board quota means the
        # company list needs more entries.
        print("[*] Quota not filled: "
              + ', '.join(f"{name} short {n}" for name, n in shortfall.items())
              + f" (score bar {min_score}, max {MAX_PER_COMPANY} per company)")

    # Keep the source column (job[6]) so the digest can show where each job came
    # from. Returns (id, title, company, score, link, best_cv, source) tuples.
    return [job[:7] for job in selected]

def get_direct_jobs(limit=DIRECT_DIGEST_SIZE, min_score=MIN_DIGEST_SCORE):
    """
    The direct-from-company shortlist: unsent jobs from company careers boards
    only (ATS_SOURCES), best first, capped per company.

    These are the highest-signal jobs in the system. A current opening posted
    by a company on its own site puts an application in front of that
    company's recruiter, not an aggregator's copy. They get their own message
    so aggregator volume cannot bury them.

    Same score gate and per-company cap as the main digest, so a slot is never
    padded with a weak match. An empty result is normal, and the run says so
    rather than sending a filler message.

    Returns (id, title, company, score, link, best_cv, source) tuples.
    """
    sources = list(ATS_SOURCES)
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        placeholders = ','.join('?' for _ in sources)
        cursor.execute(f"""
            SELECT id, job_title, company, relevance_score, link, best_cv, source
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)
              AND source IN ({placeholders})
              AND relevance_score >= ?
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT 200
        """, (*sources, min_score))
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[!] Error fetching direct jobs: {type(e).__name__}: {e}")
        return []

    selected = []
    company_counts = defaultdict(int)
    for job in rows:
        if len(selected) >= limit:
            break
        company = (job[2] or 'Unknown').strip().lower()
        if company_counts[company] >= MAX_PER_COMPANY:
            continue
        selected.append(job)
        company_counts[company] += 1

    return [job[:7] for job in selected]

def mark_jobs_sent(job_ids):
    """Mark jobs as sent in Telegram."""
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()

        for job_id in job_ids:
            cursor.execute("""
                INSERT INTO telegram_sent_jobs (job_id)
                VALUES (?)
                ON CONFLICT(job_id) DO NOTHING
            """, (job_id,))

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[!] Error marking jobs sent: {e}")
        return False

def send_telegram_message(message):
    """Send message to Telegram."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        print("[!] Telegram credentials not configured. Skipping message.")
        return False

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': Config.TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            print("[+] Telegram message sent successfully!")
            return True
        else:
            print(f"[!] Telegram error: HTTP {response.status_code}")
            return False
    except Exception as e:
        # Print the exception class only. requests puts the full request URL
        # into connection errors, and that URL contains the bot token.
        print(f"[!] Error sending Telegram message: {type(e).__name__}")
        return False

def _render_job_lines(jobs):
    """
    Render the numbered job list shared by both digests.

    One renderer, so the main and direct-company digests can never drift apart
    in how a job is shown. Expects (id, title, company, score, link, best_cv,
    source) tuples.
    """
    lines = ""
    for i, (job_id, title, company, score, link, best_cv, source) in enumerate(jobs, 1):
        score_pct = round(score, 1) if score else 0
        # Recompute the scam-risk flag from the fields the digest has. The scorer
        # already sank this job; the marker tells you why so you verify first.
        risky = scam_risk(title, company=company, link=link or '')
        flag = " ⚠️" if risky else ""
        lines += f"<b>{i}. {title}{flag}</b>\n"
        lines += f"   💼 {company}\n"
        lines += f"   ⭐ Match: {score_pct}%\n"
        if risky:
            lines += "   ⚠️ Possible scam, verify the employer before applying\n"
        if source:
            lines += f"   🌐 {source}\n"
        cv_label = format_cv_label(best_cv)
        if cv_label:
            lines += f"   📄 CV: {cv_label}\n"
        if link:
            lines += f"   🔗 <a href='{link}'>View Job</a>\n"
        lines += "\n"
    return lines

def format_job_digest(jobs):
    """Format the main job digest for Telegram."""
    # A label lets two engines running side by side be told apart in the chat.
    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    if not jobs:
        message = f"📭 <b>No new jobs found</b>{label}\n\nAll caught up, check again tomorrow!"
        return message, []

    job_ids = [job[0] for job in jobs]
    message = f"🔍 <b>Daily Job Digest</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> new jobs:\n\n"
    message += _render_job_lines(jobs)
    return message, job_ids

def format_direct_digest(jobs):
    """
    Format the direct-from-company digest for Telegram.

    Returns (message, job_ids). job_ids is empty when there is nothing to
    send, which the caller reads as "skip this message" rather than posting a
    filler note every day.
    """
    if not jobs:
        return "", []

    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    job_ids = [job[0] for job in jobs]
    message = f"🏢 <b>Direct company openings</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> straight from company careers pages:\n\n"
    message += _render_job_lines(jobs)
    return message, job_ids

def main():
    print("[*] Initializing Telegram tracking...")
    init_telegram_tracking()

    # Direct-from-company digest goes first, and marks its jobs sent before the
    # main digest is selected. That ordering is what stops the same job showing
    # up in both messages: the main digest only ever sees jobs not yet sent.
    print("[*] Fetching direct-from-company jobs...")
    direct = get_direct_jobs()
    direct_msg, direct_ids = format_direct_digest(direct)
    if direct_ids:
        print(f"[*] Sending {len(direct_ids)} direct-company jobs...")
        if send_telegram_message(direct_msg):
            print(f"[+] Marking {len(direct_ids)} direct jobs as sent...")
            mark_jobs_sent(direct_ids)
        else:
            print("[!] Direct digest failed to send - not marking those jobs sent")
    else:
        print("[*] No direct-from-company jobs today")

    print("[*] Fetching unsent jobs...")
    # Liveness check drops expired links from the aggregators before they reach
    # the digest. ATS jobs skip it inside get_unsent_jobs.
    jobs = get_unsent_jobs(limit=10, is_live=check_link_live)

    digest, job_ids = format_job_digest(jobs)

    if not job_ids:
        print("[*] No new jobs to send")
        return

    print(f"[*] Found {len(job_ids)} new jobs to send")
    print("[*] Sending to Telegram...")

    if send_telegram_message(digest):
        print(f"[+] Marking {len(job_ids)} jobs as sent...")
        mark_jobs_sent(job_ids)
    else:
        print("[!] Failed to send - not marking jobs as sent")

if __name__ == '__main__':
    main()
