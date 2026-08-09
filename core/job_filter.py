"""
Job filtering and relevance scoring based on extracted skills.
Ranks jobs by how well they match your CV skills.
"""

import json
import re
from pathlib import Path
from core.config import Config
from core.utils import setup_logging
from core.synonym_map import skill_matches
from core.cv_variants import load_variants

logger = setup_logging('job_filter')

# ── Negative keywords — jobs containing these are auto-rejected ──────────────
NEGATIVE_KEYWORDS = [
    # Clearance / citizenship
    'security clearance', 'clearance required', 'top secret', 'secret clearance',
    'ts/sci', 'must be a us citizen', 'us citizenship required',
    'active clearance', 'government clearance',
    # Experience overreach
    '15+ years', '15 years of experience', '20+ years', '20 years of experience',
    # Strict on-site (belt-and-suspenders alongside remote filter)
    'no remote', 'not a remote', 'on-site only', 'onsite only', 'must be onsite',
    'relocation required', 'must relocate',
]

# ── Geo-restriction filter ────────────────────────────────────────────────────
# Phrases that explicitly lock the job to a non-European geography
GEO_BLOCK_PHRASES = [
    # US work authorisation
    'must be authorized to work in the us',
    'must be authorized to work in the united states',
    'authorized to work in the united states',
    'legally authorized to work in the us',
    'legally authorized to work in the united states',
    'must be eligible to work in the us',
    'eligible to work in the united states',
    'us work authorization required',
    'work authorization required in the us',
    # Residence / location requirements
    'must reside in the us', 'must be located in the us',
    'must be based in the us', 'must live in the us',
    'must reside in the united states', 'must be located in the united states',
    'us residents only', 'united states only', 'us only',
    'us-based candidates only', 'us based candidates only',
    'candidates must be in the us', 'candidates must be in the united states',
    # Canada-only
    'must be authorized to work in canada', 'canada only', 'canadian residents only',
    # Australia-only
    'must be authorized to work in australia', 'australia only',
]

# Phrases that confirm European / worldwide eligibility — if present the job passes
GEO_ALLOW_PHRASES = [
    'europe', 'european', 'emea', 'worldwide', 'global', 'anywhere in the world',
    'open to candidates worldwide', 'international candidates',
    'bosnia', 'serbia', 'balkans', 'remote worldwide', 'fully remote worldwide',
    'work from anywhere',
]

def is_geo_restricted(job_title: str, job_description: str, location: str = '') -> bool:
    """
    Return True if the job appears geo-restricted to a non-European region.

    Logic:
      1. If any GEO_BLOCK_PHRASE is found → blocked, UNLESS a GEO_ALLOW_PHRASE
         is also present (e.g. "US preferred but open to Europe").
      2. If no block phrase is found → allowed (assume open unless stated otherwise).
    """
    text = (job_title + ' ' + job_description + ' ' + location).lower()

    blocked = any(phrase in text for phrase in GEO_BLOCK_PHRASES)
    if not blocked:
        return False  # No explicit block — pass through

    # Block phrase found — check if an allow phrase overrides it
    allowed = any(phrase in text for phrase in GEO_ALLOW_PHRASES)
    if allowed:
        return False  # Overridden — e.g. "US preferred but open to EMEA"

    return True  # Geo-restricted, no override

# ── Blocked sources ───────────────────────────────────────────────────────────
# Fake or malicious job boards: near-identical sites under rotating names that
# redirect to third-party pages rather than a real employer. A job whose source,
# company or link matches any marker below is dropped before it is ever scored.
#
# Matching is done on an alphanumeric-only, lowercased form of the field, so a
# single marker catches the display name, the hyphenated slug and the domain
# alike: 'vacancyglobalpro' matches 'Vacancy Global Pro', 'vacancy-global-pro'
# and 'vacancyglobalpro.com'. To block another site, add its squashed name here.
BLOCKED_SOURCE_MARKERS = (
    'vacancyglobalpro',   # Vacancy Global Pro
    'remotezestjobs',     # Remote Zest Jobs
    'remoteclickjobs',    # Remote Click Jobs
)


