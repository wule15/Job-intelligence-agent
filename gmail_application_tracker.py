#!/usr/bin/env python3
"""
Gmail Application Tracker
Scans Gmail inbox for job application confirmation AND rejection emails
from any source (LinkedIn, Workday, Greenhouse, Jobot, Recruitee, etc.),
extracts job title + company, then:
  - Confirmation → marks job as "applied" in DB
  - Rejection    → marks job as "rejected" in DB
  - If job not found in DB → creates a new entry

Account scanned (via IMAP):
  - GMAIL_USER         (primary account)
  - GMAIL_USER_2       (secondary account)

Subject patterns handled:
  - "Your Application for TITLE at COMPANY"  (Workday, Persona)
  - "COMPANY Application Confirmation for TITLE"  (CSOD)
  - "Thank you for applying to COMPANY - TITLE"  (Eaton TalentHub)
  - "Application Received! TITLE"  (Jobot)
  - "TITLE - Confirmation of your application"  (Recruitee)
  - "Application Submitted - TITLE on PLATFORM"  (Mercor)
  - "You applied to TITLE at COMPANY"  (LinkedIn direct)
  - "Your application was sent to COMPANY"  (LinkedIn direct)
  + rejection detection via subject + body keywords
"""

import imaplib
import email
import re
import sqlite3
from email.header import decode_header as _decode_header
from datetime import datetime, timedelta
from config import Config
from utils import setup_logging

logger = setup_logging('gmail_application_tracker')

# Lazy-loaded scorer — initialised once on first use
_scorer = None

def _get_scorer():
    global _scorer
    if _scorer is None:
        try:
            from job_filter import JobFilter
            _scorer = JobFilter()
        except Exception as e:
            logger.warning(f"[Tracker] Could not load JobFilter: {e}")
            _scorer = False  # sentinel so we don't retry on every call
    return _scorer if _scorer else None

# ── Rejection detection ───────────────────────────────────────────────────────

REJECTION_SUBJECT_KEYWORDS = [
    'unsuccessful', 'not selected', 'not moving forward',
    'decided not to progress', 'regret to inform', 'sorry to inform',
    'will not be moving', 'not be considered',
]

REJECTION_BODY_KEYWORDS = [
    'decided not to move forward', 'decided not to progress',
    'moving forward with other', 'move forward with other candidates',
    'not be moving forward', 'unsuccessful', 'not shortlisted',
    'not been selected', 'unable to offer', 'not meet our requirements',
    'did not meet', 'sorry to inform you', 'regret to inform',
    'will not be considered', 'not selected for', 'results did not',
    'not proceed', 'not progress', 'chosen not to proceed',
]

# ── Subject patterns ──────────────────────────────────────────────────────────
# Each entry: (compiled_regex, group_type)
# group_type: 'tc' = (title, company), 'ct' = (company, title),
#             't'  = (title,),          'c'  = (company,)

