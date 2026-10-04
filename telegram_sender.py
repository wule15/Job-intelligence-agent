#!/usr/bin/env python3
"""Send job digest summary to Telegram - only NEW jobs (no duplicates)."""

import html
import ipaddress
import socket
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from functools import lru_cache
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from pathlib import Path

from core import source_health
from core.config import PROJECT_ROOT, Config
from core.countries import COUNTRY_NAMES, location_countries
from core.job_filter import (
    ALWAYS_INCLUDE_SOURCES, content_drop_reason, fails_advert_language, fails_eligibility,
    is_flow_equipment_role,
    matches_region, requires_unspoken_language, scam_risk, title_drop_reason,
)
from core.job_normalize import (
    canonical_url, dedup_key, history_key, matches_exclusion, parse_exclusions,
)
from core.multilingual import fold_diacritics
from core.utils import force_utf8_streams, format_cv_label
from sources.ats import posting_is_open, reset_liveness_caches

# ── Digest composition ───────────────────────────────────────────────────────
# Slots are a ceiling, not a floor. A job only occupies one if it clears the
# score bar (see MIN_DIGEST_SCORE below). An unfilled slot means nothing
# qualified, and the run says so rather than padding with weak matches. A day
# where nothing clears the bar sends no jobs, only a one-line note.
DIGEST_SIZE = 15
MAX_PER_COMPANY = 2

# The separate direct-from-company digest. Kept small: it is a shortlist of
# the highest-signal jobs, not a second full feed.
DIRECT_DIGEST_SIZE = 10

# The score bar comes from .env: DIGEST_MIN_SCORE for the main and direct
# digests, REGIONAL_MIN_SCORE for the regional one. The default of 15 sits one
# step above the storage cutoff (MIN_RELEVANCE_SCORE = 10), because worth
# keeping on the dashboard is a lower bar than worth a Telegram message. At 15
# almost every stored job qualified, so the slot counts, not the bar, set the
# daily volume. Each getter reads the bar when it runs, so a test or a changed
# setting takes effect without a re-import. MIN_DIGEST_SCORE mirrors the
# setting as it was read at start-up.
MIN_DIGEST_SCORE = Config.DIGEST_MIN_SCORE

# Company careers boards, read from their applicant tracking system. Held to
# their own quota because they are the highest signal source and would
# otherwise be crowded out by aggregator volume.
ATS_SOURCES = {'Greenhouse', 'Lever', 'Ashby', 'SmartRecruiters', 'Workday', 'SuccessFactors'}

# Aggregators that repeatedly serve expired or low-signal listings (Indeed via
# Apify or the plain adapter, and JSearch), plus WeWorkRemotely, which fills the
# digest with senior US software roles far outside the target. They are still
# searched, but in the digest they only fill slots left over after ATS boards
# and the quality aggregators, so they can never dominate a day again. Matched
# as a substring so 'Apify / Indeed', 'Indeed' and 'JSearch' all qualify.
LOW_PRIORITY_SOURCE_MARKERS = ('indeed', 'jsearch', 'weworkremotely')

# Shown on a pump, valve or flow-equipment role, the profile most wanted. The
# database keeps no description, so the digest recognises these from the title
# and company; the scorer already lifted the ones it recognised from the text.
FLOW_EQUIPMENT_MARKER = "🔧 Pump / valve / flow equipment"

# Phrases a careers page or aggregator shows once a posting is gone. Presence
# of any one marks the link expired.
EXPIRED_PAGE_MARKERS = (
    'no longer accepting applications', 'no longer available',
    'this job has expired', 'job posting has expired', 'posting has expired',
    'position has been filled', 'this position is no longer',
    'posting is no longer active', 'job is no longer available',
    'this job is no longer', 'nicht mehr verfügbar',
)

# Liveness is checked when a job is chosen for a digest, not when it was
# found, because the digest sends jobs stored up to a week ago. Each check is
# one request, so a run makes at most LIVENESS_MAX_CHECKS of them and stops
# checking after LIVENESS_BUDGET_SECS. A job chosen after that is sent
# unverified, and the run log says how many were.
LIVENESS_MAX_CHECKS = 60
LIVENESS_BUDGET_SECS = 90


def is_low_priority_source(source):
    """True for the demoted aggregators (Indeed, JSearch, WeWorkRemotely)."""
    s = (source or '').lower()
    return any(marker in s for marker in LOW_PRIORITY_SOURCE_MARKERS)


