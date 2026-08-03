"""
Extract job listings from Gmail drafts.
User can save job opportunities in drafts, system processes them.
"""

import imaplib
import email
from email.header import decode_header
from datetime import datetime
from core.config import Config
from core.database import Database
from core.job_filter import JobFilter
from core.utils import setup_logging
import re

logger = setup_logging('job_search_gmail')

class GmailDraftJobExtractor:
    """Extract job listings from Gmail drafts."""

    def __init__(self):
        self.db = Database()
        self.filter = JobFilter()

    def connect_gmail(self):
        """Connect to Gmail IMAP."""
        try:
            mail = imaplib.IMAP4_SSL(Config.GMAIL_IMAP_HOST, Config.GMAIL_IMAP_PORT)
            mail.login(Config.GMAIL_USER, Config.GMAIL_APP_PASSWORD)
            logger.info(f"[+] Connected to Gmail as {Config.GMAIL_USER}")
            return mail
        except Exception as e:
            logger.error(f"Error connecting to Gmail: {e}")
            return None

    def parse_job_from_draft(self, email_body):
        """
        Parse job information from draft email.

        Expected format in draft:
        Job Title: [title]
        Company: [company]
        Description: [description]
        Link: [url]
        Salary: [salary]
        """
        job = {
            'title': '',
            'company': '',
            'description': '',
            'link': '',
            'salary': None,
            'source': 'Gmail Draft',
            'extracted_date': datetime.now().isoformat()
        }

        try:
            lines = email_body.split('\n')

            for line in lines:
                if line.startswith('Job Title:'):
                    job['title'] = line.replace('Job Title:', '').strip()
                elif line.startswith('Company:'):
                    job['company'] = line.replace('Company:', '').strip()
                elif line.startswith('Description:'):
                    job['description'] = line.replace('Description:', '').strip()
                elif line.startswith('Link:'):
                    job['link'] = line.replace('Link:', '').strip()
                elif line.startswith('Salary:'):
                    job['salary'] = line.replace('Salary:', '').strip()

            # If no structured format, try to extract from plain text
            if not job['title']:
                # Look for common patterns
                title_match = re.search(r'([A-Za-z\s]+(?:Developer|Engineer|Manager|Architect|Writer|Designer))', email_body)
                if title_match:
                    job['title'] = title_match.group(1).strip()

            if not job['company']:
                # Look for company patterns
                company_match = re.search(r'(?:@|at|company|Company)\s+([A-Za-z0-9\s&]+)', email_body)
                if company_match:
                    job['company'] = company_match.group(1).strip()

            # Use subject as fallback for title
            if not job['title']:
                job['title'] = 'Job Opportunity'

            if not job['company']:
                job['company'] = 'Unknown'

            return job if job['title'] and job['company'] else None

        except Exception as e:
            logger.error(f"Error parsing job from draft: {e}")
            return None

    def extract_jobs_from_drafts(self):
        """Extract all job listings from Gmail drafts."""
        jobs = []
        mail = self.connect_gmail()

        if not mail:
            return jobs

        try:
            # Select Drafts folder
            mail.select('[Gmail]/Drafts', readonly=True)
            logger.info("[*] Reading Gmail drafts...")

            # Search for emails with job-related keywords
            status, messages = mail.search(None, 'ALL')

            if status != 'OK':
                logger.warning("No drafts found")
                return jobs

            message_ids = messages[0].split()
            logger.info(f"[*] Found {len(message_ids)} drafts")

            for msg_id in message_ids:
                try:
                    status, msg_data = mail.fetch(msg_id, '(RFC822)')

                    if status != 'OK':
                        continue

                    msg = email.message_from_bytes(msg_data[0][1])

                    # Get subject and body
                    subject = msg.get('subject', 'No Subject')
                    if isinstance(subject, email.header.Header):
                        subject = decode_header(subject)[0][0]

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == 'text/plain':
                                body = part.get_payload(decode=True).decode()
                                break
                    else:
                        body = msg.get_payload(decode=True).decode()

                    # Check if this looks like a job posting
                    job_keywords = ['job', 'position', 'role', 'title', 'company', 'description', 'link', 'remote']
                    if any(keyword.lower() in (subject + body).lower() for keyword in job_keywords):
                        job = self.parse_job_from_draft(subject + '\n' + body)
                        if job:
                            jobs.append(job)
                            logger.info(f"[+] Extracted: {job['title']} @ {job['company']}")

                except Exception as e:
                    logger.debug(f"Error processing draft: {e}")

            mail.close()
            mail.logout()

        except Exception as e:
            logger.error(f"Error accessing Gmail drafts: {e}")

        logger.info(f"[+] Extracted {len(jobs)} jobs from Gmail drafts")
        return jobs

    def process_draft_jobs(self):
        """Extract, filter, and store draft jobs."""
        logger.info("Starting Gmail draft job extraction...")

        # Extract jobs from drafts
        jobs = self.extract_jobs_from_drafts()

        if not jobs:
            logger.warning("No jobs found in drafts")
            return []

        logger.info(f"Total jobs from drafts: {len(jobs)}")

        # Filter and rank
        filtered = self.filter.filter_jobs(jobs, min_score=1, remote_only=True)
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
    extractor = GmailDraftJobExtractor()
    jobs = extractor.process_draft_jobs()

    print(f"\n[+] Found {len(jobs)} relevant jobs from Gmail drafts:\n")
    for job in jobs:
        print(f"{job['title']} @ {job['company']}")
        print(f"   Relevance: {job['relevance_score']}%")
        if job.get('link'):
            print(f"   Link: {job['link'][:70]}...")
        print()