SUBJECT_PATTERNS = [
    # "You applied to TITLE at COMPANY"  (LinkedIn)
    (re.compile(r"you applied to (.+?) at (.+)", re.IGNORECASE), 'tc'),
    # "Your Application for TITLE at COMPANY"  (Workday, Persona)
    (re.compile(r"your application(?:s)? for (.+?) at (.+)", re.IGNORECASE), 'tc'),
    # "Application submitted: TITLE at COMPANY"
    (re.compile(r"application submitted[:\-–]?\s*(.+?) at (.+)", re.IGNORECASE), 'tc'),
    # "Application Submitted - TITLE on PLATFORM"  (Mercor)
    (re.compile(r"application submitted\s*[-–]\s*(.+?)\s+on\s+(.+)", re.IGNORECASE), 'tc'),
    # "Application confirmation – TITLE – COMPANY"
    (re.compile(r"application confirmation[:\-–]+\s*(.+?)[–\-]+\s*(.+)", re.IGNORECASE), 'tc'),
    # "TITLE – Application submitted"
    (re.compile(r"(.+?)\s*[–\-]+\s*application submitted", re.IGNORECASE), 't'),
    # "COMPANY Application Confirmation for TITLE"  (CSOD / N-SIDE)
    (re.compile(r"^(.+?)\s+application confirmation for\s+(.+)", re.IGNORECASE), 'ct'),
    # "Thank you for applying to COMPANY - TITLE"  (Eaton TalentHub, etc.)
    # Strip Unicode zero-width chars that Eaton appends
    (re.compile(r"thank you for applying to (.+?)\s*[-–—]\s*(.+?)[\u200b\u200c\u200d\ufeff\s]*$", re.IGNORECASE), 'ct'),
    # "Thank you for applying to COMPANY, FIRSTNAME" or bare company
    (re.compile(r"thank you for applying to (.+?)(?:,\s*\w+)?$", re.IGNORECASE), 'c'),
    # "Application Received! TITLE"  (Jobot)
    (re.compile(r"application received[!.:\s]+(.+)", re.IGNORECASE), 't'),
    # "TITLE - Confirmation of your application"  (Recruitee)
    (re.compile(r"(.+?)\s*[-–]\s*confirmation of your application", re.IGNORECASE), 't'),
    # "Your application was sent to COMPANY"  (LinkedIn)
    (re.compile(r"your application was sent to (.+)", re.IGNORECASE), 'c'),
    # "Application sent to COMPANY"
    (re.compile(r"application sent to (.+)", re.IGNORECASE), 'c'),
    # "Action Needed: Your Application for TITLE"  (Jobot)
    (re.compile(r"(?:action needed[:\s]+)?your application for (.+)", re.IGNORECASE), 't'),
]

# Body fallback patterns
BODY_TITLE_PATTERNS = [
    re.compile(r"applied (?:for|to)(?: the)? (.+?) (?:position|role|job)", re.IGNORECASE),
    re.compile(r"application for(?: the)? (.+?) (?:at|with|@)", re.IGNORECASE),
    re.compile(r"job title[:\s]+(.+)", re.IGNORECASE),
    re.compile(r"position[:\s]+(.+)", re.IGNORECASE),
    re.compile(r"role[:\s]+(.+?)(?:\s+at\s+|\s+with\s+|$)", re.IGNORECASE),
]

BODY_COMPANY_PATTERNS = [
    # Require capital letter start — avoids lowercase sentence fragments like "sea"
    re.compile(r"(?:at|with|@)\s+([A-Z][A-Za-z0-9][A-Za-z0-9\s&.'-]{1,38})\b"),
    re.compile(r"company[:\s]+([A-Z][A-Za-z0-9\s&.'-]{1,38})\b"),
]

# Application confirmation keywords — email must contain at least one
APPLICATION_KEYWORDS = [
    'applied', 'application', 'submitted', 'sent to', 'confirmation',
    'thank you for applying', 'your application has been',
    'we have received your application', 'application landed',
    'received your application',
]


# ── Helper utilities ──────────────────────────────────────────────────────────

def _decode(value):
    """Safely decode email header value to string."""
    if not value:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    parts = _decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            result.append(str(part))
    return ' '.join(result).strip()


# ATS / job board URL patterns — these are posting pages, not marketing links
_JOB_URL_PATTERNS = [
    re.compile(r'https?://[^\s"\'<>]*(?:jobs\.lever\.co|boards\.greenhouse\.io|'
               r'ashbyhq\.com|recruitee\.com|jobs\.workday\.com|wd\d+\.myworkday\.com|'
               r'apply\.workable\.com|pinpoint\.email|jobvite\.com|icims\.com|'
               r'taleo\.net|csod\.com|smartrecruiters\.com)[^\s"\'<>]*', re.IGNORECASE),
    re.compile(r'https?://[^\s"\'<>]*/(?:job|jobs|career|careers|position|posting|'
               r'apply|job-details?|job-listing)[^\s"\'<>]*', re.IGNORECASE),
]

_URL_SKIP = re.compile(
    r'(?:unsubscribe|optout|privacy|terms|login|signin|support|'
    r'help|mailto:|tel:|#|tracking|click\?|redirect|\.png|\.jpg|\.gif|\.css)',
    re.IGNORECASE,
)


