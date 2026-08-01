"""
Automated job search using Selenium browser automation.
Opens a real browser to scrape LinkedIn and other sites.
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from datetime import datetime
from config import Config
from database import Database
from job_filter import JobFilter
from utils import setup_logging
import time

logger = setup_logging('job_search_selenium')

class SeleniumJobScraper:
    """Use browser automation to scrape jobs."""

    def __init__(self):
        self.db = Database()
        self.filter = JobFilter()
        self.driver = None

    def init_browser(self):
        """Initialize Chrome browser with anti-detection measures."""
        chrome_options = Options()

        # Anti-detection settings
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)

        # Basic options
        chrome_options.add_argument('--start-maximized')
        chrome_options.add_argument('--disable-notifications')
        chrome_options.add_argument('--disable-popup-blocking')

        # User agent
        chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

        try:
            self.driver = webdriver.Chrome(options=chrome_options)
            logger.info("[+] Browser initialized")
            return True
        except Exception as e:
            logger.error(f"Error initializing browser: {e}")
            logger.info("    Make sure chromedriver is installed: https://chromedriver.chromium.org/")
            return False

    def close_browser(self):
        """Close the browser."""
        if self.driver:
            self.driver.quit()

    def search_linkedin_selenium(self, keywords="remote python"):
        """Search LinkedIn using Selenium."""
        jobs = []

        try:
            logger.info(f"Searching LinkedIn for: {keywords}")

            url = f"https://www.linkedin.com/jobs/search/?keywords={keywords}&location=Remote&sortBy=recent"
            self.driver.get(url)

            # Wait for jobs to load
            time.sleep(3)

            # Scroll to load more jobs
            for _ in range(3):
                self.driver.execute_script("window.scrollBy(0, 500)")
                time.sleep(1)

            # Find job listings
            job_cards = self.driver.find_elements(By.CLASS_NAME, "base-card")

            logger.info(f"Found {len(job_cards)} job cards")

            for card in job_cards[:30]:
                try:
                    # Extract job info
                    title = card.find_element(By.CLASS_NAME, "base-search-card__title").text
                    company = card.find_element(By.CLASS_NAME, "base-search-card__subtitle").text

                    try:
                        location = card.find_element(By.CLASS_NAME, "job-search-card__location").text
                    except:
                        location = "Remote"

                    try:
                        link = card.find_element(By.TAG_NAME, "a").get_attribute("href")
                    except:
                        link = ""

                    job = {
                        'title': title,
                        'company': company,
                        'location': location,
                        'description': f"{title} at {company}",
                        'link': link,
                        'source': 'LinkedIn',
                        'extracted_date': datetime.now().isoformat()
                    }
                    jobs.append(job)

                except Exception as e:
                    logger.debug(f"Error parsing job card: {e}")

        except Exception as e:
            logger.error(f"Error searching LinkedIn with Selenium: {e}")

        return jobs

    def search_all_selenium(self):
        """Search jobs using Selenium."""
        logger.info("Starting Selenium-based job search...")

        if not self.init_browser():
            return []

        all_jobs = []

        try:
            # Search LinkedIn
            all_jobs.extend(self.search_linkedin_selenium("remote python"))
            all_jobs.extend(self.search_linkedin_selenium("remote javascript"))
            all_jobs.extend(self.search_linkedin_selenium("remote engineer"))

            logger.info(f"Total jobs found: {len(all_jobs)}")

            # Remove duplicates
            seen = set()
            unique_jobs = []
            for job in all_jobs:
                title_company = (job.get('title', ''), job.get('company', ''))
                if title_company not in seen:
                    seen.add(title_company)
                    unique_jobs.append(job)

            # Filter and rank
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

        finally:
            self.close_browser()


if __name__ == '__main__':
    scraper = SeleniumJobScraper()
    jobs = scraper.search_all_selenium()

    print(f"\n[+] Found {len(jobs)} relevant jobs:")
    for job in jobs[:10]:
        print(f"\n{job['title']} @ {job['company']}")
        print(f"   Score: {job['relevance_score']}% | Location: {job.get('location', 'Remote')}")
