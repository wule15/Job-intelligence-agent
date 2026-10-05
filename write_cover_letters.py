#!/usr/bin/env python3
"""
Draft cover letters for the best matching jobs and save them as DOCX.

This is a manual step, not part of the scheduled run, and it sends nothing.
Letters land in OUTPUT_DIR and you review them before they go anywhere.

Two ways to choose the jobs:

    python write_cover_letters.py              stored jobs with no letter yet
    python write_cover_letters.py --search     search first, then use the results

The default reads what the daily run already stored, which is the cheaper
path and the one to use most days. --search is for when you want letters for
listings that have appeared since the last scheduled run.

Every letter costs one Claude API call, so the job count is capped and
adjustable with --limit.

Both ways file each letter under the id of the job's row in the database and
skip any job that already has a letter, so running the script again never
pays for the same letter twice.
"""

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from core.config import Config
from core.cover_letter_generator import CoverLetterGenerator, save_as_docx
from core.database import Database
from core.utils import setup_logging, format_cv_label

logger = setup_logging('write_cover_letters')

# Claude API calls are sequential here on purpose. The delay is politeness
# towards the rate limit, not a correctness requirement.
DELAY_BETWEEN_LETTERS_SECONDS = 2


def jobs_from_database(db, limit):
    """Stored jobs that have no letter yet, best scoring first."""
    rows = db.get_jobs_without_cover_letters(limit=limit)
    return [
        {
            'id': row[0],
            'title': row[1],
            'company': row[2],
            'description': row[3],
            'link': row[4],
            'best_cv': row[5],
        }
        for row in rows
    ]


def letter_jobs_from_results(db, found, limit):
    """
    Search results that need a letter, each under its stored job id.

    The search stores every job it returns, but the results themselves carry
    no database id, and some boards put their own posting id in that field.
    So each result is matched to its row the way it was stored, by title and
    company. A result with no row is skipped rather than written under no id,
    and a result that already has a letter is skipped, so the limit counts
    only letters that will actually be written.
    """
    picked = []
    seen = set()
    for job in found:
        job_id = db.find_job_id(job.get('title'), job.get('company'))
        if job_id is None or job_id in seen or db.job_has_cover_letter(job_id):
            continue
        seen.add(job_id)
        picked.append({
            'id': job_id,
            'title': job.get('title'),
            'company': job.get('company'),
            'description': job.get('description'),
            'link': job.get('link'),
            'best_cv': job.get('best_cv'),
        })
        if len(picked) >= limit:
            break
    return picked


def jobs_from_search(db, limit):
    """Run a live search and return its best matches that have no letter."""
    # Imported here rather than at module scope so the default path does not
    # pay for loading every connector.
    from job_search_smart import SmartJobSearcher

    print("Searching all sources first.")
    found = SmartJobSearcher().search_all_sources()
    return letter_jobs_from_results(db, found or [], limit)


def safe_filename(text, length=50):
    """Text made safe for a file name on Windows, macOS and Linux.

    Windows refuses <>:"/\\|?* in a name, except the colon, which it reads as
    a hidden side stream: the letter would land in an empty-looking file cut
    off at the colon. Anything other than letters, digits, spaces, dots and
    hyphens becomes a hyphen.
    """
    return re.sub(r'[^\w .-]', '-', text or '')[:length].strip(' .') or 'job'


def write_letter(generator, db, job):
    """
    Generate one letter, record it, and save it as DOCX.

    Returns the path written, or None when nothing was written. A letter is
    written only for a job stored in the database, filed under that job's id,
    and never for a job that already has one. The checks sit here as well as
    where the jobs are chosen, so no caller can pay for a letter filed under
    no id or written twice.
    """
    job_id = job.get('id')
    if not db.job_exists(job_id):
        logger.warning(f"Skipped {job.get('title')!r}: not a stored job, no letter written")
        return None
    if db.job_has_cover_letter(job_id):
        logger.info(f"Skipped job {job_id}: it already has a letter")
        return None

    letter = generator.generate_cover_letter(
        job['title'],
        job['company'],
        job['description'],
        cv_name_hint=job['best_cv'],
    )

    if not letter:
        return None

    formatted = generator.format_cover_letter(job['title'], job['company'], letter)

    # Recorded before the file is saved. If the save then fails, the text is
    # still in the database and the next run does not pay for it again.
    recorded = db.add_cover_letter(
        job_id=job_id,
        job_title=job['title'],
        company=job['company'],
        selected_cv=job['best_cv'] or 'auto',
        generated_letter=formatted,
    )
    if recorded is None:
        return None

    docx_dir = Path(Config.OUTPUT_DIR) / 'docx cover letters'
    docx_dir.mkdir(parents=True, exist_ok=True)

    filepath = docx_dir / f"cover_letter_{job_id}_{safe_filename(job['title'])}.docx"
    save_as_docx(formatted, str(filepath))

    return filepath


def notify_telegram(count):
    """Tell Telegram how many letters were written. Optional and best effort."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        return

    plural = '' if count == 1 else 's'
    message = (
        f"<b>Cover letters drafted</b>\n"
        f"{count} new letter{plural}\n"
        f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                'chat_id': Config.TELEGRAM_CHAT_ID,
                'text': message,
                'parse_mode': 'HTML',
            },
            timeout=10,
        )
    except Exception as exc:
        # Never print the exception object here. requests puts the full request
        # URL into connection errors, and that URL contains the bot token.
        logger.warning(f"Telegram notification failed: {type(exc).__name__}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().split('\n')[0])
    parser.add_argument(
        '--search',
        action='store_true',
        help='search all sources first instead of reading stored jobs',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=3,
        help='how many letters to write (default 3, one API call each)',
    )
    args = parser.parse_args()

    with Database() as db:
        jobs = jobs_from_search(db, args.limit) if args.search else jobs_from_database(db, args.limit)

        if not jobs:
            print(
                "Nothing to write. Every stored job already has a letter."
                if not args.search
                else "Nothing to write. The search found no stored job without a letter."
            )
            return 0

        print(f"Writing {len(jobs)} letter(s).\n")
        generator = CoverLetterGenerator()
        written = 0

        for index, job in enumerate(jobs, 1):
            cv_label = format_cv_label(job['best_cv']) or 'auto'
            print(f"{index}. {job['title']} at {job['company']}  [CV: {cv_label}]")

            try:
                filepath = write_letter(generator, db, job)
            except Exception as exc:
                logger.error(f"Failed on job {job['id']}: {exc}")
                print(f"   failed: {exc}")
                filepath = None

            if filepath:
                written += 1
                print(f"   saved {filepath.name}")
            else:
                print("   no letter written")

            if index < len(jobs):
                time.sleep(DELAY_BETWEEN_LETTERS_SECONDS)

        print(f"\nWrote {written} of {len(jobs)}. Review them in {Config.OUTPUT_DIR}.")

        if written:
            notify_telegram(written)

    return 0


if __name__ == '__main__':
    sys.exit(main())
