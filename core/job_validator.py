"""
Validate that jobs are still active.
Checks links and filters out dead/old job postings.
"""

import requests
from datetime import datetime, timedelta
from core.http_client import build_session
from core.utils import setup_logging

logger = setup_logging('job_validator')

class JobValidator:
    """Validate job postings are still active."""

    def __init__(self):
        # Retrying session: a job board that rate limits the check would
        # otherwise have its listing marked dead, which is the failure mode
        # this class already caused once.
        self.session = build_session()

    # Phrases that appear on a job board page when a listing is expired.
    #
    # Every entry must be specific enough that it cannot occur on a live
    # listing. The list once contained '404', 'page not found' and
    # 'does not exist'. Those are matched as bare substrings against the
    # entire lowercased page body, and live pages are full of them: build
    # hashes such as app.404abc12.css, inline error handlers, analytics
    # payloads. That single detail discarded roughly 86 percent of valid
    # listings, silently, because each rejection was logged at DEBUG.
    #
    # Do not add a short or generic phrase here. See tests/test_job_validator.py.
    EXPIRED_PHRASES = [
        'no longer available', 'job is no longer', 'position has been filled',
        'this job has expired', 'listing has expired', 'job has been filled',
        'job has closed', 'this position is closed', 'posting has been removed',
        'job is closed', 'no longer accepting', 'position is no longer',
        'this job is closed', 'application closed', 'vacancy closed',
        'role has been filled', 'position filled', 'job removed',
        'sorry, this job', 'this job posting has been removed',
    ]

    def check_link_active(self, url, timeout=8):
        """
        Check if a job link still has an active listing.
        Uses HEAD first (fast), then GET + content check if HEAD succeeds
        (catches job boards that return 200 on expired listings).
        Returns True if active, False if dead/expired.
        """
        if not url:
            return False

        try:
            # Step 1: HEAD request — catch hard 404/410 quickly
            response = self.session.head(url, timeout=timeout, allow_redirects=True)
            if response.status_code in [404, 410]:
                logger.debug(f"Dead link ({response.status_code}): {url}")
                return False
            if response.status_code >= 400:
                try:
                    response = self.session.get(url, timeout=timeout)
                    if response.status_code >= 400:
                        return False
                except Exception:
                    return False

            # Step 2: GET + content check — catch "expired" pages that still return 200
            try:
                get_resp = self.session.get(url, timeout=timeout)
                body = get_resp.text.lower()
                for phrase in self.EXPIRED_PHRASES:
                    if phrase in body:
                        logger.debug(f"Expired listing ({phrase!r}): {url}")
                        return False
            except Exception:
                pass  # If GET fails but HEAD was ok, assume active

            return True

        except requests.exceptions.Timeout:
            logger.debug(f"Link timeout: {url}")
            return False
        except requests.exceptions.RequestException as e:
            logger.debug(f"Error checking link: {e}")
            return False

    def is_recent_posting(self, job_data, days=14):
        """
        Check if job was posted recently (within N days).
        Returns True if recent, False if old.
        """
        # Check for date_posted field (from JSearch API)
        date_posted = job_data.get('date_posted')
        if date_posted:
            try:
                # Try to parse ISO format date
                posted_date = datetime.fromisoformat(date_posted.replace('Z', '+00:00'))
                cutoff_date = datetime.now(posted_date.tzinfo) - timedelta(days=days)
                is_recent = posted_date >= cutoff_date
                return is_recent
            except:
                pass

        # Check extracted_date as fallback
        extracted_date = job_data.get('extracted_date')
        if extracted_date:
            try:
                extracted = datetime.fromisoformat(extracted_date)
                cutoff = datetime.now() - timedelta(days=days)
                return extracted >= cutoff
            except:
                pass

        # If no date info, assume it's ok
        return True

    def validate_jobs(self, jobs, check_links=True, max_age_days=14, max_jobs_to_check=50):
        """
        Validate a list of jobs.

        Args:
            jobs: List of job dicts
            check_links: Whether to check if links are active (slower)
            max_age_days: Filter out jobs older than this
            max_jobs_to_check: Limit link checks to N jobs (to avoid slowdown)

        Returns:
            Filtered list of valid jobs
        """
        valid_jobs = []
        checked_count = 0

        for job in jobs:
            # Check recency first (fast)
            if not self.is_recent_posting(job, days=max_age_days):
                logger.debug(f"Skipping old job: {job.get('title')} @ {job.get('company')}")
                continue

            # Check link validity (slow, limit to N jobs)
            if check_links and checked_count < max_jobs_to_check:
                if job.get('link'):
                    if self.check_link_active(job['link']):
                        valid_jobs.append(job)
                        checked_count += 1
                    else:
                        logger.debug(f"Skipping dead link: {job.get('title')}")
                else:
                    # No link, skip it
                    continue
            elif check_links:
                # Already checked max jobs — pass remaining through unverified
                logger.debug(f"Skipped link check (already validated {checked_count} jobs) — passing through")
                valid_jobs.append(job)
            else:
                # Not checking links, just add it
                valid_jobs.append(job)

        logger.info(f"Validated {len(valid_jobs)} active jobs from {len(jobs)} total")
        return valid_jobs


if __name__ == '__main__':
    validator = JobValidator()

    # Test with sample jobs
    test_jobs = [
        {
            'title': 'Software Engineer',
            'company': 'Tech Corp',
            'link': 'https://www.linkedin.com/jobs/view/1234567890',
            'extracted_date': datetime.now().isoformat()
        },
        {
            'title': 'Old Job',
            'company': 'Dead Corp',
            'link': 'https://example.com/dead-job',
            'extracted_date': (datetime.now() - timedelta(days=30)).isoformat()
        }
    ]

    valid = validator.validate_jobs(test_jobs, check_links=True)
    print(f"Valid jobs: {len(valid)}")
    for job in valid:
        print(f"  - {job['title']} @ {job['company']}")
