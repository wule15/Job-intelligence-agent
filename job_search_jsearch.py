"""
Job search using JSearch API (RapidAPI).
Searches real jobs from LinkedIn, Indeed, Glassdoor, etc.
"""

import requests
from datetime import datetime
from core.config import Config
from core.database import Database
from core.job_filter import JobFilter
from core.utils import setup_logging
import os

logger = setup_logging('job_search_jsearch')

class JSearchJobScraper:
    """Search jobs using JSearch API."""

    def __init__(self):
        self.db = Database()
        self.filter = JobFilter()

        # Get API key from environment
        self.api_key = os.getenv('JSEARCH_API_KEY')
        if not self.api_key:
            logger.warning("JSEARCH_API_KEY not found in .env file")

        self.api_host = "jsearch.p.rapidapi.com"
        self.api_url = "https://jsearch.p.rapidapi.com/search"

        # Set once the API reports the monthly quota is gone. Every later
        # call returns immediately instead of spending another request to be
        # told the same thing.
        self.quota_exhausted = False

    def search_jobs(self, query="remote python", num_pages=2):
        """
        Search for jobs using JSearch API.

        Args:
            query: Search query (e.g., "remote python developer")
            num_pages: Number of pages to retrieve (each page ~10 jobs)

        Returns:
            List of job dicts
        """
        if not self.api_key:
            logger.error("JSEARCH_API_KEY not configured")
            return []

        if self.quota_exhausted:
            logger.debug(f"JSearch quota already exhausted, not calling for {query!r}")
            return []

        jobs = []

        try:
            for page in range(1, num_pages + 1):
                logger.info(f"[*] Searching JSearch page {page}...")

                headers = {
                    'X-RapidAPI-Key': self.api_key,
                    'X-RapidAPI-Host': self.api_host
                }

                params = {
                    'query': query,
                    'page': str(page),
                    'num_pages': '1',
                    'date_posted': 'week'  # Only jobs posted in last week
                }

                response = requests.get(self.api_url, headers=headers, params=params, timeout=10)

                if response.status_code == 429:
                    # Latch the flag before breaking. The old code raised here,
                    # but the raise was caught by this method's own except
                    # block, so the caller never learned and kept looping. One
                    # exhausted quota cost 34 further requests in a single run.
                    self.quota_exhausted = True
                    logger.error("JSearch quota exhausted (429), skipping all remaining queries")
                    logger.error(f"Response: {response.text[:200]}")
                    break
                if response.status_code != 200:
                    logger.error(f"JSearch API error: {response.status_code}")
                    logger.error(f"Response: {response.text[:200]}")
                    continue

                data = response.json()

                if 'data' not in data:
                    logger.warning(f"No jobs in response for page {page}")
                    continue

                for job_data in data['data']:
                    try:
                        job = {
                            'title': job_data.get('job_title', ''),
                            'company': job_data.get('employer_name', ''),
                            'description': job_data.get('job_description', ''),
                            'link': job_data.get('job_apply_link', ''),
                            'location': job_data.get('job_city', ''),
                            'salary_min': job_data.get('job_salary_min'),
                            'salary_max': job_data.get('job_salary_max'),
                            'salary_currency': job_data.get('job_salary_currency'),
                            'source': job_data.get('job_job_title', 'JSearch'),
                            'extracted_date': datetime.now().isoformat()
                        }

                        # Format salary
                        if job['salary_min'] and job['salary_max']:
                            job['salary'] = f"${job['salary_min']:,} - ${job['salary_max']:,}"
                        else:
                            job['salary'] = None

                        jobs.append(job)

                    except Exception as e:
                        logger.debug(f"Error parsing job: {e}")

        except Exception as e:
            logger.error(f"Error searching JSearch: {e}")

        logger.info(f"[+] Found {len(jobs)} jobs from JSearch")
        return jobs

    def search_all(self):
        """Search multiple job queries and rank by relevance."""
        logger.info("Starting JSearch job search...")
        all_jobs = []

        # Search multiple relevant queries
        queries = [
            "remote python",
            "remote javascript",
            "remote engineer",
            "remote developer",
            "remote technical"
        ]

        for query in queries:
            jobs = self.search_jobs(query, num_pages=1)
            all_jobs.extend(jobs)

            # Be respectful to API
            import time
            time.sleep(0.5)

        logger.info(f"Total jobs found: {len(all_jobs)}")

        # Remove duplicates
        seen = set()
        unique_jobs = []
        for job in all_jobs:
            title_company = (job.get('title', ''), job.get('company', ''))
            if title_company not in seen:
                seen.add(title_company)
                unique_jobs.append(job)

        # Filter and rank by relevance (min_score=1 to get more matches)
        filtered = self.filter.filter_jobs(unique_jobs, min_score=1, remote_only=True)
        logger.info(f"Relevant jobs after filtering: {len(filtered)}")

        # Store in database
        for job in filtered:
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

        return filtered


if __name__ == '__main__':
    scraper = JSearchJobScraper()
    jobs = scraper.search_all()

    print(f"\n[+] Found {len(jobs)} relevant remote jobs:\n")
    for job in jobs[:10]:
        print(f"{job['title']} @ {job['company']}")
        print(f"   Relevance: {job['relevance_score']}% | Salary: {job.get('salary', 'N/A')}")
        print(f"   Location: {job.get('location', 'Remote')}")
        print(f"   Link: {job['link'][:70]}...")
        print()
