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
"""

import argparse
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


def jobs_from_search(limit):
    """Run a live search and return its best matches."""
    # Imported here rather than at module scope so the default path does not
    # pay for loading every connector.
    from job_search_smart import SmartJobSearcher

    print("Searching all sources first.")
    jobs = SmartJobSearcher().search_all_sources()
    if not jobs:
        return []

    return [
        {
            'id': job.get('id'),
            'title': job.get('title'),
            'company': job.get('company'),
            'description': job.get('description'),
            'link': job.get('link'),
            'best_cv': job.get('best_cv'),
        }
        for job in jobs[:limit]
    ]


def write_letter(generator, db, job):
    """
    Generate one letter, save it, and record it.

    Returns the path written, or None if generation failed. Recording it in
    the database is what stops the next run picking the same job up again.
    """
    letter = generator.generate_cover_letter(
        job['title'],
        job['company'],
        job['description'],
        cv_name_hint=job['best_cv'],
    )

    if not letter:
        return None

    formatted = generator.format_cover_letter(job['title'], job['company'], letter)

    docx_dir = Path(Config.OUTPUT_DIR) / 'docx cover letters'
    docx_dir.mkdir(parents=True, exist_ok=True)

    safe_title = job['title'].replace('/', '-').replace('\\', '-')[:50]
    filepath = docx_dir / f"cover_letter_{job['id']}_{safe_title}.docx"
    save_as_docx(formatted, str(filepath))

    db.add_cover_letter(
        job_id=job['id'],
        job_title=job['title'],
        company=job['company'],
        selected_cv=job['best_cv'] or 'auto',
        generated_letter=formatted,
    )

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
        jobs = jobs_from_search(args.limit) if args.search else jobs_from_database(db, args.limit)

        if not jobs:
            print(
                "Nothing to write. Every stored job already has a letter."
                if not args.search
                else "Nothing to write. The search returned no jobs."
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
                print("   no letter generated")

            if index < len(jobs):
                time.sleep(DELAY_BETWEEN_LETTERS_SECONDS)

        print(f"\nWrote {written} of {len(jobs)}. Review them in {Config.OUTPUT_DIR}.")

        if written:
            notify_telegram(written)

    return 0


if __name__ == '__main__':
    sys.exit(main())
