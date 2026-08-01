"""
Combined job search: JSearch API + Gmail drafts.
Gets automated jobs from JSearch, supplements with manually saved draft jobs.
"""

import sys
from job_search_jsearch import JSearchJobScraper
from job_search_gmail_drafts import GmailDraftJobExtractor
from job_filter import JobFilter
from database import Database
from utils import setup_logging

logger = setup_logging('job_search_combined')

class CombinedJobSearcher:
    """Search jobs from multiple sources."""

    def __init__(self):
        self.jsearch = JSearchJobScraper()
        self.gmail = GmailDraftJobExtractor()
        self.filter = JobFilter()
        self.db = Database()

    def search_all_sources(self):
        """Search JSearch API and Gmail drafts."""
        logger.info("="*70)
        logger.info("COMBINED JOB SEARCH (JSearch API + Gmail Drafts)")
        logger.info("="*70)

        all_jobs = []

        # 1. Search JSearch API
        print("\n[*] Searching JSearch API...")
        jsearch_jobs = self.jsearch.search_all()
        print(f"[+] JSearch found {len(jsearch_jobs)} relevant jobs")
        all_jobs.extend(jsearch_jobs)

        # 2. Extract from Gmail drafts
        print("\n[*] Checking Gmail drafts...")
        try:
            draft_jobs = self.gmail.process_draft_jobs()
            print(f"[+] Gmail drafts found {len(draft_jobs)} relevant jobs")
            all_jobs.extend(draft_jobs)
        except Exception as e:
            logger.warning(f"Could not access Gmail drafts: {e}")
            print(f"[!] Gmail drafts unavailable: {e}")

        # 3. Remove duplicates
        print(f"\n[*] Total jobs from all sources: {len(all_jobs)}")

        seen = set()
        unique_jobs = []
        for job in all_jobs:
            title_company = (job.get('title', ''), job.get('company', ''))
            if title_company not in seen:
                seen.add(title_company)
                unique_jobs.append(job)

        print(f"[+] Unique jobs (after dedup): {len(unique_jobs)}")

        # 4. Re-rank all jobs by relevance
        print(f"\n[*] Ranking by relevance to your profile...")
        scored_jobs = []
        for job in unique_jobs:
            score = self.filter.score_job(
                job.get('title', ''),
                job.get('description', ''),
                job.get('company', '')
            )
            job['relevance_score'] = score
            if score >= 1:  # Very low threshold to include everything
                scored_jobs.append(job)

        # Sort by relevance
        scored_jobs.sort(key=lambda x: x['relevance_score'], reverse=True)

        print(f"[+] Final matching jobs: {len(scored_jobs)}")

        # 5. Store all in database
        for job in scored_jobs:
            try:
                self.db.add_job(
                    job_title=job.get('title'),
                    company=job.get('company'),
                    description=job.get('description'),
                    link=job.get('link'),
                    salary=job.get('salary'),
                    source=job.get('source'),
                    relevance_score=job.get('relevance_score', 0)
                )
            except Exception as e:
                logger.debug(f"Error storing job: {e}")

        return scored_jobs

    def display_results(self, jobs):
        """Display job results."""
        print("\n" + "="*70)
        print("TOP MATCHING JOBS")
        print("="*70)

        if not jobs:
            print("\n[-] No matching jobs found")
            return

        for i, job in enumerate(jobs[:15], 1):
            print(f"\n{i}. {job['title']}")
            print(f"   Company: {job['company']}")
            print(f"   Relevance: {job.get('relevance_score', 0)}%")
            print(f"   Source: {job.get('source', 'Unknown')}")
            if job.get('salary'):
                print(f"   Salary: {job['salary']}")
            if job.get('location'):
                print(f"   Location: {job['location']}")
            if job.get('link'):
                print(f"   Apply: {job['link'][:70]}...")


def main():
    """Run combined job search."""
    try:
        searcher = CombinedJobSearcher()
        jobs = searcher.search_all_sources()
        searcher.display_results(jobs)

        print("\n" + "="*70)
        print(f"Summary: Found {len(jobs)} relevant remote jobs")
        print("="*70)
        print("\nTo generate cover letters for these jobs:")
        print("  python run.py")

        return 0

    except Exception as e:
        logger.error(f"Error in combined search: {e}")
        print(f"\n[-] Error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