def _squash(value: str) -> str:
    """Lowercase and strip to letters and digits, so name, slug and domain
    forms of the same source collapse to one comparable token."""
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def is_blocked_source(job: dict) -> bool:
    """Return True if the job comes from a known fake or malicious board.

    Checks the fields that identify where a listing originated, source,
    company and link, against BLOCKED_SOURCE_MARKERS. The title and description
    are deliberately not checked: a real job can mention any word, and matching
    there would drop legitimate listings.
    """
    haystack = _squash(
        f"{job.get('source', '')} {job.get('company', '')} {job.get('link', '')}"
    )
    return any(marker in haystack for marker in BLOCKED_SOURCE_MARKERS)


# ── US location downrank ──────────────────────────────────────────────────────
# A US-located listing carrying no worldwide or European eligibility signal is
# pushed down the digest rather than dropped, because such a listing is
# occasionally a genuinely worldwide-remote role. The penalty multiplies the
# relevance score and is applied after the minimum-score gate, so it reorders
# the digest without ever removing a job the score alone would have kept.
US_LOCATION_PENALTY = 0.6

_US_STATE_CODES = frozenset((
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI', 'ID',
    'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI', 'MN', 'MS',
    'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC', 'ND', 'OH', 'OK',
    'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT', 'VT', 'VA', 'WA', 'WV',
    'WI', 'WY', 'DC',
))
_US_NAME_MARKERS = ('united states', 'usa', 'u.s.a', 'u.s.')


def is_us_located(location: str) -> bool:
    """Best-effort detection of a United States location, covering the
    'City, ST' form job boards emit and the spelled-out country name."""
    loc = (location or '').lower()
    if any(marker in loc for marker in _US_NAME_MARKERS):
        return True
    for part in re.split(r'[,/|]', location or ''):
        head = part.strip().upper().split(' ')[0] if part.strip() else ''
        if head in _US_STATE_CODES or head == 'US':
            return True
    return False


def us_location_multiplier(job: dict) -> float:
    """Score multiplier for the US downrank. 1.0 leaves the score unchanged;
    US_LOCATION_PENALTY sinks a US-located job with no worldwide/EU signal."""
    text = (job.get('title', '') + ' ' + job.get('description', '') + ' '
            + job.get('location', '')).lower()
    if any(phrase in text for phrase in GEO_ALLOW_PHRASES):
        return 1.0  # worldwide or European eligibility stated, no penalty
    if is_us_located(job.get('location', '')):
        return US_LOCATION_PENALTY
    return 1.0


# ── Target role keywords — presence in title boosts score ────────────────────
TITLE_BOOST_KEYWORDS = [
    'sales engineer', 'technical sales', 'pre-sales', 'presales',
    'application engineer', 'solutions engineer', 'technical account',
    'mechanical engineer', 'process engineer', 'cfd engineer',
    'content strategist', 'technical writer', 'seo specialist',
    'ai engineer', 'automation engineer', 'systems engineer',
    'field engineer', 'product engineer',
    # Appointment setter / SDR track
    'appointment setter', 'sales development', 'sdr', 'bdr',
    'sales representative', 'business development representative',
    'outbound sales', 'lead generation', 'inside sales',
]
TITLE_BOOST_MULTIPLIER = 1.4  # 40% bonus when role matches title

