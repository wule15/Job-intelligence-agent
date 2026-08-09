#!/usr/bin/env python3
"""Send job digest summary to Telegram - only NEW jobs (no duplicates)."""

import sqlite3
from collections import defaultdict
from datetime import datetime

import requests

from core.config import Config
from core.job_filter import ALWAYS_INCLUDE_SOURCES
from core.utils import format_cv_label

# ── Digest composition ───────────────────────────────────────────────────────
# Slots are a ceiling, not a floor. A job only occupies one if it clears
# MIN_DIGEST_SCORE against the CV profile. An unfilled slot means nothing
# qualified, and the run says so rather than padding with weak matches.
DIGEST_SIZE = 15
ATS_SLOTS = 5
AGGREGATOR_SLOTS = 5
MAX_PER_COMPANY = 2

# Higher than the storage cutoff. Everything above MIN_RELEVANCE_SCORE is
# worth keeping and reviewing on the dashboard; only the stronger matches are
# worth interrupting your day for.
MIN_DIGEST_SCORE = 25

# Company careers boards, read from their applicant tracking system. Held to
# their own quota because they are the highest signal source and would
# otherwise be crowded out by aggregator volume.
ATS_SOURCES = {'Greenhouse', 'Lever', 'Ashby', 'SmartRecruiters', 'Workday'}

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

def get_unsent_jobs(limit=DIGEST_SIZE, min_score=MIN_DIGEST_SCORE):
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
      3. Aggregators, best first, capped per company
      4. Wildcard, best remaining regardless of source

    Returns a list of (id, title, company, score, link, best_cv) tuples.
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
        """Fill up to `slots` from `candidates`, respecting the company cap."""
        taken = 0
        for job in candidates:
            if taken >= slots or len(selected) >= limit:
                break
            if job[0] in chosen_ids:
                continue
            company = (job[2] or 'Unknown').strip().lower()
            if company_counts[company] >= per_company:
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
    aggregator = [j for j in qualified if j[6] not in ATS_SOURCES]

    quota_filled = {
        'ATS boards': take(ats, ATS_SLOTS, MAX_PER_COMPANY),
        'aggregators': take(aggregator, AGGREGATOR_SLOTS, MAX_PER_COMPANY),
    }

    # 4. Wildcard: best of whatever is left, any source. This is what absorbs
    #    an unfilled quota rather than leaving the digest short.
    quota_filled['wildcard'] = take(qualified, limit - len(selected), MAX_PER_COMPANY)

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

    return [job[:6] for job in selected]

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

def format_job_digest(jobs):
    """Format job digest for Telegram."""
    # A label lets two engines running side by side be told apart in the chat.
    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    if not jobs:
        message = f"📭 <b>No new jobs found</b>{label}\n\nAll caught up — check again tomorrow!"
        return message, []

    job_ids = [job[0] for job in jobs]
    message = f"🔍 <b>Daily Job Digest</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> new jobs:\n\n"

    for i, (job_id, title, company, score, link, best_cv) in enumerate(jobs, 1):
        score_pct = round(score, 1) if score else 0
        message += f"<b>{i}. {title}</b>\n"
        message += f"   💼 {company}\n"
        message += f"   ⭐ Match: {score_pct}%\n"
        cv_label = format_cv_label(best_cv)
        if cv_label:
            message += f"   📄 CV: {cv_label}\n"
        if link:
            message += f"   🔗 <a href='{link}'>View Job</a>\n"
        message += "\n"

    return message, job_ids

def main():
    print("[*] Initializing Telegram tracking...")
    init_telegram_tracking()

    print("[*] Fetching unsent jobs...")
    jobs = get_unsent_jobs(limit=10)

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
