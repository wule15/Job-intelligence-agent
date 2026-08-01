"""
LinkedIn job search using linkedin-api library.
Searches for remote job openings on LinkedIn.
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime
from config import Config
from utils import setup_logging
import time

logger = setup_logging('linkedin_jobs')

class LinkedInJobScraper:
    """Scrape job listings from LinkedIn."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def search_linkedin_jobs(self, keywords="remote python", location="", num_results=50):
        """
        Search LinkedIn jobs.

        Note: This uses LinkedIn's public search, not API (which requires authentication).
        """
        jobs = []

        # LinkedIn jobs search URL
        base_url = "https://www.linkedin.com/jobs/search/"

        params = {
            'keywords': keywords,
            'location': 'Remote',
            'sortBy': 'recent',
            'pageNum': 0
        }

        try:
            logger.info(f"Searching LinkedIn for: {keywords}")

            # Attempt to fetch jobs
            # Note: LinkedIn actively blocks scrapers, so this may fail
            resp = self.session.get(base_url, params=params, timeout=10)

            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')

                # Try to find job listings
                job_cards = soup.find_all('div', class_='base-card')

                logger.info(f"Found {len(job_cards)} job cards on LinkedIn")

                for card in job_cards[:num_results]:
                    try:
                        # Extract job info
                        title_elem = card.find('h3', class_='base-search-card__title')
                        company_elem = card.find('h4', class_='base-search-card__subtitle')
                        location_elem = card.find('span', class_='job-search-card__location')

                        if title_elem and company_elem:
                            job = {
                                'title': title_elem.get_text(strip=True),
                                'company': company_elem.get_text(strip=True),
                                'location': location_elem.get_text(strip=True) if location_elem else 'Remote',
                                'description': '',
                                'link': card.find('a').get('href', '') if card.find('a') else '',
                                'source': 'LinkedIn',
                                'extracted_date': datetime.now().isoformat()
                            }
                            jobs.append(job)
                    except Exception as e:
                        logger.debug(f"Error parsing LinkedIn job: {e}")
            else:
                logger.warning(f"LinkedIn returned status {resp.status_code} - may be blocking requests")

        except Exception as e:
            logger.error(f"Error searching LinkedIn: {e}")

        return jobs

    def search_linkedin_via_google(self, keywords="remote python"):
        """
        Alternative: Search LinkedIn jobs via Google (bypasses some blocks).
        """
        jobs = []

        try:
            # Search Google for LinkedIn job posts
            query = f'site:linkedin.com/jobs {keywords}'
            google_url = "https://www.google.com/search"

            params = {
                'q': query,
                'num': 30
            }

            resp = self.session.get(google_url, params=params, timeout=10)
            soup = BeautifulSoup(resp.text, 'html.parser')

            # Parse Google results for LinkedIn links
            links = soup.find_all('a')
            linkedin_links = [l.get('href', '') for l in links if 'linkedin.com/jobs' in str(l)]

            logger.info(f"Found {len(linkedin_links)} LinkedIn job links via Google")

        except Exception as e:
            logger.error(f"Error searching via Google: {e}")

        return jobs


def search_linkedin_json():
    """
    Alternative approach: Use LinkedIn's internal API endpoint.
    Returns JSON job data (if not blocked).
    """
    jobs = []

    try:
        # LinkedIn API endpoint (may require authentication or session)
        url = "https://www.linkedin.com/jobs/search/api/jobs"

        params = {
            'keywords': 'remote',
            'location': 'Remote',
            'limit': 50
        }

        # This typically requires valid LinkedIn session cookies
        # For now, it will likely fail without authentication
        logger.info("Attempting LinkedIn JSON API (may require authentication)")

    except Exception as e:
        logger.error(f"LinkedIn API error: {e}")

    return jobs


if __name__ == '__main__':
    scraper = LinkedInJobScraper()

    # Test search
    jobs = scraper.search_linkedin_jobs("remote python", num_results=20)

    print(f"\n[+] Found {len(jobs)} LinkedIn jobs:")
    for job in jobs[:5]:
        print(f"\n{job['title']} @ {job['company']}")
        print(f"   Location: {job.get('location', 'Remote')}")
        print(f"   Link: {job['link'][:80]}...")
