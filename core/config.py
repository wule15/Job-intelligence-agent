"""
Configuration management for Job Search + Cover Letter system.
Loads and validates environment variables from .env file.
"""

import os
from dotenv import dotenv_values
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).parent

# Load .env file from project root
env_file = PROJECT_ROOT / '.env'
env_vars = dotenv_values(env_file) if env_file.exists() else {}

# Set environment variables
for key, value in env_vars.items():
    if value:
        os.environ[key] = value
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
CACHE_DIR = PROJECT_ROOT / "cache"
OUTPUT_DIR = PROJECT_ROOT / "output"
RESUMES_DIR = PROJECT_ROOT / "resumes"
LINKEDIN_DIR = PROJECT_ROOT / "linkedin profile"
CONFIG_DIR = PROJECT_ROOT / "config"

# Create directories if they don't exist
for directory in [DATA_DIR, LOGS_DIR, CACHE_DIR, OUTPUT_DIR, CONFIG_DIR]:
    directory.mkdir(exist_ok=True)


def code_list(raw):
    """A comma separated setting as a list of upper-case codes, blanks dropped."""
    return [part.strip().upper() for part in (raw or '').split(',') if part.strip()]


def term_list(raw):
    """A comma separated setting as a list of lower-case terms, blanks dropped."""
    return [part.strip().lower() for part in (raw or '').split(',') if part.strip()]


def score_setting(raw, default, name=''):
    """
    A score setting as a float between 0 and 100.

    Blank gives the default. A value that is not a number also gives the
    default, with one printed warning, because a typo in .env must not stop
    the scheduled morning run. A number outside 0 to 100 is clamped.
    """
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        value = float(str(raw).strip())
    except ValueError:
        print(f"[!] {name or 'Score setting'} is not a number, using {default:g}")
        return float(default)
    return min(100.0, max(0.0, value))


def flag_setting(raw):
    """An on/off setting. On only for 1, true, yes or on, in any case."""
    return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'on')


