#!/usr/bin/env python3
"""
Integrate multiple job board connectors:
- Apollo (B2B leads + jobs)
- LinkedIn Jobs
- Indeed
- GitHub Jobs
"""

import requests
from config import Config
from utils import setup_logging

logger = setup_logging('job_connectors')

class ApolloJobConnector:
    """Search Apollo for job listings."""

    def __init__(self):
        self.api_key = Config.APOLLO_API_KEY if hasattr(Config, 'APOLLO_API_KEY') else None

    def search(self, query, limit=10):
        """Search Apollo for jobs."""
        if not self.api_key:
            logger.warning("[!] Apollo API key not configured")
            return []

        try:
            url = "https://api.apollo.io/v1/opportunities"
            headers = {"X-Api-Key": self.api_key}
            params = {
                "q": f"{query} remote job",
                "limit": limit,
                "filters": {"job_title": query}
            }

            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                jobs = []
                for opp in response.json().get('opportunities', []):
                    jobs.append({
                        'title': opp.get('title', query),
                        'company': opp.get('company', 'Unknown'),
                        'link': opp.get('source_url', ''),
                        'description': opp.get('description', ''),
                        'source': 'Apollo'
                    })
                logger.info(f"[+] Found {len(jobs)} jobs from Apollo")
                return jobs
        except Exception as e:
            logger.warning(f"[!] Apollo search failed: {e}")

        return []


class LinkedInJobConnector:
    """Search LinkedIn for jobs (if API available)."""

    def __init__(self):
        self.api_key = Config.LINKEDIN_API_KEY if hasattr(Config, 'LINKEDIN_API_KEY') else None

    def search(self, query, limit=10):
        """Search LinkedIn Jobs."""
        if not self.api_key:
            logger.warning("[!] LinkedIn API key not configured")
            return []

        try:
            url = "https://www.linkedin.com/jobs/search"
            params = {
                "keywords": f"{query} remote",
                "location": "Anywhere",
                "pageNum": 0,
                "sortBy": "DD"  # Date descending
            }

            # LinkedIn requires session/auth, fallback to scraping via RapidAPI
            url = "https://linkedin-jobs-api.p.rapidapi.com/search"
            headers = {
                "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
                "X-RapidAPI-Host": "linkedin-jobs-api.p.rapidapi.com"
            }
            params = {
                "keywords": f"{query} remote",
                "location": "Anywhere"
            }

            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                jobs = []
                for job in response.json().get('jobs', [])[:limit]:
                    jobs.append({
                        'title': job.get('title', query),
                        'company': job.get('company', 'Unknown'),
                        'link': job.get('link', ''),
                        'description': job.get('description', ''),
                        'source': 'LinkedIn'
                    })
                logger.info(f"[+] Found {len(jobs)} jobs from LinkedIn")
                return jobs
        except Exception as e:
            logger.warning(f"[!] LinkedIn search failed: {e}")

        return []


class IndeedJobConnector:
    """Search Indeed for jobs."""

    def search(self, query, limit=10):
        """Search Indeed Jobs via RapidAPI."""
        try:
            url = "https://indeed12.p.rapidapi.com/jobs/search"
            headers = {
                "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
                "X-RapidAPI-Host": "indeed12.p.rapidapi.com"
            }
            params = {
                "query": f"{query} remote",
                "location": "Anywhere",
                "count": limit
            }

            response = requests.get(url, headers=headers, params=params, timeout=10)
            if response.status_code == 200:
                jobs = []
                for job in response.json().get('jobs', [])[:limit]:
                    jobs.append({
                        'title': job.get('job_title', query),
                        'company': job.get('company_name', 'Unknown'),
                        'link': job.get('job_apply_link', ''),
                        'description': job.get('job_description', ''),
                        'salary': job.get('salary', None),
                        'source': 'Indeed'
                    })
                logger.info(f"[+] Found {len(jobs)} jobs from Indeed")
                return jobs
        except Exception as e:
            logger.warning(f"[!] Indeed search failed: {e}")

        return []


class GitHubJobsConnector:
    """Search GitHub Jobs."""

    def search(self, query, limit=10):
        """Search GitHub Jobs API."""
        try:
            url = "https://jobs.github.com/positions.json"
            params = {
                "description": f"{query} remote",
                "location": "remote",
                "page": 1
            }

            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                jobs = []
                for job in response.json()[:limit]:
                    jobs.append({
                        'title': job.get('title', query),
                        'company': job.get('company', 'Unknown'),
                        'link': job.get('url', ''),
                        'description': job.get('description', ''),
                        'source': 'GitHub Jobs'
                    })
                logger.info(f"[+] Found {len(jobs)} jobs from GitHub Jobs")
                return jobs
        except Exception as e:
            logger.warning(f"[!] GitHub Jobs search failed: {e}")

        return []


class MultiConnectorJobSearch:
    """Search multiple job sources in parallel."""

    def __init__(self):
        self.apollo = ApolloJobConnector()
        self.linkedin = LinkedInJobConnector()
        self.indeed = IndeedJobConnector()
        self.github = GitHubJobsConnector()

    def search_all(self, query, limit=5):
        """Search all connectors."""
        all_jobs = []

        logger.info(f"[*] Searching connectors for: {query}")

        # Try each connector
        all_jobs.extend(self.apollo.search(query, limit))
        all_jobs.extend(self.linkedin.search(query, limit))
        all_jobs.extend(self.indeed.search(query, limit))
        all_jobs.extend(self.github.search(query, limit))

        logger.info(f"[+] Total jobs from connectors: {len(all_jobs)}")
        return all_jobs
