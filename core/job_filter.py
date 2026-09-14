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

# Pure software-development roles the candidate does not want, matched on the
# TITLE (high precision, avoids false positives from tech mentioned in a
# sales/engineering job description). A role that also signals AI/ML/automation
# is kept, because that hybrid work IS wanted, see AI_CONTEXT_MARKERS.
PURE_DEV_TITLE_MARKERS = [
    'software engineer', 'software developer', 'sw engineer',
    'backend engineer', 'back-end engineer', 'backend developer',
    'frontend engineer', 'front-end engineer', 'frontend developer',
    'full stack', 'full-stack', 'fullstack', 'web developer',
    'devops engineer', 'site reliability', 'sre engineer', 'platform engineer',
    'java developer', 'python developer', '.net developer', 'c++ developer',
    'c# developer', 'golang developer', 'go developer', 'rust developer',
    'php developer', 'ruby developer', 'node developer', 'node.js developer',
    'ios developer', 'android developer', 'mobile developer',
    'embedded software', 'firmware engineer',
]

# A dev-titled role is only dropped if it ALSO scores below this against the
# CVs, i.e. the candidate genuinely has no overlap with it. A dev role their
# skills DO match (AI/automation/Python-heavy work) scores above this and is
# kept. The CV score is the reliable "can I do this?" signal; a keyword match
# is not. This bar self-adjusts as skills are added to the master CV.
PURE_DEV_KEEP_SCORE = 20.0

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

# HARD locks: a requirement the user can never satisfy, so the job is dropped
# wherever it is. Citizenship or permanent residence (not obtainable on a work
# permit), and the US / UK / CA / AU region locks (no realistic route for a
# Serbian national). "EU citizens/nationals only" is a citizenship demand, which
# a work permit or Blue Card does NOT satisfy, so it stays hard.
GEO_HARD_LOCK_PHRASES = [
    'must be a citizen', 'must be a permanent resident',
    'us residents only', 'united states only', 'us only', 'us-based candidates only',
    'us based candidates only', 'us citizens only', 'green card',
    'eu citizens only', 'eu nationals only', 'uk residents only',
    'right to work in the uk', 'canada only', 'canadian residents only',
    'australia only',
]

# SOFT locks: a generic "you must be authorized to work here" requirement, no
# country attached. Inside the EU / EEA a Serbian national has a realistic
# permit / Blue Card route, so an EU-located role carrying this boilerplate is
# takeable and kept. Outside the EU (or with no location to place it), the same
# phrase still drops the job, no sponsorship route.
GEO_SOFT_LOCK_PHRASES = [
    'must be authorized to work in', 'must be authorised to work in',
    'must have the right to work in', 'must be eligible to work in',
    'must be legally authorized to work', 'must be legally authorised to work',
    'work authorization required', 'work authorisation required',
    'must hold a valid work permit', 'valid work permit required',
    'must have existing work authorization',
]