# ── Sector boost — companies/descriptions in these industries score higher ────
SECTOR_BOOST_KEYWORDS = [
    # Industrial / manufacturing
    'industrial', 'manufacturing', 'valve', 'fluid', 'hydraulic', 'pneumatic',
    'automation', 'process industry', 'oil and gas', 'oil & gas', 'mining',
    'energy', 'utilities', 'pipeline', 'instrumentation', 'sensors',
    'mechanical', 'engineering components', 'distribution', 'mro',
    # SaaS / B2B tech
    'saas', 'b2b software', 'enterprise software', 'platform', 'developer tools',
    'devops', 'cloud', 'infrastructure', 'iot', 'industry 4.0',
    'field service', 'asset management', 'cmms', 'erp', 'plm', 'scada',
    # Technical content / martech
    'technical marketing', 'product marketing', 'developer marketing',
    'content operations', 'demand generation',
]
SECTOR_BOOST_MULTIPLIER = 1.3  # 30% bonus when company/description matches target sector

# ── Non-English title markers ────────────────────────────────────────────────
# Several aggregators return German postings for English queries. The
# description is often English enough to score well, so the title is the only
# reliable signal. Matched as whole words to avoid catching English words that
# happen to contain them.
NON_ENGLISH_TITLE_MARKERS = [
    'entwickler', 'ingenieur', 'leiter', 'kaufmann', 'kauffrau',
    'vertrieb', 'werkstudent', 'praktikant', 'ausbildung',
    'mitarbeiter', 'sachbearbeiter', 'buchhalter',
]

# Sources whose jobs bypass scoring and filtering. A job you saved by hand is
# a job you already decided you wanted.
ALWAYS_INCLUDE_SOURCES = ('Gmail Draft',)


def title_prescreen(job_title: str, skills) -> bool:
    """
    Cheap, network-free test for whether a title is worth investigating.

    Some sources return titles only. Scoring those against a CV is close to
    meaningless, so the pipeline fetches the full description for the ones
    that pass this screen. That costs one HTTP request per job, which is why
    the screen exists at all: on a 200 job board it turns 200 requests into
    a handful.

    Deliberately generous. A false positive costs one request. A false
    negative loses a job you would have wanted, which is far worse, so the
    bar is "could plausibly be relevant", not "is a good match".

    Args:
        job_title: the title to screen
        skills: iterable of skill strings from the CV profile

    Returns:
        True if the title is worth fetching a description for.
    """
    title = (job_title or '').lower()
    if not title:
        return False

    if any(keyword in title for keyword in TITLE_BOOST_KEYWORDS):
        return True

    if any(keyword in title for keyword in SECTOR_BOOST_KEYWORDS):
        return True

    # Any single meaningful skill token appearing in the title. Two character
    # tokens and shorter are dropped, they match too much.
    for skill in skills or ():
        for token in str(skill).lower().split():
            if len(token) > 2 and token in title:
                return True

    return False


def is_non_english_title(job_title: str) -> bool:
    """Return True if the title contains a non-English role marker."""
    title = (job_title or '').lower()
    return any(
        re.search(rf'\b{re.escape(marker)}', title)
        for marker in NON_ENGLISH_TITLE_MARKERS
    )

