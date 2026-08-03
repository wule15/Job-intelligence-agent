"""
Complete system validation.
Tests: job search → cover letter generation → output files.
"""

import sys
from pathlib import Path
from job_search_smart import SmartJobSearcher
from core.cover_letter_generator import CoverLetterGenerator
from core.config import Config
from core.utils import setup_logging

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

        # Step 2: Generate cover letters
        print("\n[STEP 2] Generating cover letters for top 3 jobs...")
        print("-" * 70)

        generator = CoverLetterGenerator()
        cover_letters = []

        for i, job in enumerate(jobs[:3], 1):
            print(f"[*] Generating letter {i}/3: {job['title']} @ {job['company']}")

            letter = generator.generate_cover_letter(
                job.get('title'),
                job.get('company'),
                job.get('description')
            )

            if letter:
                formatted = generator.format_cover_letter(
                    job.get('title'),
                    job.get('company'),
                    letter
                )

                cover_letters.append({
                    'job_title': job.get('title'),
                    'company': job.get('company'),
                    'letter': formatted,
                    'salary': job.get('salary', 'N/A')
                })

                print(f"    [✓] Generated ({len(letter)} chars)")
            else:
                print(f"    [-] Failed to generate")

        if not cover_letters:
            print("[-] No cover letters generated. Validation FAILED")
            return 1

        print(f"\n[✓] Generated {len(cover_letters)} cover letters")

        # Step 3: Save to files
        print("\n[STEP 3] Saving cover letters to output folder...")
        print("-" * 70)

        output_dir = Config.OUTPUT_DIR
        output_dir.mkdir(exist_ok=True)

        saved_files = []
        for cl in cover_letters:
            filename = f"CoverLetter_{cl['company'].replace(' ', '_')}_{cl['job_title'].replace(' ', '_')[:20]}.txt"
            filepath = output_dir / filename

            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(cl['letter'])
                saved_files.append(filepath)
                print(f"[✓] Saved: {filename}")
            except Exception as e:
                print(f"[-] Error saving {filename}: {e}")

        if not saved_files:
            print("[-] No files saved. Validation FAILED")
            return 1

        # Step 4: Final report
        print("\n" + "="*70)
        print("VALIDATION COMPLETE ✓")
        print("="*70)

        print(f"\n[✓] Jobs found: {len(jobs)}")
        print(f"[✓] Cover letters generated: {len(cover_letters)}")
        print(f"[✓] Files saved: {len(saved_files)}")
        print(f"\n[✓] Output directory: {Config.OUTPUT_DIR}")

        print("\n" + "="*70)
        print("SYSTEM READY FOR DAILY AUTOMATION")
        print("="*70)

        print("\nTo run daily at 9 AM, execute:")
        print("  powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1")

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