# EU / EEA signals used to place a soft-locked role. Region words plus member
# states, matched against the location field first (reliable), then the text as
# a fallback. The Balkan-accession and no-permit signals live in
# GEO_ALLOW_PHRASES already, this set only decides "is this role in the EU/EEA".
EU_EEA_MARKERS = [
    'european union', 'eea', 'schengen', 'europe', 'emea',
    'austria', 'belgium', 'bulgaria', 'croatia', 'cyprus', 'czech', 'czechia',
    'denmark', 'estonia', 'finland', 'france', 'germany', 'greece', 'hungary',
    'ireland', 'italy', 'latvia', 'lithuania', 'luxembourg', 'malta',
    'netherlands', 'poland', 'portugal', 'romania', 'slovakia', 'slovenia',
    'spain', 'sweden', 'norway', 'iceland', 'liechtenstein',
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


def matches_region(location, terms):
    """True when the job's location contains any of the region terms.

    Case-insensitive substring match. Used by the regional Telegram digest to
    pick out home-market jobs (e.g. the Balkans) from stored listings. Empty
    location or empty terms means no match, so the feature is inert until the
    user sets REGIONAL_MATCH_TERMS in their .env.
    """
    if not location or not terms:
        return False
    loc = location.lower()
    return any(t.lower() in loc for t in terms)


def _is_eu_located(location: str, text: str) -> bool:
    """Return True when the role sits in the EU / EEA, where a Serbian national
    has a realistic work-permit / Blue Card route.

    The location field is the reliable signal and is checked first. Only when it
    is empty or unhelpful does this fall back to the full text, and even then it
    ignores the broad region words (europe / emea), which turn up in unrelated
    context ("we serve European markets") on jobs that are not actually in the EU.
    """
    loc = (location or '').lower()
    if any(marker in loc for marker in EU_EEA_MARKERS):
        return True
    if not loc.strip() or loc in ('not stated', 'n/a', 'remote'):
        # Country names only, not the region words, to avoid false positives.
        countries = [m for m in EU_EEA_MARKERS if m not in ('europe', 'emea')]
        if any(country in text for country in countries):
            return True
    return False


def is_geo_restricted(job_title: str, job_description: str, location: str = '',
                      eligible_regions=None) -> bool:
    """Return True when the user could not legally take this job.

    Order: an explicit sponsorship / relocation offer or a worldwide / no-permit
    signal keeps the job; an explicit refusal to sponsor drops it; a hard lock
    (citizenship, or a US / UK / CA / AU region lock) drops it; a generic
    work-authorization requirement drops it UNLESS the role is in a region the
    user can obtain a permit in. Nothing stated is treated as open.

    eligible_regions: where the user can pursue work authorization. Defaults to
    Config.WORK_ELIGIBLE_REGIONS (read from the user's .env). Accepted values:
      - 'ANY' (or 'WORLDWIDE' / 'GLOBAL'): willing to pursue a permit anywhere,
        so a role asking only for generic authorization is kept wherever it is.
      - 'EU' / 'EEA': keep such a role only when it is EU/EEA-located.
      - empty: strict, the generic public default, a role gated on local
        authorization is kept only with explicit sponsorship or a worldwide /
        remote signal, never on region membership.
    A hard citizenship / residency lock (US citizens only, green card, EU
    nationals only, ...) is never satisfiable and always drops, regardless of
    this setting. This is what keeps the shared engine correct for any user.
    """
    if eligible_regions is None:
        from core.config import Config
        eligible_regions = Config.WORK_ELIGIBLE_REGIONS
    regions = {r.upper() for r in (eligible_regions or [])}

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
    if any(phrase in text for phrase in GEO_HARD_LOCK_PHRASES):
        return True  # citizenship / residency lock, never satisfiable
    if any(phrase in text for phrase in GEO_SOFT_LOCK_PHRASES):
        # Generic authorization requirement. Kept if the user can pursue a
        # permit where the role sits.
        if regions & {'ANY', 'WORLDWIDE', 'GLOBAL'}:
            return False  # willing to pursue authorization anywhere
        if (regions & {'EU', 'EEA'}) and _is_eu_located(location, text):
            return False
        return True
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


# ── Scam-risk screen ──────────────────────────────────────────────────────────
# A softer layer than is_blocked_source. That function DROPS jobs it can prove
# are malicious (known boards, free-PaaS apply links, known funnel domains).
# This one flags jobs that merely look risky, on the limited fields an aggregator
# like Indeed returns list-only (title, company, link). A hit does not drop the
# job: it heavily downranks it and marks it so the digest can warn before you
# apply. Phrases are chosen to be high precision, things a real employer does not
# put in a posting, so legitimate jobs are not swept up.
SCAM_RISK_PHRASES = (
    # Off-platform "apply by messaging a number", the classic recruitment scam
    'via whatsapp', 'on whatsapp', 'whatsapp:', 'contact me on whatsapp',
    'reach me on whatsapp', 'via telegram', 'telegram to apply', 'apply on telegram',
    'message me on telegram', 'dm me on telegram', 'text us to apply', 'text to apply',
    'text me to apply', 'sms to apply',
    # Pay-to-work, never legitimate
    'registration fee', 'processing fee', 'application fee', 'startup fee',
    'onboarding fee', 'pay a fee to', 'refundable deposit', 'security deposit required',
    # Money-mule / reshipping fronts
    'reshipping', 'package forwarding', 'money mule', 'payment processing agent from home',
)

# How hard a scam-risk hit sinks the score. Heavier than the US or remote
# penalties: a suspected scam should fall to the bottom, usually below the digest
# bar, but is not deleted outright since the screen is a heuristic.
SCAM_RISK_PENALTY = 0.35


def scam_risk(title: str, description: str = '', company: str = '', link: str = '') -> bool:
    """
    True when a listing shows scam-risk signals but was not outright blocked.

    Deliberately usable with only title, company and link, so the digest can
    recompute it at display time (where the description is not available) and
    show the same warning the scorer used to downrank it.
    """
    text = f"{title} {description} {company}".lower()
    if any(phrase in text for phrase in SCAM_RISK_PHRASES):
        return True
    # A free-PaaS or known-funnel host anywhere in the link or body is a strong
    # decoy signal even when is_blocked_source did not have enough to drop it.
    for host, _path in _urls_in(f"{link} {description}"):
        if _is_free_paas(host) or _is_scam_destination(host):
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
    US_LOCATION_PENALTY sinks a US-located job with no worldwide/EU signal.

    SUPERSEDED 2026-09-09 by region_preference_multiplier for digest ranking.
    Kept because callers and tests still reference it, and because the flat US
    downrank is still the right answer for anyone whose preferences differ.
    """
    text = (job.get('title', '') + ' ' + job.get('description', '') + ' '
            + job.get('location', '')).lower()
    if (any(phrase in text for phrase in SPONSORSHIP_PHRASES)
            or any(phrase in text for phrase in GEO_ALLOW_PHRASES)):
        return 1.0  # sponsored, worldwide, or no-permit, a US role here is wanted
    if is_us_located(job.get('location', '')):
        return US_LOCATION_PENALTY
    return 1.0


# ── Excluded locations ────────────────────────────────────────────────────────
# Countries or cities the user has ruled out entirely, remote and on-site alike.
# Read from Config.EXCLUDED_LOCATIONS (their .env), so the public engine excludes
# nothing by default.
#
# Matched on the location field only, never the description: a role in one
# country whose description happens to mention an office in an excluded one must
# not be dropped.
#
# Matched on word boundaries rather than as bare substrings. A substring test for
# a country name will also hit unrelated places that merely contain it, silently
# dropping jobs that were never meant to be excluded.
def _excluded_location_pattern(terms):
    if not terms:
        return None
    return re.compile(
        r'|'.join(r'\b' + re.escape(t) + r'\b' for t in terms), re.IGNORECASE
    )


def is_excluded_location(location: str, terms=None) -> bool:
    """True when the role sits somewhere the user has ruled out entirely."""
    if terms is None:
        terms = Config.EXCLUDED_LOCATIONS
    pattern = _excluded_location_pattern(terms)
    if pattern is None:
        return False
    return bool(pattern.search(location or ''))


# ── Region preference ─────────────────────────────────────────────────────────
# Ranks Europe above other acceptable regions without hiding them, for a user who
# can pursue work authorisation in the EU/EEA but is willing to go further afield.
# The strength is Config.NON_EUROPE_PREFERENCE, which defaults to 1.0, meaning no
# preference at all unless the user sets one.
#
# This is deliberately gentler than the older flat US downrank, which was built
# for a narrower case and buries an entire continent. Use that one instead when
# non-European roles genuinely are a fallback rather than a real option.
def region_preference_multiplier(job: dict) -> float:
    """Rank Europe first and keep every other accepted region close behind.

    Returns 1.0 for Europe, for anything carrying a sponsorship or worldwide
    signal, and for a job with no usable location, which is normally
    remote-anywhere. Everywhere else gets Config.NON_EUROPE_PREFERENCE.
    """
    location = job.get('location', '') or ''
    text = (job.get('title', '') + ' ' + job.get('description', '') + ' '
            + location).lower()
    if (any(phrase in text for phrase in SPONSORSHIP_PHRASES)
            or any(phrase in text for phrase in GEO_ALLOW_PHRASES)):
        return 1.0
    if _is_eu_located(location, text):
        return 1.0
    if not location.strip() or location.strip().lower() in ('not stated', 'n/a', 'remote'):
        return 1.0
    return Config.NON_EUROPE_PREFERENCE


# ── Visa-sponsorship uprank ───────────────────────────────────────────────────
# On-site/hybrid roles in permit-required countries are kept, not dropped:
# postings rarely state sponsorship even when the employer would sponsor, so a
# hard filter would throw away most of the real EU inventory. Instead, a job
# that explicitly signals sponsorship is surfaced higher, so the ones a non-EU
# candidate can actually take without a permit float to the top of the digest.
SPONSORSHIP_BOOST = 1.25


def sponsorship_multiplier(job: dict) -> float:
    """Score multiplier that lifts postings which explicitly offer visa
    sponsorship. 1.0 leaves the score unchanged."""
    text = (job.get('title', '') + ' ' + job.get('description', '') + ' '
            + job.get('company', '')).lower()
    if any(phrase in text for phrase in SPONSORSHIP_PHRASES):
        return SPONSORSHIP_BOOST
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

# How many matched skills count as a perfect match for one CV variant. Never
# more than the variant actually holds, so a small focused variant can still
# reach 100 when a job mentions all of it. Raised from 15 to 25 on 2026-09-11:
# the variants had grown to 56-62 skills, so 15 was reachable by any decent
# job and a quarter of a day's digest tied on exactly 100, leaving the top of
# the list unrankable.
SKILL_MATCH_DENOMINATOR_CAP = 25


def apply_boost(score, multiplier):
    """
    Lift a score toward 100 without collapsing the jobs it lifts into a tie.

    The boosts used to multiply and then clamp: 1.4 x 1.3 = 1.82, so anything
    above a raw 55 came out as exactly 100 no matter how well it really
    matched. Spending the multiplier on the remaining headroom instead keeps
    the result strictly increasing in score and inside 0-100.
    """
    return round(score + (100 - score) * (multiplier - 1.0), 1)

# ── Non-English title markers ────────────────────────────────────────────────
# Several aggregators return German postings for English queries. The
# description is often English enough to score well, so the title is the only
# reliable signal. Matched as whole words to avoid catching English words that
# happen to contain them.
NON_ENGLISH_TITLE_MARKERS = [
    # German
    'entwickler', 'ingenieur', 'leiter', 'kaufmann', 'kauffrau',
    'vertrieb', 'werkstudent', 'praktikant', 'ausbildung',
    'mitarbeiter', 'sachbearbeiter', 'buchhalter',
    # Danish. The dk SerpApi market returns these for English queries.
    'ingeniør', 'medarbejder', 'projektleder', 'salgs', 'udvikler',
    'erfaren', 'tekniker', 'konsulent', 'lærling',
    # Dutch, from the nl and be markets.
    'medewerker', 'ontwikkelaar', 'werkvoorbereider', 'verkoop',
    'onderhoud', 'adviseur', 'beheerder', 'inkoper', 'monteur',
    'leidinggevende', 'technicus', 'vacature',
]

# ── English as the stated working language ───────────────────────────────────
# A local-language title is not automatically a dead end. Plenty of Danish and
# Dutch employers advertise in their own language and then run the job in
# English, and those are worth keeping.
#
# Deliberately narrow. It matches an explicit statement about the language the
# work is conducted in, never a passing mention of English, because the common
# case is a local-language role that also wants English on top. Matching
# "fluent in English" would rescue exactly the jobs this filter exists to drop.
ENGLISH_WORKING_LANGUAGE_PHRASES = [
    'working language is english',
    'english is the working language',
    'english is our working language',
    'company language is english',
    'corporate language is english',
    'business language is english',
    'communication is in english',
    'all communication in english',
    'we speak english',
    'english speaking environment',
    'english-speaking environment',
    'no danish required',
    'no dutch required',
    'danish is not required',
    'dutch is not required',
    'voertaal is engels',          # Dutch: "the working language is English"
    'arbejdssproget er engelsk',   # Danish: same
    'koncernsproget er engelsk',
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


def states_english_working_language(job_description: str) -> bool:
    """
    Return True only if the posting explicitly says the work is done in English.

    Used to rescue a job whose title is in another language. Kept strict on
    purpose: a posting that merely asks for English on top of the local language
    must not pass, or the language filter stops filtering anything.
    """
    text = (job_description or '').lower()
    return any(phrase in text for phrase in ENGLISH_WORKING_LANGUAGE_PHRASES)

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

    def is_dev_titled(self, job_title):
        """Return True if the title is a pure software-development role. This is
        only a signal, not a verdict: the caller drops it only when the CV score
        is also low (see PURE_DEV_KEEP_SCORE), so a dev role the candidate's
        skills actually match is kept."""
        title = (job_title or '').lower()
        return any(marker in title for marker in PURE_DEV_TITLE_MARKERS)

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
            # A job description will never mention all CV skills, so dividing by
            # the full list (50+) made every score artificially low. See
            # SKILL_MATCH_DENOMINATOR_CAP for why the cap is what it is.
            denominator = min(len(cv_skills), SKILL_MATCH_DENOMINATOR_CAP)
            score = round(min(100, (matches / denominator) * 100), 1)
            if score > best_score:
                best_score = score
                best_cv = cv_name

        # Title boost, target role in job title
        if best_score > 0 and any(kw in title_lower for kw in TITLE_BOOST_KEYWORDS):
            best_score = apply_boost(best_score, TITLE_BOOST_MULTIPLIER)

        # Sector boost, industrial / SaaS company or description
        sector_text = (job_description + ' ' + company).lower()
        if best_score > 0 and any(kw in sector_text for kw in SECTOR_BOOST_KEYWORDS):
            best_score = apply_boost(best_score, SECTOR_BOOST_MULTIPLIER)

        # Fallback: score against merged skills if no CV-level data
        if best_score == 0 and self.all_skills:
            matches = sum(1 for s in self.all_skills if skill_matches(s, desc_lower))
            denominator = min(len(self.all_skills), SKILL_MATCH_DENOMINATOR_CAP)
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
            'pure_programming': 0,
            'geo_restricted': 0,
            'excluded_location': 0,
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

                # A ruled-out country is dropped outright, remote or not.
                if is_excluded_location(job.get('location', '')):
                    logger.debug(f"Excluded location: {title} @ {company}")
                    rejected['excluded_location'] += 1
                    continue

                if (is_non_english_title(title)
                        and not states_english_working_language(description)):
                    logger.debug(f"Non-English title: {title} @ {company}")
                    rejected['non_english'] += 1
                    continue

            score, best_cv = self.score_job_with_cv(title, description, company)

            # Drop a pure software-dev role only when the CV score confirms the
            # candidate has no real overlap with it. A dev role their skills DO
            # match (AI/automation/Python work) scores above the bar and stays.
            if not always_include and score < PURE_DEV_KEEP_SCORE and self.is_dev_titled(title):
                logger.debug(f"Pure programming, low fit ({score}%): {title}")
                rejected['pure_programming'] += 1
                continue

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
                multiplier = region_preference_multiplier(job)
                if not self.is_remote(job):
                    multiplier *= REMOTE_PREFERENCE_PENALTY
                # Lift roles that explicitly offer visa sponsorship, so the
                # takeable-without-a-permit ones rise above equally scored jobs.
                multiplier *= sponsorship_multiplier(job)
                # Suspected scam: sink it and mark it, but do not delete, since
                # this is a heuristic. The digest recomputes the flag to warn.
                if scam_risk(title, description, company, job.get('link', '')):
                    multiplier *= SCAM_RISK_PENALTY
                    job['scam_risk'] = True
            # Cap at 100: the sponsorship boost can push a high score past it,
            # and a >100% relevance reads as a bug in the digest.
            job['relevance_score'] = round(min(100, score * multiplier))
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
