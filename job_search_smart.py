"""
Smart job search using user's extracted skills.
Builds targeted search queries from your CV + LinkedIn profile.
Searches across all internet job boards via JSearch API.
"""

import json
from pathlib import Path
from sources.free_boards import FreeJobSearcher
from sources.jsearch import JSearchJobScraper
from sources.serpapi import SerpAPIJobSearcher
from sources.gmail_drafts import GmailDraftJobExtractor
from sources.duckduckgo import DDGJobSearcher
from sources.apify import ApifyJobSearcher
from sources.linkedin import LinkedInJobSearcher
from sources.ats import ATSJobSearcher, enrich_descriptions
from core.job_filter import JobFilter, title_prescreen
from core.job_validator import JobValidator
from core.job_normalize import canonical_url, dedup_key, find_near_duplicates
from core.database import Database
from core.config import Config
from core.utils import setup_logging, force_utf8_streams
from core import source_health
logger = setup_logging('job_search_smart')

# Minimum relevance score a job needs to reach the digest. At the capped
# denominator of 15 this is roughly two skill matches.
MIN_RELEVANCE_SCORE = 10

class SmartJobSearcher:
    """Intelligent job search based on user skills."""

    def __init__(self):
        self.free_search = FreeJobSearcher()
        self.jsearch = JSearchJobScraper()
        self.serpapi = SerpAPIJobSearcher()
        self.gmail = GmailDraftJobExtractor()
        self.ddg   = DDGJobSearcher()
        self.apify = ApifyJobSearcher()
        self.linkedin = LinkedInJobSearcher()
        self.ats = ATSJobSearcher()
        self.filter = JobFilter()
        self.validator = JobValidator()
        self.db = Database()
        self.skills_data = self.load_skills()

    def load_skills(self):
        """Load the user's skills from the master CV variants, the SAME source
        the scorer uses (core.cv_variants.load_variants).

        The old path re-extracted skills from PDF/DOCX files in resumes/ and
        cached them to keywords.json. The CV redesign moved skills into
        master-cv.yaml's variants: section and switched the scorer over, but not
        this query builder, so it kept refreshing an empty PDF cache and returned
        no skills. That silently emptied the search queries and starved every
        query-driven source (Adzuna, Jooble, Arbeitnow, Himalayas, JSearch,
        LinkedIn). Reading the variants directly keeps this in lockstep with the
        scorer and cannot drift out of sync again."""
        try:
            from core.cv_variants import load_variants
            data = load_variants(Config.MASTER_CV_PATH)
            if not data.get('cvs'):
                logger.warning(
                    f"[Skills] No variants found in {Config.MASTER_CV_PATH}; "
                    f"search queries will be empty. Check the variants: section.")
            return data
        except Exception as e:
            logger.error(f"Error loading skills from variants: {e}")
            return {}

    def extract_top_skills(self, limit=10):
        """Extract top skills from user profile."""
        all_skills = {}

        # Get skills from all CVs
        for cv_name, cv_data in self.skills_data.get('cvs', {}).items():
            if isinstance(cv_data, dict) and 'skills' in cv_data:
                for skill, years in cv_data['skills'].items():
                    if skill not in all_skills:
                        all_skills[skill] = years

        # Get skills from LinkedIn
        linkedin = self.skills_data.get('linkedin', {})
        if isinstance(linkedin, dict) and 'skills' in linkedin:
            for skill in linkedin['skills'].keys():
                if skill not in all_skills:
                    all_skills[skill] = 1

        # Sort by years of experience
        sorted_skills = sorted(all_skills.items(), key=lambda x: x[1] if isinstance(x[1], (int, float)) else 0, reverse=True)
        top_skills = [skill for skill, _ in sorted_skills[:limit]]

        logger.info(f"Top skills: {top_skills}")
        return top_skills

    def get_job_categories(self):
        """Get job categories from user profile."""
        categories = {
            'engineering': ['python', 'javascript', 'java', 'c++', 'golang', 'rust', 'engineer', 'developer', 'backend', 'frontend', 'fullstack'],
            'ai_ml': ['ai', 'machine learning', 'ml', 'deep learning', 'neural', 'tensorflow', 'pytorch', 'nlp', 'computer vision'],
            'content_strategy': ['content', 'strategy', 'writing', 'documentation', 'technical writing', 'marketing', 'seo'],
            'architecture': ['architect', 'solutions', 'system design', 'infrastructure', 'cloud'],
            'data': ['data', 'analytics', 'sql', 'database', 'warehouse', 'bigquery', 'spark'],
            'devops': ['devops', 'kubernetes', 'docker', 'aws', 'gcp', 'azure', 'ci/cd', 'infrastructure']
        }

        # Get user's key domains from CV
        key_domains = []
        for cv_data in self.skills_data.get('cvs', {}).values():
            if isinstance(cv_data, dict) and 'key_domains' in cv_data:
                key_domains.extend(cv_data['key_domains'])

        logger.info(f"Key domains from CV: {key_domains}")
        return categories, key_domains

    def build_search_queries(self):
        """Build search queries directly from extracted CV skills."""
        top_skills = self.extract_top_skills(200)  # pull all skills, no arbitrary cap
        _, key_domains = self.get_job_categories()

        queries = set()

        # 1. Skill-based queries, filter noise aggressively, generate up to 20 queries
        noise_exact = {
            # languages / nationalities
            'english', 'german', 'serbian', 'native', 'fluent', 'basic',
            # generic soft skills that never appear in job search
            'analytical skills', 'communication', 'teamwork', 'problem solving',
            'critical thinking', 'time management', 'attention to detail',
            'organizational skills', 'interpersonal skills', 'adaptability',
            # overly broad academic / degree terms
            'mechanical engineering', 'oil and gas engineering', 'energy engineering',
            'process engineering', 'oil and gas', 'oil and gas systems',
            # single-letter or abbreviations too short to be useful
            'ms', 'iso', 'api', 'erp', 'crm', 'b2b', 'sop', 'kpi', 'roi',
            'ppt', 'cad', 'ppt', 'bom', 'btu', 'fea', 'p&id',
            # already covered by combo queries below, skip to avoid duplicates
            'sales', 'sales engineering', 'sales development',
        }
        noise_partial = [
            # partial-match blocklist, skip if skill contains any of these
            'microsoft', 'office suite', 'ms word', 'ms excel', 'ms powerpoint',
        ]
        skill_queries = 0
        for skill in top_skills:
            if skill_queries >= 20:
                break
            skill_clean = skill.strip().lower()
            if len(skill_clean) <= 3:
                continue
            if skill_clean in noise_exact:
                continue
            if any(n in skill_clean for n in noise_partial):
                continue
            queries.add(f"remote {skill_clean}")
            skill_queries += 1

        # 2. Specific skill-combo queries based on CV tracks
        # These target your unique intersection of skills, much more precise than generic terms
        # Check top skills for engineering/content tracks, but also scan ALL CV skills so
        # newer CVs (e.g. AppointmentSetter) with lower experience years aren't silently ignored.
        skill_lower = [s.lower() for s in top_skills]
        all_cv_skills = [
            s.lower()
            for cv_data in self.skills_data.get('cvs', {}).values()
            if isinstance(cv_data, dict)
            for s in cv_data.get('skills', {}).keys()
        ]

        has_cfd      = any('cfd' in s or 'fluent' in s or 'openfoam' in s or 'star-ccm' in s for s in skill_lower)
        has_valve    = any('valve' in s or 'fluid' in s or 'process' in s for s in skill_lower)
        has_content  = any('content' in s or 'seo' in s or 'writing' in s for s in skill_lower)
        has_ai       = any('ai' in s or 'automation' in s or 'claude' in s or 'chatgpt' in s for s in skill_lower)
        has_sales    = any('sales' in s for s in skill_lower)
        has_domain   = any('engineering' in d.lower() or 'mechanical' in d.lower() for d in key_domains)
        # Outbound track, check all CV skills so AppointmentSetter CV is always detected
        has_outbound = any(
            'outbound' in s or 'prospecting' in s or 'hubspot' in s
            or 'appointment' in s or 'lead qualif' in s or 'cold outreach' in s
            or 'sales development' in s or 'bant' in s
            for s in all_cv_skills
        )

        if has_cfd or has_domain:
            queries.add("remote CFD engineer")
            queries.add("remote aerodynamics engineer")

        if has_valve or has_domain:
            queries.add("remote valve engineer")
            queries.add("remote fluid systems engineer")
            queries.add("remote process engineer")
            queries.add("remote application engineer")
            queries.add("remote commissioning engineer")
            queries.add("remote field application engineer")

        if has_content and has_ai:
            queries.add("remote AI content strategist")
            queries.add("remote technical content AI")

        if has_content:
            queries.add("remote technical content strategist")
            queries.add("remote B2B technical writer")

        if has_sales and (has_valve or has_domain):
            queries.add("remote technical sales engineer")
            queries.add("remote sales engineer fluid systems")
            queries.add("remote solutions engineer industrial")
            queries.add("remote pre-sales engineer")

        # AI-enabled roles are wanted. These used to be gated behind content to
        # avoid pulling pure programming; the is_pure_programming filter now
        # drops non-AI dev roles downstream, so the queries can run freely.
        if has_ai:
            queries.add("remote AI automation engineer")
            queries.add("remote AI implementation specialist")
            queries.add("remote automation specialist")
            queries.add("remote AI operations specialist")
            if has_content:
                queries.add("remote AI content operations")
            if has_sales:
                queries.add("remote AI solutions engineer")

        if has_outbound or has_sales:
            queries.add("remote appointment setter")
            queries.add("remote sales development representative")
            queries.add("remote SDR outbound B2B")
            queries.add("remote inside sales representative")
            queries.add("remote lead generation specialist")
            queries.add("remote business development representative")

        # 3. Sort by specificity (longer = more specific) so Apify gets best 3
        queries = sorted(queries, key=lambda q: len(q), reverse=True)

        logger.info(f"Built {len(queries)} search queries:")
        for q in queries:
            logger.info(f"  - {q}")

        return queries

    def search_all_sources(self):
        """Search with intelligent queries across all sources."""
        logger.info("="*70)
        logger.info("SMART JOB SEARCH (Based on Your Skills)")
        logger.info("="*70)

        # Clean up old jobs from database (older than 7 days) for fresh results
        print("\n[*] Cleaning up old jobs from database for fresh daily results...")
        try:
            self.db.cleanup_old_entries(days=7)
            print("[+] Database cleaned - only keeping jobs from last 7 days")
        except Exception as e:
            logger.warning(f"Could not cleanup database: {e}")

        # Build intelligent queries, sorted by specificity (longest first)
        queries = self.build_search_queries()
        top_keywords = self.extract_top_skills(limit=15)

        # Split queries by specificity tier so each source gets the queries it's best at.
        # Specific (multi-word, skill-named) → paid/premium sources with full descriptions.
        # Broad (short, role-based) → free sources that filter client-side anyway.
        specific_queries = [q for q in queries if len(q.split()) >= 3]   # e.g. "remote CFD engineer"
        broad_queries    = [q for q in queries if len(q.split()) < 3]    # e.g. "remote engineer"

        # Ensure at least some queries in each tier
        if not specific_queries:
            specific_queries = queries[:3]
        if not broad_queries:
            broad_queries = queries[3:] or queries

        logger.info(f"Specific queries ({len(specific_queries)}): {specific_queries}")
        logger.info(f"Broad queries ({len(broad_queries)}): {broad_queries}")

        # Every source runs inside source_health.track, which times it,
        # captures any exception and records the outcome. One dead source
        # cannot end the run, and no source can fail without being reported.
        source_health.init_tables()
        results = []

        # 1. Company careers pages, read from their ATS. No key, no quota,
        #    full descriptions, and the company is one you chose. Highest
        #    signal source here, so it runs first.
        print(f"\n[*] Checking company careers pages (Greenhouse, Lever, Ashby)...")
        with source_health.track('ATS boards') as r:
            r.jobs = self.ats.search_all()
        results.append(r)

        # 2. JSearch, specific queries only, paid, full descriptions
        print(f"\n[*] Trying JSearch ({len(specific_queries)} specific queries)...")
        with source_health.track('JSearch', queries=len(specific_queries)) as r:
            for query in specific_queries:
                r.jobs.extend(self.jsearch.search_jobs(query, num_pages=2))
        results.append(r)

        # 3. Free aggregators, all queries, no quota
        print(f"\n[*] Searching free sources...")
        with source_health.track('Free aggregators', queries=len(queries)) as r:
            r.jobs = self.free_search.search_all(queries=queries, keywords=top_keywords)
        results.append(r)

        # 4. LinkedIn via the public guest endpoint. Replaced the RSS
        #    connector, which returns a login page rather than XML.
        print(f"\n[*] Searching LinkedIn...")
        with source_health.track('LinkedIn', queries=len(specific_queries[:5])) as r:
            r.jobs = self.linkedin.search_all(specific_queries[:5], pages=2)
        results.append(r)

        # 5. SerpAPI, Google Jobs. Needs SERPAPI_KEY, skipped without one.
        print(f"\n[*] Searching Google Jobs via SerpAPI...")
        with source_health.track('SerpAPI', queries=len(specific_queries[:5])) as r:
            r.jobs = self.serpapi.search_all(specific_queries[:5])
        results.append(r)

        # 6. Apify, LinkedIn and Indeed with full descriptions, metered
        print(f"\n[*] Searching via Apify...")
        apify_queries = specific_queries[:3]
        with source_health.track('Apify', queries=len(apify_queries)) as r:
            r.jobs = self.apify.search_all(apify_queries)
        results.append(r)

        # 7. DuckDuckGo, scrapes public job board pages, throttled
        print(f"\n[*] Searching via DuckDuckGo...")
        with source_health.track('DuckDuckGo', queries=len(broad_queries[:10])) as r:
            r.jobs = self.ddg.search_all(broad_queries[:10])
        results.append(r)

        all_jobs = []
        for r in results:
            all_jobs.extend(r.jobs)

        # Extract from Gmail drafts
        print("\n[*] Checking Gmail drafts for manually saved jobs...")
        try:
            draft_jobs = self.gmail.process_draft_jobs()
            print(f"[+] Gmail drafts found {len(draft_jobs)} jobs")
            all_jobs.extend(draft_jobs)
        except Exception as e:
            logger.warning(f"Could not access Gmail: {e}")

        # Deduplicate. Three layers, all comparing normalised values, because
        # the same posting arrives from several sources with different
        # tracking URLs and decorated titles.
        #
        #   1. canonical link already seen in an earlier run
        #   2. dedup key already seen in this run
        #   3. near-duplicate title at the same company
        seen_keys = set()
        unique_jobs = []
        skipped_old_listings = 0
        skipped_same_run = 0

        for job in all_jobs:
            link = canonical_url(job.get('link', ''))
            if link:
                job['link'] = link

            if link and self.db.is_job_link_seen(link):
                skipped_old_listings += 1
                continue

            key = dedup_key(job.get('title', ''), job.get('company', ''))
            if key in seen_keys:
                skipped_same_run += 1
                continue

            seen_keys.add(key)
            unique_jobs.append(job)

        near_dupes = find_near_duplicates(unique_jobs)
        if near_dupes:
            unique_jobs = [j for i, j in enumerate(unique_jobs) if i not in near_dupes]

        print(
            f"\n[*] {len(unique_jobs)} unique jobs from {len(all_jobs)} raw "
            f"(skipped {skipped_old_listings} seen before, "
            f"{skipped_same_run} duplicates this run, "
            f"{len(near_dupes)} near duplicates)"
        )

        # Validate jobs (check links are active, not too old)
        print(f"[*] Validating job links (checking if still active)...")
        valid_jobs = self.validator.validate_jobs(unique_jobs, check_links=False, max_age_days=14, max_jobs_to_check=30)
        print(f"[+] {len(valid_jobs)} jobs are still active")

        # Some sources return titles with no description. Scoring those
        # against a CV measures how much text the source happened to return,
        # not how well the job fits, so a Flowserve Sales Engineer scores
        # below a keyword-stuffed listing from an aggregator. Fetch the real
        # descriptions before scoring, but only for titles that survive a
        # cheap screen, so this costs a handful of requests rather than one
        # per job on the board.
        cv_skills = self.filter.all_skills
        enriched, over_budget = enrich_descriptions(
            valid_jobs,
            should_fetch=lambda job: title_prescreen(job.get('title', ''), cv_skills),
        )
        if enriched or over_budget:
            print(f"[*] Fetched {enriched} missing job descriptions"
                  + (f", {over_budget} skipped over budget" if over_budget else ""))

        # Score and filter. JobFilter.filter_jobs owns every rejection rule:
        # dealbreaker keywords, geo restriction, non-English titles and the
        # score cutoff. It logs a breakdown of what it rejected and why.
        print(f"[*] Ranking jobs by relevance to your profile...")
        scored_jobs = self.filter.filter_jobs(valid_jobs, min_score=MIN_RELEVANCE_SCORE)

        print(f"[+] Found {len(scored_jobs)} relevant jobs")

        # Store in database
        for job in scored_jobs:
            try:
                job_id = self.db.add_job(
                    job_title=job.get('title'),
                    company=job.get('company'),
                    description=job.get('description'),
                    link=job.get('link'),
                    salary=job.get('salary'),
                    source=job.get('source'),
                    relevance_score=job.get('relevance_score', 0),
                    best_cv=job.get('best_cv'),
                    scam_risk=job.get('scam_risk', False),
                    location=job.get('location'),
                )
                # Mark link as seen so we don't re-process old re-listings
                if job.get('link'):
                    self.db.mark_job_link_seen(job.get('link'), job_id)
            except Exception as e:
                logger.debug(f"Error storing job: {e}")

        # Per source accounting. Printed and stored, so a source that dies
        # quietly shows up as a number rather than as a smaller digest.
        table = source_health.summary_table(results)
        print("\n" + table)
        logger.info("Source summary\n" + table)

        try:
            # Recovery is checked before this run is recorded, so it compares
            # against the previous runs only.
            recovered = source_health.recovered_sources(results)
            source_health.record(results)
        except Exception as e:
            logger.warning(f"Could not record source health: {type(e).__name__}: {e}")
            recovered = []

        for name in recovered:
            message = f"RECOVERED  {name} is producing again after being stale"
            print(message)
            logger.info(message)

        # Stale sources are still called on every run. Free monthly tiers
        # reset and scrapers stop being rate limited, and a source that was
        # skipped could never be seen coming back.
        for name, runs, last_error in source_health.stale_sources():
            quiet_days = source_health.days_since_last_result(name)
            message = f"STALE  {name} returned nothing for {runs} consecutive runs"
            if quiet_days is not None:
                message += f", last produced {quiet_days} days ago"
            else:
                message += ", has never produced anything"
            if last_error:
                message += f". Last error: {last_error[:80]}"
            message += ". Still being called every run."
            print(message)
            logger.warning(message)

        return scored_jobs

    def display_results(self, jobs):
        """Display results."""
        print("\n" + "="*70)
        print("TOP MATCHING JOBS FOR YOU")
        print("="*70)

        if not jobs:
            print("\n[-] No matching jobs found")
            return

        for i, job in enumerate(jobs[:20], 1):
            print(f"\n{i}. {job['title']}")
            print(f"   Company: {job['company']}")
            print(f"   Match Score: {job.get('relevance_score', 0)}%")
            print(f"   Source: {job.get('source', 'Unknown')}")
            if job.get('salary'):
                print(f"   Salary: {job['salary']}")
            if job.get('location'):
                print(f"   Location: {job['location']}")
            if job.get('link'):
                print(f"   Link: {job['link'][:70]}...")


def main():
    """Run smart job search."""
    force_utf8_streams()  # Serbian job titles must not crash a cp1252 console/log
    try:
        searcher = SmartJobSearcher()
        jobs = searcher.search_all_sources()
        searcher.display_results(jobs)

        print("\n" + "="*70)
        print(f"Summary: Found {len(jobs)} relevant jobs matching your profile")
        print("="*70)
        print("\nNext: Generate cover letters for top matches:")
        print("  python write_cover_letters.py")

        return 0

    except Exception as e:
        logger.error(f"Error: {e}")
        print(f"\n[-] Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    import sys
    sys.exit(main())