def _extract_job_url(msg) -> str | None:
    """
    Extract the best job-posting URL from an email's HTML links.
    Prioritises known ATS domains, then generic /jobs/ paths.
    """
    html_raw = ''
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                try:
                    html_raw = part.get_payload(decode=True).decode('utf-8', errors='replace')
                    break
                except Exception:
                    pass
    else:
        try:
            raw = msg.get_payload(decode=True).decode('utf-8', errors='replace')
            if '<' in raw:
                html_raw = raw
        except Exception:
            pass

    if not html_raw:
        return None

    # Pull all href values
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html_raw, re.IGNORECASE)

    candidates: list[str] = []
    for href in hrefs:
        if _URL_SKIP.search(href):
            continue
        for pat in _JOB_URL_PATTERNS:
            if pat.search(href):
                candidates.append(href)
                break

    if not candidates:
        return None

    # Prefer ATS-specific domains over generic /jobs/ paths
    for url in candidates:
        if any(d in url for d in ['lever.co', 'greenhouse.io', 'ashbyhq.com',
                                   'recruitee.com', 'workable.com', 'jobvite.com']):
            return url
    return candidates[0]


def _scrape_job_description(url: str) -> str | None:
    """
    Fetch a job posting URL and extract the description text.
    Handles Lever, Greenhouse, Recruitee, Ashby, and generic ATS pages.
    Returns plain text (up to 3000 chars), or None if it can't be extracted.
    """
    import requests as _req
    from bs4 import BeautifulSoup

    HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'en-US,en;q=0.9',
    }

    # Skip pages known to require JavaScript/login to render
    if any(d in url for d in ['myworkday.com', 'workday.com', 'linkedin.com',
                               'taleo.net', 'csod.com', 'icims.com']):
        return None

    try:
        resp = _req.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Remove noise tags
        for tag in soup(['script', 'style', 'nav', 'footer', 'header',
                         'noscript', 'iframe', 'form']):
            tag.decompose()

        # ATS-specific selectors (most specific first)
        selectors = [
            '.posting-body',             # Lever
            '.job__description',         # Greenhouse
            '#content .section',         # Greenhouse alt
            '.ashby-job-posting-description',  # Ashby
            '[data-ui="job-description"]',     # Recruitee
            '.job-description',
            '.description',
            '[class*="jobDescription"]',
            '[class*="job-detail"]',
            '[class*="posting-body"]',
            '[class*="job_description"]',
            'main article',
            'main',
            'article',
        ]

        for sel in selectors:
            elem = soup.select_one(sel)
            if elem:
                text = elem.get_text(separator=' ', strip=True)
                if len(text) > 150:
                    return re.sub(r'\s+', ' ', text)[:3000]

    except Exception as e:
        logger.debug(f"[Tracker] Scrape failed for {url}: {e}")

    return None


