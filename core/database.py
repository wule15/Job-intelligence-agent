"""
SQLite database management for Job Search + Cover Letter system.
Handles schema creation, operations, and idempotency.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from core.config import Config
from core.job_normalize import dedup_key

class Database:
    """Database operations for job search and cover letter system."""

    def __init__(self, db_path=None):
        """Initialize database connection."""
        self.db_path = db_path or Config.DATABASE_PATH
        self.connection = None
        self.connect()
        self._run_migrations()

    # One shared connection per database path, per process. A run creates
    # several Database() objects (the searcher, the JSearch source, the Gmail
    # source), and separate SQLite connections all writing the same file under
    # WAL is what produced "database is locked" on every storage write. Sharing
    # one connection makes the process a single writer, which SQLite is happy
    # with. check_same_thread is off so the shared handle survives the (rare)
    # cross-thread use; the run is effectively single-threaded for writes.
    _shared = {}

    def connect(self):
        """Connect to database, reusing a shared per-path connection."""
        conn = Database._shared.get(self.db_path)
        if conn is None:
            # timeout: sqlite3's default busy handler is 0, so a transient lock
            # (Defender scan, WAL checkpoint) raised immediately. 30s of patient
            # retry plus a single writer eliminates "database is locked".
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 30000;")
            Database._shared[self.db_path] = conn
        self.connection = conn

    def _run_migrations(self):
        """Run schema migrations on every startup, safe to call repeatedly."""
        cursor = self.connection.cursor()
        try:
            # Ensure core tables exist (idempotent)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    job_title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    description TEXT,
                    link TEXT UNIQUE,
                    salary TEXT,
                    source TEXT,
                    extracted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    relevance_score REAL DEFAULT 0.0,
                    best_cv TEXT,
                    seen_count INTEGER DEFAULT 1,
                    dedup_key TEXT,
                    scam_risk INTEGER DEFAULT 0
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS job_links_seen (
                    link TEXT PRIMARY KEY,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    job_id INTEGER,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                )
            ''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_job_links_seen ON job_links_seen(link)')

            # Add new columns to existing tables (ignore if already present)
            for col, definition in [
                ('best_cv', 'TEXT'),
                ('seen_count', 'INTEGER DEFAULT 1'),
                ('dedup_key', 'TEXT'),
                ('scam_risk', 'INTEGER DEFAULT 0'),
                ('location', 'TEXT'),
            ]:
                try:
                    cursor.execute(f'ALTER TABLE jobs ADD COLUMN {col} {definition}')
                except Exception:
                    pass

            # Backfill dedup_key for rows stored before the column existed.
            cursor.execute(
                'SELECT id, job_title, company FROM jobs WHERE dedup_key IS NULL OR dedup_key = ""')
            backfill = [
                (dedup_key(title, company), row_id)
                for row_id, title, company in cursor.fetchall()
            ]
            if backfill:
                cursor.executemany('UPDATE jobs SET dedup_key = ? WHERE id = ?', backfill)

            # Unique on the normalised key, not the raw strings. "Sales
            # Engineer (Remote)" at "Acme Inc" and "Sales Engineer" at "Acme"
            # are one job, and the old index treated them as two.
            # Drop the old index first, keeping the newest row per key.
            cursor.execute('DROP INDEX IF EXISTS idx_jobs_title_company')
            cursor.execute('''
                DELETE FROM jobs WHERE id NOT IN (
                    SELECT MAX(id) FROM jobs GROUP BY dedup_key
                )
            ''')
            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup_key
                ON jobs(dedup_key)
            ''')
            self.connection.commit()
        except Exception as e:
            print(f"[!] Migration warning: {e}")

    def close(self):
        """Close the shared database connection and drop it from the registry
        so the next Database() reopens cleanly."""
        if self.connection:
            try:
                self.connection.close()
            finally:
                if Database._shared.get(self.db_path) is self.connection:
                    del Database._shared[self.db_path]
                self.connection = None

    def init_database(self):
        """Initialize database schema."""
        cursor = self.connection.cursor()

        # Table: processed_emails (Component 1)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_emails (
                id INTEGER PRIMARY KEY,
                message_id TEXT UNIQUE NOT NULL,
                processed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                subject TEXT,
                email_from TEXT
            )
        ''')

        # Table: jobs (main job storage)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                description TEXT,
                link TEXT UNIQUE,
                salary TEXT,
                source TEXT,
                extracted_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                relevance_score REAL DEFAULT 0.0,
                best_cv TEXT,
                seen_count INTEGER DEFAULT 1,
                dedup_key TEXT
            )
        ''')

        # Table: cv_profiles (Component 2)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cv_profiles (
                id INTEGER PRIMARY KEY,
                cv_name TEXT UNIQUE NOT NULL,
                skills TEXT NOT NULL,
                experience TEXT,
                goals TEXT,
                achievements TEXT,
                parsed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_modified TIMESTAMP
            )
        ''')

        # Table: linkedin_profile (Component 2)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS linkedin_profile (
                id INTEGER PRIMARY KEY,
                skills TEXT NOT NULL,
                roles TEXT,
                companies TEXT,
                headline TEXT,
                summary TEXT,
                achievements TEXT,
                parsed_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_modified TIMESTAMP
            )
        ''')

        # Table: merged_skills
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS merged_skills (
                id INTEGER PRIMARY KEY,
                skills TEXT NOT NULL,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Table: cover_letters_sent (Component 2)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cover_letters_sent (
                id INTEGER PRIMARY KEY,
                job_id INTEGER NOT NULL,
                job_title TEXT NOT NULL,
                company TEXT NOT NULL,
                selected_cv TEXT,
                generated_letter TEXT,
                email_sent_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_responded INTEGER DEFAULT 0,
                FOREIGN KEY(job_id) REFERENCES jobs(id),
                UNIQUE(job_id, selected_cv)
            )
        ''')

        # Table: sent_digests
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sent_digests (
                id INTEGER PRIMARY KEY,
                digest_date DATE NOT NULL,
                job_count INTEGER,
                telegram_response TEXT,
                sent_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Create indices for performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_extracted_date ON jobs(extracted_date)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(relevance_score)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cover_letters_job_id ON cover_letters_sent(job_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_cover_letters_sent_time ON cover_letters_sent(email_sent_time)')

        # Track job links to avoid re-processing old listings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS job_links_seen (
                link TEXT PRIMARY KEY,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                job_id INTEGER,
                FOREIGN KEY(job_id) REFERENCES jobs(id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_job_links_seen ON job_links_seen(link)')

        self.connection.commit()
        print("[+] Database initialized successfully")

    def add_job(self, job_title, company, description, link, salary=None, source=None,
                relevance_score=0.0, best_cv=None, scam_risk=0, location=None):
        """
        Insert a new job or update an existing one (same title+company).
        On duplicate: increments seen_count, refreshes extracted_date so the
        dashboard shows it as active today, and updates score/cv if improved.
        The scam_risk flag is sticky: once a listing is flagged it stays flagged.
        location is backfilled on conflict only when the stored row has none, so
        a later sighting can supply it without overwriting a good value.
        Returns job_id always.
        """
        try:
            key = dedup_key(job_title, company)
            cursor = self.connection.cursor()
            cursor.execute('''
                INSERT INTO jobs
                    (job_title, company, description, link, salary, source,
                     relevance_score, best_cv, seen_count, dedup_key, scam_risk, location)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(dedup_key) DO UPDATE SET
                    seen_count    = seen_count + 1,
                    extracted_date = CURRENT_TIMESTAMP,
                    relevance_score = CASE
                        WHEN excluded.relevance_score > relevance_score
                        THEN excluded.relevance_score ELSE relevance_score END,
                    best_cv = CASE
                        WHEN excluded.relevance_score > relevance_score
                        THEN excluded.best_cv ELSE best_cv END,
                    scam_risk = CASE WHEN excluded.scam_risk = 1 THEN 1 ELSE scam_risk END,
                    location = CASE
                        WHEN (location IS NULL OR location = '')
                             AND excluded.location IS NOT NULL AND excluded.location != ''
                        THEN excluded.location ELSE location END
            ''', (job_title, company, description, link, salary, source,
                  relevance_score, best_cv, key, 1 if scam_risk else 0, location))
            self.connection.commit()
            # Return the id whether it was inserted or updated
            if cursor.lastrowid:
                return cursor.lastrowid
            cursor.execute('SELECT id FROM jobs WHERE dedup_key=?', (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            print(f"[!] Error adding job: {e}")
            return None

    def get_jobs(self, limit=10, order_by='relevance_score DESC'):
        """Get jobs from database."""
        cursor = self.connection.cursor()
        cursor.execute(f'SELECT * FROM jobs ORDER BY {order_by} LIMIT ?', (limit,))
        return cursor.fetchall()

    def job_has_cover_letter(self, job_id):
        """Check if job already has a cover letter generated."""
        cursor = self.connection.cursor()
        cursor.execute('SELECT COUNT(*) FROM cover_letters_sent WHERE job_id = ?', (job_id,))
        return cursor.fetchone()[0] > 0

    def get_jobs_without_cover_letters(self, limit=3):
        """
        Best scoring jobs that have no letter yet, highest score first.

        Letter generation costs an API call each, so the caller works through
        a few at a time and this is what decides which few. Excluding jobs
        already in cover_letters_sent is what makes repeated runs safe.
        """
        cursor = self.connection.cursor()
        cursor.execute('''
            SELECT id, job_title, company, description, link, best_cv
            FROM jobs
            WHERE id NOT IN (SELECT job_id FROM cover_letters_sent)
            ORDER BY relevance_score DESC, extracted_date DESC
            LIMIT ?
        ''', (limit,))
        return cursor.fetchall()

    def is_job_link_seen(self, job_link):
        """Check if we've already processed this job link."""
        try:
            cursor = self.connection.cursor()
            cursor.execute('SELECT job_id FROM job_links_seen WHERE link = ?', (job_link,))
            result = cursor.fetchone()
            return result is not None
        except Exception as e:
            print(f"[!] Error checking job link: {e}")
            return False

    def mark_job_link_seen(self, job_link, job_id=None):
        """Mark a job link as seen."""
        try:
            cursor = self.connection.cursor()
            cursor.execute('''
                INSERT INTO job_links_seen (link, job_id)
                VALUES (?, ?)
                ON CONFLICT(link) DO UPDATE SET last_seen = CURRENT_TIMESTAMP
            ''', (job_link, job_id))
            self.connection.commit()
        except Exception as e:
            print(f"[!] Error marking job link: {e}")

    def add_cover_letter(self, job_id, job_title, company, selected_cv, generated_letter):
        """Add a generated cover letter to the database."""
        try:
            cursor = self.connection.cursor()
            cursor.execute('''
                INSERT INTO cover_letters_sent
                (job_id, job_title, company, selected_cv, generated_letter)
                VALUES (?, ?, ?, ?, ?)
            ''', (job_id, job_title, company, selected_cv, generated_letter))
            self.connection.commit()
            return cursor.lastrowid
        except sqlite3.IntegrityError:
            # Duplicate cover letter
            return None

    def save_cv_profile(self, cv_name, skills, experience, goals, achievements):
        """Save extracted CV profile to database."""
        cursor = self.connection.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO cv_profiles
            (cv_name, skills, experience, goals, achievements, parsed_date)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (cv_name, json.dumps(skills), json.dumps(experience), goals, json.dumps(achievements)))
        self.connection.commit()

    def get_cv_profiles(self):
        """Get all saved CV profiles."""
        cursor = self.connection.cursor()
        cursor.execute('SELECT * FROM cv_profiles')
        results = cursor.fetchall()
        return [dict(row) for row in results]

    def save_linkedin_profile(self, skills, roles, companies, headline, summary, achievements):
        """Save extracted LinkedIn profile."""
        cursor = self.connection.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO linkedin_profile
            (skills, roles, companies, headline, summary, achievements, parsed_date)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (json.dumps(skills), json.dumps(roles), json.dumps(companies), headline, summary, json.dumps(achievements)))
        self.connection.commit()

    def get_linkedin_profile(self):
        """Get saved LinkedIn profile."""
        cursor = self.connection.cursor()
        cursor.execute('SELECT * FROM linkedin_profile LIMIT 1')
        row = cursor.fetchone()
        return dict(row) if row else None

    def cleanup_old_entries(self, days=30):
        """Remove old entries from database (older than X days)."""
        cutoff_date = datetime.now() - timedelta(days=days)
        cursor = self.connection.cursor()

        # Remove old processed emails
        cursor.execute('DELETE FROM processed_emails WHERE processed_date < ?', (cutoff_date,))

        # Remove old jobs without cover letters
        cursor.execute(
            'DELETE FROM jobs WHERE extracted_date < ? AND id NOT IN (SELECT job_id FROM cover_letters_sent)',
            (cutoff_date,)
        )

        # Purge telegram_sent_jobs entries whose job was deleted (prevents ghost re-sends)
        try:
            cursor.execute(
                'DELETE FROM telegram_sent_jobs WHERE job_id NOT IN (SELECT id FROM jobs)'
            )
        except Exception:
            pass  # Table may not exist yet on first run

        self.connection.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