class Config:
    """Configuration class for the application."""

    # Directory Paths
    RESUMES_DIR = RESUMES_DIR
    LINKEDIN_DIR = LINKEDIN_DIR
    DATA_DIR = DATA_DIR
    LOGS_DIR = LOGS_DIR
    CACHE_DIR = CACHE_DIR
    OUTPUT_DIR = OUTPUT_DIR
    CONFIG_DIR = CONFIG_DIR

    # Candidate identity. Used to sign generated cover letters and to shorten
    # CV filenames into readable labels. All read from .env so no personal
    # detail is committed to the repository.
    CANDIDATE_NAME = os.getenv("CANDIDATE_NAME", "")
    CANDIDATE_EMAIL = os.getenv("CANDIDATE_EMAIL", "")
    CANDIDATE_LINKEDIN = os.getenv("CANDIDATE_LINKEDIN", "")

    # Prefix stripped from CV filenames when building a short label.
    # Example: "Jane_Doe_CV_" turns "Jane_Doe_CV_Sales.pdf" into "Sales".
    CV_FILENAME_PREFIX = os.getenv("CV_FILENAME_PREFIX", "")

    # Gmail Configuration (IMAP), primary account
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
    GMAIL_IMAP_HOST = "imap.gmail.com"
    GMAIL_IMAP_PORT = 993

    # Second Gmail account, application tracking inbox
    GMAIL_USER_2 = os.getenv("GMAIL_USER_2")
    GMAIL_APP_PASSWORD_2 = os.getenv("GMAIL_APP_PASSWORD_2")

    # Gmail Configuration (SMTP)
    GMAIL_SMTP_USER = os.getenv("GMAIL_SMTP_USER", GMAIL_USER)
    GMAIL_SMTP_PASSWORD = os.getenv("GMAIL_SMTP_PASSWORD")
    GMAIL_SMTP_HOST = "smtp.gmail.com"
    GMAIL_SMTP_PORT = 587
    RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")

    # Telegram Configuration
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # Claude API Configuration
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

    # Adzuna API (free tier, 250 req/day, has salary data)
    # Get keys at: https://developer.adzuna.com/
    ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID")
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

    # Jooble API (free tier, global aggregator, 140k+ sources)
    # Get key at: https://jooble.org/api/about
    JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY")

    # SerpApi Google Jobs (free tier 250 searches/month). Needs SERPAPI_KEY.
    # Google Jobs returns nothing for a bare query, so each country is queried
    # with its own gl/domain/hl; see the verified MARKETS table in
    # sources/serpapi.py. SERPAPI_COUNTRIES picks which targets to run, comma
    # separated. Use the special code "remote" for a fully-remote-anywhere pass,
    # plus real country codes for localised markets, e.g. "remote,de,at,fr,nl,
    # be,dk". Empty by default so the public engine adds nothing extra.
    # SERPAPI_BUDGET caps searches per run to stay in the monthly quota
    # (free tier ~= 8/day); set it to at least the number of targets.
    SERPAPI_KEY = os.getenv("SERPAPI_KEY")
    SERPAPI_COUNTRIES = [
        s.strip().lower() for s in os.getenv("SERPAPI_COUNTRIES", "").split(",") if s.strip()
    ]
    SERPAPI_BUDGET = int(os.getenv("SERPAPI_BUDGET", "6"))
    # JSEARCH_BUDGET caps how many queries JSearch runs per pass. Added
    # 2026-09-09: JSearch was the only paid source with no cap at all, so it ran
    # every specific query at two pages each, 124 calls in a single run, while
    # the logs showed it returning zero results for nearly all of them. Without
    # a cap, every query added anywhere else silently cost two more JSearch
    # calls. Queries are sorted longest-first, so the cap keeps the most
    # specific ones and drops the broad tail.
    JSEARCH_BUDGET = int(os.getenv("JSEARCH_BUDGET", "40"))
    # EXCLUDED_LOCATIONS drops a job outright when its location field matches any
    # of these terms, remote or on-site alike. Personal to the user and read from
    # .env, so the public engine excludes nothing by default. Comma separated,
    # country or city names, e.g. "some-country,some-city,another-city".
    #
    # Terms are matched on word boundaries, never as bare substrings, because a
    # substring test for a country name will also hit unrelated place names that
    # merely contain it.
    EXCLUDED_LOCATIONS = [
        s.strip().lower()
        for s in os.getenv("EXCLUDED_LOCATIONS", "").split(",")
        if s.strip()
    ]
    # EXCLUDED_TITLE_TERMS drops a job whose title contains any of these terms
    # as a whole word, accents ignored, e.g. "payroll,recruiter,hr,marketing".
    # For job functions the user never applies to. A company careers board is
    # read whole, and its shared boilerplate (AI, cloud, platform) lifts every
    # advert on it, so a payroll role there scores like an engineering one and
    # only its title tells them apart. Personal, read from .env; the public
    # engine excludes nothing by default.
    EXCLUDED_TITLE_TERMS = term_list(os.getenv("EXCLUDED_TITLE_TERMS", ""))
    # DROP_LOCAL_TRADE_TITLES drops Serbian, Croatian and Bosnian technician,
    # electrician and civil-engineering titles ("tehnicar", "elektricar",
    # "elektroinstalater", "gradjevinski"), and technician and electrician
    # titles in English or German ("Facility Technician", "Elektroniker")
    # unless the title also names an engineer, see core/job_filter.py. Which
    # trades to rule out is personal, so it is off by default.
    DROP_LOCAL_TRADE_TITLES = flag_setting(os.getenv("DROP_LOCAL_TRADE_TITLES"))
    # NON_EUROPE_PREFERENCE ranks Europe above other acceptable regions without
    # hiding them. 1.0 disables the preference entirely.
    NON_EUROPE_PREFERENCE = float(os.getenv("NON_EUROPE_PREFERENCE", "1.0"))
    # Curated queries for the SerpApi market passes. Google Jobs is query
    # sensitive (broad engineering nouns return, many phrasings return nothing),
    # so a fixed list of terms known to hit beats whatever the CV happens to
    # rank first. Comma separated; empty falls back to the CV-derived queries.
    SERPAPI_QUERIES = [
        s.strip() for s in os.getenv("SERPAPI_QUERIES", "").split(",") if s.strip()
    ]

    # Logging Configuration
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_DIR = str(LOGS_DIR)
    LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
    LOG_BACKUP_COUNT = 5

    # Database Configuration
    DATABASE_PATH = str(DATA_DIR / "job_digest.db")
    DB_CLEANUP_DAYS = 30

    # Email Configuration
    EMAIL_SUBJECT_FILTER = "10 Job Suggestions"
    GMAIL_FOLDER_SEARCH = "[Gmail]/Drafts"  # Search in Drafts folder

    # Job Search Configuration
    REQUEST_TIMEOUT = 10
    RETRY_ATTEMPTS = 3
    RETRY_BACKOFF = 1  # seconds, exponential

    # File Paths
    KEYWORDS_CACHE = str(DATA_DIR / "keywords.json")
    JOB_SEARCH_CONFIG = str(CONFIG_DIR / "job_search_config.json")
    # Master CV: single source of truth the scorer reads its variants from.
    # Gitignored and never published; see master-cv.example.yaml for the format.
    MASTER_CV_PATH = os.getenv("MASTER_CV_PATH", str(PROJECT_ROOT / "master-cv.yaml"))
    # Optional label stamped on the Telegram digest header, so two engines
    # running in parallel can be told apart from the message alone.
    DIGEST_LABEL = os.getenv("DIGEST_LABEL", "")
    # Optional file of jobs never to send, one "company | title" per line, for
    # jobs already handled outside the digest (applied to directly, rejected,
    # closed). Lines starting with # are comments. A relative path resolves
    # against PROJECT_ROOT (this core folder), never the working directory, so
    # "data/digest_exclude.txt" lands in the gitignored data folder. Empty by
    # default: the public engine excludes nothing.
    DIGEST_EXCLUDE_FILE = os.getenv("DIGEST_EXCLUDE_FILE", "")

    # Daily volume. The score a stored job needs to be sent. Slots in each
    # digest are a ceiling, so a higher bar means fewer jobs, and a day where
    # nothing clears it sends none. DIGEST_MIN_SCORE covers the main and
    # direct-company digests; 15 was the fixed value before it could be set.
    # REGIONAL_MIN_SCORE covers the regional digest and falls back to the main
    # bar. It is separate because home-market adverts are mostly on-site and
    # short, so they score lower for the same fit. Each digest reads its bar
    # when it runs. The run log prints every sent job's score, which is the
    # data to choose these from.
    DIGEST_MIN_SCORE = score_setting(os.getenv("DIGEST_MIN_SCORE"), 15.0, "DIGEST_MIN_SCORE")
    REGIONAL_MIN_SCORE = score_setting(
        os.getenv("REGIONAL_MIN_SCORE"), DIGEST_MIN_SCORE, "REGIONAL_MIN_SCORE")
    # Adds graduate, junior, trainee, associate and intern queries for the
    # engineering and sales tracks the CV supports, one of them rotated to the
    # head of the list each day. Off by default.
    EARLY_CAREER_QUERIES = flag_setting(os.getenv("EARLY_CAREER_QUERIES"))

    # Regions where the user can obtain work authorization (a permit or, in the
    # EU, a Blue Card), so a role there that asks only for generic authorization
    # is takeable rather than dropped. Comma separated, e.g. "EU,EEA". Empty
    # (the default) means strict eligibility: only an explicit sponsorship offer
    # or a worldwide / remote / no-permit signal keeps an authorization-gated
    # job. This is personal to the user, read from .env, never committed, so the
    # public engine stays generic and each user sets their own real eligibility.
    WORK_ELIGIBLE_REGIONS = [
        r.strip().upper()
        for r in os.getenv("WORK_ELIGIBLE_REGIONS", "").split(",")
        if r.strip()
    ]

    # Countries the user can work in, as ISO codes plus the group tokens EU,
    # EEA and EUROPE (EUROPE means Europe-wide wording in the location, such
    # as "Remote - Europe" or "EMEA"). A job whose location names only
    # countries outside this list is dropped. A location that names no
    # country ("Remote", a bare city) is not judged here and is left to the
    # work-eligibility text rules. Empty (the default) turns the check off.
    # Example: "EU,EEA,GB,CH,EUROPE". Personal, read from .env.
    ALLOWED_COUNTRIES = code_list(os.getenv("ALLOWED_COUNTRIES", ""))
    # Countries where a job is worth sending only with explicit visa
    # sponsorship, as ISO codes, e.g. "US". A job located there without a
    # sponsorship offer is dropped, and an advert requiring existing work
    # authorization there is dropped wherever it is located, whatever
    # WORK_ELIGIBLE_REGIONS says. Empty (the default) turns it off.
    SPONSORSHIP_ONLY_COUNTRIES = code_list(os.getenv("SPONSORSHIP_ONLY_COUNTRIES", ""))

    # Languages the user does not speak at a fluent level, as English names,
    # e.g. "german,dutch,french". An advert requiring one of them fluent or at
    # C1 or above is dropped; one asking for B1 or B2, or naming it as a plus,
    # is kept. Empty (the default) turns the check off. Personal, read from .env.
    NON_FLUENT_LANGUAGES = [
        s.strip().lower() for s in os.getenv("NON_FLUENT_LANGUAGES", "").split(",")
        if s.strip()
    ]
    # Languages the user cannot read an advert in, as English names, e.g.
    # "slovak,czech,french". An advert written in one of them is dropped,
    # whatever it says about languages: an advert in Slovak rarely says that
    # Slovak is required. The language is read from the advert's common words,
    # see core/multilingual.py for the languages it knows. Read conservatively:
    # a short or mixed advert is kept, and so is one that names English as the
    # working language. Separate from NON_FLUENT_LANGUAGES because a language
    # can be readable but not fluent. Empty (the default) turns it off.
    UNREADABLE_ADVERT_LANGUAGES = [
        s.strip().lower() for s in os.getenv("UNREADABLE_ADVERT_LANGUAGES", "").split(",")
        if s.strip()
    ]

    # Regional job search. Personal customization, read from .env, empty in the
    # public default so the shared engine stays generic. Two separate lists so
    # sourcing and digest matching can differ:
    #   REGIONAL_JOB_LOCATIONS drives extra Jooble location queries. Use country
    #     names, which Jooble matches best, e.g. "Serbia,Montenegro".
    #   REGIONAL_MATCH_TERMS decides which stored jobs go in the regional Telegram
    #     digest, matched as whole words against the job's location, case and
    #     accents ignored. Add city names too for reliable matching, e.g.
    #     "serbia,kragujevac,sarajevo". Falls back to REGIONAL_JOB_LOCATIONS if unset.
    # REGIONAL_DIGEST_LABEL is the header shown on that third message.
    REGIONAL_JOB_LOCATIONS = [
        s.strip() for s in os.getenv("REGIONAL_JOB_LOCATIONS", "").split(",") if s.strip()
    ]
    REGIONAL_MATCH_TERMS = [
        s.strip() for s in os.getenv("REGIONAL_MATCH_TERMS", "").split(",") if s.strip()
    ]
    REGIONAL_DIGEST_LABEL = os.getenv("REGIONAL_DIGEST_LABEL", "Regional jobs")
    # Dedicated regional job boards to scrape, comma separated, matched to a
    # search_* function registered in sources/free_boards.py _run_regional_boards (e.g.
    # "infostud"). Empty by default so the public engine scrapes no local board.
    REGIONAL_BOARDS = [
        s.strip() for s in os.getenv("REGIONAL_BOARDS", "").split(",") if s.strip()
    ]
    # Cities to run the Infostud board in, comma separated, as they appear in
    # Infostud's URLs (e.g. "city-a,city-b"). Empty searches the whole
    # country, the behaviour before this setting existed.
    INFOSTUD_CITIES = [
        s.strip() for s in os.getenv("INFOSTUD_CITIES", "").split(",") if s.strip()
    ]
    # Queries used specifically for the regional sourcing passes (Jooble by
    # location, and the local boards). Comma separated. Empty by default, then
    # the passes fall back to the normal CV-derived queries. This exists because
    # a location aggregator can be language-sensitive: Jooble Bosnia returns
    # results for "engineer" but nothing for "inzenjer", so a fixed set of broad
    # role terms (mixing English and local) gives steadier regional coverage
    # than whatever the CV happens to produce. Personal, read from .env.
    REGIONAL_QUERIES = [
        s.strip() for s in os.getenv("REGIONAL_QUERIES", "").split(",") if s.strip()
    ]
    # Local-language role words that let a title through the title screen, so
    # its full advert is fetched before scoring. The screen otherwise matches
    # only the CV's English skill words. Comma separated, matched ignoring
    # diacritics. Empty by default. Personal, read from .env.
    TITLE_SCREEN_TERMS = [
        s.strip() for s in os.getenv("TITLE_SCREEN_TERMS", "").split(",") if s.strip()
    ]

    @classmethod
    def validate(cls):
        """Validate that all required credentials are present."""
        required = [
            'GMAIL_USER',
            'GMAIL_APP_PASSWORD',
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_CHAT_ID',
            'ANTHROPIC_API_KEY'
        ]

        missing = [key for key in required if not getattr(cls, key)]

        if missing:
            raise ValueError(
                f"Missing required environment variables in .env: {', '.join(missing)}\n"
                f"Please fill in your .env file at: {PROJECT_ROOT / '.env'}"
            )

    @classmethod
    def print_config(cls):
        """Print current configuration (excluding sensitive data)."""
        print("\n" + "="*60)
        print("CONFIGURATION LOADED")
        print("="*60)
        print(f"Gmail User: {cls.GMAIL_USER}")
        print(f"Telegram Chat ID: {cls.TELEGRAM_CHAT_ID}")
        print(f"Database: {cls.DATABASE_PATH}")
        print(f"Log Level: {cls.LOG_LEVEL}")
        print("="*60 + "\n")
