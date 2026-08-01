"""
Multi-source job search for remote positions.
Uses working APIs and web scraping methods.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime
from config import Config
from database import Database
from job_filter import JobFilter
from utils import setup_logging
import time
import json

logger = setup_logging('job_search')

class JobSearcher:
    """Search for remote jobs across multiple platforms."""

    def __init__(self):
        self.db = Database()
        self.filter = JobFilter()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def search_remoteok_website(self):
        """Scrape RemoteOK website directly."""
        jobs = []
        url = "https://remoteok.io/"

        try:
            resp = self.session.get(url, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Find job listings
            job_rows = soup.find_all('tr', class_='job')

            for row in job_rows[:50]:  # Limit to first 50
                try:
                    # Extract job info
                    title_elem = row.find('a', class_='job-link')
                    company_elem = row.find('span', class_='company')

                    if title_elem and company_elem:
                        job = {
                            'title': title_elem.get_text(strip=True),
                            'company': company_elem.get_text(strip=True),
                            'description': title_elem.get_text(strip=True),
                            'link': title_elem.get('href', ''),
                            'source': 'RemoteOK',
                            'extracted_date': datetime.now().isoformat()
                        }
                        jobs.append(job)
                except Exception as e:
                    logger.debug(f"Error parsing RemoteOK job: {e}")

        except Exception as e:
            logger.error(f"Error searching RemoteOK: {e}")

        logger.info(f"[+] Found {len(jobs)} jobs on RemoteOK website")
        return jobs

    def search_hacker_news(self):
        """Search HackerNews 'Who is Hiring' threads."""
        jobs = []
        url = "https://news.ycombinator.com/jobs"

        try:
            resp = self.session.get(url, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Find job listings
            job_rows = soup.find_all('a', class_='titlelink')

            for row in job_rows[:30]:  # Limit to first 30
                try:
                    title = row.get_text(strip=True)
                    link = row.get('href', '')

                    if title and link:
                        job = {
                            'title': title,
                            'company': 'HackerNews',
                            'description': title,
                            'link': link,
                            'source': 'HackerNews',
                            'extracted_date': datetime.now().isoformat()
                        }
                        jobs.append(job)
                except Exception as e:
                    logger.debug(f"Error parsing HN job: {e}")

        except Exception as e:
            logger.error(f"Error searching HackerNews: {e}")

        logger.info(f"[+] Found {len(jobs)} jobs on HackerNews")
        return jobs

    def search_github_jobs(self):
        """Search GitHub Jobs API."""
        jobs = []
        url = "https://api.github.com/search/repositories"

        try:
            params = {
                'q': 'remote hiring python javascript',
                'sort': 'stars',
                'order': 'desc',
                'per_page': 20
            }

            resp = self.session.get(url, params=params, timeout=10)
            data = resp.json()

            for item in data.get('items', []):
                job = {
                    'title': item.get('name', ''),
                    'company': item.get('owner', {}).get('login', ''),
                    'description': item.get('description', ''),
                    'link': item.get('html_url', ''),
                    'source': 'GitHub',
                    'extracted_date': datetime.now().isoformat()
                }
                jobs.append(job)

        except Exception as e:
            logger.error(f"Error searching GitHub: {e}")

        logger.info(f"[+] Found {len(jobs)} repositories on GitHub")
        return jobs

    def search_stackoverflow_rss(self):
        """Search Stack Overflow jobs via RSS feed."""
        jobs = []
        url = "https://stackoverflow.com/jobs/feed"

        try:
            resp = self.session.get(url, timeout=10)
            soup = BeautifulSoup(resp.text, 'xml')

            items = soup.find_all('item')

            for item in items[:20]:
                try:
                    title = item.find('title').get_text(strip=True) if item.find('title') else ''
                    description = item.find('description').get_text(strip=True) if item.find('description') else ''
                    link = item.find('link').get_text(strip=True) if item.find('link') else ''

                    if title:
                        job = {
                            'title': title,
                            'company': 'Stack Overflow',
                            'description': description[:500],
                            'link': link,
                            'source': 'Stack Overflow',
                            'extracted_date': datetime.now().isoformat()
                        }
                        jobs.append(job)
                except Exception as e:
                    logger.debug(f"Error parsing SO job: {e}")

        except Exception as e:
            logger.error(f"Error searching Stack Overflow: {e}")

        logger.info(f"[+] Found {len(jobs)} jobs on Stack Overflow")
        return jobs

    def search_all(self):
        """Search all sources and rank by relevance."""
        logger.info("Starting multi-source job search...")
        all_jobs = []

        # Search each source
        all_jobs.extend(self.search_remoteok_website())
        all_jobs.extend(self.search_hacker_news())
        all_jobs.extend(self.search_github_jobs())
        all_jobs.extend(self.search_stackoverflow_rss())

        logger.info(f"Total jobs found: {len(all_jobs)}")

        # Remove duplicates
        seen = set()
        unique_jobs = []
        for job in all_jobs:
            title_company = (job.get('title', ''), job.get('company', ''))
            if title_company not in seen:
                seen.add(title_company)
                unique_jobs.append(job)

        # Filter and rank by relevance
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
    searcher = JobSearcher()
    jobs = searcher.search_all()

    print(f"\n[+] Found {len(jobs)} relevant remote jobs:")
    for job in jobs[:10]:
        print(f"\n{job['title']} @ {job['company']}")
        print(f"   Score: {job['relevance_score']}% | Source: {job['source']}")
        print(f"   {job['description'][:100]}...")
