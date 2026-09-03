"""
Utility functions for logging, file operations, and helpers.
"""

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from core.config import Config


def force_utf8_streams():
    """Make stdout/stderr UTF-8 so a diagnostic print or log of non-ASCII job
    data (e.g. Serbian titles) cannot crash a run under a Windows cp1252 console
    or a redirected log. Both entry points (job_search_smart, telegram_sender)
    call this before they print. Idempotent; a stream without .reconfigure
    (pytest capture, StringIO) is left as is. reconfigure mutates the stream in
    place, so handlers already bound to it (the logging StreamHandler) benefit too.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def setup_logging(name=__name__, log_file=None):
    """Setup rotating file logger."""
    if not log_file:
        log_file = Path(Config.LOG_DIR) / "job_search.log"

    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, Config.LOG_LEVEL))

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=Config.LOG_MAX_BYTES,
        backupCount=Config.LOG_BACKUP_COUNT,
        encoding='utf-8'
    )

    # Console handler
    console_handler = logging.StreamHandler()

    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

def format_cv_label(cv_filename):
    """
    Turn a CV filename into a short label for display.

    Strips the configured CV_FILENAME_PREFIX, drops the extension, replaces
    separators with spaces and removes a trailing version number.
    Returns None when given nothing.

    "Jane_Doe_CV_Sales_Engineer_2.pdf" -> "Sales Engineer"
    """
    if not cv_filename:
        return None

    label = str(cv_filename)

    prefix = Config.CV_FILENAME_PREFIX
    if prefix and label.startswith(prefix):
        label = label[len(prefix):]

    label = re.sub(r'\.(pdf|docx?|txt)$', '', label, flags=re.IGNORECASE)
    label = label.replace('_', ' ').replace('-', ' ').strip()
    label = re.sub(r'\s+\d+$', '', label)
    label = re.sub(r'\s{2,}', ' ', label)

    return label or None


def sanitize_filename(filename):
    """Remove invalid filename characters."""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename.strip()

def extract_domain(url):
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except:
        return None

def is_remote_job(description):
    """Check if job description mentions 'remote'."""
    if not description:
        return False
    keywords = ['remote', 'work from home', 'virtual', 'distributed', 'telecommute']
    text = description.lower()
    return any(keyword in text for keyword in keywords)

def normalize_salary(salary_str):
    """Extract and normalize salary information."""
    if not salary_str:
        return None
    # Simple normalization - keep as-is for now
    return salary_str.strip()
