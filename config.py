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

    # Gmail Configuration (IMAP) — primary account
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
    GMAIL_IMAP_HOST = "imap.gmail.com"
    GMAIL_IMAP_PORT = 993

    # Second Gmail account — application tracking inbox
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

    # Adzuna API (free tier — 250 req/day, has salary data)
    # Get keys at: https://developer.adzuna.com/
    ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID")
    ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

    # Jooble API (free tier — global aggregator, 140k+ sources)
    # Get key at: https://jooble.org/api/about
    JOOBLE_API_KEY = os.getenv("JOOBLE_API_KEY")

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