class JobFilter:
    """Filter and rank jobs by relevance to user skills."""

    def __init__(self):
        self.skills_data = self.load_keywords()
        self.all_skills = self.extract_all_skills()

    def load_keywords(self):
        """Load the skills profile the scorer matches against.

        Prefers the master CV: its `variants:` section is the single source of
        truth, read directly, no PDF extraction. Falls back to the old extracted
        keyword cache when no master CV is present, so existing setups keep
        working.
        """
        variants = load_variants(Config.MASTER_CV_PATH)
        if variants:
            return variants

        cache_file = Path(Config.KEYWORDS_CACHE)
        try:
            if cache_file.exists():
                with open(cache_file, 'r') as f:
                    return json.load(f)
            else:
                logger.warning(
                    f"No master CV at {Config.MASTER_CV_PATH} and no keyword "
                    f"cache at {cache_file}"
                )
        except Exception as e:
            logger.error(f"Error loading keywords: {e}")
        return {'cvs': {}, 'linkedin': {}, 'merged_skills': {}}

    def extract_all_skills(self):
        """Extract all skills from cache data."""
        skills = set()

        # Add CV skills
        for cv_data in self.skills_data.get('cvs', {}).values():
            if isinstance(cv_data, dict) and 'skills' in cv_data:
                skills.update(cv_data['skills'].keys())

        # Add LinkedIn skills
        linkedin_data = self.skills_data.get('linkedin', {})
        if isinstance(linkedin_data, dict) and 'skills' in linkedin_data:
            skills.update(linkedin_data['skills'].keys())

        # Add merged skills
        merged = self.skills_data.get('merged_skills', {})
        if isinstance(merged, dict):
            skills.update(merged.keys())

        return {skill.lower() for skill in skills if skill}

    def is_negative_match(self, job_title, job_description):
        """Return True if the job contains a dealbreaker keyword."""
        text = (job_title + ' ' + job_description).lower()
        for phrase in NEGATIVE_KEYWORDS:
            if phrase in text:
                logger.debug(f"Negative match ({phrase!r}): {job_title}")
                return True
        return False

    def score_job_with_cv(self, job_title, job_description, company=""):
        """
        Score a job against each CV individually.
        Returns (best_score, best_cv_name).
        Applies title-boost when target role keywords appear in the job title.
        """
        # Some sources return a title and no description: the LinkedIn guest
        # cards, and the SmartRecruiters list endpoint. Returning 0 here
        # discarded every one of them. Score on whatever text exists instead,
        # and record that the score is title-only so the digest can say so.
        if not (job_description or job_title):
            return 0, None

        desc_lower = " ".join(
            part for part in (job_description, job_title, company) if part
        ).lower()
        title_lower = job_title.lower()
        best_score = 0.0
        best_cv = None

        for cv_name, cv_data in self.skills_data.get('cvs', {}).items():
            if not isinstance(cv_data, dict) or 'skills' not in cv_data:
                continue
            cv_skills = [s for s in cv_data['skills'].keys() if s]
            if not cv_skills:
                continue
            matches = sum(1 for s in cv_skills if skill_matches(s, desc_lower))
            # Cap denominator at 15 — a job description will never mention all CV skills.
            # Dividing by total skills (50+) made every score artificially low.
            # 3 matches out of 15 = 20%, not 6%.
            denominator = min(len(cv_skills), 15)
            score = round(min(100, (matches / denominator) * 100), 1)
            if score > best_score:
                best_score = score
                best_cv = cv_name

        # Title boost — target role in job title
        if best_score > 0 and any(kw in title_lower for kw in TITLE_BOOST_KEYWORDS):
            best_score = round(min(100, best_score * TITLE_BOOST_MULTIPLIER), 1)

        # Sector boost — industrial / SaaS company or description
        sector_text = (job_description + ' ' + company).lower()
        if best_score > 0 and any(kw in sector_text for kw in SECTOR_BOOST_KEYWORDS):
            best_score = round(min(100, best_score * SECTOR_BOOST_MULTIPLIER), 1)

        # Fallback: score against merged skills if no CV-level data
        if best_score == 0 and self.all_skills:
            matches = sum(1 for s in self.all_skills if skill_matches(s, desc_lower))
            denominator = min(len(self.all_skills), 15)
            best_score = round(min(100, (matches / denominator) * 100), 1)

        return best_score, best_cv

    def score_job(self, job_title, job_description, company=""):
        """
        Score a job based on skill match.
        Returns score 0-100. Uses per-CV scoring for better accuracy.
        """
        score, _ = self.score_job_with_cv(job_title, job_description, company)
        return score

    def is_remote(self, job_data):
        """Check if job is remote/work-from-home."""
        remote_keywords = [
            'remote', 'work from home', 'wfh', 'virtual',
            'distributed', 'telecommute', 'home-based'
        ]

        text = str(job_data).lower()
        return any(keyword in text for keyword in remote_keywords)

    def filter_jobs(self, jobs, min_score=10, remote_only=False):
        """
        Score and filter jobs. This is the only filtering path in the pipeline.

        Rejection order is cheapest test first, so a job that fails on a
        keyword never gets scored:

          1. remote check, off by default
          2. dealbreaker keywords, NEGATIVE_KEYWORDS
          3. geo restriction, GEO_BLOCK_PHRASES with an allow-list override
          4. non-English title markers
          5. relevance score below min_score

        Jobs from ALWAYS_INCLUDE_SOURCES bypass all five. They are still
        scored so the digest can show a number.

        Args:
            jobs: list of job dicts with title, description, company, source
            min_score: minimum relevance score, 0 to 100
            remote_only: apply the remote keyword check. Off by default
                because every configured source is already remote-only, and
                the check reads the whole dict as a string, which is crude.

        Returns:
            List sorted by relevance_score, highest first, each job carrying
            relevance_score and best_cv.
        """
        scored_jobs = []
        # Count rejections by reason so a run reports what it threw away
        # instead of only what it kept. Silent discarding is how the link
        # validation bug survived for weeks.
        rejected = {
            'blocked_source': 0,
            'not_remote': 0,
            'dealbreaker': 0,
            'geo_restricted': 0,
            'non_english': 0,
            'below_min_score': 0,
        }

        for job in jobs:
            title = job.get('title', '')
            description = job.get('description', '')
            company = job.get('company', '')

            always_include = job.get('source') in ALWAYS_INCLUDE_SOURCES

            if not always_include:
                # Cheapest and most decisive test: a fake or malicious board is
                # dropped outright, before any keyword or scoring work.
                if is_blocked_source(job):
                    logger.debug(f"Blocked source: {title} @ {company}")
                    rejected['blocked_source'] += 1
                    continue

                if remote_only and not self.is_remote(job):
                    rejected['not_remote'] += 1
                    continue

                if self.is_negative_match(title, description):
                    rejected['dealbreaker'] += 1
                    continue

                if is_geo_restricted(title, description, job.get('location', '')):
                    logger.debug(f"Geo-restricted: {title} @ {company}")
                    rejected['geo_restricted'] += 1
                    continue

                if is_non_english_title(title):
                    logger.debug(f"Non-English title: {title} @ {company}")
                    rejected['non_english'] += 1
                    continue

            score, best_cv = self.score_job_with_cv(title, description, company)

            if not always_include and score < min_score:
                rejected['below_min_score'] += 1
                continue

            # US downrank is applied after the min-score gate and skipped for
            # hand-saved jobs, so it only reorders and never drops.
            multiplier = 1.0 if always_include else us_location_multiplier(job)
            job['relevance_score'] = round(score * multiplier)
            job['best_cv'] = best_cv
            scored_jobs.append(job)

        scored_jobs.sort(key=lambda x: x['relevance_score'], reverse=True)

        total_rejected = sum(rejected.values())
        logger.info(
            f"Filtered {len(scored_jobs)} of {len(jobs)} jobs "
            f"({total_rejected} rejected: "
            + ', '.join(f"{k}={v}" for k, v in rejected.items() if v)
            + ')'
        )
        return scored_jobs


if __name__ == '__main__':
    jf = JobFilter()
    print(f"Loaded {len(jf.all_skills)} skills from profile")

    # Test with sample jobs
    test_jobs = [
        {
            'title': 'Python Developer',
            'company': 'Tech Corp',
            'description': 'Looking for Python and JavaScript developer for remote role'
        },
        {
            'title': 'Sales Manager',
            'company': 'Sales Inc',
            'description': 'On-site sales management position'
        }
    ]

    filtered = jf.filter_jobs(test_jobs, min_score=5)
    for job in filtered:
        print(f"{job['title']} @ {job['company']}: {job['relevance_score']}%")