def _get_body(msg):
    """Extract plain text body from email message, stripping HTML tags."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == 'text/plain':
                try:
                    body = part.get_payload(decode=True).decode('utf-8', errors='replace')
                    break
                except Exception:
                    pass
            elif ct == 'text/html' and not body:
                try:
                    raw = part.get_payload(decode=True).decode('utf-8', errors='replace')
                    body = re.sub(r'<[^>]+>', ' ', raw)
                except Exception:
                    pass
    else:
        try:
            raw = msg.get_payload(decode=True).decode('utf-8', errors='replace')
            if '<' in raw and '>' in raw:
                raw = re.sub(r'<[^>]+>', ' ', raw)
            body = raw
        except Exception:
            pass
    # Decode HTML entities and collapse whitespace
    import html as _html
    body = _html.unescape(body)
    return re.sub(r'\s+', ' ', body).strip()


def _is_clean(value: str, max_words: int = 8) -> bool:
    """Return False if value looks like HTML/URL garbage or a sentence fragment."""
    if not value or len(value) < 2:
        return False
    # Too long or too many words → sentence fragment
    if len(value) > 80:
        return False
    words = value.split()
    if len(words) > max_words:
        return False
    # HTML/URL indicators
    bad_substrings = [
        'http', 'href=', '<a ', '</', '&nbsp', '@media', 'font-', '<!--',
        'px;', 'color:', 'margin', 'padding', '.com/', 'www.',
        'no. 07', '&amp', 'style=',
    ]
    # Sentence-fragment indicators (common words that appear in email body prose)
    bad_words = [
        'your', 'our', 'you are', 'thank', 'time', 'needs', 'department',
        'selected', 'interest', 'national', 'waitlisted', 'capture',
        'spent', 'reviewing', 'feedback', 'difference', 'recruiting',
        'regarding', 'applying', 'applied', 'application', 'submitted',
        'received', 'reviewed', 'decided', 'pursue', 'marine storekeeper',
        'recruitment team', 'talent team', 'hiring team',
    ]
    low = value.lower()
    if any(b in low for b in bad_substrings + bad_words):
        return False
    # Must start with a letter or digit
    if not re.match(r'^[A-Za-z0-9"\'(]', value):
        return False
    # Must contain at least one letter
    if not re.search(r'[A-Za-z]', value):
        return False
    return True


def _extract_company_from_sender(sender: str) -> str | None:
    """
    Try to derive a company name from sender email/name.
    e.g. "Acme TalentHub <noreply@talenthub.acme.com>" → "Acme"
         "Acme Workday Notifications <acme@myworkday.com>" → "Acme"
         "alerts@jobs.acme.com" → "Acme"
         "e+xyz@acme.recruitee.com" → "Acme"
    """
    # Pull display name from "Name <addr>" format
    name_match = re.match(r'^(.+?)\s*<', sender)
    display_name = name_match.group(1).strip() if name_match else ''

    # Strip common ATS suffixes from display name
    for suffix in [
        'Workday Notifications', 'TalentHub', 'Talent Acquisition',
        'Applications Team', 'Hiring Team', 'Recruitment Team',
        'Recruiting', 'Recruiter', 'Alerts', 'Notifications',
        'HR', 'Careers', 'Jobs',
    ]:
        display_name = re.sub(rf'\s*{re.escape(suffix)}.*$', '', display_name, flags=re.IGNORECASE).strip()

    if display_name and len(display_name) > 1:
        return display_name

    # Fall back to domain parsing
    addr_match = re.search(r'@([\w.\-]+)', sender)
    if not addr_match:
        return None

    domain = addr_match.group(1).lower()

    # Known ATS domains — extract subdomain as company name
    for ats in ['myworkday.com', 'recruitee.com', 'ashbyhq.com', 'greenhouse.io',
                'lever.co', 'smartrecruiters.com', 'jobvite.com', 'icims.com',
                'taleo.net', 'csod.com', 'manatal.com', 'applytojob.com',
                'pinpoint.email', 'allibo.com']:
        if domain.endswith(ats):
            sub = domain.replace('.' + ats, '').split('.')[-1]
            if sub and sub not in ('mail', 'jobs', 'hire', 'hr', 'noreply', 'recruiting'):
                return sub.capitalize()
            return None  # generic ATS, can't tell company

    # Known job boards
    for board_domain, board_name in [
        ('jobot.com', 'Jobot'),
        ('mercor.com', 'Mercor'),
        ('micro1.ai', 'micro1'),
        ('tally.so', None),       # form tool, not useful
    ]:
        if board_domain in domain:
            return board_name

    # Generic: first part of domain before TLD
    parts = domain.replace('www.', '').split('.')
    if len(parts) >= 2:
        candidate = parts[-2]
        if len(candidate) > 2 and candidate not in ('mail', 'jobs', 'noreply', 'alerts'):
            return candidate.capitalize()

    return None


def _is_rejection(subject: str, body: str) -> bool:
    """Return True if this email looks like a rejection."""
    subj_lower = subject.lower()
    if any(kw in subj_lower for kw in REJECTION_SUBJECT_KEYWORDS):
        return True
    body_lower = body[:2000].lower()
    if any(kw in body_lower for kw in REJECTION_BODY_KEYWORDS):
        return True
    return False


def _parse_application(subject: str, body: str, sender: str):
    """
    Extract (job_title, company, status) from subject + body.
    status: 'applied' | 'rejected'
    Returns (title, company, status) or (None, None, None) if not a job email.
    """
    subj_lower = subject.lower()
    body_lower = body[:2000].lower()

    # Must look like a job application email
    if not any(kw in subj_lower or kw in body_lower for kw in APPLICATION_KEYWORDS):
        return None, None, None

    # Detect rejection
    status = 'rejected' if _is_rejection(subject, body) else 'applied'

    title, company = None, None

    # Try subject patterns
    for pattern, gtype in SUBJECT_PATTERNS:
        m = pattern.search(subject)
        if not m:
            continue
        groups = [g.strip().rstrip('.').strip() if g else '' for g in m.groups()]

        if gtype == 'tc' and len(groups) >= 2:
            title, company = groups[0], groups[1]
        elif gtype == 'ct' and len(groups) >= 2:
            company, title = groups[0], groups[1]
        elif gtype == 't' and len(groups) >= 1:
            title = groups[0]
        elif gtype == 'c' and len(groups) >= 1:
            company = groups[0]
        break

    # Body fallback for title
    if not title:
        for pat in BODY_TITLE_PATTERNS:
            m = pat.search(body[:1500])
            if m:
                title = m.group(1).strip().rstrip('.')[:80]
                break

    # Body fallback for company
    if not company:
        for pat in BODY_COMPANY_PATTERNS:
            m = pat.search(body[:1500])
            if m:
                company = m.group(1).strip().rstrip('.')[:80]
                break

    # Last resort: extract company from sender
    if not company:
        company = _extract_company_from_sender(sender)

    # Clean up
    for val, name in [(title, 'title'), (company, 'company')]:
        pass  # processed below
    if title:
        title = re.sub(r'\s+', ' ', title).strip()
        # Strip trailing job-id patterns like "- R242570"
        title = re.sub(r'\s*[-–]\s*[A-Z]\d{4,}$', '', title).strip()
    if company:
        company = re.sub(r'\s+', ' ', company).strip()

    # Validate — reject HTML/URL garbage
    if not _is_clean(company):
        company = None
    if not _is_clean(title):
        title = None

    if not company:
        return None, None, None

    if not title:
        title = 'Position (unknown)'

    return title, company, status


# ── Database operations ───────────────────────────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables(conn):
    """Make sure required tables exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_status (
            job_id INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'none',
            updated_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(job_id) REFERENCES jobs(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            subject TEXT,
            email_from TEXT,
            processed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _mark_status(conn, job_id, status='applied'):
    """
    Upsert application status.
    Won't downgrade 'interviewing' → 'applied', but will update
    'none'/'applied' → 'rejected', etc.
    """
    conn.execute("""
        INSERT INTO application_status (job_id, status, updated_date)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(job_id) DO UPDATE SET
            status = CASE
                WHEN excluded.status = 'rejected' THEN 'rejected'
                WHEN status IN ('none', '') THEN excluded.status
                WHEN status = 'applied' AND excluded.status = 'interviewing' THEN 'interviewing'
                ELSE status
            END,
            updated_date = CURRENT_TIMESTAMP
    """, (job_id, status))
    conn.commit()


def _find_or_create_job(conn, title, company, source_account, job_url=None, message_id=None):
    """Find existing job by title+company or create a new entry. Returns job_id."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM jobs
        WHERE LOWER(job_title) = LOWER(?) AND LOWER(company) = LOWER(?)
        LIMIT 1
    """, (title, company))
    row = cursor.fetchone()
    if row:
        # Backfill link and message_id if we now have them and didn't before
        updates, args = [], []
        if job_url:
            updates.append("link = COALESCE(NULLIF(link, ''), ?)")
            args.append(job_url)
        if message_id:
            updates.append("source_message_id = COALESCE(NULLIF(source_message_id, ''), ?)")
            args.append(message_id)
        if updates:
            args.append(row['id'])
            conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", args)
            conn.commit()
        return row['id']

    cursor.execute("""
        INSERT INTO jobs (job_title, company, description, link, source, relevance_score, seen_count, source_message_id)
        VALUES (?, ?, ?, ?, ?, 0.0, 1, ?)
        ON CONFLICT(job_title, company) DO UPDATE SET
            seen_count = seen_count + 1,
            extracted_date = CURRENT_TIMESTAMP
    """, (title, company,
          f'Applied via email tracker (from {source_account})',
          job_url,
          f'Email / {source_account}',
          message_id))
    conn.commit()

    cursor.execute("""
        SELECT id FROM jobs WHERE LOWER(job_title)=LOWER(?) AND LOWER(company)=LOWER(?) LIMIT 1
    """, (title, company))
    row = cursor.fetchone()
    return row['id'] if row else None


def _ddg_description(title: str, company: str) -> str | None:
    """
    Search DuckDuckGo for the job posting and return aggregated snippet text.
    Free, no API key required.
    """
    try:
        from ddgs import DDGS
        query = f'"{title}" "{company}" job requirements responsibilities'
        results = DDGS().text(query, max_results=5)
        if not results:
            # Broader fallback
            results = DDGS().text(f"{title} {company} job description", max_results=5)
        if not results:
            return None
        # Concatenate snippets — enough keywords for scoring
        snippets = ' '.join(r.get('body', '') for r in results if r.get('body'))
        return snippets[:3000] if snippets.strip() else None
    except Exception:
        return None


def _score_and_update(conn, job_id, title, company):
    """
    Fetch the real job description and rescore.
    Priority:
      1. Scrape URL already stored in the jobs.link column (from email)
      2. DuckDuckGo web search snippets
      3. Title-only fallback
    Only runs when the stored score is still 0.
    """
    cur = conn.cursor()
    cur.execute("SELECT relevance_score, link FROM jobs WHERE id=?", (job_id,))
    row = cur.fetchone()
    if row and row['relevance_score'] and row['relevance_score'] > 0:
        return

    existing_link = row['link'] if row else None
    scorer = _get_scorer()
    if not scorer:
        return

    description = None
    source_label = 'title-only'

    # 1. Scrape the URL already on the job record (set by _find_or_create_job)
    if existing_link:
        description = _scrape_job_description(existing_link)
        if description:
            source_label = 'email link'

    # 2. DDG fallback
    if not description:
        description = _ddg_description(title, company)
        if description:
            source_label = 'web search'

    try:
        score_text = description if description else title
        score, best_cv = scorer.score_job_with_cv(title, score_text, company)
        if score > 0:
            update_fields = "relevance_score = ?, best_cv = COALESCE(NULLIF(best_cv, ''), ?)"
            args: list = [score, best_cv]
            if description:
                update_fields += ", description = ?"
                args.append(description[:2000])
            args.append(job_id)
            conn.execute(f"UPDATE jobs SET {update_fields} WHERE id = ?", args)
            conn.commit()
            logger.info(f"[Tracker] Scored {title} @ {company}: {score:.1f}% ({source_label})")
    except Exception as e:
        logger.debug(f"[Tracker] Scoring failed for job_id={job_id}: {e}")


def _is_processed(conn, message_id):
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed_emails WHERE message_id=?", (message_id,))
    return cursor.fetchone() is not None


def _mark_processed(conn, message_id, subject, sender):
    try:
        conn.execute("""
            INSERT OR IGNORE INTO processed_emails (message_id, subject, email_from)
            VALUES (?, ?, ?)
        """, (message_id, subject[:200], sender[:200]))
        conn.commit()
    except Exception:
        pass


# ── IMAP scanning ─────────────────────────────────────────────────────────────

def _scan_account(gmail_user, gmail_password, days_back=90):
    """
    Scan one Gmail account for job application emails.
    Uses Gmail's X-GM-RAW extension for full Gmail search syntax.
    Returns list of (title, company, date, message_id, subject, sender, status).
    """
    if not gmail_user or not gmail_password:
        logger.warning(f"[Tracker] No credentials for {gmail_user} — skipping")
        return []

    logger.info(f"[Tracker] Connecting to {gmail_user}...")
    try:
        mail = imaplib.IMAP4_SSL(Config.GMAIL_IMAP_HOST, Config.GMAIL_IMAP_PORT)
        mail.login(gmail_user, gmail_password)
    except Exception as e:
        logger.error(f"[Tracker] Login failed for {gmail_user}: {e}")
        return []

    results = []
    since_date = (datetime.now() - timedelta(days=days_back)).strftime('%Y/%m/%d')

    try:
        mail.select('INBOX', readonly=True)

        # Run multiple simple SUBJECT searches and merge results
        since_imap = (datetime.now() - timedelta(days=days_back)).strftime('%d-%b-%Y')
        seen = set()
        msg_ids = []
        for keyword in ['application', 'applied', 'confirmation', 'submitted']:
            try:
                st, dat = mail.search(None, 'SINCE', since_imap, 'SUBJECT', keyword)
                if st == 'OK' and dat[0]:
                    for mid in dat[0].split():
                        if mid not in seen:
                            seen.add(mid)
                            msg_ids.append(mid)
            except Exception as e:
                logger.debug(f"[Tracker] Search '{keyword}' failed: {e}")

        if not msg_ids:
            logger.info(f"[Tracker] No application emails found in {gmail_user}")
            mail.logout()
            return []
        logger.info(f"[Tracker] {gmail_user}: {len(msg_ids)} candidate emails to check")

        for msg_id in msg_ids:
            try:
                status, msg_data = mail.fetch(msg_id, '(RFC822)')
                if status != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                message_id = _decode(msg.get('Message-ID', '')) or str(msg_id)
                subject    = _decode(msg.get('Subject', ''))
                sender     = _decode(msg.get('From', ''))
                body       = _get_body(msg)

                title, company, app_status = _parse_application(subject, body, sender)
                if title and company:
                    job_url = _extract_job_url(msg)
                    results.append((title, company, datetime.now(), message_id,
                                    subject, sender, app_status, job_url))

            except Exception as e:
                logger.debug(f"[Tracker] Error processing email: {e}")

    except Exception as e:
        logger.error(f"[Tracker] IMAP error for {gmail_user}: {e}")
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass

    applied_count  = sum(1 for r in results if r[6] == 'applied')
    rejected_count = sum(1 for r in results if r[6] == 'rejected')
    logger.info(f"[Tracker] {gmail_user}: {applied_count} applications, {rejected_count} rejections found")
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def run_tracker(days_back=90):
    """
    Scan both Gmail accounts, extract job emails, update DB.
    Returns list of dicts describing what was processed.
    """
    seen_users = set()
    accounts = []
    for user, pwd, label in [
        (Config.GMAIL_USER,   Config.GMAIL_APP_PASSWORD,   Config.GMAIL_USER   or 'account1'),
        (Config.GMAIL_USER_2, Config.GMAIL_APP_PASSWORD_2, Config.GMAIL_USER_2 or 'account2'),
    ]:
        if user and user not in seen_users:
            accounts.append((user, pwd, label))
            seen_users.add(user)

    conn = _get_conn()
    _ensure_tables(conn)

    processed  = []
    applied_ct = 0
    rejected_ct = 0

    for user, password, label in accounts:
        emails = _scan_account(user, password, days_back=days_back)

        for title, company, date, message_id, subject, sender, app_status, job_url in emails:
            if _is_processed(conn, message_id):
                continue

            job_id = _find_or_create_job(conn, title, company, label,
                                         job_url=job_url, message_id=message_id)
            if job_id:
                _score_and_update(conn, job_id, title, company)
                _mark_status(conn, job_id, app_status)
                _mark_processed(conn, message_id, subject, sender)

                if app_status == 'applied':
                    applied_ct += 1
                else:
                    rejected_ct += 1

                processed.append({
                    'title':   title,
                    'company': company,
                    'job_id':  job_id,
                    'account': label,
                    'status':  app_status,
                })
                log_msg = f"[Tracker] {app_status.upper()}: {title} @ {company} (job_id={job_id})"
                logger.info(log_msg.encode('ascii', errors='replace').decode('ascii'))

    conn.close()
    print(f"[+] Tracker: {applied_ct} applications, {rejected_ct} rejections processed")
    return processed


def _safe_print(text):
    """Print with emoji/unicode stripped for Windows cp1252 terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('ascii', errors='replace').decode('ascii'))


def main():
    _safe_print("=" * 60)
    _safe_print("GMAIL APPLICATION TRACKER")
    _safe_print("=" * 60)
    _safe_print("[*] Scanning inboxes for job application emails...")

    results = run_tracker(days_back=90)

    if not results:
        _safe_print("[*] No new emails found")
        return 0

    applied  = [r for r in results if r['status'] == 'applied']
    rejected = [r for r in results if r['status'] == 'rejected']

    if applied:
        _safe_print(f"\n[+] Applied ({len(applied)}):")
        for r in applied:
            _safe_print(f"  + {r['title']} @ {r['company']}  ({r['account']})")

    if rejected:
        _safe_print(f"\n[-] Rejected ({len(rejected)}):")
        for r in rejected:
            _safe_print(f"  x {r['title']} @ {r['company']}  ({r['account']})")

    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
