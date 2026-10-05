"""
Complete system validation.
Tests: job search, then cover letters for the top results, saved as DOCX.

The letters go through the same code as write_cover_letters.py: each one is
filed under its stored job id, and a job that already has a letter is
skipped, so a second run does not pay for the same letters again.
"""

import sys
from job_search_smart import SmartJobSearcher
from core.cover_letter_generator import CoverLetterGenerator
from core.config import Config
from core.database import Database
from core.utils import setup_logging
from write_cover_letters import letter_jobs_from_results, write_letter

logger = setup_logging('validate_system')

def main():
    """Run complete system validation."""
    print("\n" + "="*70)
    print("SYSTEM VALIDATION - COMPLETE END-TO-END TEST")
    print("="*70)

    try:
        # Step 1: Search for jobs
        print("\n[STEP 1] Searching for validated jobs...")
        print("-" * 70)

        searcher = SmartJobSearcher()
        jobs = searcher.search_all_sources()

        if not jobs:
            print("[-] No jobs found. Validation FAILED")
            return 1

        print(f"[✓] Found {len(jobs)} validated, active jobs")
        print("\nTop 3 jobs:")
        for i, job in enumerate(jobs[:3], 1):
            print(f"  {i}. {job['title']} @ {job['company']} ({job['relevance_score']}%)")

        # Step 2: Cover letters for the top jobs that have none yet
        print("\n[STEP 2] Writing cover letters for up to 3 top jobs...")
        print("-" * 70)

        with Database() as db:
            to_write = letter_jobs_from_results(db, jobs, 3)
            if not to_write:
                print("[✓] Every job found already has a letter. Nothing to pay for.")

            generator = CoverLetterGenerator() if to_write else None
            saved_files = []
            for i, job in enumerate(to_write, 1):
                print(f"[*] Letter {i}/{len(to_write)}: {job['title']} @ {job['company']}")
                filepath = write_letter(generator, db, job)
                if filepath:
                    saved_files.append(filepath)
                    print(f"    [✓] Saved: {filepath.name}")
                else:
                    print("    [-] No letter written")

        if to_write and not saved_files:
            print("[-] No cover letters written. Validation FAILED")
            return 1

        # Step 3: Final report
        print("\n" + "="*70)
        print("VALIDATION COMPLETE ✓")
        print("="*70)

        print(f"\n[✓] Jobs found: {len(jobs)}")
        print(f"[✓] Cover letters written: {len(saved_files)}")
        print(f"\n[✓] Output directory: {Config.OUTPUT_DIR}")

        print("\nTo run manually anytime:")
        print("  python job_search_smart.py")
        print("  python write_cover_letters.py")

        logger.info("System validation PASSED")
        return 0

    except Exception as e:
        logger.error(f"Validation error: {e}")
        print(f"\n[-] Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