def _is_internal_url(url):
    """
    True if a URL targets anything that is not a public web address: a non
    http(s) scheme, or a host that resolves to a private, loopback, link-local
    (this covers the cloud metadata address 169.254.169.254), or reserved IP.

    The liveness check visits apply links that come from scraped listings, i.e.
    a stranger chooses the address this machine fetches. Refusing internal
    targets stops that being used to probe the local network (an SSRF guard).
    """
    try:
        parts = urlparse(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            return True
        for info in socket.getaddrinfo(parts.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return True
        return False
    except Exception:
        # Unresolvable or malformed: not fetchable anyway, let it fall through
        # to a normal (failing) request rather than blocking a real host.
        return False


def _is_board_error_redirect(url):
    """True for the page Greenhouse redirects a closed posting to: the
    company's board with error=true, served with HTTP 200. Limited to
    Greenhouse hosts, the one place this was confirmed; on another site the
    same parameter could mean a login or a bot check."""
    parts = urlparse(url)
    return ((parts.hostname or '').endswith('greenhouse.io')
            and 'true' in parse_qs(parts.query).get('error', []))


@lru_cache(maxsize=2048)
def check_link_live(url, timeout=6):
    """
    Best-effort check that a job link still points at a live posting.

    Returns False ONLY when the posting is clearly gone: an HTTP 404 or 410, or
    a 200 page whose text says the role is filled or expired. Anything
    ambiguous, a timeout, a bot block, or any other non-200, returns True.
    Dropping a real job over a transient error is worse than letting one stale
    link through, so the check only removes what it can prove is dead. A link to
    a private/internal address is refused outright (see _is_internal_url).

    Redirects are followed by hand, one hop at a time, so the internal-address
    guard is applied to every hop and not just the first, which is what closes
    the redirect-to-internal SSRF bypass. Cached per run so the same URL is
    fetched at most once.

    What it catches: a real HTTP 404 or 410 (RemoteOK and The Muse do this),
    a 200 page carrying explicit expired text, and the Greenhouse redirect to
    the board with "?error=true" that stands for a closed posting. What it
    does NOT catch: any other soft 404 that answers 200 and quietly serves an
    index page (Jobicy does this, and so does every Ashby page), and a board
    that bot-blocks the request (Indeed answers 200 with a block page). Both
    read as live here.
    """
    if not url:
        return True
    current = url
    for _hop in range(5):
        if _is_internal_url(current):
            return False  # never fetch a private/internal address
        try:
            resp = requests.get(
                current, timeout=timeout, allow_redirects=False,
                headers={'User-Agent': 'Mozilla/5.0 (job-digest liveness check)'})
        except Exception:
            return True
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get('Location')
            if not location:
                return True
            current = urljoin(current, location)
            if _is_board_error_redirect(current):
                return False
            continue
        if resp.status_code in (404, 410):
            return False
        if resp.status_code != 200:
            return True
        return not any(marker in resp.text.lower() for marker in EXPIRED_PAGE_MARKERS)
    return True  # too many redirects is not proof the job is gone


_liveness = {}


def reset_liveness():
    """Start a fresh check budget and forget earlier answers. Once per run."""
    _liveness.clear()
    _liveness.update(results={}, closed=[], closed_page=[], unverified=set(), checked=0,
                     deadline=time.monotonic() + LIVENESS_BUDGET_SECS)
    reset_liveness_caches()


def is_job_live(link, source='', company=''):
    """
    Whether a job chosen for a digest is still open.

    A company-board job asks its vendor's API (posting_is_open), and falls
    back to the page check when the vendor cannot tell. Every other job gets
    the page check (check_link_live). A hand-saved job is never checked: you
    keep a job you chose even if its link has gone stale.

    False only on proof the posting is closed. Each link is checked once per
    run, and repeat answers come from memory without spending the budget.
    Past the budget the job is accepted unverified. Never raises.

    A closure the vendor's API proved is listed under 'closed', and the row
    is deleted at the end of the run. A closure only the page check saw is
    listed under 'closed_page': the job is skipped today and checked again
    tomorrow, because the page check reads loose markers such as "no longer
    available" anywhere in the page.
    """
    if source in ALWAYS_INCLUDE_SOURCES or not link:
        return True
    try:
        if not _liveness:
            reset_liveness()
        results = _liveness['results']
        if link in results:
            return results[link]
        if (_liveness['checked'] >= LIVENESS_MAX_CHECKS
                or time.monotonic() > _liveness['deadline']):
            _liveness['unverified'].add(link)
            return True
        _liveness['checked'] += 1
        verdict = posting_is_open(link, source, company) if source in ATS_SOURCES else None
        proven_by = 'closed'
        if verdict is None:
            verdict = check_link_live(link)
            proven_by = 'closed_page'
        verdict = verdict is not False
        results[link] = verdict
        if not verdict:
            _liveness[proven_by].append(link)
        return verdict
    except Exception as e:
        print(f"[!] Liveness check failed, sending unchecked: {type(e).__name__}")
        return True


def liveness_summary():
    """This run's checks so far: how many were made, the links the vendor
    proved closed, the links only the page check read as closed, and how
    many chosen jobs went unverified because the budget was spent."""
    return {
        'checked': _liveness.get('checked', 0),
        'closed': list(_liveness.get('closed', ())),
        'closed_page': list(_liveness.get('closed_page', ())),
        'unverified': len(_liveness.get('unverified', ())),
    }


def drop_closed_postings(links):
    """
    Delete the unsent rows whose posting the vendor's API proved closed.

    Left in place, a closed job would be checked again on every run until
    the weekly cleanup removed it. Its link stays in job_links_seen, so a
    later search does not store the same posting again. Only vendor proof
    is passed in (see is_job_live): a page that merely reads as closed is
    skipped for the day, not deleted. A row that was sent, or has a cover
    letter, is never deleted. Each deleted row is logged with its full link.
    Never fatal. Returns how many rows were deleted.
    """
    links = list(dict.fromkeys(link for link in links if link))
    if not links:
        return 0
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            placeholders = ','.join('?' for _ in links)
            rows = conn.execute(f"""
                SELECT id, job_title, company, link FROM jobs
                WHERE link IN ({placeholders})
                  AND id NOT IN (SELECT job_id FROM telegram_sent_jobs)
                  AND id NOT IN (SELECT job_id FROM cover_letters_sent)""", links).fetchall()
            for job_id, title, company, link in rows:
                print(f"[closed] id={job_id} | {title} | {company} | {link}")
            if rows:
                conn.executemany('DELETE FROM jobs WHERE id = ?', [(row[0],) for row in rows])
                conn.commit()
            return len(rows)
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Could not remove closed postings: {type(e).__name__}")
        return 0


def init_telegram_tracking():
    """
    Create the two tables that record what has been sent.

    telegram_sent_jobs holds one row per stored job id that the digests must
    skip: jobs sent, and jobs held back as repeats, exclusions or no longer
    eligible. Its rows go
    when the daily cleanup deletes their job, so on its own it cannot stop a
    resend after a week.

    sent_history is the durable record of what actually reached the chat:
    one row per job sent, with its dedup key, link, title, company, location
    and date. The cleanup never touches it, and only mark_jobs_sent writes
    to it.
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telegram_sent_jobs (
                    job_id INTEGER PRIMARY KEY,
                    sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
            """)
            conn.commit()
            _init_sent_history(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Error initializing tracking: {e}")


def _init_sent_history(conn):
    """
    Create sent_history, and fill it once from the sends still on record.

    The fill runs only in the call that creates the table, inside the same
    transaction, so it happens exactly once. That matters: later on,
    telegram_sent_jobs also holds jobs that were held back and never sent,
    and copying those would record them as sends. Before this table existed,
    telegram_sent_jobs held real sends only, so the first copy is safe. It
    reaches back as far as the jobs table does, about a week.

    One row per send, not one per key: the same title at the same company
    can be sent again for another site or after the repeat window (see
    suppress_repeats), and each send's link must stay on record.
    """
    tables = {name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if 'sent_history' in tables:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(sent_history)')}
        if 'location' not in columns:
            conn.execute('ALTER TABLE sent_history ADD COLUMN location TEXT')
            conn.commit()
        return
    conn.execute('BEGIN')
    try:
        conn.execute("""
            CREATE TABLE sent_history (
                id INTEGER PRIMARY KEY,
                dedup_key TEXT,
                link TEXT,
                title TEXT,
                company TEXT,
                location TEXT,
                sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_history_link ON sent_history(link)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_history_key ON sent_history(dedup_key)')
        if 'jobs' in tables:
            conn.execute("""
                INSERT INTO sent_history (dedup_key, link, title, company, location, sent_date)
                SELECT j.dedup_key, j.link, j.job_title, j.company, j.location, t.sent_date
                FROM telegram_sent_jobs t JOIN jobs j ON j.id = t.job_id
                WHERE j.dedup_key IS NOT NULL AND j.dedup_key != ''
            """)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def load_exclusions():
    """
    Read the optional list of jobs handled outside the digest.

    The path comes from Config.DIGEST_EXCLUDE_FILE. A relative path resolves
    against the core folder, never the working directory, because the
    scheduled task does not run from there. An empty setting reads nothing.
    A missing or unreadable file prints one warning line and the digest goes
    out without it: a broken list must never stop the day's jobs.

    Returns a list of (company, title) pairs.
    """
    setting = (Config.DIGEST_EXCLUDE_FILE or '').strip()
    if not setting:
        return []
    path = Path(setting)
    if not path.is_absolute():
        path = Path(PROJECT_ROOT) / path
    if not path.exists():
        print(f"[!] Exclusion list not found, sending without it: {path}")
        return []
    try:
        # utf-8-sig, because a file saved from PowerShell starts with a byte
        # order mark that would otherwise stick to the first company name.
        return parse_exclusions(path.read_text(encoding='utf-8-sig'))
    except Exception as e:
        print(f"[!] Could not read the exclusion list, sending without it: {type(e).__name__}")
        return []


# How long a title and company already sent hold back a new posting with the
# same title at the same company. A link already sent is held back for good.
REPEAT_TITLE_WINDOW_DAYS = 45


def _has_title(key):
    """False for a history key whose title part is empty. "Intern - Sales"
    and "Internship (m/f/div.)" normalise to nothing, and an empty title
    would make every such posting at the company look like one job."""
    return bool(key.split('|', 1)[0].strip())


def _placed_countries(location):
    """The countries a location names, without region tokens."""
    return {code for code in location_countries(location or '') if code in COUNTRY_NAMES}


def _sent_from_here(earlier_sends, countries):
    """True when one of the earlier sends of a title could be this posting:
    its location or this one is unknown, or they share a country."""
    return any(not sent or not countries or sent & countries for sent in earlier_sends)


def suppress_repeats(exclusions=()):
    """
    Hold back every unsent job that has already reached the chat, or that is
    on the exclusion list, before any digest is chosen.

    A repeat is a job whose link matches a link in sent_history, at any time,
    or whose title and company (accents folded) match a send from the last
    REPEAT_TITLE_WINDOW_DAYS days in the same country. That catches a posting
    that comes back under a new link after the cleanup deleted the first
    copy, which is how the same job used to be sent twice, days or weeks
    apart. The title part is the normalised title, which drops brackets and
    anything after " - ", so "Sales Engineer - Germany" and "Sales Engineer -
    Netherlands" share it. The country and the window are what let the
    second site, or the same title reposted months later, through. A title
    that normalises to nothing is never matched on title. A different
    opening at the same company still gets through. Hand-saved jobs are held
    to the same rule.

    An excluded job matches the private list of jobs handled elsewhere (see
    matches_exclusion for how loose that match is).

    Held-back ids go into telegram_sent_jobs, which all three digest queries
    already skip, so their selection code needs no change. They are not added
    to sent_history, because they were never sent. Each one is logged with
    its full link, so a wrong match can be spotted in the run log.

    Never fatal: on any error the digest goes out unfiltered. Returns how many
    jobs were held back.
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            recent, sent_links = defaultdict(list), set()
            for key, link, title, company, location, in_window in conn.execute(
                    """SELECT dedup_key, link, title, company, location,
                              COALESCE(julianday(sent_date) >= julianday('now', ?), 1)
                       FROM sent_history""", (f'-{REPEAT_TITLE_WINDOW_DAYS} days',)):
                canonical = canonical_url(link or '')
                if canonical:
                    sent_links.add(canonical)
                if not in_window:
                    continue
                countries = _placed_countries(location)
                for candidate in {fold_diacritics(key or ''), history_key(title, company)}:
                    if _has_title(candidate):
                        recent[candidate].append(countries)

            held = []
            for job_id, title, company, link, location in conn.execute("""
                    SELECT id, job_title, company, link, location FROM jobs
                    WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)""").fetchall():
                canonical = canonical_url(link or '')
                key = history_key(title, company)
                if ((canonical and canonical in sent_links)
                        or (_has_title(key) and key in recent
                            and _sent_from_here(recent[key], _placed_countries(location)))):
                    reason = 'repeat'
                elif exclusions and matches_exclusion(title, company, exclusions):
                    reason = 'excluded'
                else:
                    continue
                held.append(job_id)
                print(f"[{reason}] id={job_id} | {title} | {company} | {link or ''}")

            if held:
                conn.executemany("""
                    INSERT INTO telegram_sent_jobs (job_id) VALUES (?)
                    ON CONFLICT(job_id) DO NOTHING
                """, [(job_id,) for job_id in held])
                conn.commit()
            return len(held)
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Could not check for repeats, sending unfiltered: {type(e).__name__}")
        return 0

def suppress_ineligible():
    """
    Hold back every unsent job that breaks a hard rule today.

    Jobs are filtered once, when they are found, and stored for a week. A rule
    added or tightened later never reached the rows already stored, so for up
    to a week they kept going out: export-controlled roles were sent for two
    days after the export-control rule shipped. This runs the same hard rules
    (the title rules, dealbreakers, export control, work eligibility, excluded
    locations, the country allow-list, a required language, an advert written
    in a language the user cannot read, too many required years, an
    electrical-only degree) on every unsent row before any digest is chosen.

    Held-back ids go into telegram_sent_jobs, like repeats, so the selection
    code needs no change, and each one is logged with its reason and full
    link. Hand-saved jobs are exempt, as they are from the filter. No network
    call. Never fatal: on any error the digest goes out unchecked. Returns how
    many jobs were held back.
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            rows = conn.execute("""
                SELECT id, job_title, company, link, source, description, location
                FROM jobs
                WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)""").fetchall()
            held = []
            for job_id, title, company, link, source, description, location in rows:
                if source in ALWAYS_INCLUDE_SOURCES:
                    continue
                reason = (title_drop_reason(title, source or '')
                          or fails_eligibility(title, description, location))
                if not reason and requires_unspoken_language(
                        f"{title or ''}\n{description or ''}"):
                    reason = 'language_required'
                if not reason and fails_advert_language(description or ''):
                    reason = 'advert_language'
                if not reason:
                    reason = content_drop_reason(title or '', description or '')
                if reason:
                    held.append(job_id)
                    print(f"[ineligible:{reason}] id={job_id} | {title} | {company} | {link or ''}")
            if held:
                conn.executemany("""
                    INSERT INTO telegram_sent_jobs (job_id) VALUES (?)
                    ON CONFLICT(job_id) DO NOTHING
                """, [(job_id,) for job_id in held])
                conn.commit()
            return len(held)
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Could not recheck eligibility, sending unchecked: {type(e).__name__}")
        return 0


def get_unsent_jobs(limit=DIGEST_SIZE, min_score=None, is_live=None):
    """
    Choose the day's digest: a general pull of the best available jobs.

    This is the regular feed, and it is meant to read as a straight best-of
    across every source. Company careers boards are NOT given a guaranteed
    floor here: they compete on score like everything else. That keeps this
    digest distinct from the separate direct-from-company digest (which is
    company boards only), so the same role does not headline both messages.

    Two rails are kept, because both prevent a real failure seen in practice:
      - a per-company cap, so one employer listing 200 roles cannot fill the
        digest with itself
      - Indeed / JSearch held to a last-resort backfill, so their volume can
        never crowd out the higher-signal sources

    Order of selection:
      1. Jobs you saved by hand, always, ignoring score
      2. Best available by score from the preferred sources (company boards +
         quality aggregators), capped per company
      3. Last resort: the demoted aggregators (Indeed / JSearch), only if the
         digest is still short

    The score gate keeps it honest: the cap is a ceiling, never a floor. If
    only two jobs clear the bar, you get two, not two plus padding. An empty
    slot is information.

    Jobs already sent in the direct-from-company digest this run are excluded
    by the telegram_sent_jobs table (the direct digest marks them sent before
    this query runs), so a company job never appears in both messages.

    is_live: optional callable(link, source, company) -> bool, normally
    is_job_live. When given, a candidate it rejects is skipped as closed and
    the next one takes the slot. Company-board jobs are checked too: a board
    lists only open roles on the day it is read, and the digest sends jobs
    stored days earlier. Hand-saved jobs are not checked. Left None (the
    default) there is no network call, which keeps the unit tests offline.

    min_score left None means Config.DIGEST_MIN_SCORE, read now.

    Returns a list of (id, title, company, score, link, best_cv, source) tuples.
    """
    if min_score is None:
        min_score = Config.DIGEST_MIN_SCORE
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, job_title, company, relevance_score, link, best_cv, source, scam_risk
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT 400
        """)
        all_jobs = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[!] Error fetching jobs: {type(e).__name__}: {e}")
        return []

    if not all_jobs:
        return []

    selected = []
    chosen_ids = set()
    # Counted across every pass, not per pass. Tracking it per pass let one
    # employer take its cap in the board quota and its cap again in the
    # wildcard, which is the concentration this function exists to prevent.
    company_counts = defaultdict(int)

    def take(candidates, slots, per_company):
        """Fill up to `slots` from `candidates`, respecting the company cap.

        A candidate is dropped when the liveness check proves its posting
        closed. Hand-saved jobs skip that check: a job you saved by hand you
        keep even if its link has since gone stale.
        """
        taken = 0
        for job in candidates:
            if taken >= slots or len(selected) >= limit:
                break
            if job[0] in chosen_ids:
                continue
            company = (job[2] or 'Unknown').strip().lower()
            if company_counts[company] >= per_company:
                continue
            if (is_live and job[6] not in ALWAYS_INCLUDE_SOURCES
                    and not is_live(job[4], job[6], job[2])):
                continue
            selected.append(job)
            chosen_ids.add(job[0])
            company_counts[company] += 1
            taken += 1
        return taken

    # 1. Manually saved jobs bypass everything. You already chose these.
    take([j for j in all_jobs if j[6] in ALWAYS_INCLUDE_SOURCES],
         slots=limit, per_company=limit)

    # Everything else must clear the score bar to be eligible at all.
    qualified = [j for j in all_jobs if (j[3] or 0) >= min_score]

    # A general pull of the best available, highest score first (all_jobs
    # arrives already sorted by score DESC). Company boards and quality
    # aggregators compete together on merit; only Indeed / JSearch are held
    # back to a last-resort backfill so their volume cannot crowd out the rest.
    preferred = [j for j in qualified if not is_low_priority_source(j[6])]
    low_priority = [j for j in qualified if is_low_priority_source(j[6])]

    take(preferred, limit - len(selected), MAX_PER_COMPANY)
    take(low_priority, limit - len(selected), MAX_PER_COMPANY)

    if len(selected) < limit:
        # Short digest: little cleared the score bar, or the per-company cap
        # bit. Worth seeing rather than padding the message with weak matches.
        print(f"[*] Digest short: {len(selected)}/{limit} filled "
              f"(score bar {min_score}, max {MAX_PER_COMPANY} per company)")

    # Keep the source column (job[6]) so the digest can show where each job came
    # from. Returns (id, title, company, score, link, best_cv, source) tuples.
    return [job[:8] for job in selected]

def get_direct_jobs(limit=DIRECT_DIGEST_SIZE, min_score=None, is_live=None):
    """
    The direct-from-company shortlist: unsent jobs from company careers boards
    only (ATS_SOURCES), best first, capped per company.

    These are the highest-signal jobs in the system. A current opening posted
    by a company on its own site puts an application in front of that
    company's recruiter, not an aggregator's copy. They get their own message
    so aggregator volume cannot bury them.

    Same score gate and per-company cap as the main digest, so a slot is never
    padded with a weak match. An empty result is normal, and the run says so
    rather than sending a filler message.

    is_live works as in get_unsent_jobs: a job it proves closed gives its slot
    to the next one, and None (the default) makes no network call.

    min_score left None means Config.DIGEST_MIN_SCORE, read now.

    Returns (id, title, company, score, link, best_cv, source, scam_risk) tuples.
    """
    if min_score is None:
        min_score = Config.DIGEST_MIN_SCORE
    sources = list(ATS_SOURCES)
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        cursor = conn.cursor()
        placeholders = ','.join('?' for _ in sources)
        cursor.execute(f"""
            SELECT id, job_title, company, relevance_score, link, best_cv, source, scam_risk
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)
              AND source IN ({placeholders})
              AND relevance_score >= ?
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT 200
        """, (*sources, min_score))
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[!] Error fetching direct jobs: {type(e).__name__}: {e}")
        return []

    selected = []
    company_counts = defaultdict(int)
    for job in rows:
        if len(selected) >= limit:
            break
        company = (job[2] or 'Unknown').strip().lower()
        if company_counts[company] >= MAX_PER_COMPANY:
            continue
        if is_live and not is_live(job[4], job[6], job[2]):
            continue
        selected.append(job)
        company_counts[company] += 1

    return [job[:8] for job in selected]

def get_regional_jobs(limit=DIGEST_SIZE, min_score=None, is_live=None):
    """
    The home-market shortlist: unsent jobs whose location matches the user's
    region terms, best first, capped per company.

    Region terms come from Config.REGIONAL_MATCH_TERMS (falling back to
    REGIONAL_JOB_LOCATIONS). Both are empty in the public default, so this
    returns nothing and the third message never appears until the user sets
    them in their .env. That keeps the shared engine generic while giving the
    user a dedicated feed for jobs the global sources under-serve (e.g. the
    Balkans, which Adzuna does not cover).

    Same per-company cap and optional is_live check as the other digests. The
    score bar is its own: min_score left None means Config.REGIONAL_MIN_SCORE,
    read now. Returns (id, title, company, score, link, best_cv, source,
    scam_risk) tuples.
    """
    if min_score is None:
        min_score = Config.REGIONAL_MIN_SCORE
    terms = Config.REGIONAL_MATCH_TERMS or Config.REGIONAL_JOB_LOCATIONS
    if not terms:
        return []
    # Match the region IN the query, not only in Python afterwards. If we took
    # the top 400 unsent jobs by score first and filtered after, a backlog of
    # higher-scoring non-region jobs could fill that whole window and hide every
    # lower-scoring region job, leaving this digest empty though qualifying jobs
    # exist (the direct digest avoids this by filtering source in SQL). The
    # query calls matches_region itself, registered as an SQL function, so the
    # score order and LIMIT apply within region jobs and the query and the
    # check below agree. A LIKE clause used to stand in for it, and LIKE can
    # neither match whole words nor ignore accents.
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        conn.create_function(
            'in_region', 1, lambda location: matches_region(location, terms),
            deterministic=True)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, job_title, company, relevance_score, link, best_cv, source,
                   scam_risk, location
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM telegram_sent_jobs)
              AND relevance_score >= ?
              AND location IS NOT NULL AND location != ''
              AND in_region(location)
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT 400
        """, (min_score,))
        rows = cursor.fetchall()
        conn.close()
    except Exception as e:
        print(f"[!] Error fetching regional jobs: {type(e).__name__}: {e}")
        return []

    selected = []
    company_counts = defaultdict(int)
    for job in rows:
        if len(selected) >= limit:
            break
        if not matches_region(job[8], terms):
            continue
        company = (job[2] or 'Unknown').strip().lower()
        if company_counts[company] >= MAX_PER_COMPANY:
            continue
        if is_live and not is_live(job[4], job[6], job[2]):
            continue
        # Drop the location column so the tuple matches the shared renderer.
        selected.append(job[:8])
        company_counts[company] += 1

    return selected

def mark_jobs_sent(job_ids):
    """
    Record jobs as sent, after Telegram accepted the message.

    telegram_sent_jobs is written and committed first, so today's later
    digests skip the job even if the second write fails. Then sent_history,
    in its own transaction, so no later run sends the job again whatever the
    cleanup deletes (see _record_sends). This is the only writer of
    sent_history, which is what keeps it meaning "reached the chat".
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            conn.executemany("""
                INSERT INTO telegram_sent_jobs (job_id)
                VALUES (?)
                ON CONFLICT(job_id) DO NOTHING
            """, [(job_id,) for job_id in job_ids])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Error marking jobs sent: {e}")
        return False
    _record_sends(job_ids)
    return True


def _record_sends(job_ids):
    """
    Add one sent_history row per job just sent. A failure here (a locked
    database, a table the start-up could not create) is logged and does not
    undo the telegram_sent_jobs rows already committed.
    """
    try:
        conn = sqlite3.connect(Config.DATABASE_PATH)
        try:
            for job_id in job_ids:
                row = conn.execute(
                    'SELECT dedup_key, link, job_title, company, location FROM jobs WHERE id = ?',
                    (job_id,)).fetchone()
                if row:
                    key, link, title, company, location = row
                    conn.execute("""
                        INSERT OR IGNORE INTO sent_history (dedup_key, link, title, company, location)
                        VALUES (?, ?, ?, ?, ?)
                    """, (key or dedup_key(title, company), link, title, company, location))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[!] Could not add the sent jobs to the send history: {type(e).__name__}")

def send_telegram_message(message):
    """Send message to Telegram."""
    if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
        print("[!] Telegram credentials not configured. Skipping message.")
        return False

    url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': Config.TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            print("[+] Telegram message sent successfully!")
            return True
        else:
            print(f"[!] Telegram error: HTTP {response.status_code}")
            return False
    except Exception as e:
        # Print the exception class only. requests puts the full request URL
        # into connection errors, and that URL contains the bot token.
        print(f"[!] Error sending Telegram message: {type(e).__name__}")
        return False

def _render_job_lines(jobs):
    """
    Render the numbered job list shared by both digests.

    One renderer, so the main and direct-company digests can never drift apart
    in how a job is shown. Expects (id, title, company, score, link, best_cv,
    source) tuples.
    """
    lines = ""
    for i, (job_id, title, company, score, link, best_cv, source, scam_flag) in enumerate(jobs, 1):
        score_pct = round(score, 1) if score else 0
        # Trust the flag the scorer persisted (it saw the full description), and
        # recompute from the visible fields as a backup. The scorer already sank
        # this job; the marker tells you why so you verify before applying.
        risky = bool(scam_flag) or scam_risk(title, company=company, link=link or '')
        flag = " ⚠️" if risky else ""
        # The message is sent as HTML, so every scraped value is escaped. A
        # "<" in a title would otherwise make Telegram reject the whole
        # message, and a quote mark in a link would cut the link short. The
        # checks above run on the raw values.
        lines += f"<b>{i}. {html.escape(title or '')}{flag}</b>\n"
        lines += f"   💼 {html.escape(company or '')}\n"
        lines += f"   ⭐ Match: {score_pct}%\n"
        if risky:
            lines += "   ⚠️ Possible scam, verify the employer before applying\n"
        if is_flow_equipment_role(title or '', '', company or ''):
            lines += f"   {FLOW_EQUIPMENT_MARKER}\n"
        if source:
            lines += f"   🌐 {html.escape(source)}\n"
        cv_label = format_cv_label(best_cv)
        if cv_label:
            lines += f"   📄 CV: {html.escape(cv_label)}\n"
        if link:
            # The full link, escaped for the attribute; Telegram decodes it
            # back. It stays behind "View Job" rather than shown as text,
            # because visible text counts toward Telegram's 4096 character
            # message limit and a long ATS link can be 150 characters.
            lines += f"   🔗 <a href=\"{html.escape(link, quote=True)}\">View Job</a>\n"
        lines += "\n"
    return lines

def format_job_digest(jobs):
    """Format the main job digest for Telegram."""
    # A label lets two engines running side by side be told apart in the chat.
    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    if not jobs:
        message = f"📭 <b>No new jobs found</b>{label}\n\nAll caught up, check again tomorrow!"
        return message, []

    job_ids = [job[0] for job in jobs]
    message = f"🔍 <b>Daily Job Digest</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> new jobs:\n\n"
    message += _render_job_lines(jobs)
    return message, job_ids

def format_direct_digest(jobs):
    """
    Format the direct-from-company digest for Telegram.

    Returns (message, job_ids). job_ids is empty when there is nothing to
    send, which the caller reads as "skip this message" rather than posting a
    filler note every day.
    """
    if not jobs:
        return "", []

    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    job_ids = [job[0] for job in jobs]
    message = f"🏢 <b>Direct company openings</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> straight from company careers pages:\n\n"
    message += _render_job_lines(jobs)
    return message, job_ids

def format_regional_digest(jobs):
    """
    Format the home-market (regional) digest for Telegram.

    Returns (message, job_ids). job_ids is empty when there is nothing to send,
    which the caller reads as "skip this message" rather than posting a filler
    note every day.
    """
    if not jobs:
        return "", []

    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    header = Config.REGIONAL_DIGEST_LABEL or "Regional jobs"
    job_ids = [job[0] for job in jobs]
    message = f"🌍 <b>{header}</b>{label}\n"
    message += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M')}</i>\n\n"
    message += f"<b>{len(jobs)}</b> from your region:\n\n"
    message += _render_job_lines(jobs)
    return message, job_ids

def format_health_note(alerts):
    """The source health note appended to the digest, or '' when all is well.

    Escaped, because the message is sent as HTML and an error string can
    contain angle brackets.
    """
    if not alerts:
        return ''
    lines = '\n'.join(f"• {html.escape(alert)}" for alert in alerts)
    return f"\n⚠️ <b>Source health</b>\n{lines}\n"


def _health_note():
    """The note for this run. A failure here must never stop the digest."""
    try:
        return format_health_note(source_health.health_alerts())
    except Exception as e:
        print(f"[!] Could not read source health: {type(e).__name__}")
        return ''


def format_quiet_day_note(main_bar, regional_bar):
    """
    The one message on a day when no digest has a job to send.

    Without it a day where nothing cleared the score bar looks the same in
    the chat as a run that never happened. Both bars are named, so a long run
    of quiet days points at a bar set too high rather than a broken run.
    """
    label = f" {Config.DIGEST_LABEL}" if Config.DIGEST_LABEL else ""
    return (f"📭 <b>No new jobs today</b>{html.escape(label)}\n"
            f"No job cleared the score bar (main and direct {_fmt_score(main_bar)}, "
            f"regional {_fmt_score(regional_bar)}).")


def _fmt_score(value):
    """A score for the run log: 40 rather than 40.0, 32.8 kept as it is."""
    try:
        return f"{float(value or 0):g}"
    except (TypeError, ValueError):
        return '0'


def _log_selected(tag, jobs):
    """One run-log line per chosen job, with its score and full link, so any
    job in the chat can be traced back and opened from the log alone, and a
    score bar can be chosen later from the log without opening the database.
    Format: [tag] id=N | score=S | title | company | link"""
    for job in jobs:
        print(f"[{tag}] id={job[0]} | score={_fmt_score(job[3])} | "
              f"{job[1]} | {job[2]} | {job[4] or ''}")


def _report_liveness():
    """Log this run's liveness checks and remove the rows the vendor proved
    closed. A page that only read as closed is logged and kept for a recheck."""
    summary = liveness_summary()
    print(f"[*] Liveness: {summary['checked']} checked, {len(summary['closed'])} closed, "
          f"{len(summary['closed_page'])} skipped as closed by the page check, "
          f"{summary['unverified']} sent unverified (check budget spent)")
    for link in summary['closed_page']:
        print(f"[closed-page, skipped today] {link}")
    drop_closed_postings(summary['closed'])


def main():
    force_utf8_streams()  # Serbian job titles must not crash a cp1252 console/log
    print("[*] Initializing Telegram tracking...")
    init_telegram_tracking()
    print(f"[*] Score bar: main/direct {_fmt_score(Config.DIGEST_MIN_SCORE)}, "
          f"regional {_fmt_score(Config.REGIONAL_MIN_SCORE)}")

    # Before any digest is chosen: hold back jobs already sent on an earlier
    # day, and jobs on the private list of ones handled elsewhere.
    held = suppress_repeats(load_exclusions())
    if held:
        print(f"[*] Held back {held} jobs already sent or handled")
    ineligible = suppress_ineligible()
    if ineligible:
        print(f"[*] Held back {ineligible} stored jobs that fail today's hard rules")

    # Every job chosen below is checked for a closed posting first, within
    # one budget for the whole run.
    reset_liveness()

    # Direct-from-company digest goes first, and marks its jobs sent before the
    # main digest is selected. That ordering is what stops the same job showing
    # up in both messages: the main digest only ever sees jobs not yet sent.
    print("[*] Fetching direct-from-company jobs...")
    direct = get_direct_jobs(is_live=is_job_live)
    _log_selected('direct', direct)
    direct_msg, direct_ids = format_direct_digest(direct)
    if direct_ids:
        print(f"[*] Sending {len(direct_ids)} direct-company jobs...")
        if send_telegram_message(direct_msg):
            print(f"[+] Marking {len(direct_ids)} direct jobs as sent...")
            mark_jobs_sent(direct_ids)
        else:
            print("[!] Direct digest failed to send - not marking those jobs sent")
    else:
        print("[*] No direct-from-company jobs today")

    # Regional (home-market) digest goes next, and like the direct one marks its
    # jobs sent before the main digest is selected, so a regional job is never
    # repeated in the general feed. Inert unless the user set the region terms.
    print("[*] Fetching regional jobs...")
    regional = get_regional_jobs(is_live=is_job_live)
    _log_selected('regional', regional)
    regional_msg, regional_ids = format_regional_digest(regional)
    if regional_ids:
        print(f"[*] Sending {len(regional_ids)} regional jobs...")
        if send_telegram_message(regional_msg):
            print(f"[+] Marking {len(regional_ids)} regional jobs as sent...")
            mark_jobs_sent(regional_ids)
        else:
            print("[!] Regional digest failed to send - not marking those jobs sent")
    else:
        print("[*] No regional jobs today")

    print("[*] Fetching unsent jobs...")
    jobs = get_unsent_jobs(limit=10, is_live=is_job_live)
    _log_selected('main', jobs)
    _report_liveness()

    digest, job_ids = format_job_digest(jobs)

    # A broken source is named in the chat, not only in the log. On a day with
    # no main digest the note goes out alone, so a dead source is never silent.
    health = _health_note()

    if not job_ids:
        print("[*] No new jobs to send")
        if not direct and not regional:
            # Nothing cleared the bar in any digest. One short line, with the
            # health note if there is one, so a quiet day is not mistaken for
            # a run that never happened.
            print("[*] No job cleared the score bar today")
            send_telegram_message(format_quiet_day_note(
                Config.DIGEST_MIN_SCORE, Config.REGIONAL_MIN_SCORE) + health.rstrip())
        elif health:
            print("[*] Sending source health note...")
            send_telegram_message(health.strip())
        return

    digest += health

    print(f"[*] Found {len(job_ids)} new jobs to send")
    print("[*] Sending to Telegram...")

    if send_telegram_message(digest):
        print(f"[+] Marking {len(job_ids)} jobs as sent...")
        mark_jobs_sent(job_ids)
    else:
        print("[!] Failed to send - not marking jobs as sent")

if __name__ == '__main__':
    main()
