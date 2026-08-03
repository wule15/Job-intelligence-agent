#!/usr/bin/env python3
"""
Weekly job search summary — sent every Monday via Telegram.
Covers the past 7 days: jobs found, applied, interviews, top matches, source breakdown.

Add to run_job_search.bat or Task Scheduler:
  python weekly_digest.py   (runs every day but only sends on Mondays)
"""

import sqlite3
import requests
from datetime import datetime, timedelta
from core.config import Config
from core.utils import format_cv_label


def get_weekly_stats():
    """Query DB for the last 7 days of activity."""
    conn = sqlite3.connect(Config.DATABASE_PATH)
    cursor = conn.cursor()
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

    # Jobs found this week
    cursor.execute(
        "SELECT COUNT(*) FROM jobs WHERE DATE(extracted_date) >= ?", (week_ago,)
    )
    jobs_found = cursor.fetchone()[0]

    # Application statuses
    cursor.execute("""
        SELECT a.status, COUNT(*) FROM application_status a
        JOIN jobs j ON j.id = a.job_id
        WHERE DATE(j.extracted_date) >= ? OR DATE(a.updated_date) >= ?
        GROUP BY a.status
    """, (week_ago, week_ago))
    status_map = {row[0]: row[1] for row in cursor.fetchall()}

    # Top 5 matches this week
    cursor.execute("""
        SELECT job_title, company, relevance_score, best_cv, link
        FROM jobs
        WHERE DATE(extracted_date) >= ?
        ORDER BY relevance_score DESC
        LIMIT 5
    """, (week_ago,))
    top_jobs = cursor.fetchall()

    # Source breakdown this week
    cursor.execute("""
        SELECT source, COUNT(*) FROM jobs
        WHERE DATE(extracted_date) >= ?
        GROUP BY source ORDER BY COUNT(*) DESC
    """, (week_ago,))
    sources = cursor.fetchall()

    # Average score this week
    cursor.execute(
        "SELECT AVG(relevance_score), MAX(relevance_score) FROM jobs "
        "WHERE DATE(extracted_date) >= ?", (week_ago,)
    )
    avg_score, max_score = cursor.fetchone()

    # Cover letters generated this week
    cursor.execute("""
        SELECT COUNT(*) FROM cover_letters_sent cl
        JOIN jobs j ON j.id = cl.job_id
        WHERE DATE(j.extracted_date) >= ?
    """, (week_ago,))
    cover_letters = cursor.fetchone()[0]

    conn.close()

    return {
        'jobs_found':    jobs_found,
        'applied':       status_map.get('applied', 0),
        'interviewing':  status_map.get('interviewing', 0),
        'rejected':      status_map.get('rejected', 0),
        'top_jobs':      top_jobs,
        'sources':       sources,
        'avg_score':     round(avg_score or 0, 1),
        'max_score':     round(max_score or 0, 1),
        'cover_letters': cover_letters,
    }


def format_weekly_message(stats):
    """Build the Telegram HTML message."""
    week_start = (datetime.now() - timedelta(days=7)).strftime('%b %d')
    week_end   = datetime.now().strftime('%b %d, %Y')

    msg  = f"📊 <b>Weekly Job Report</b>\n"
    msg += f"<i>{week_start} – {week_end}</i>\n\n"

    # Overview
    msg += f"<b>Overview</b>\n"
    msg += f"  🔍 Jobs found:      <b>{stats['jobs_found']}</b>\n"
    msg += f"  ✉️  Cover letters:   <b>{stats['cover_letters']}</b>\n"
    msg += f"  📤 Applied:         <b>{stats['applied']}</b>\n"
    if stats['interviewing']:
        msg += f"  🗣 Interviewing:   <b>{stats['interviewing']}</b>\n"
    if stats['rejected']:
        msg += f"  ❌ Rejected:       <b>{stats['rejected']}</b>\n"
    msg += f"  ⭐ Avg / Max score: <b>{stats['avg_score']}% / {stats['max_score']}%</b>\n\n"

    # Top matches
    if stats['top_jobs']:
        msg += f"<b>Top Matches</b>\n"
        for title, company, score, best_cv, link in stats['top_jobs']:
            cv_label = format_cv_label(best_cv) or '—'
            score_str = f"{round(score, 1)}%" if score else "—"
            title_short = title[:45] + '…' if len(title) > 45 else title
            if link:
                msg += f"  • <a href='{link}'>{title_short}</a> @ {company}\n"
            else:
                msg += f"  • {title_short} @ {company}\n"
            msg += f"    ⭐ {score_str}  📄 {cv_label}\n"
        msg += "\n"

    # Source breakdown
    if stats['sources']:
        msg += "<b>Sources</b>\n"
        for source, count in stats['sources'][:6]:
            msg += f"  {source or 'Unknown'}: {count}\n"

    return msg


def send_telegram(message):
    """Send message to Telegram."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        print("[!] Telegram not configured")
        return False
    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, data={
            'chat_id': Config.TELEGRAM_CHAT_ID,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        # Print the exception class only. requests puts the full request URL
        # into connection errors, and that URL contains the bot token.
        print(f"[!] Telegram error: {type(e).__name__}")
        return False


def main():
    today = datetime.now().weekday()  # 0 = Monday

    if today != 0:
        print(f"[*] Not Monday (weekday={today}) — skipping weekly digest")
        return 0

    print("[*] Monday — generating weekly digest...")
    stats = get_weekly_stats()
    message = format_weekly_message(stats)

    print(message)
    if send_telegram(message):
        print("[+] Weekly digest sent to Telegram")
    else:
        print("[!] Failed to send weekly digest")

    return 0


if __name__ == '__main__':
    import sys
    # Pass --force to send regardless of day (for testing)
    if '--force' in sys.argv:
        stats = get_weekly_stats()
        message = format_weekly_message(stats)
        print(message)
        send_telegram(message)
    else:
        sys.exit(main())
