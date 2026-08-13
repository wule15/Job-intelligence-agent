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

# ── Negative keywords, jobs containing these are auto-rejected ──────────────
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

# ── Work-eligibility filter ───────────────────────────────────────────────────
# The user is a non-EU / non-US national (Serbia). A job anywhere, EU, US, UK, is
# takeable only if they can legally work it: it either offers visa sponsorship, or
# does not require existing local work authorization (remote-global roles, or ones
# that simply do not restrict). A job that requires existing authorization and does
# not sponsor is dropped, wherever it is. Geography is no longer the filter; the
# right to actually take the job is.

# Positive: an explicit sponsorship / relocation offer, or worldwide eligibility.
# Any of these keeps the job AND exempts it from the US relocation downrank.
SPONSORSHIP_PHRASES = [
    'visa sponsorship', 'sponsor a visa', 'sponsor your visa', 'we sponsor',
    'will sponsor', 'happy to sponsor', 'happily sponsor', 'glad to sponsor',
    'we can sponsor', 'we do sponsor', 'sponsor visas', 'sponsoring visas',
    'sponsor a work visa', 'sponsor work visas', 'sponsorship available',
    'offer sponsorship', 'provide sponsorship', 'sponsorship provided',
    'work visa sponsorship', 'work permit sponsorship', 'visa support',
    'relocation support', 'relocation assistance', 'relocation package',
    'we relocate', 'open to international', 'international applicants welcome',
    'hire internationally', 'visa sponsorship available',
]

# Explicit refusals of sponsorship. Checked BEFORE the positive
# SPONSORSHIP_PHRASES, because "no visa sponsorship" contains "visa sponsorship"
# and would otherwise read as an offer.
NO_SPONSOR_PHRASES = [
    'no visa sponsorship', 'no sponsorship available', 'not offer sponsorship',
    'do not offer sponsorship', 'do not provide sponsorship', 'cannot sponsor',
    'unable to sponsor', 'unable to provide sponsorship', 'without sponsorship',
    'not able to sponsor', 'sponsorship is not available', 'we do not sponsor',
    'no relocation',
]

# The job requires existing local work authorization. Country-agnostic
# ("authorized to work in ...") plus the US / UK / EU / CA / AU residence locks.
# Dropped UNLESS a sponsorship or worldwide phrase overrides.
GEO_BLOCK_PHRASES = [
    # existing authorization required (generic, any country)
    'must be authorized to work in', 'must be authorised to work in',
    'must have the right to work in', 'must be eligible to work in',
    'must be legally authorized to work', 'must be legally authorised to work',
    'work authorization required', 'work authorisation required',
    'must hold a valid work permit', 'valid work permit required',
    'must have existing work authorization',
    'must be a citizen', 'must be a permanent resident',
    # region locks
    'us residents only', 'united states only', 'us only', 'us-based candidates only',
    'us based candidates only', 'us citizens only', 'green card',
    'eu citizens only', 'eu nationals only', 'uk residents only',
    'right to work in the uk', 'canada only', 'canadian residents only',
    'australia only',
]

# Confirms worldwide / remote-global eligibility, or that no permit is needed.
# Overrides a block phrase and exempts from the US relocation downrank. Note:
# "europe"/"EMEA" is NOT here, a Serbian national has no automatic EU work right,
# so an EU-authorization requirement must still be caught unless sponsored.
GEO_ALLOW_PHRASES = [
    'worldwide', 'global', 'anywhere in the world', 'work from anywhere',
    'open to candidates worldwide', 'remote worldwide', 'fully remote worldwide',
    'international candidates', 'no visa required', 'no work permit required',
    'no sponsorship required', 'bosnia', 'serbia', 'balkans',
]


def is_geo_restricted(job_title: str, job_description: str, location: str = '') -> bool:
    """Return True when the user could not legally take this job: it requires
    existing local work authorization and offers no sponsorship.

    A sponsorship / relocation offer, or a worldwide / remote-global / no-permit
    signal, keeps the job. Nothing stated at all is treated as open, worth applying.
    """
    text = (job_title + ' ' + job_description + ' ' + location).lower()
    no_sponsor = any(phrase in text for phrase in NO_SPONSOR_PHRASES)
    worldwide = any(phrase in text for phrase in GEO_ALLOW_PHRASES)
    # A refusal ("no visa sponsorship") must not read as an offer, so require
    # no_sponsor to be false before trusting a positive sponsorship phrase.
    offers_sponsorship = (not no_sponsor) and any(
        phrase in text for phrase in SPONSORSHIP_PHRASES)
    if offers_sponsorship or worldwide:
        return False  # sponsored, or worldwide / no permit needed, takeable
    if no_sponsor:
        return True  # explicitly will not sponsor, cannot take
    if any(phrase in text for phrase in GEO_BLOCK_PHRASES):
        return True  # needs existing authorization, no sponsorship, cannot take
    return False  # unstated, assume open

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
    'vacancyjobspro',     # Vacancy Jobs Pro (rebrand)
    'remotezestjobs',     # Remote Zest Jobs
    'zestjobs',           # Zest Jobs (rebrand)
    'remoteclickjobs',    # Remote Click Jobs
)

# Free hosting subdomains a legitimate employer does not use for an apply page,
# but AI-generated fake boards spin up by the dozen and rotate through. A job
# whose apply link, or a link inside its description, points at one of these is
# almost certainly a decoy that funnels applicants to a scam destination.
FREE_PAAS_HOSTS = (
    'railway.app', 'onrender.com', 'render.com', 'vercel.app', 'netlify.app',
    'fly.dev', 'pages.dev', 'glitch.me', 'replit.app', 'repl.co', 'herokuapp.com',
)

