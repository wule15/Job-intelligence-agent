"""
Job filtering and relevance scoring based on extracted skills.
Ranks jobs by how well they match your CV skills.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from core.config import Config
from core.countries import (
    COUNTRY_NAMES, US_STATE_CODES, expand_codes, is_placeholder_location, is_remote_place,
    location_countries, names_for, text_countries,
)
from core.utils import setup_logging
from core.synonym_map import skill_matches
from core.multilingual import canonical_language, english_equivalents, fold_diacritics, written_in
from core.cv_variants import load_variants
from core.job_normalize import find_same_postings

logger = setup_logging('job_filter')

# ── Negative keywords, jobs containing these are auto-rejected ──────────────
NEGATIVE_KEYWORDS = [
    # Clearance / citizenship. Clearance wording itself is matched by
    # CLEARANCE_PATTERNS below, on word boundaries, so "customs clearance" is
    # not caught. "U.S. citizen" with dots is caught by the work-eligibility
    # filter further down.
    'top secret', 'ts/sci', 'must be a us citizen', 'us citizenship required',
    # Experience overreach used to live here and drop the job outright. It now
    # lowers the rank instead, see experience_multiplier, so a posting asking
    # for many years is still shown, just further down. A senior TITLE is
    # dropped, see SENIOR_LEVEL_PATTERN.
    # Strict on-site (belt-and-suspenders alongside remote filter)
    'no remote', 'not a remote', 'on-site only', 'onsite only', 'must be onsite',
    'relocation required', 'must relocate',
]

# Security clearance and vetting. A non-national cannot hold one, so a role
# that needs one is never takeable. UK adverts write "SC" or "DV clearance"
# and "security vetting"; US ones write "security clearance". A plain
# substring test for "clearance required" also caught "customs clearance
# required", a logistics duty, so the lookbehinds rule that reading out.
CLEARANCE_PATTERNS = [re.compile(p) for p in (
    r'\b(?:sc|dv|nv1|nv2|ctc|security|government|secret|active|federal|dod)\s+clearances?\b',
    r'(?<!customs )(?<!medical )(?<!site )\bclearance (?:is )?required\b',
    r'\b(?:sc|dv|security)[- ]cleared\b',
    r'\bsecurity vetting\b',
)]


def is_dealbreaker(job_title: str, job_description: str) -> bool:
    """True when the advert contains a dealbreaker keyword or clearance demand."""
    text = f"{job_title or ''} {job_description or ''}".lower()
    if any(phrase in text for phrase in NEGATIVE_KEYWORDS):
        return True
    return any(pattern.search(text) for pattern in CLEARANCE_PATTERNS)

# ── Titles the candidate never applies to ───────────────────────────────────
# Three kinds of title are dropped before any scoring, whatever the score would
# have been, because no score makes them a fit. Titles are matched on the
# folded form (lower case, accents removed), so a local title matches written
# either way.

# Senior titles. Years of experience asked in the advert only lower the rank
# (experience_multiplier); a senior TITLE is dropped, because the hiring
# manager screens on it. "Manager" is deliberately absent: sales, account and
# project manager roles are wanted. "Lead" as in "lead generation" or "lead
# qualification" is the entry sales track, not seniority, so it is let through.
# "Staff development" is a graduate scheme, not a staff-level role.
SENIOR_LEVEL_PATTERN = re.compile(
    r'\b(?:senior|snr|sr|staff(?![\s-]+development)|principal|chief|directors?|svp|evp|avp|vp|'
    r'vice[- ]president|head of|team[- ]leaders?|'
    r'lead(?![\s-]+(?:(?:generation|gen|qualification)\b|development rep)))\b')


def is_senior_title(job_title: str) -> bool:
    """True for a senior, lead or executive title. Manager titles are not."""
    return bool(SENIOR_LEVEL_PATTERN.search(fold_diacritics(job_title or '')))


# Pure software-development roles the candidate does not want, matched on the
# title only: a sales or engineering advert often mentions software in its
# text. The list used to be exact substrings, which missed "front-end
# developer", "Java engineer", "DevOps specialist" and a bare "SRE". Dev titles
# used to survive when the CV score cleared 20, but the sector boost lifts any
# advert that says "cloud" or "platform", so that bar kept dev roles at SaaS
# firms. A dev title is now dropped outright, unless DEV_KEEP_PATTERN applies.
_DEV_ROLE = r'(?:developers?|engineers?|programm?ers?)'
_DEV_LANGUAGES = (
    r'java|javascript|typescript|python|ruby(?: on rails)?|rails|php|go|golang|'
    r'rust|scala|kotlin|swift|node(?:\.?js)?|react(?:\.?js)?|angular|vue(?:\.?js)?|'
    r'ios|android|web')
# "Engineering Manager" on its own is the title of a software team's manager.
# At an equipment maker the same words carry a discipline, and it can sit
# right before them ("Sales Engineering Manager", "Regional Engineering
# Manager") or anywhere else in the title ("Engineering Manager, Mission
# Mechanical", "Engineering Manager, Water Safety"). Only the first place
# used to count, so pump, valve and compressor makers' engineering managers
# in the measured batch were dropped as software roles.
#
# Words a software title also uses after the words count only right before
# them: "Engineering Manager, Design Systems" and "Engineering Manager, Data
# Quality" are software teams. _HARDWARE_WORDS, which software titles do not
# use, count anywhere.
_MANAGED_DISCIPLINES = (
    'sales', 'application', 'applications', 'solutions', 'field', 'service',
    'process', 'manufacturing', 'production', 'mechanical', 'electrical',
    'quality', 'plant', 'project', 'maintenance', 'industrial', 'design',
    'regional', 'reliability', 'controls', 'automation', 'validation',
    'facilities', 'facility', 'packaging', 'tooling', 'equipment', 'civil',
    'construction', 'structural',
)
_ENGINEERING_MANAGER = re.compile(
    r'\b' + ''.join(rf'(?<!\b{word} )(?<!\b{word}-)' for word in _MANAGED_DISCIPLINES)
    + r'engineering managers?\b')
_HARDWARE_WORDS = re.compile(
    r'\b(?:mechanical|electrical|electronics?|hardware|mechatronics?|avionics|'
    r'propulsion|drivetrain|thermal|fluids?|hydraulics?|pneumatics?|valves?|'
    r'actuators?|pumps?|compressors?|turbines?|hvac|piping|water|wastewater|'
    r'manufacturing|maintenance|sustaining|lean|continuous improvement|'
    r'commissioning|branch|launch|project)\b')


def _is_software_engineering_manager(title: str) -> bool:
    """True for an engineering manager title that names no hardware or
    plant discipline. `title` is already folded."""
    return bool(_ENGINEERING_MANAGER.search(title) and not _HARDWARE_WORDS.search(title))
PURE_DEV_TITLE_PATTERN = re.compile(
    r'\b(?:software|sw)[- ](?:development[- ])?' + _DEV_ROLE + r'\b'
    r'|\b(?:' + _DEV_LANGUAGES + r')[- ]' + _DEV_ROLE + r'\b'
    # "Mobile engineer" is also a travelling field-service role, so only the
    # developer and programmer forms count.
    r'|\b(?:mobile(?:[- ]app)?|app|application)[- ](?:developers?|programm?ers?)\b'
    # \b does not work next to "." "#" or "+", so a lookbehind stands in.
    r'|(?<![\w+#.])(?:asp\.net|vb\.net|\.net|c#|c\+\+)[- ]' + _DEV_ROLE + r'\b'
    r'|\b(?:front|back)[- ]?end(?:[- ]\w+)?[- ]' + _DEV_ROLE + r'\b'
    r'|\bfull[- ]?stack\b|\bdev(?:sec)?ops\b|\bsre\b|\bsite reliability\b'
    r'|\bdatabase administrator\b|\bdba\b'
    r'|\bplatform engineers?\b|\bembedded software\b|\bfirmware engineers?\b'
    r'|\bprogramm?ers?\b'
    # Families a measurement on a live batch found passing, October 2026:
    # data science and data engineering, machine learning, forward deployed
    # engineering, cloud and security engineering, and developer relations.
    # Engineering management is the same measurement's, checked on its own in
    # is_pure_dev_title because it needs the whole title.
    r'|\bdata scien(?:ce|tists?)\b'
    r'|\b(?:big[- ])?data (?:platform[- ]|warehouse[- ])?engineers?\b'
    r'|\b(?:machine learning|ml|mlops|deep learning)[- ]'
    r'(?:engineers?|scientists?|researchers?|developers?)\b|\bmlops\b'
    r'|\bforward[- ]deployed\b'
    # "Cloud Sales Engineer" and "Cloud Solutions Engineer" are presales
    # titles, so only the engineering kinds of cloud work are listed.
    r'|\bcloud[- ](?:(?:infrastructure|platform|security|devops|operations|ops|native|'
    r'software|data|network|systems?)[- ])?(?:engineers?|developers?)\b'
    r'|\b(?:cyber[- ]?)?security engineers?\b'
    r'|\bdeveloper (?:advocates?|relations|evangelists?|experience)\b|\bdevrel\b')

# Software titles that are wanted: QA and test automation, AI integration
# work, industrial control programming (PLC, SCADA, robots), which is
# automation engineering rather than software development, vehicle platform
# engineering, and CFD and simulation work, which is his engineering track.
DEV_KEEP_PATTERN = re.compile(
    r'\b(?:qa|quality assurance|tests?|tester|testing|sdet|'
    r'plc|scada|hmi|dcs|cnc|robot\w*|'
    r'vehicles?|chassis|powertrain|cfd|cae|fea|simulations?)\b')
_AI_TITLE_MARK = re.compile(r'\b(?:ai|llms?|genai|gen ai)\b')
_AI_TITLE_WORK = re.compile(r'\b(?:integrations?|implementation|solutions?|enablement|adoption)\b')


def is_pure_dev_title(job_title: str) -> bool:
    """True for a software-development title the candidate does not want."""
    title = fold_diacritics(job_title or '')
    if not (PURE_DEV_TITLE_PATTERN.search(title) or _is_software_engineering_manager(title)):
        return False
    if DEV_KEEP_PATTERN.search(title):
        return False
    return not (_AI_TITLE_MARK.search(title) and _AI_TITLE_WORK.search(title))


# Trade titles written in Serbian, Croatian or Bosnian: technicians,
# electricians and civil engineering. The stems only occur in South Slavic
# titles, so the rule is safe on every source, which also catches the same
# titles arriving through an aggregator. Off unless DROP_LOCAL_TRADE_TITLES
# is set, because which trades to rule out is a personal choice.
#
# A technician or electrician title is dropped whatever else it says:
# "Tehnicar odrzavanja" is a maintenance technician, not a maintenance
# engineer. "tehnicar" is not anchored on the left, so "elektrotehnicar" and
# "termotehnicar" are caught too. A civil-engineering word is dropped unless
# the title also names other work (mechanical, process, thermal, maintenance,
# sales, technical support, automation, inspection): "Komercijalista, prodaja
# gradjevinskog materijala" is a sales job selling building materials.
_LOCAL_TRADE_DROP = re.compile(r'tehnicar\w*|\belektricar\w*|\belektroinstalater\w*')
_LOCAL_CIVIL = re.compile(r'\bgra(?:dj|d)evin\w*')
_LOCAL_TRADE_KEEP = re.compile(
    r'\bmasinsk|\bprocesn|\btermo|\bodrzavanj|\bprodaj|\btehnick\w* podrsk|'
    r'\bautomatizacij|\binspektor')


def is_local_trade_title(job_title: str) -> bool:
    """True for a local technician, electrician or civil engineering title."""
    title = fold_diacritics(job_title or '')
    if _LOCAL_TRADE_DROP.search(title):
        return True
    return bool(_LOCAL_CIVIL.search(title) and not _LOCAL_TRADE_KEEP.search(title))


# The same trades written in English or German. A company careers board lists
# its technician jobs in English ("Facility Technician" at a plant in the home
# market), and its German sites list "Elektroniker" and "Servicetechniker"
# roles. None of those words is South Slavic, so the rule above missed them.
# Unlike the local rule, a title that also names an engineer is kept:
# "Technician / Engineer" and "Ingenieur oder Techniker" are partly
# engineering roles. "Engineering Technician" names no engineer, so it goes.
# "Anwendungstechniker", "Applikationstechniker" and "Verfahrenstechniker"
# are kept: the glossary in core/multilingual.py reads them as application
# and process engineering. So is a title that names sales or design work
# ("Vertriebstechniker", "Techniker im Vertrieb", "Konstrukteur / Techniker"),
# because technical sales and mechanical design are wanted.
_TRADE_DROP = re.compile(
    r'\btechnicians?\b|\belectricians?\b|\w*elektroniker\w*|\w*elektriker\w*|'
    r'\w*(?<!anwendungs)(?<!applikations)(?<!verfahrens)techniker\w*')
_TRADE_ENGINEER = re.compile(
    r'\bengineers?\b|ingenieur\w*|\binzenjer\w*|vertrieb\w*|\bsales\b|konstrukteur\w*')


def is_trade_title(job_title: str) -> bool:
    """True for a trade title: the local ones above, or a technician or
    electrician title in English or German that names no engineer."""
    if is_local_trade_title(job_title):
        return True
    title = fold_diacritics(job_title or '')
    return bool(_TRADE_DROP.search(title) and not _TRADE_ENGINEER.search(title))


# ── Entry-level software on the home-market board ────────────────────────────
# Junior and unranked software and QA roles on the local board are jobs the
# candidate can apply to, but they score low against an engineering and sales
# CV, so the dev-title rule above and the score cutoff dropped all of them.
# From these sources only, they skip both and are lifted to the regional
# digest's score bar (entry_software_floor: Config.REGIONAL_MIN_SCORE, never
# below ENTRY_SOFTWARE_FLOOR, the default bar), so they reach the regional
# message at the bottom of the list whatever the bar is set to. Senior
# titles, dealbreakers, export control and the other hard rules still apply.
# The same roles from global boards are dropped as dev titles, which keeps
# the main digest from filling with junior dev jobs.
#
# SENIOR_TITLE_PATTERN below is stricter than SENIOR_LEVEL_PATTERN on purpose:
# a software "architect" or "manager" is not an entry-level role, though a
# sales manager is a wanted one.
ENTRY_SOFTWARE_SOURCES = ('Infostud',)
ENTRY_SOFTWARE_FLOOR = 15


def entry_software_floor() -> float:
    """The score a kept entry-level software job is lifted to: the regional
    bar, read now, so raising the bar never silently drops these jobs."""
    return max(ENTRY_SOFTWARE_FLOOR, Config.REGIONAL_MIN_SCORE)


SOFTWARE_TITLE_PATTERN = re.compile(
    r'\b(developer|programer|programmer|software|tester|testing|qa|'
    r'front-?end|back-?end|full[- ]?stack)\b')
SENIOR_TITLE_PATTERN = re.compile(
    r'\b(senior|sr|lead|principal|staff|head|architect|director|manager)\b')


def is_entry_software_title(job_title: str) -> bool:
    """True for a software or QA title with no senior or lead marker."""
    title = (job_title or '').lower()
    return bool(SOFTWARE_TITLE_PATTERN.search(title)
                and not SENIOR_TITLE_PATTERN.search(title))


# ── Job functions the user never applies to ──────────────────────────────────
# A private list of title terms (Config.EXCLUDED_TITLE_TERMS): payroll, HR,
# recruiting, marketing and the like. A company careers board is read whole,
# not searched, and its adverts share paragraphs about AI, cloud and the
# platform, so a payroll role there matches as many CV skills as an
# engineering one. The score cannot tell them apart; the title can.
#
# A term matches as a whole word, so "hr" does not catch "Throughput", and a
# space in a term also matches a hyphen or a slash. Titles and terms are both
# folded (lower case, accents removed), so a local title matches written
# either way. Empty by default: the public engine excludes nothing.
@lru_cache(maxsize=32)
def _excluded_title_pattern(terms):
    parts = []
    for term in terms:
        folded = fold_diacritics(term).strip()
        if folded:
            words = (re.escape(word) for word in folded.split())
            parts.append(r'(?<![a-z0-9])' + r'[\s/-]+'.join(words) + r'(?![a-z0-9])')
    return re.compile('|'.join(parts)) if parts else None


def is_excluded_title(job_title: str, terms=None) -> bool:
    """True when the title names a job function the user has ruled out."""
    if terms is None:
        terms = getattr(Config, 'EXCLUDED_TITLE_TERMS', None) or ()
    pattern = _excluded_title_pattern(tuple(terms))
    return bool(pattern and pattern.search(fold_diacritics(job_title or '')))


def title_drop_reason(job_title: str, source: str = ''):
    """
    The reason a title alone rules a job out, as the filter's reason name, or
    None. Checked before anything else, since it needs no advert text.

    An entry-level software title from ENTRY_SOFTWARE_SOURCES is not a
    pure_programming drop. A senior one is still a senior_title drop. The
    trade rule runs only when Config.DROP_LOCAL_TRADE_TITLES is on, and then
    covers English and German trade titles too, on every source. Its reason
    keeps the name local_trade_title, which the setting's name also carries.
    """
    if is_senior_title(job_title):
        return 'senior_title'
    if getattr(Config, 'DROP_LOCAL_TRADE_TITLES', False) and is_trade_title(job_title):
        return 'local_trade_title'
    if is_pure_dev_title(job_title) and not (
            source in ENTRY_SOFTWARE_SOURCES and is_entry_software_title(job_title)):
        return 'pure_programming'
    if is_excluded_title(job_title):
        return 'excluded_title'
    return None

# ── Work-eligibility filter ───────────────────────────────────────────────────
# The candidate is not an EU or US national. A job anywhere is takeable only if
# he can legally work it: it offers visa sponsorship, or it does not require
# existing local work authorization (remote-global roles, or ones that simply
# do not restrict). A job that requires authorization he does not have and does
# not sponsor is dropped, wherever it is.
#
# The text is normalised first (see _normalise): lower case, straight
# apostrophes, and "U.S." written as "us", so one pattern covers every spelling
# and the dots inside "U.S." never end a sentence.

# Where a sponsorship sentence names a visa, a permit or immigration, it is
# about the right to work, not about sponsoring a football club or a
# certification. An offer needs this context, or one of the noun phrases in
# _OFFER_NOUN. Relocation help is deliberately NOT evidence: a relocation
# package moves someone who may already work there, and US adverts commonly
# offer one in the same breath as "must have current authorization to work in
# the United States".
_SPONSOR_ANCHOR = re.compile(r'\bsponsor(?:s|ed|ing|ship)?\b|\b(?:visa|immigration) support\b')
_VISA_CONTEXT = re.compile(
    r'\b(?:visas?|permits?|immigration|h-?1b\d?|skilled worker|blue card|'
    r'work authori[sz]ation|green cards?|tier 2|employment sponsorship)\b')
# A form field at the end of an advert: "Employment Sponsorship Offered: No",
# "Visa sponsorship available: No", "Sponsorship: Yes". The answer decides,
# because the label alone ("sponsorship offered") reads like an offer. The
# label must be short and must not ask about the candidate ("Requires
# sponsorship? No" is an answer on an application form, not the employer's).
_FIELD_ANSWER = re.compile(
    r'\s*(?:(?:is\s+)?(?:offered|available|provided|possible)\b)?\s*'
    r'[?:\-–]\s*(no|none|yes)\b(?!\s+matter)')
_FIELD_LABEL_MAX_WORDS = 4
_CANDIDATE_NEED = re.compile(r'\b(?:require|requires|required|need|needs|needed)\b')
# A caveat on an offer: "we aren't able to sponsor visas for every role",
# "sponsorship is not available for all positions", "not every role is
# eligible for sponsorship". The employer sponsors some roles, so this is
# neither an offer nor a refusal on its own; an offer elsewhere still counts.
_PARTIAL_CAVEAT_AFTER = re.compile(
    r'^\W*(?:[a-z\-]+\s+){0,3}?(?:for|to|in)\s+(?:every|all|each)\s+(?:[a-z\-]+\s+)?'
    r'(?:roles?|positions?|candidates?|applicants?|jobs?|cases?|openings?|vacancies|'
    r'hires?|locations?|countries)\b')
_PARTIAL_CAVEAT_BEFORE = re.compile(r'\bnot\s+(?:every|all|each)\b')
_OFFER_NOUN = re.compile(
    r'\bsponsorship (?:is |will be )?(?:available|provided|offered|possible)\b|'
    r'\b(?:offer|offers|offering|provide|provides|providing) (?:visa )?sponsorship\b')
_NEGATION = re.compile(
    r"\b(?:no|not|cannot|unable|without|never|unavailable|ineligible|nor|neither)\b|n't\b")
# Negation words that do not negate the offer: "including but not limited to
# visa sponsorship", "no matter where you are".
_NEGATION_NOISE = re.compile(r'\bnot limited to\b|\bno matter\b|\bwhether or not\b|\bnot only\b')
# A negation counts only inside the clause that holds the sponsorship word.
# Commas, colons and semicolons end a clause, except a comma inside a list:
# in "we do not offer relocation, visa sponsorship or remote work" the
# "not" still governs the sponsorship. A list item is a few words with none
# of the words that open a new clause.
_CLAUSE_OPENERS = re.compile(
    r'\b(?:we|you|they|it|if|who|which|that|but|so|because|while|our|your|'
    r'please|this|there|unless|when|where)\b')
# A new subject between the negation and the sponsorship word means the
# negation sits in an earlier clause about the candidate: "if you do not hold
# a permit we will sponsor your visa".
_NEW_SUBJECT = re.compile(r'\b(?:we|our company|the company|they)\b')
# "Sponsor" as a verb ("we cannot sponsor", "unable to sponsor"), as opposed
# to the noun in "executive sponsor" or "sponsor approval".
_SPONSOR_VERB_BEFORE = re.compile(
    r"(?:\bto|\bcan|\bcannot|\bcould|\bwill|\bwould|\bshall|\bmay|\bdo|\bdoes|"
    r"\bdid|n't|\bnot|\bnever|\bbe|\bwe|\bthey)\s*$")
# "No visa sponsorship is needed for EU citizens" says who needs none, not
# that the employer refuses.
_NOT_NEEDED_FOR = re.compile(r'^\s*(?:is\s+|are\s+|will\s+be\s+)?(?:needed|required|necessary)\s+for\b')
# After the sponsorship word, a refusal reads "... is not available",
# "... for this role is unavailable", "...: No". Only these filler words may
# sit between the two, so "sponsorship is available for candidates who do not
# hold a permit" stays an offer.
_FILLER = (r'(?:for|of|to|in|on|at|this|the|that|these|those|our|your|all|any|such|'
           r'work|employment|authori[sz]ation|role|roles|position|positions|job|jobs|'
           r'opportunity|candidates?|applicants?|visa|visas|status|purposes?|currently)')
_REFUSED_AFTER = re.compile(
    rf"^[\s:\-\u2013,]*(?:{_FILLER}\s+)*"
    r"(?:is|are|will|would|was|were|can|could|does|do|has|have|wo|ca)?\s*(?:be\s+)?"
    r"(?:not\b|unavailable\b|no\b|n't|cannot\b|never\b)")
# An application-form question pasted into the advert ("Will you now or in
# the future require sponsorship") is neither an offer nor a refusal.
_QUESTION_START = re.compile(
    r'^\W*(?:will|would|do|does|did|are|is|have|has|can|could)\s+'
    r'(?:you|the candidate|the applicant|applicants|candidates)\b')
_SENTENCE_ENDS = '.!?;\n\u2022'
REFUSAL_LOOKBACK_WORDS = 8

# Worldwide eligibility. In the location field the bare words count
# ("Worldwide", "Global", "Anywhere") when no country is named next to them.
# In the advert text only anchored phrases do, because "global" there is
# usually company boilerplate ("5 global offices", "serving customers
# worldwide") and used to override every lock.
WORLDWIDE_TEXT_PHRASES = (
    'remote worldwide', 'work from anywhere', 'anywhere in the world',
    'open to candidates worldwide', 'hire globally', 'hiring globally',
    'international candidates welcome', 'international applicants welcome',
    'open to international', 'hire internationally',
    'no visa required', 'no work permit required',
)
# The phrases that leave no doubt the role is open everywhere. "Work from
# anywhere" alone often means anywhere in one country, so it is not enough
# to outweigh a location that names a country.
STRONG_WORLDWIDE_PHRASES = tuple(p for p in WORLDWIDE_TEXT_PHRASES if p != 'work from anywhere')

# Citizenship: never obtainable on a work permit, so dropped even when the
# advert also offers sponsorship (that offer is for other roles or other
# people). "US citizen" counts only next to only / must / required, so an
# equal-opportunity line about "citizenship status" is not a lock.
CITIZENSHIP_LOCK_PHRASES = ('must be a citizen', 'eu citizens only', 'eu nationals only')
_US_CITIZEN = re.compile(r'\b(?:us|usa|united states)\s+citizens?(?:hip)?\b')
_LOCK_WORDS = re.compile(r'\b(?:only|must|required|requires|require|mandatory)\b')

# Region locks: residence or a national right to work. Dropped unless the
# advert offers sponsorship, which is the route around them. Word boundaries,
# because a substring "us only" also matched "we focus only on". The pronoun
# "us" after a verb ("contact us only via the form", "reach out to us only
# if") is not the country.
_PRONOUN_US = (r'(?<!contact )(?<!email )(?<!e-mail )(?<!call )(?<!message )'
               r'(?<!tell )(?<!let )(?<!join )(?<!out to )(?<!write to )(?<!talk to )'
               r'(?<!speak to )(?<!apply with )')
REGION_LOCK_PATTERNS = [re.compile(p) for p in (
    r'\bmust be a permanent resident\b',
    r'\bgreen card\b',
    r'\b(?:us|usa|united states|uk|canadian|australian) residents only\b',
    _PRONOUN_US + r'\b(?:us|usa|united states|canada|australia)[- ]only\b',
    r'\b(?:us|usa)[- ]based (?:candidates|applicants) only\b',
    r'\bright to work in the (?:uk|united kingdom)\b',
)]

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
    'authorization to work in', 'authorisation to work in',
]

# EU / EEA signals used to place a soft-locked role. Region words plus member
# states, matched against the location field first (reliable), then the text as
# a fallback.
EU_EEA_MARKERS = [
    'european union', 'eea', 'schengen', 'europe', 'emea',
    'austria', 'belgium', 'bulgaria', 'croatia', 'cyprus', 'czech', 'czechia',
    'denmark', 'estonia', 'finland', 'france', 'germany', 'greece', 'hungary',
    'ireland', 'italy', 'latvia', 'lithuania', 'luxembourg', 'malta',
    'netherlands', 'poland', 'portugal', 'romania', 'slovakia', 'slovenia',
    'spain', 'sweden', 'norway', 'iceland', 'liechtenstein',
]


def _normalise(text: str) -> str:
    """Lower case, straight apostrophes, and "U.S." / "U.S.A." as "us" / "usa"."""
    t = (text or '').lower().replace('\u2019', "'").replace('\u2018', "'")
    t = re.sub(r'\bu\.\s?s\.\s?a\.?(?![a-z])', 'usa', t)
    t = re.sub(r'\bu\.\s?s\.?(?![a-z])', 'us', t)
    return re.sub(r'\b(e\.g|i\.e)\.', lambda m: m.group(1).replace('.', ''), t)


def _sentence_bounds(text, start, end):
    """Start and end index of the sentence containing text[start:end]."""
    left = max(text.rfind(c, 0, start) for c in _SENTENCE_ENDS) + 1
    rights = [i for i in (text.find(c, end) for c in _SENTENCE_ENDS) if i != -1]
    return left, (min(rights) if rights else len(text))


def _clause_before(sentence_start: str) -> str:
    """The part of the sentence, up to the sponsorship word, that belongs to
    the same clause. See _CLAUSE_OPENERS for how a list comma is told apart."""
    cut = len(sentence_start)
    while True:
        idx = max(sentence_start.rfind(c, 0, cut) for c in ',:;')
        if idx == -1:
            return sentence_start
        item = sentence_start[idx + 1:cut]
        if (sentence_start[idx] == ',' and len(item.split()) <= 3
                and not _CLAUSE_OPENERS.search(item)):
            cut = idx
            continue
        return sentence_start[idx + 1:]


def _about_the_right_to_work(t, match, left, right, refusal=False) -> bool:
    """
    Whether a sponsorship mention is about a visa, a permit or immigration.

    An offer needs a visa word or an offer noun nearby. A refusal may also
    use the noun "sponsorship" or the verb ("we cannot sponsor"), but never
    the noun "sponsor": "report to the executive sponsor, not the line
    manager" and "no project proceeds without sponsor approval" refuse
    nothing.
    """
    word = match.group(0)
    if not word.startswith('sponsor'):
        return True
    near = t[max(left, match.start() - 60):min(right, match.end() + 60)]
    if _VISA_CONTEXT.search(near) or _OFFER_NOUN.search(near):
        return True
    if not refusal:
        return False
    return word == 'sponsorship' or bool(_SPONSOR_VERB_BEFORE.search(t[left:match.start()]))


def sponsorship_stance(text: str):
    """
    What an advert says about visa sponsorship: 'offer', 'refuse' or None.

    Each mention of sponsorship is read inside its own clause. A negation in
    the few words before it ("we cannot provide sponsorship", "we do not
    offer relocation or visa sponsorship"), or right after it ("visa
    sponsorship is not available", "sponsorship ... unavailable"), is a
    refusal, and any refusal wins. Otherwise a mention that names a visa,
    permit or immigration, or says sponsorship is available or provided, is
    an offer. Questions are skipped.

    A negation about the candidate is not a refusal: "If you are not an EU
    citizen, we offer visa sponsorship" and "Candidates without a work
    permit: we sponsor visas" are offers. Neither is a sponsor that has
    nothing to do with visas (see _about_the_right_to_work).

    A form field is read by its answer: "Employment Sponsorship Offered: No"
    is a refusal, though "sponsorship offered" alone reads as an offer. A
    refusal limited to some roles ("we aren't able to sponsor visas for every
    role") is a caveat, neither offer nor refusal, so "We do sponsor visas!"
    before it still makes the advert an offer.

    The old check looked for offer phrases anywhere, and "provide
    sponsorship" sits inside "we cannot provide sponsorship", so refusals
    were read as offers and the job was kept and lifted.
    """
    t = _normalise(text)
    offer = False
    for match in _SPONSOR_ANCHOR.finditer(t):
        left, right = _sentence_bounds(t, match.start(), match.end())
        if _QUESTION_START.match(t[left:right]):
            continue
        label = _clause_before(t[left:match.start()])
        field = _FIELD_ANSWER.match(t, match.end())
        if (field and len(label.split()) <= _FIELD_LABEL_MAX_WORDS
                and not _CANDIDATE_NEED.search(label)):
            if field.group(1) != 'yes':
                if _about_the_right_to_work(t, match, left, right, refusal=True):
                    return 'refuse'
            elif _about_the_right_to_work(t, match, left, right):
                offer = True
            continue
        if right < len(t) and t[right] == '?':
            continue
        after = _NEGATION_NOISE.sub(' ', t[match.end():right])
        if _NOT_NEEDED_FOR.match(after):
            continue
        before = _NEGATION_NOISE.sub(' ', label)
        before = ' '.join(before.split()[-REFUSAL_LOOKBACK_WORDS:])
        negations = list(_NEGATION.finditer(before))
        refused = ((negations and not _NEW_SUBJECT.search(before[negations[-1].end():]))
                   or _REFUSED_AFTER.match(after))
        if refused and (_PARTIAL_CAVEAT_AFTER.match(after)
                        or _PARTIAL_CAVEAT_BEFORE.search(before)):
            continue  # some roles are sponsored, see _PARTIAL_CAVEAT_AFTER
        if refused and _about_the_right_to_work(t, match, left, right, refusal=True):
            return 'refuse'
        if not refused and _about_the_right_to_work(t, match, left, right):
            offer = True
    return 'offer' if offer else None


# "Work from anywhere in the US" names a country, so it is not worldwide.
_PLACE_AFTER_ANYWHERE = re.compile(r'\s+(?:in|within|across)\s+(?!the world\b)')


def _worldwide_location(location: str) -> bool:
    """A location such as "Worldwide" or "Remote - Global" that names no
    country. "Anywhere in the United States" and "India - Anywhere" name
    one, so they are not worldwide."""
    codes = location_countries(location)
    return 'WORLDWIDE' in codes and not any(code in COUNTRY_NAMES for code in codes)


def _worldwide_text(text: str, phrases=WORLDWIDE_TEXT_PHRASES) -> bool:
    t = _normalise(text)
    for phrase in phrases:
        for found in re.finditer(re.escape(phrase), t):
            if 'anywhere' in phrase and _PLACE_AFTER_ANYWHERE.match(t, found.end()):
                continue
            return True
    return False


def is_worldwide(text: str, location: str = '') -> bool:
    """True for a worldwide or no-permit signal: a worldwide word in a
    location that names no country, or an anchored phrase in the advert
    text that is not followed by a place."""
    return _worldwide_location(location) or _worldwide_text(text)


# An alternative after "US citizenship" that a sponsored hire can meet: "US
# citizenship or work authorization". One he cannot meet (permanent
# residence, a green card) keeps the lock.
_CITIZEN_ALTERNATIVE = re.compile(
    r'\s*(?:status\s+)?,?\s*(?:or|and/or)\s+(?:[a-z\-]+\s+){0,3}?'
    r'(?:work authori[sz]\w*|authori[sz]\w* to work|eligib\w* to work|right to work)')


def _citizenship_locked(t: str) -> bool:
    if any(phrase in t for phrase in CITIZENSHIP_LOCK_PHRASES):
        return True
    for match in _US_CITIZEN.finditer(t):
        left, right = _sentence_bounds(t, match.start(), match.end())
        if _CITIZEN_ALTERNATIVE.match(t, match.end()):
            continue  # "verification of US citizenship or work authorization"
        window = t[max(left, match.start() - 60):min(right, match.end() + 40)]
        if _LOCK_WORDS.search(window):
            return True
    return False


@lru_cache(maxsize=16)
def _authorisation_pattern(codes):
    """
    "Must have current authorization to work in the United States", "U.S.
    work authorized", "eligible to work in the US", built from every name the
    country module reads as one of `codes`. None when there are no names.

    Also a remote role limited to one of those countries: "fully remote
    within the United States", "work from anywhere in the US", "must be
    based in the US", "US-based candidates". Each of these limits who may be
    hired to people already allowed to work there, which a sponsored hire is
    not on day one.
    """
    names = set()
    for code in codes:
        names |= names_for(code)
    if not names:
        return None
    alt = '|'.join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    place = rf'(?:the\s+)?(?:{alt})(?![a-z])'
    return re.compile(
        r'\b(?:authori[sz]ed|authori[sz]ation|eligible|eligibility|right|permitted|'
        r'permission|entitled)\s+(?:[a-z\-]+\s+){0,3}?to\s+(?:live\s+and\s+)?work\s+'
        rf'(?:[a-z\-]+\s+){{0,2}}?in\s+{place}'
        rf'|(?<![a-z])(?:{alt})\s+work\s+authori[sz](?:ed|ation)\b'
        rf'|\bwork\s+authori[sz]ation\s+(?:[a-z\-]+\s+){{0,2}}?(?:in|for)\s+{place}'
        rf'|\b(?:remote\b(?:\s+[a-z\-]+){{0,2}}?|anywhere)\s+(?:with)?in\s+{place}'
        rf'|\b(?:must|need to|needs to|required to|have to)\s+(?:be\s+)?'
        rf'(?:based|located|resident|reside|live|living|residing)\s+(?:with)?in\s+{place}'
        rf'|(?<![a-z])(?:{alt})[- ]based\s+(?:candidates|applicants|employees|hires|'
        r'remote|role|position)\b')


@lru_cache(maxsize=16)
def _region_pattern(terms):
    """One pattern for a tuple of region terms: each term as whole words,
    folded, with a space in a term also matching a hyphen."""
    parts = []
    for term in terms:
        words = fold_diacritics(term).split()
        if words:
            parts.append(r'(?<!\w)' + r'[\s-]+'.join(re.escape(w) for w in words) + r'(?!\w)')
    return re.compile('|'.join(parts)) if parts else None


def matches_region(location, terms):
    """True when the job's location names any of the region terms.

    Used by the regional Telegram digest to pick out home-market jobs (e.g.
    the Balkans) from stored listings. A term matches as whole words, with
    case and accents ignored: "Sel" matches "Šel, Serbia" and "Šel" matches
    "Sel". It used to be a plain substring test, and a three-letter town term
    matched inside "Cinisello Balsamo", so an Italian job reached the
    regional digest. Empty location or empty terms means no match, so the
    feature is inert until the user sets REGIONAL_MATCH_TERMS in their .env.
    """
    if not location or not terms:
        return False
    pattern = _region_pattern(tuple(terms))
    return bool(pattern and pattern.search(fold_diacritics(location)))


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
                      eligible_regions=None, sponsorship_only=None, allowed=None) -> bool:
    """Return True when the user could not legally take this job.

    Order, first match decides:
      1. a refusal to sponsor drops the job
      2. a citizenship lock (US citizens only, EU nationals only) drops it,
         even when the advert also offers sponsorship
      3. a region lock (right to work in the UK, US only, green card) drops
         it unless the advert offers sponsorship
      4. a sponsorship offer keeps it
      5. a requirement for existing work authorization in one of the
         sponsorship-only countries, or a remote role limited to one of them,
         drops it, whatever eligible_regions says, unless the job is located
         in an allowed country
      6. a worldwide signal keeps it
      7. a generic work-authorization requirement drops it unless the role is
         in a region the user can obtain a permit in
      8. nothing stated is treated as open

    Locks come before the worldwide signal on purpose. Boilerplate such as "5
    global offices" used to count as worldwide eligibility and hid a "Right to
    work in the UK" line in the same advert. For the same reason "work from
    anywhere" no longer outweighs "must have current authorization to work
    in the United States": only a real sponsorship offer does.

    eligible_regions: where the user can pursue work authorization. Defaults to
    Config.WORK_ELIGIBLE_REGIONS (read from the user's .env). Accepted values:
      - 'ANY' (or 'WORLDWIDE' / 'GLOBAL'): willing to pursue a permit anywhere,
        so a role asking only for generic authorization is kept wherever it is.
      - 'EU' / 'EEA': keep such a role only when it is EU/EEA-located.
      - empty: strict, the generic public default, a role gated on local
        authorization is kept only with explicit sponsorship or a worldwide /
        remote signal, never on region membership.
    sponsorship_only and allowed default to Config.SPONSORSHIP_ONLY_COUNTRIES
    and Config.ALLOWED_COUNTRIES, both empty in the public default.
    """
    if eligible_regions is None:
        eligible_regions = Config.WORK_ELIGIBLE_REGIONS
    if sponsorship_only is None:
        sponsorship_only = Config.SPONSORSHIP_ONLY_COUNTRIES
    if allowed is None:
        allowed = Config.ALLOWED_COUNTRIES
    regions = {r.upper() for r in (eligible_regions or [])}

    advert = f"{job_title or ''} {job_description or ''}"
    text = _normalise(f"{advert} {location or ''}")
    stance = sponsorship_stance(text)
    if stance == 'refuse':
        return True  # explicitly will not sponsor, cannot take
    if _citizenship_locked(text):
        return True  # citizenship, never satisfiable
    offers = stance == 'offer'
    if not offers and any(p.search(text) for p in REGION_LOCK_PATTERNS):
        return True  # residence or national right to work, and no sponsorship
    if offers:
        return False  # sponsored, takeable

    sponsorship_only = tuple(sorted({c.strip().upper() for c in sponsorship_only or ()
                                     if c.strip()}))
    pattern = _authorisation_pattern(sponsorship_only) if sponsorship_only else None
    if pattern and pattern.search(text):
        allowed_set = expand_codes(allowed)
        placed = location_countries(location, protect=allowed_set & US_STATE_CODES)
        if not placed & allowed_set:
            return True  # needs existing authorization where only sponsorship helps

    if is_worldwide(advert, location):
        return False  # worldwide / no permit needed, takeable

    if any(phrase in text for phrase in GEO_SOFT_LOCK_PHRASES):
        # Generic authorization requirement. Kept if the user can pursue a
        # permit where the role sits.
        if regions & {'ANY', 'WORLDWIDE', 'GLOBAL'}:
            return False  # willing to pursue authorization anywhere
        if (regions & {'EU', 'EEA'}) and _is_eu_located(location, text):
            return False
        return True
    return False  # unstated, assume open


# ── Country allow-list ────────────────────────────────────────────────────────
# A job located only in countries the user cannot work in is dropped, read
# from the location field alone (see core/countries.py). Two settings, both
# empty and so inert in the public default:
#   ALLOWED_COUNTRIES          countries and regions the user can work in
#   SPONSORSHIP_ONLY_COUNTRIES countries worth a job only with a visa offer
# A location that names no country ("Remote", a bare city) is not judged
# here: the work-eligibility text rules above decide it. Neither is a
# worldwide location that names no country ("Worldwide", "Remote - Global").
# A worldwide word next to a country ("US - Remote (Anywhere)", "Anywhere in
# the United States", "India - Anywhere") is judged on the country.
#
# A placeholder ("City-State-Country", "2 Locations") names no place at all,
# so the countries the advert itself names stand in for it: kept when one of
# them is allowed or none is named, judged on them otherwise. Only a
# placeholder is read this way. A real place, even one the country module
# cannot place, is not second-guessed by the advert, whose country names are
# often customers or offices.
def is_outside_allowed_countries(job: dict, allowed=None, sponsorship_only=None,
                                 text_ready: bool = True) -> bool:
    """
    True when the job's location names only countries the user cannot take.

    A remote job whose advert says plainly that it is open worldwide ("work
    from anywhere in the world", "hiring globally") is kept: its location
    often carries the company's country ("Remote (United States)"), which
    says nothing about where the hire may live.

    text_ready=False is for the screen before descriptions are fetched: a job
    in a sponsorship-only country is not judged yet, because the evidence
    that it sponsors is in the text still to come.
    """
    if allowed is None:
        allowed = Config.ALLOWED_COUNTRIES
    if sponsorship_only is None:
        sponsorship_only = Config.SPONSORSHIP_ONLY_COUNTRIES
    allowed_set = expand_codes(allowed)
    sponsor_set = {c.strip().upper() for c in sponsorship_only or () if c.strip()}
    if not allowed_set and not sponsor_set:
        return False

    location = job.get('location') or ''
    advert = f"{job.get('title') or ''} {job.get('description') or ''}"
    codes = location_countries(location, protect=allowed_set & US_STATE_CODES) - {'WORLDWIDE'}
    if not codes and is_placeholder_location(location):
        named = text_countries(advert)
        if named & allowed_set:
            return False
        codes = {code for code in named if code in COUNTRY_NAMES}
    if not codes:
        return False  # unplaced, or open worldwide: left to the text rules
    if codes & allowed_set:
        return False
    if (text_ready and is_remote_place(location)
            and _worldwide_text(advert, STRONG_WORLDWIDE_PHRASES)):
        return False
    if codes & sponsor_set:
        if not text_ready:
            return False
        return sponsorship_stance(advert) != 'offer'
    return bool(allowed_set)


def fails_eligibility(title, description, location):
    """
    The first hard rule a job breaks, as the filter's reason name, or None.

    The same rules filter_jobs applies before storing a job. The digest runs
    this again on stored rows just before sending, so a rule added today also
    stops rows stored before it.
    """
    title, description, location = title or '', description or '', location or ''
    if is_dealbreaker(title, description):
        return 'dealbreaker'
    if is_export_controlled(f"{title} {description}"):
        return 'export_controlled'
    if is_geo_restricted(title, description, location):
        return 'geo_restricted'
    if is_excluded_location(location):
        return 'excluded_location'
    if is_outside_allowed_countries(
            {'title': title, 'description': description, 'location': location}):
        return 'outside_allowed_countries'
    return None

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
    advert = f"{job.get('title') or ''} {job.get('description') or ''}"
    if (sponsorship_stance(advert) == 'offer'
            or is_worldwide(advert, job.get('location', ''))):
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
    advert = f"{job.get('title') or ''} {job.get('description') or ''}"
    text = f"{advert} {location}".lower()
    # Only a real visa offer cancels the downrank. A refusal or a relocation
    # package used to cancel it too, which lifted US boards over EU ones.
    if sponsorship_stance(advert) == 'offer' or is_worldwide(advert, location):
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
    sponsorship. 1.0 leaves the score unchanged. A refusal or a relocation
    package earns nothing, see sponsorship_stance."""
    text = (f"{job.get('title') or ''} {job.get('description') or ''} "
            f"{job.get('company') or ''}")
    if sponsorship_stance(text) == 'offer':
        return SPONSORSHIP_BOOST
    return 1.0


# ── Export control ────────────────────────────────────────────────────────────
# Roles restricted under US export-control law (ITAR, the Export Administration
# Regulations) can only be filled by a "U.S. person", which a non-US national on
# a work visa is not. These are dropped outright. An ordinary US role is kept:
# most US postings say nothing about sponsorship even when an employer would
# sponsor, so only an explicit exclusion is treated as one.
#
# Regular expressions on word boundaries rather than substrings. "EAR" alone
# is never matched, since it would hit "year", "clear" and "ear for detail",
# and "us person" must not fire on "contact us personally" or "focus person".
EXPORT_CONTROL_PATTERNS = [re.compile(p) for p in (
    r'\bitar\b',
    r'\bu\.?\s?s\.?\s?persons?\b',   # U.S. person, U.S person, US person
    r'\bexport administration regulations\b',
    r'\bear[- ]controlled\b',
    r'\bsubject to the ear\b',
    r'\bexport[- ]controll?ed\b',
    r'\bexport controls?\b',
)]


def is_export_controlled(text: str) -> bool:
    """True when a posting explicitly limits the role to US persons under
    export-control rules, which a non-US national can never satisfy."""
    t = (text or '').lower()
    return any(p.search(t) for p in EXPORT_CONTROL_PATTERNS)


# ── Required experience ───────────────────────────────────────────────────────
# Years of experience lower a job's rank. The candidate has a little under
# three years, so up to two years is a full match, three or four is a stretch
# worth only a slight nudge, and five or more is a real gap. A requirement
# marked preferred or a plus counts for half the penalty. Above
# Config.MAX_REQUIRED_YEARS, when it is set, a requirement drops the job.
#
# Only a number in the same sentence as the word "experience" (Serbian
# "iskustvo", German "Erfahrung") is read, and it must sit within 45
# characters before or 60 after it. Skipped:
#   - the company describing itself: "founded 20 years ago", "we have 30
#     years of experience", "With over 10 years of experience, Acme is a
#     leader", "Sa vise od 10 godina iskustva, nasa firma je lider"
#   - an upper limit: "up to 5 years", "less than 5 years", "do 5 godina"
#   - years that measure something else: a degree, a contract, a programme
#     ("a 4 year college degree", "a 5 year contract", "the programme lasts
#     between two and four years"), or a point in the future ("within 3 years")
_WORD_NUMBERS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6,
    'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'twelve': 12,
    'fifteen': 15, 'twenty': 20,
}
_NUM = r'(\d{1,2}|' + '|'.join(_WORD_NUMBERS) + r')'
# "five (5) years": the digits repeated in brackets.
_IN_BRACKETS = r'(?:\s*\(\d{1,2}\))?'
# "5 or more years", "5 ili vise godina", "3 i vise godina".
_OR_MORE = r'(?:\s+(?:or|and|i|ili|oder|und)\s+(?:more|above|vise|preko|mehr))?'
# "3-5", "3 to 5", "3 do 5", "between 3 and 5", "3 bis 5". The en dash, em
# dash and minus sign count as a hyphen.
_RANGE_SEP = r'(?:\s*[-\u2013\u2014\u2212]\s*|\s+(?:to|or|and|do|ili|i|bis)\s+)'
_YEARS_RE = re.compile(
    r'(?<![\d.,])\b' + _NUM + _IN_BRACKETS + r'\s*(?:\+|plus)?' + _OR_MORE
    + r'(?:' + _RANGE_SEP + _NUM + _IN_BRACKETS + r'\s*\+?)?'
    r'[\s-]*(?:years?|yrs?|godin[aeu]?|god|jahre?n?)\b')
_EXPERIENCE_WORDS = ('experience', 'iskustv', 'erfahrung')
_MORE_THAN = r'(?:(?:over|more than|almost|nearly|vise od|preko|skoro|uber|mehr als)\s+)?'
# Words that put the number in the company's own history. The first group
# may sit anywhere in the 45 characters before the number. The second must
# lead straight into it, so "we have an opening for an engineer with 5 years
# of experience" is still read as a requirement.
_COMPANY_ABOUT_ITSELF = re.compile(
    r"\b(?:founded|established|in business|history of|zahvaljujuci|osnovan\w*|"
    r"posluje\w*|postoji\w*|gegrundet)\b"
    r"|\b(?:our|nas\w*|unser\w*)\s+(?:\w+\s+)?(?:experience|iskustv\w*|erfahrung)\b"
    r"|\b(?:we have|we've|we bring|we've got|imamo|wir haben|seit)\s+" + _MORE_THAN + r"$"
    r"|\b(?:company|group|firm|kompanij\w*|firm\w*|preduzec\w*)\s+"
    r"(?:has|have|brings?|with|sa|ima|mit|hat)\s+" + _MORE_THAN + r"$")
# A sentence that opens "With over 10 years of experience," and goes on to
# name a subject other than the candidate is the company about itself.
_SELF_OPENING = re.compile(r'^\W*(?:with|sa|mit)\s+' + _MORE_THAN + r'$')
_NOT_THE_CANDIDATE = re.compile(
    r'^[^,]*,\s*(?!you\b|your\b|the (?:ideal |right |successful )?candidate|'
    r'vi\b|ti\b|sie\b|du\b|kandidat)\w')
_UPPER_LIMIT = re.compile(
    r'(?:\bup to|\bless than|\bfewer than|\bunder|\bmax(?:imum|imal)?(?:\s+of)?|\bno more than|'
    r'\bnot more than|\bat most|\bdo|\bnajvise|\bmaksimalno|\bmanje od|\bbis zu|'
    r'\bhochstens|\bweniger als|<)\s*$')
# Years of a degree, a contract or a programme, read from the words after
# the number ("4 year college degree", "4 years of university studies", "5
# year contract") or before it ("Bachelor's degree (4 years)", "will last
# between two and four years", "within 3 years").
_NOT_EXPERIENCE_AFTER = re.compile(
    r"\s*(?:(?:of\s+)?(?:(?:engineering|technical|university|college|academic|"
    r"higher|vocational|full[\s-]time)\s+)?(?:college|university|degree|bachelor\w*|"
    r"undergraduate|diplom\w*|programmes?|programs?|apprenticeships?|course|"
    r"stud(?:y|ies)|studij\w*|school)\b|(?:contract|warranty|guarantee|fixed[\s-]term)\b)")
_NOT_EXPERIENCE_BEFORE = re.compile(
    r"(?:\b(?:degree|diplom\w*|bachelor\w*|stud(?:y|ies)|studij\w*|programmes?|"
    r"programs?|course|apprenticeship)\s*\(\s*"
    r"|\b(?:within|after|over the next|in the next|for the next|nakon|u roku od|"
    r"innerhalb(?: von)?|nach)\s+)$"
    r"|\b(?:lasts?|lasting|duration|traje\w*|dauer\w*)\b")
_PREFERRED_MARKERS = (
    'preferred', 'nice to have', 'nice-to-have', 'a plus', 'an advantage',
    'advantageous', 'ideally', 'desirable', 'bonus',
)
# The same, in the languages an advert is written in. Shared with the
# language rule below.
LANGUAGE_PREFERRED_MARKERS = _PREFERRED_MARKERS + (
    'preferabl', 'beneficial', 'an asset', 'optional', 'von vorteil',
    'wunschenswert', 'idealerweise', 'ein plus', 'pluspunkt', 'gerne gesehen',
    'een pre', 'un atout', 'un plus', 'prednost', 'pozeljn',
)
# Matched at the start of a word, so "between previous" is not "een pre".
_PREFERRED_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(m) for m in LANGUAGE_PREFERRED_MARKERS) + r')')
EXPERIENCE_TIERS = (  # (at least this many years, multiplier)
    (8, 0.75),
    (5, 0.85),
    (3, 0.95),
)
# No advert requires more than 15 years. A larger number is the company
# describing itself in words _COMPANY_ABOUT_ITSELF does not know.
_MOST_YEARS_REQUIRED = 15
# An alternative route joined by "or", with no comma or semicolon between
# the "or" and the second route: "2+ years of experience, or 4+ years",
# "Bachelor's with 5 years or Master's with 3 years".
_ROUTE_OR = re.compile(r'\b(?:or|ili|oder|alternatively)\b[^,;]{0,30}$')


def _undot(text):
    """
    Folded text with the full stops of common abbreviations removed, so a
    split into sentences does not cut "min. 5 years", "B.Sc. in Electrical"
    or "Electrical Eng. or Mechanical Eng." apart.
    """
    text = re.sub(r'\b([bm])\.\s?(sc|eng|tech)\b\.?', r'\1\2', text)
    text = re.sub(r'\b([bm])\.([as])\.', r'\1\2', text)
    text = re.sub(r'\be\.g\.', 'eg', text)
    text = re.sub(r'\bi\.e\.', 'ie', text)
    text = re.sub(r'\bz\.\s?b\.', 'zb', text)
    return re.sub(r'\b(min|max|mind|ca|cca|approx|npr|eng|dipl|ing|incl|inkl|bzw|evtl|'
                  r'yrs|god)\.', r'\1', text)


def _years_value(token):
    return int(token) if token.isdigit() else _WORD_NUMBERS[token]


def _tier_multiplier(years):
    for floor, mult in EXPERIENCE_TIERS:
        if years >= floor:
            return mult
    return 1.0


def _sentence_bounds(text, start, end):
    """
    Start and end of the sentence (or line) containing text[start:end]. A
    heading line that ends in a colon ("Experience:", "Nice to have:") is
    read with the line under it.
    """
    left = max(text.rfind('.', 0, start), text.rfind('\n', 0, start)) + 1
    if left > 0 and text[left - 1] == '\n':
        heading_end = len(text[:left - 1].rstrip())
        if heading_end and text[heading_end - 1] == ':':
            left = max(text.rfind('.', 0, heading_end), text.rfind('\n', 0, heading_end)) + 1
    rights = [i for i in (text.find('.', end), text.find('\n', end)) if i != -1]
    return left, min(rights) if rights else len(text)


def _marked_preferred(text, left, right, start, end):
    """
    True when the years at text[start:end] are marked preferred or a plus.

    Read on the part of the sentence between commas or semicolons, so
    "Minimum 5 years of experience in sales, knowledge of SAP is a plus"
    still requires the five years. A heading ("Nice to have: ...") marks the
    whole sentence, and so does a marker that opens the next part ("5 years
    of experience in sales, preferred").
    """
    heading = text[left:right].split(':', 1)
    if len(heading) == 2 and _PREFERRED_RE.search(heading[0]) and left + len(heading[0]) < start:
        return True
    part_left = max(text.rfind(',', left, start), text.rfind(';', left, start), left - 1) + 1
    stops = [i for i in (text.find(',', end, right), text.find(';', end, right)) if i != -1]
    part_right = min(stops) if stops else right
    if _PREFERRED_RE.search(text[part_left:part_right]):
        return True
    if part_right < right:
        following = text[part_right + 1:right]
        return bool(_PREFERRED_RE.match(following.strip()))
    return False


def _experience_mentions(text: str):
    """
    The experience requirements in the text, as (folded text, mentions).
    Each mention is (years, preferred, start, end, sentence start). A range
    such as "3-5 years" counts as its lower bound, since that is what the
    employer accepts.
    """
    t = _undot(fold_diacritics(text or ''))
    mentions = []
    for m in _YEARS_RE.finditer(t):
        left, right = _sentence_bounds(t, m.start(), m.end())
        before = t[max(left, m.start() - 45):m.start()]
        after = t[m.end():min(right, m.end() + 60)]
        if not any(word in before or word in after for word in _EXPERIENCE_WORDS):
            continue
        if after.lstrip().startswith('ago') or _COMPANY_ABOUT_ITSELF.search(before):
            continue
        if _SELF_OPENING.match(t[left:m.start()]) and _NOT_THE_CANDIDATE.match(t[m.end():right]):
            continue
        if (_UPPER_LIMIT.search(before) or _NOT_EXPERIENCE_AFTER.match(after)
                or _NOT_EXPERIENCE_BEFORE.search(before)):
            continue
        mentions.append((_years_value(m.group(1)),
                         _marked_preferred(t, left, right, m.start(), m.end()),
                         m.start(), m.end(), left))
    return t, mentions


def required_experience(text: str):
    """Return (years, preferred) for the most demanding experience requirement
    in the text, or (None, False) when there is none."""
    best = None  # (multiplier, years, preferred)
    for years, preferred, *_ in _experience_mentions(text)[1]:
        mult = _tier_multiplier(years)
        if preferred:
            mult = 1 - (1 - mult) / 2
        if best is None or mult < best[0] or (mult == best[0] and years > best[1]):
            best = (mult, years, preferred)
    if best is None:
        return None, False
    return best[1], best[2]


def experience_multiplier(job: dict) -> float:
    """Score multiplier for the experience a posting asks for. 1.0 leaves the
    score unchanged. Never drops a job, only lowers its rank."""
    years, preferred = required_experience(job.get('description', ''))
    if years is None:
        return 1.0
    mult = _tier_multiplier(years)
    return 1 - (1 - mult) / 2 if preferred else mult


def requires_too_many_years(text: str, limit=None) -> bool:
    """
    True when the advert requires more years of experience than `limit`.

    limit defaults to Config.MAX_REQUIRED_YEARS, and None never drops. Only a
    requirement counts: years marked preferred or a plus are skipped. When
    the same sentence offers a route within the limit, joined by "or" ("2+
    years of experience, or 4+ years"), the lower route decides.
    """
    if limit is None:
        limit = getattr(Config, 'MAX_REQUIRED_YEARS', None)
    if limit is None:
        return False
    t, mentions = _experience_mentions(text)
    required = [m for m in mentions if not m[1] and m[0] <= _MOST_YEARS_REQUIRED]
    for years, _, start, end, left in required:
        if years <= limit:
            continue
        if any(o_left == left and o_years <= limit
               and _ROUTE_OR.search(t[min(end, o_end):max(start, o_start)])
               for o_years, _, o_start, o_end, o_left in required):
            continue
        return True
    return False


# ── Electrical-only degree ────────────────────────────────────────────────────
# An advert whose degree requirement names electrical or electronics
# engineering and nothing a mechanical engineer holds: "BSc/MSc in Electrical
# / Electronics Engineering", "Diplomirani inzenjer elektrotehnike". The
# electrical discipline must be the first one named after the degree word, so
# "degree in engineering and knowledge of electrical systems" is kept.
#
# The job is kept when the advert offers an alternative:
#   - a mechanical engineer named anywhere in the advert ("Mechanical
#     engineers are also welcome", "masinski fakultet")
#   - a second discipline or a generic alternative in any sentence that names
#     a degree ("or Computer Science", "or a related field", "or another
#     technical discipline", "ili srodne oblasti")
#   - an electrical degree marked preferred or a plus
# "Or equivalent" alone is not an alternative, since it means equivalent to
# the electrical degree.
_DEGREE_WORD = (r"(?:(?<!\d-)(?<!\d )degree|bachelor\w*|master(?:'|\u2019)?s\b|"
                r"master\s+(?:degree|of|in)\b|bsc|msc|beng|meng|(?:bs|ms)(?=\s+in\b)|"
                r"diplom\w*|graduate|studium|studij\w*)")
_ELECTRICAL = r'(?:electrical|electronics?|electrotechn\w*|elektro\w*|elektrotehn\w*)'
_DEGREE_FILLER = (r'(?:in|of|from|iz|the|a|an|school|faculty|engineering|engineer|'
                  r'inzenjer\w*|ingenieur\w*|fakultet\w*|or|and|/|bsc|msc|science|'
                  r'sciences|applied|der|des|power|\(\w+\))')
_ELECTRICAL_DEGREE = re.compile(
    rf'\b{_DEGREE_WORD}(?:[\s/:,(-]+{_DEGREE_FILLER}){{0,5}}[\s/:,(-]+{_ELECTRICAL}\b'
    rf'|\b{_ELECTRICAL}\s+(?:engineering\s+)?(?:fakultet\w*|faculty|degree|diplom\w*|studium)\b')
# A sentence that names a degree in any form, for the alternative check.
_DEGREE_LINE = re.compile(
    r'\b(?:degree|bachelor\w*|master\w*|bsc|msc|beng|meng|diplom\w*|graduate|studium|'
    r'studij\w*|fakultet\w*|faculty|qualification|stepen|sprem\w*|obrazovanj\w*)')
# A second discipline is named as a field of study ("Computer Science",
# "Process Engineering"), so "computer skills" or "the chemical industry" in
# the same sentence is not read as an alternative.
_OTHER_DISCIPLINE = re.compile(
    r'mechanic\w*|mechatronic\w*|meh?atronik\w*|masin\w*|strojars\w*|maschinenbau|'
    r'related|similar|srodn\w*|slicn\w*|any engineering|technical field|'
    r'\b(?:other|another|comparable|vergleichbar\w*|drug(?:i|e|a|ih|om)|'
    r'(?:process|chemical|industrial|energy|computer|software|civil|production)\s+'
    r'(?:engineering|science|technology)|chemistry|physics|fizik\w*|'
    r'informati(?:cs|ka|k|on technology|on systems)|racunarstv\w*|'
    r'(?:hemijsk|tehnolosk|racunarsk|gradjevinsk)\w*\s+(?:fakultet\w*|inzenjer\w*|nauk\w*)|'
    r'tehnick\w* (?:smer\w*|fakultet\w*|nauk\w*))\b')
_MECHANICAL_NAMED = re.compile(
    r'\b(?:mechanical|mechatronics?)\s+engineer(?:s|ing)?\b|\bmasinstv\w*|'
    r'\bmasinsk\w*\s+(?:fakultet\w*|inzenjer\w*)|\bstrojarstv\w*|\bmaschinenbau\w*')


def requires_electrical_degree(text: str) -> bool:
    """
    True when a degree requirement names only electrical or electronics
    engineering. Off unless Config.DROP_ELECTRICAL_ONLY_DEGREE is set.
    """
    if not getattr(Config, 'DROP_ELECTRICAL_ONLY_DEGREE', False) or not text:
        return False
    t = _undot(fold_diacritics(text))
    if _MECHANICAL_NAMED.search(t):
        return False
    if any(_DEGREE_LINE.search(clause) and _OTHER_DISCIPLINE.search(clause)
           for clause in _CLAUSE_SPLIT.split(t)):
        return False
    # _clauses leaves out the parts of a sentence marked preferred or a plus.
    return any(_ELECTRICAL_DEGREE.search(clause) for clause in _clauses(t))


def content_drop_reason(title: str, description: str):
    """
    The reason the advert text rules a job out on years or degree, as the
    filter's reason name, or None. Shared by the filter and the digest's
    recheck of stored rows.
    """
    if requires_too_many_years(description):
        return 'experience_required'
    if requires_electrical_degree(description):
        return 'electrical_degree'
    return None


# ── Required languages ────────────────────────────────────────────────────────
# An advert that requires a language at a level the user does not have
# ("fluent German", "German C1", "sehr gute Deutschkenntnisse",
# "verhandlungssicher", "Deutsch als Muttersprache") is dropped. A lower level
# ("German B1", "German at B2 level", "gute Deutschkenntnisse") is kept, and
# so is a language named only as a plus or as an alternative to English
# ("Deutsch C1 oder Englisch C1"). Which languages count is personal:
# Config.NON_FLUENT_LANGUAGES, empty by default, and empty means this rule
# never drops anything.
#
# A heuristic, read one sentence or bullet at a time on the folded text. A
# sentence that marks something as preferred is split again at its commas, so
# "Fluent German, English is a plus" still asks for fluent German, while a
# requirement and its plus marker in the same comma-free clause are skipped
# together; that errs on the side of sending. A sentence describing the team
# ("our team speaks German fluently") is skipped, and so is the language used
# as an adjective ("excellent German-engineered pumps", "a native German team").
#
# The names below are the spellings an advert uses for each language, in
# English, German, Dutch, French, Turkish and the South Slavic languages. A
# configured language missing here matches only its own name.
LANGUAGE_FORMS = {
    'german': r'german(?!y)|deutsch(?!land)|nemack|njemack',
    'dutch': r'dutch|nederlands|niederlandisch|holandsk',
    'french': r'french|francais|franzosisch|francusk',
    'turkish': r'turkish|turkce|turkisch|tursk',
    'korean': r'korean|koreanisch|korejsk',
    'arabic': r'arabic|arabisch|arapsk',
}
_ENGLISH = r'\b(?:english|englisch\w*|engleski\w*|engelsk\w*|engels|anglais|ingilizce)'
# A high level written before the language: "fluent German",
# "Verhandlungssichere Deutschkenntnisse", "tecno znanje nemackog jezika",
# "akici Turkce". fold_diacritics leaves the German sharp s and the Turkish
# dotless i alone, so both spellings are listed. "Proficient German speaker"
# and "proficiency in German" ask for a working command, unless a lower level
# comes first ("basic proficiency in German") or after ("at B1 level").
_HIGH_BEFORE = (
    r'(?<!basic )(?<!elementary )(?<!limited )(?<!some )(?<!intermediate )'
    r'proficien(?:t|cy)|'
    r'fluen(?:t|cy|tly)|(?<!non-)(?<!non )native|mother tongue|excellent\w*|'
    # The noun only before a level word, "Excellence knowledge of Dutch", so
    # "operational excellence in Dutch operations" is not a requirement.
    r'excellence(?=\s+(?:knowledge|command|skills?|proficiency|level))|'
    r'perfect|very good|(?<!bis )sehr gut\w*|verhandlungssicher\w*|'
    r'flie(?:ss|ß)end\w*|ausgezeichnet\w*|exzellent\w*|hervorragend\w*|'
    r'muttersprachlich\w*|perfekt\w*|vloeiend\w*|uitstekend\w*|maitrise|'
    r'parfait\w*|bilingu\w*|zweisprachig\w*|tec(?:no|nim|an|na)\w*|odlicn\w*|'
    r'vrlo dobr\w*|ak[iı]c[iı]\w*')
# Words that may sit between the level and the language: "excellent command of
# the German language", "excellent written and spoken German".
_LEVEL_FILLER = (
    r'(?:in|of|the|a|an|and|written|spoken|oral|verbal|command|knowledge|skills?|'
    r'proficiency|language|level|speaker|znanje|znanjem|poznavanje|kenntnisse|'
    r'sprachkenntnisse|jezik\w*|sprache|beheersing|van|het|de|du|la|u)')
# A list before the language: "fluent in English and German".
_LIST_ITEM = r'[a-z\-]+\s*(?:,|/|&|(?:and|und|i|et|en)\b)\s*'
# Words a requirement puts after the language, before its level: "German
# language skills (B2)", "German at C1 level", "Deutsch auf B2-Niveau",
# "Deutsch als Muttersprache", "German (at least C1)", "Deutsch mind. C1".
_SKILL_WORD = (r'(?:languages?|skills?|kenntnisse|sprachkenntnisse|jezik\w*|sprache|'
               r'proficiency|speaking|speakers?|level|taal)')
_PREPOSITION = r'(?:at|on|auf|na|op|in|au|als)\s+(?:a\s+)?'
_AT_LEAST = r'(?:at least|mindestens|mind|min|minimum)\s+'
_LEVEL_LEAD = (rf'[\w-]*(?:\s+{_SKILL_WORD}){{0,2}}\s*[(:\-–,]?\s*'
               rf'(?:{_PREPOSITION})?(?:{_AT_LEAST})?')
# A lower level after the language undoes the match: "Excellent English,
# German B1", "Fluent English and German at B2 level", "Fluent English and
# German, B2 minimum" ask for B1 or B2 German.
_LOW_TAIL = (_LEVEL_LEAD
             + r'(?:a[12]|b[12]|basic|good|gute|grund\w*|osnovn\w*|dobr\w*|'
               r'elementary|beginner|conversational|intermediate|speaking)\b')
# The language as an adjective: "German-engineered pumps", "a native German
# team", "German customers".
_ADJECTIVE_USE = (
    r'\w*[\s-]+(?:engineer\w*|made|built|quality|teams?|company|companies|'
    r'customers?|clients?|markets?|owned|manufactur\w*|technolog\w*|precision|'
    r'brands?|standards?|subsidiar\w*|headquarters?|offices?|sites?|plants?|'
    r'colleagues?|parent|machinery|industry|industrie)\b')
# A high level written after the language: "German (C1)", "German at C1
# level", "Deutsch auf C1-Niveau", "Deutsch- und Englischkenntnisse
# verhandlungssicher", "Francais courant", "Nemacki jezik - C1".
_HIGH_AFTER = (
    r'fluen(?:t|cy|tly)\b|c[12]\b|native|mother tongue|verhandlungssicher\w*|'
    r'flie(?:ss|ß)end\w*|muttersprach\w*|courant|moedertaal\w*|vloeiend\w*|'
    r'excellent\w*|perfe(?:ct|kt)\w*|sehr gut\w*|tec(?:no|nim|an|na)\w*|odlicn\w*')
_CLAUSE_SPLIT = re.compile('[.\n\r;|!?•·●▪]+')
# Abbreviations whose full stop would split "Deutschkenntnisse (mind. C1)".
_ABBREVIATION_DOT = re.compile(r'\b(mind|min|bzw|ca|evtl|inkl)\.')
# A sentence about the team rather than the candidate.
_TEAM_DESCRIPTION = re.compile(
    r'\b(?:our|the)\s+(?:\w+\s+){0,2}?(?:team|teams|colleagues|staff|employees|'
    r'engineers|people|company|office)\s+(?:speaks?|is|are|works?)\b|\bwe (?:all )?speak\b')
_OR_WORD = r'\b(?:or|oder|ili|ou|bzw)\b'


@lru_cache(maxsize=8)
def _language_patterns(names):
    """The compiled shapes for a tuple of folded language names: a high
    level before the language, a high level after it, and the language
    offered as an alternative to English."""
    forms = '|'.join(LANGUAGE_FORMS.get(name, re.escape(name)) for name in names)
    lang = rf'\b(?:{forms})'
    before = re.compile(
        rf'\b(?:{_HIGH_BEFORE})(?:[- ](?:level|niveau))?\s+'
        rf'(?:{_LEVEL_FILLER}\s+){{0,4}}(?:{_LIST_ITEM}){{0,2}}'
        rf'{lang}(?!{_LOW_TAIL})(?!{_ADJECTIVE_USE})'
        # "C1 German", "C1-level German". C1 and C2 are also a driving licence
        # class and a defence term, so the language must follow directly, and
        # "B2/C1 German" (a range starting at B2) is not read as C1.
        rf'|(?<![/\-–])\bc[12](?:[- ](?:level|niveau))?\s+(?:level\s+)?{lang}')
    after = re.compile(
        rf'{lang}[\w-]*'
        rf'(?:\s+(?:und|and|i|en|et|&)\s+[\w-]+)?'
        rf'(?:\s+{_SKILL_WORD}){{0,2}}'
        rf'\s*[(:\-–]?\s*'
        rf'(?:{_PREPOSITION})?(?:{_AT_LEAST})?'
        rf'(?:{_HIGH_AFTER})')
    alternative = re.compile(
        rf'(?:{lang})[^,;]*?{_OR_WORD}[^,;]*?{_ENGLISH}'
        rf'|{_ENGLISH}[^,;]*?{_OR_WORD}[^,;]*?(?:{lang})')
    return before, after, alternative


def _clauses(text):
    """The sentences of an advert, each split again at its commas when it
    marks something as preferred, without the parts that carry the marker."""
    text = _ABBREVIATION_DOT.sub(r'\1', fold_diacritics(text))
    for sentence in _CLAUSE_SPLIT.split(text):
        if not sentence.strip():
            continue
        if not any(marker in sentence for marker in LANGUAGE_PREFERRED_MARKERS):
            yield sentence
            continue
        for part in sentence.split(','):
            if part.strip() and not any(m in part for m in LANGUAGE_PREFERRED_MARKERS):
                yield part


def requires_unspoken_language(text: str, languages=None) -> bool:
    """
    True when the advert requires one of `languages` at a high level.

    languages defaults to Config.NON_FLUENT_LANGUAGES. Empty never drops.
    """
    if languages is None:
        languages = getattr(Config, 'NON_FLUENT_LANGUAGES', None) or ()
    names = tuple(sorted({fold_diacritics(name).strip()
                          for name in languages if name and name.strip()}))
    if not names or not text:
        return False
    before, after, alternative = _language_patterns(names)
    for clause in _clauses(text):
        if _TEAM_DESCRIPTION.search(clause) or alternative.search(clause):
            continue
        if before.search(clause) or after.search(clause):
            return True
    return False


# ── Source priority ───────────────────────────────────────────────────────────
# Two aggregators fill the digest with senior US software roles far outside the
# target: WeWorkRemotely, and Indeed through Apify. They are still searched and
# still shown, just ranked a little lower so the ten slots per message go to
# better sources first. Matched as a substring so 'Apify / Indeed' qualifies.
LOW_PRIORITY_SOURCE_MARKERS = ('weworkremotely', 'indeed')
LOW_PRIORITY_SOURCE_MULTIPLIER = 0.9


def source_priority_multiplier(job: dict) -> float:
    """0.9 for the crowding aggregators, 1.0 for everything else."""
    source = (job.get('source') or '').lower()
    if any(marker in source for marker in LOW_PRIORITY_SOURCE_MARKERS):
        return LOW_PRIORITY_SOURCE_MULTIPLIER
    return 1.0


# ── Pump, valve and flow-equipment roles ──────────────────────────────────────
# The role profile the candidate most wants: a pump, valve or flow-equipment
# maker or distributor, where the job is taking an enquiry through to an offer
# and a finished solution. Matched either on a known maker in the company or
# title, or on the description talking about the equipment and about preparing
# offers for it. Lifted a little and marked in the digest, never required.
FLOW_EQUIPMENT_COMPANIES = (
    'grundfos', 'danfoss', 'wilo', 'ksb', 'xylem', 'samson', 'flowserve',
    'sulzer', 'alfa laval', 'spirax', 'ebara', 'kitz', 'stubbe', 'stuebbe',
    'adams armaturen', 'abo valve', 'arako', 'burkert', 'b\u00fcrkert',
    'georg fischer', 'emerson', 'fisher controls', 'neles', 'metso',
    'pentair', 'watson-marlow', 'netzsch', 'seepex', 'lowara', 'caprari',
)
_FLOW_COMPANY_RE = re.compile(
    r'\b(' + '|'.join(re.escape(c) for c in FLOW_EQUIPMENT_COMPANIES) + r')\b')
_FLOW_EQUIPMENT_TERMS = re.compile(
    r'\b(pumps?|valves?|actuators?|flow control|flow meters?|fluid systems?)\b')
_OFFER_TERMS = re.compile(
    r'\b(quotations?|quotes|technical offers?|offers? for|proposals?|tenders?|'
    r'offer creation|sizing and selection)\b')
FLOW_EQUIPMENT_BOOST = 1.1


def is_flow_equipment_role(title: str, description: str, company: str) -> bool:
    """True for a role at a pump, valve or flow-equipment company, or one whose
    description is about preparing offers for that equipment."""
    name = f"{title} {company}".lower()
    if _FLOW_COMPANY_RE.search(name):
        return True
    desc = (description or '').lower()
    return bool(_FLOW_EQUIPMENT_TERMS.search(desc) and _OFFER_TERMS.search(desc))


def flow_equipment_multiplier(job: dict) -> float:
    """A small lift for flow-equipment roles, 1.0 for everything else."""
    if is_flow_equipment_role(job.get('title', ''), job.get('description', ''),
                              job.get('company', '')):
        return FLOW_EQUIPMENT_BOOST
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
# The title boost only confirms a match the advert itself supports. Below this
# base score the description barely matches the CV, so a target-role title on
# top of it is a title trap (an "Application Engineer" post about circuit
# boards, an "Automation Engineer" post about test scripts), and the title is
# not allowed to lift it. A title-only listing, with no description to read,
# still gets the boost, since the title is all the evidence there is.
TITLE_BOOST_MIN_BASE = 20.0

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

    # Local-language role words from the user's .env, matched without
    # diacritics so "inzenjer" catches "Inženjer" and the other way round.
    folded = fold_diacritics(title)
    for term in getattr(Config, 'TITLE_SCREEN_TERMS', None) or ():
        if fold_diacritics(term) in folded:
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


# ── Adverts written in a language the user cannot read ───────────────────────
# An advert written in Slovak or French rarely says Slovak or French is
# required, so requires_unspoken_language has nothing to match. Which
# languages he cannot read is personal: Config.UNREADABLE_ADVERT_LANGUAGES,
# empty by default. The reading itself is in core/multilingual.py, and it is
# conservative: a teaser, a mixed advert or one in Cyrillic is never judged.
def fails_advert_language(text: str, languages=None) -> bool:
    """
    True when the advert is clearly written in one of `languages`, which
    defaults to Config.UNREADABLE_ADVERT_LANGUAGES. Empty never drops, and
    neither does an advert that names English as the working language.
    """
    if languages is None:
        languages = getattr(Config, 'UNREADABLE_ADVERT_LANGUAGES', None) or ()
    listed = {canonical_language(name) for name in languages if name and name.strip()}
    if not listed or not text:
        return False
    if states_english_working_language(text):
        return False
    return written_in(text, listed)


class JobFilter:
    """Filter and rank jobs by relevance to user skills."""

    def __init__(self):
        self.skills_data = self.load_keywords()
        self.all_skills = self.extract_all_skills()
        self.last_rejected = {}

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
        if is_dealbreaker(job_title, job_description):
            logger.debug(f"Dealbreaker: {job_title}")
            return True
        return False

    def is_dev_titled(self, job_title):
        """Return True if the title is a pure software-development role, see
        is_pure_dev_title."""
        return is_pure_dev_title(job_title)

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

        # Score from the advert, not the title. A title can match a CV skill
        # through its synonyms (an "Application Engineer" title reads as
        # technical sales) while the job itself is about something else, so
        # when there is a description the title is left out of the skill match.
        # A title-only listing still scores on its title, the only text it has.
        if (job_description or '').strip():
            desc_lower = " ".join(
                part for part in (job_description, company) if part).lower()
        else:
            desc_lower = " ".join(
                part for part in (job_title, company) if part).lower()
        # A local-language advert gains the English equivalents of its terms,
        # so it scores on meaning rather than on how much English it contains.
        # English text gains nothing. See core/multilingual.py.
        desc_lower = f"{desc_lower} {english_equivalents(desc_lower)}"
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

        # Title boost, target role in job title. Applied only when the advert
        # already supports the match, see TITLE_BOOST_MIN_BASE. The support is
        # measured on the description alone: the title itself can match a CV
        # skill through its synonyms, so counting it would let a title trap
        # vouch for itself.
        if best_score > 0 and any(kw in title_lower for kw in TITLE_BOOST_KEYWORDS):
            if (not (job_description or '').strip()
                    or self._description_only_score(job_description, company)
                    >= TITLE_BOOST_MIN_BASE):
                best_score = apply_boost(best_score, TITLE_BOOST_MULTIPLIER)

        # Sector boost, industrial / SaaS company or description
        sector_text = (job_description + ' ' + company).lower()
        sector_text = f"{sector_text} {english_equivalents(sector_text)}"
        if best_score > 0 and any(kw in sector_text for kw in SECTOR_BOOST_KEYWORDS):
            best_score = apply_boost(best_score, SECTOR_BOOST_MULTIPLIER)

        # Fallback: score against merged skills if no CV-level data
        if best_score == 0 and self.all_skills:
            matches = sum(1 for s in self.all_skills if skill_matches(s, desc_lower))
            denominator = min(len(self.all_skills), SKILL_MATCH_DENOMINATOR_CAP)
            best_score = round(min(100, (matches / denominator) * 100), 1)

        return best_score, best_cv

    def _description_only_score(self, job_description, company=""):
        """Best CV skill-match score from the advert text alone, title excluded.
        Used to decide whether a target-role title deserves its boost."""
        text = " ".join(p for p in (job_description, company) if p).lower()
        text = f"{text} {english_equivalents(text)}"
        best = 0.0
        for cv_data in self.skills_data.get('cvs', {}).values():
            if not isinstance(cv_data, dict) or 'skills' not in cv_data:
                continue
            cv_skills = [sk for sk in cv_data['skills'].keys() if sk]
            if not cv_skills:
                continue
            matches = sum(1 for sk in cv_skills if skill_matches(sk, text))
            denominator = min(len(cv_skills), SKILL_MATCH_DENOMINATOR_CAP)
            best = max(best, min(100.0, (matches / denominator) * 100))
        return best

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
          2. the title alone: senior, local trade, pure programming or a
             job function on the user's excluded list (title_drop_reason)
          3. dealbreaker keywords and clearance, then export control
          4. work eligibility, is_geo_restricted
          5. excluded locations, then the country allow-list
          6. non-English title markers, then a required language the user
             does not speak (requires_unspoken_language), then an advert
             written in a language he cannot read (fails_advert_language)
          7. relevance score below min_score

        Jobs from ALWAYS_INCLUDE_SOURCES bypass all seven. They are still
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
            'senior_title': 0,
            'local_trade_title': 0,
            'pure_programming': 0,
            'excluded_title': 0,
            'dealbreaker': 0,
            'export_controlled': 0,
            'geo_restricted': 0,
            'excluded_location': 0,
            'outside_allowed_countries': 0,
            'non_english': 0,
            'language_required': 0,
            'advert_language': 0,
            'experience_required': 0,
            'electrical_degree': 0,
            'below_min_score': 0,
            'same_posting': 0,
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

                # The title alone rules it out: cheapest test, no text needed.
                reason = title_drop_reason(title, job.get('source', ''))
                if reason:
                    logger.debug(f"Title rule {reason}: {title} @ {company}")
                    rejected[reason] += 1
                    continue

                if self.is_negative_match(title, description):
                    rejected['dealbreaker'] += 1
                    continue

                if is_export_controlled(title + ' ' + description):
                    logger.debug(f"Export controlled: {title} @ {company}")
                    rejected['export_controlled'] += 1
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

                # Located only in countries the user cannot work in. Off
                # unless ALLOWED_COUNTRIES or SPONSORSHIP_ONLY_COUNTRIES is set.
                if is_outside_allowed_countries(job):
                    logger.debug(f"Outside allowed countries: {title} @ {company}")
                    rejected['outside_allowed_countries'] += 1
                    continue

                if (is_non_english_title(title)
                        and not states_english_working_language(description)):
                    logger.debug(f"Non-English title: {title} @ {company}")
                    rejected['non_english'] += 1
                    continue

                # The title is in English but the advert requires a language
                # the user does not speak well enough. Off unless
                # NON_FLUENT_LANGUAGES is set.
                if requires_unspoken_language(f"{title}\n{description}"):
                    logger.debug(f"Language required: {title} @ {company}")
                    rejected['language_required'] += 1
                    continue

                # Written in a language the user cannot read. Off unless
                # UNREADABLE_ADVERT_LANGUAGES is set.
                if fails_advert_language(description):
                    logger.debug(f"Advert language: {title} @ {company}")
                    rejected['advert_language'] += 1
                    continue

                # Too many years required, or a degree only an electrical
                # engineer holds. Both off unless set in .env.
                content_reason = content_drop_reason(title, description)
                if content_reason:
                    logger.debug(f"{content_reason}: {title} @ {company}")
                    rejected[content_reason] += 1
                    continue

            score, best_cv = self.score_job_with_cv(title, description, company)

            # Entry-level software from the home-market board skips the score
            # cutoff and is lifted to the regional score bar below.
            entry_software = (not always_include
                              and job.get('source') in ENTRY_SOFTWARE_SOURCES
                              and is_entry_software_title(title))

            if not always_include and not entry_software and score < min_score:
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
                # Required experience, the crowding aggregators and flow-equipment
                # roles only reorder. None of them drops a job.
                multiplier *= experience_multiplier(job)
                multiplier *= source_priority_multiplier(job)
                multiplier *= flow_equipment_multiplier(job)
                # Suspected scam: sink it and mark it, but do not delete, since
                # this is a heuristic. The digest recomputes the flag to warn.
                if scam_risk(title, description, company, job.get('link', '')):
                    multiplier *= SCAM_RISK_PENALTY
                    job['scam_risk'] = True
            # Cap at 100: the sponsorship boost can push a high score past it,
            # and a >100% relevance reads as a bug in the digest.
            job['relevance_score'] = round(min(100, score * multiplier))
            # A suspected scam keeps its penalty, the floor never lifts it.
            if entry_software and not job.get('scam_risk'):
                job['relevance_score'] = max(job['relevance_score'], entry_software_floor())
            job['best_cv'] = best_cv
            scored_jobs.append(job)

        scored_jobs.sort(key=lambda x: x['relevance_score'], reverse=True)

        # One posting published in two languages arrives as two adverts with
        # different links and titles but the same posting number. Compared
        # only among the jobs that passed, best score first, so the copy kept
        # is the best one the candidate can read, whichever arrived first.
        # A hand-saved job is never dropped here.
        same = {i for i in find_same_postings(scored_jobs)
                if scored_jobs[i].get('source') not in ALWAYS_INCLUDE_SOURCES}
        if same:
            for i in sorted(same):
                logger.debug(f"Same posting: {scored_jobs[i].get('title')} "
                             f"@ {scored_jobs[i].get('company')}")
            rejected['same_posting'] = len(same)
            scored_jobs = [job for i, job in enumerate(scored_jobs) if i not in same]

        # Kept on the instance so a caller or a test can read the breakdown.
        self.last_rejected = rejected
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