# Known scam funnel destinations these boards redirect applicants to. Add new
# ones here as they surface; this is the stable signal, the board names rotate.
BLOCKED_DESTINATION_DOMAINS = (
    'victorytuitions.in',
)

# host in group 1, path in group 2
_URL_RE = re.compile(r'https?://([a-z0-9.\-]+)([^\s"\'<>)]*)', re.I)
# path fragments that mark a URL as a job posting rather than, say, a demo link
_JOB_PATH_RE = re.compile(r'/(job|jobs|apply|career|vacan|position|opening|hiring)', re.I)


def _squash(value: str) -> str:
    """Lowercase and strip to letters and digits, so name, slug and domain
    forms of the same source collapse to one comparable token."""
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def _urls_in(text: str) -> list:
    """Return (host, path) for every http(s) URL in text, host lowercased."""
    return [(h.lower(), p) for h, p in _URL_RE.findall(text or '')]


def _is_scam_destination(host: str) -> bool:
    return any(host == d or host.endswith('.' + d) for d in BLOCKED_DESTINATION_DOMAINS)


def _is_free_paas(host: str) -> bool:
    return any(host == p or host.endswith('.' + p) for p in FREE_PAAS_HOSTS)


def is_blocked_source(job: dict) -> bool:
    """Return True if the job comes from a known fake or malicious board.

    Two layers, weakest to strongest:
      1. Name match, source, company and squashed link against
         BLOCKED_SOURCE_MARKERS. Cheap, but the board names rotate.
      2. Destination match, the reliable signal. These decoys funnel applicants
         through a free-PaaS subdomain to a scam destination, so the apply link
         and the description are scanned for those hosts. This catches the whole
         family even when the listing arrives through a legitimate aggregator
         (e.g. Indeed) whose own link is an indeed.com URL, because the real
         destination still sits in the body.
    """
    haystack = _squash(
        f"{job.get('source', '')} {job.get('company', '')} {job.get('link', '')}"
    )
    if any(marker in haystack for marker in BLOCKED_SOURCE_MARKERS):
        return True

    # The apply link is the strong signal: a free-PaaS host or a known scam
    # destination there is decisive, a real employer never applies through one.
    for host, _path in _urls_in(job.get('link', '')):
        if _is_scam_destination(host) or _is_free_paas(host):
            return True

    # The description is weaker: block a known scam destination outright, but a
    # free-PaaS host only when the URL itself looks like a job/apply page, so a
    # legitimate listing that merely links a demo on vercel.app is not dropped.
    for host, path in _urls_in(job.get('description', '')):
        if _is_scam_destination(host):
            return True
        if _is_free_paas(host) and _JOB_PATH_RE.search(path):
            return True
    return False


# ── US location downrank ──────────────────────────────────────────────────────
# A US-located listing carrying no worldwide or European eligibility signal is
# pushed down the digest rather than dropped, because such a listing is
# occasionally a genuinely worldwide-remote role. The penalty multiplies the
# relevance score and is applied after the minimum-score gate, so it reorders
# the digest without ever removing a job the score alone would have kept.
US_LOCATION_PENALTY = 0.6

# Remote is preferred but not required. A non-remote job (on-site or hybrid) is
# pushed down rather than dropped, so EU on-site industrial roles still appear,
# just below equally scored remote ones. Milder than the US penalty on purpose:
# an on-site role in the user's own region is worth surfacing.
REMOTE_PREFERENCE_PENALTY = 0.85

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
    if (any(phrase in text for phrase in SPONSORSHIP_PHRASES)
            or any(phrase in text for phrase in GEO_ALLOW_PHRASES)):
        return 1.0  # sponsored, worldwide, or no-permit, a US role here is wanted
    if is_us_located(job.get('location', '')):
        return US_LOCATION_PENALTY
    return 1.0


# ── Target role keywords, presence in title boosts score ────────────────────
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

# ── Sector boost, companies/descriptions in these industries score higher ────
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
            # Cap denominator at 15, a job description will never mention all CV skills.
            # Dividing by total skills (50+) made every score artificially low.
            # 3 matches out of 15 = 20%, not 6%.
            denominator = min(len(cv_skills), 15)
            score = round(min(100, (matches / denominator) * 100), 1)
            if score > best_score:
                best_score = score
                best_cv = cv_name

        # Title boost, target role in job title
        if best_score > 0 and any(kw in title_lower for kw in TITLE_BOOST_KEYWORDS):
            best_score = round(min(100, best_score * TITLE_BOOST_MULTIPLIER), 1)

        # Sector boost, industrial / SaaS company or description
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

            # A fake or malicious board is dropped outright, and this runs even
            # for always-include sources: a scam must never bypass the block on a
            # trusted label. Cheapest and most decisive test, before any scoring.
            if is_blocked_source(job):
                logger.debug(f"Blocked source: {title} @ {company}")
                rejected['blocked_source'] += 1
                continue

            always_include = job.get('source') in ALWAYS_INCLUDE_SOURCES

            if not always_include:
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

            # Location downranks are applied after the min-score gate and skipped
            # for hand-saved jobs, so they only reorder and never drop. A US
            # location and a non-remote (on-site/hybrid) role each push the job
            # down; remote in the user's region stays on top, on-site still shows.
            if always_include:
                multiplier = 1.0
            else:
                multiplier = us_location_multiplier(job)
                if not self.is_remote(job):
                    multiplier *= REMOTE_PREFERENCE_PENALTY
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
