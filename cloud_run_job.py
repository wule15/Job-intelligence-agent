"""
Run the daily digest once as a Google Cloud Run job.

The job starts this file instead of the image's default command:

    python cloud_run_job.py

It does what the scheduled task does on the PC, a search and then the
Telegram send, around a SQLite database that is kept in a private Cloud
Storage bucket between runs. Standard library only, plus python-dotenv.

1. Settings. Cloud Run mounts each secret as a file, and an environment
   variable names each path. ENV_FILE_PATH is the agent's .env file. Its
   values are added to the environment the two scripts run with, and a
   variable set on the job wins over the same key in the file. The file is
   not copied to core/.env, because core/config.py lets that file override
   the environment, which would undo the job's own settings. CV_FILE_PATH,
   COMPANIES_FILE_PATH and EXCLUDE_FILE_PATH are copied to
   core/master-cv.yaml, core/config/companies.json and
   core/data/digest_exclude.txt, and MASTER_CV_PATH and DIGEST_EXCLUDE_FILE
   are pointed at the copies whatever the mounted .env says. A path variable
   left unset is skipped. One that is set must name a file that exists.

2. Download. BUCKET and DB_OBJECT name the database, for example
   DB_OBJECT=db/job_digest.db. The object's generation and MD5 are read,
   that generation is downloaded to core/data/job_digest.db, and the copy
   must match the MD5, pass PRAGMA quick_check and hold the jobs and
   telegram_sent_jobs tables. Anything else stops the run before the search.
   It never starts from an empty database, which would resend every job.

3. Search and send. job_search_smart.py, then telegram_sender.py, each a
   child process with a time limit. The send runs even if the search
   failed, as on the PC. Just before the send the generation is read again.
   If another run wrote the database in the meantime, the send is skipped.

4. Save. The WAL is checkpointed into the main file, quick_check runs
   again, and the file is uploaded only if the object is still the
   generation that was downloaded (ifGenerationMatch), with its MD5 so that
   Cloud Storage refuses a damaged upload. Network errors and server errors
   are retried a few times within the run's deadline. A refused upload is
   checked against the live object's MD5, because an earlier attempt whose
   answer was lost may have landed. A real conflict saves this run's copy as
   conflicts/<UTC time>.db and leaves the live object alone. An upload that
   fails for any other reason saves it as unsaved/<UTC time>.db if it can,
   so the record of what was sent is not lost with the container.

Any failure sends one plain line to Telegram naming what failed and whether
re-running is safe. Neither that line nor the printed log carries a URL, a
token or a value from the env file.

A shadow run is the same image as a second job with
DB_OBJECT=shadow/job_digest.db and DIGEST_LABEL=[SHADOW], so it works on its
own copy of the database and its messages are marked.

The job needs a task timeout of 30 minutes, max retries 0, and a service
account that can read, create and replace objects in the bucket. Replacing
an object needs storage.objects.delete as well as create, so a create-only
role such as Storage Object Creator is not enough; Storage Object User is. A retry after the
send would send the digest again, so this script refuses to run as one.

Exit codes
   0  Search and send finished and the database was saved.
   1  Unexpected error in this script.
   2  Setup: a required setting or a named file is missing, or Cloud Run
      retried the task. Nothing ran.
   3  A Cloud Storage request failed before the send. Nothing was sent or
      saved.
   4  The database object is not in the bucket. Nothing ran.
   5  The download did not match its MD5, or the object has none. Nothing
      ran.
   6  The downloaded database failed quick_check or lacks a table. Nothing
      ran.
   7  The database in the bucket changed during the search. The send was
      skipped and nothing was saved.
   8  The search failed or timed out. The send ran and the database was
      saved.
   9  The send failed or timed out. The database was saved.
  10  The send ran but the database was not saved: it failed its check, or
      the upload failed. After a failed upload this run's copy is kept as
      unsaved/<UTC time>.db when the bucket accepts it.
  11  Another run wrote the database during this one. This run's copy went
      to conflicts/ and the live object was left alone.
"""

import base64
import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent

OK = 0
UNEXPECTED = 1
SETUP = 2
STORAGE_UNREACHABLE = 3
DB_MISSING = 4
DOWNLOAD_CORRUPT = 5
DB_DAMAGED = 6
DB_CHANGED = 7
SEARCH_FAILED = 8
SEND_FAILED = 9
NOT_SAVED = 10
CONFLICT = 11

# Seconds. The Cloud Run task timeout is 30 minutes and the run keeps two of
# them in hand. The search and the send each leave room for what follows.
RUN_DEADLINE_SECS = 28 * 60
SEARCH_LIMIT_SECS = 20 * 60
SEND_LIMIT_SECS = 5 * 60
UPLOAD_RESERVE_SECS = 2 * 60
HTTP_TIMEOUT_SECS = 60
HTTP_ATTEMPTS = 4
FIRST_RETRY_WAIT_SECS = 2

SEARCH_SCRIPT = 'job_search_smart.py'
SEND_SCRIPT = 'telegram_sender.py'
REQUIRED_TABLES = ('jobs', 'telegram_sent_jobs')

TOKEN_URL = ('http://metadata.google.internal/computeMetadata/v1/'
             'instance/service-accounts/default/token')
STORAGE_API = 'https://storage.googleapis.com/storage/v1'
UPLOAD_API = 'https://storage.googleapis.com/upload/storage/v1'
TELEGRAM_API = 'https://api.telegram.org'

NOT_SAVED_TAIL = ("The bucket keeps the copy from before this run, which does not "
                  "know what was sent today. A re-run now would repeat today's jobs.")


class NetworkError(Exception):
    """A request that got no HTTP answer. Holds the error's class name only."""


class StorageError(Exception):
    """A Cloud Storage request that failed for good. Names the step, never a URL."""


class Stop(Exception):
    """Ends the run with an exit code and the line for the failure notice."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def say(text):
    print(text, flush=True)


def md5_b64(data):
    """MD5 in the form Cloud Storage uses: base64 of the binary digest."""
    return base64.b64encode(hashlib.md5(data).digest()).decode('ascii')


def urllib_http(method, url, headers=None, body=None, timeout=HTTP_TIMEOUT_SECS):
    """
    One request with the standard library. Returns (status, body).

    An HTTP error status is returned, not raised. A request that gets no
    answer raises NetworkError with the error's class name only, because
    some error texts include the URL, and a Telegram URL holds the bot token.
    """
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        try:
            return error.code, error.read()
        except Exception:
            return error.code, b''
    except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
        raise NetworkError(type(error).__name__) from None


class _Retryable(Exception):
    pass


class Storage:
    """The few Cloud Storage JSON API calls the run needs, over plain HTTPS."""

    def __init__(self, bucket, http, sleep, time_left):
        self.bucket = bucket
        self.http = http
        self.sleep = sleep
        self.time_left = time_left

    def _timeout(self):
        return max(5, min(HTTP_TIMEOUT_SECS, self.time_left()))

    def _send(self, method, url, headers, body):
        # A fresh token from the metadata server for every request: it is a
        # local call, and a cached token could expire during a long search.
        status, data = self.http('GET', TOKEN_URL, {'Metadata-Flavor': 'Google'},
                                 None, self._timeout())
        if status != 200:
            raise _Retryable(f'no access token, HTTP {status}')
        token = json.loads(data)['access_token']
        headers = dict(headers or {})
        headers['Authorization'] = f'Bearer {token}'
        return self.http(method, url, headers, body, self._timeout())

    def _request(self, step, method, url, headers=None, body=None):
        """
        Send a request, retrying no answer, 408, 429 and 5xx while attempts and
        time remain. Returns (status, body) for any other answer.
        """
        wait = FIRST_RETRY_WAIT_SECS
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            try:
                status, data = self._send(method, url, headers, body)
            except NetworkError as error:
                problem = f'no answer, {error}'
            except _Retryable as error:
                problem = str(error)
            else:
                if status not in (408, 429) and status < 500:
                    return status, data
                problem = f'HTTP {status}'
            if attempt == HTTP_ATTEMPTS or self.time_left() <= wait:
                break
            say(f'[!] {step}: {problem}, retrying in {wait} s')
            self.sleep(wait)
            wait *= 2
        raise StorageError(f'{step} failed after {attempt} attempts, {problem}')

    def _object_url(self, name, **query):
        quoted = urllib.parse.quote
        url = f'{STORAGE_API}/b/{quoted(self.bucket, safe="")}/o/{quoted(name, safe="")}'
        return f'{url}?{urllib.parse.urlencode(query)}' if query else url

    def metadata(self, name):
        """(generation, md5) of the live object, or None if there is none."""
        status, data = self._request('reading the database details', 'GET',
                                     self._object_url(name, fields='generation,md5Hash'))
        if status == 404:
            return None
        if status != 200:
            raise StorageError(f'reading the database details was refused, HTTP {status}')
        meta = json.loads(data)
        return int(meta['generation']), meta.get('md5Hash')

    def download(self, name, generation):
        status, data = self._request('the download', 'GET',
                                     self._object_url(name, alt='media', generation=generation))
        if status != 200:
            raise StorageError(f'the download was refused, HTTP {status}')
        return data

    def upload(self, name, data, md5, if_generation_match):
        """
        True when the object now holds data, False when another write holds
        it. if_generation_match 0 means only if no such object exists yet.
        """
        query = urllib.parse.urlencode({'uploadType': 'media', 'name': name,
                                        'ifGenerationMatch': if_generation_match})
        url = f'{UPLOAD_API}/b/{urllib.parse.quote(self.bucket, safe="")}/o?{query}'
        headers = {'Content-Type': 'application/octet-stream', 'X-Goog-Hash': f'md5={md5}'}
        try:
            status, _ = self._request('the upload', 'POST', url, headers, data)
        except StorageError:
            # The last attempt may have landed with its answer lost.
            try:
                if self.holds(name, md5):
                    return True
            except StorageError:
                pass
            raise
        if status == 200:
            return True
        if status == 412:
            return self.holds(name, md5)
        raise StorageError(f'the upload was refused, HTTP {status}')

    def holds(self, name, md5):
        meta = self.metadata(name)
        return meta is not None and meta[1] == md5


def check_database(path):
    """None if the database passes quick_check and has the tables, else why not."""
    try:
        with closing(sqlite3.connect(path)) as conn:
            result = conn.execute('PRAGMA quick_check').fetchall()
            if result != [('ok',)]:
                first = ' '.join(str(result[0][0]).split()) if result else 'no answer'
                return f'quick_check: {first}'[:160]
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
    except sqlite3.DatabaseError as error:
        return f'{type(error).__name__}: {error}'[:160]
    missing = [table for table in REQUIRED_TABLES if table not in names]
    return f"missing table {', '.join(missing)}" if missing else None


def finish_database(path):
    """
    Fold the WAL into the main file, so the one file uploaded holds every
    write, then check it again. None if it is ready, else why not.
    """
    try:
        with closing(sqlite3.connect(path)) as conn:
            busy = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]
    except sqlite3.DatabaseError as error:
        return f'{type(error).__name__}: {error}'[:160]
    if busy:
        return 'the WAL checkpoint was blocked'
    problem = check_database(path)
    if problem:
        return problem
    wal = Path(f'{path}-wal')
    if wal.exists() and wal.stat().st_size:
        return 'the WAL file still holds writes'
    return None


def minutes(seconds):
    seconds = int(seconds)
    return f'{seconds // 60} minutes' if seconds >= 60 and not seconds % 60 else f'{seconds} seconds'


class _Run:
    def __init__(self, env, http, runner, sleep, time_left, root, python):
        self.env = env
        self.http = http
        self.runner = runner
        self.sleep = sleep
        self.time_left = time_left
        self.root = root
        self.python = python
        self.core = root / 'core'
        self.db_path = self.core / 'data' / 'job_digest.db'
        self.secrets = set()
        self.storage = None
        self.search_problem = None
        self.send_started = False

    def run(self):
        self.prepare()
        name = self.env['DB_OBJECT'].strip()
        generation = self.fetch(name)

        self.search_problem = self.step(
            SEARCH_SCRIPT, 'the job search',
            min(SEARCH_LIMIT_SECS, self.time_left() - SEND_LIMIT_SECS - UPLOAD_RESERVE_SECS))
        self.check_unchanged(name, generation)

        self.send_started = True
        send_problem = self.step(
            SEND_SCRIPT, 'the Telegram send',
            min(SEND_LIMIT_SECS, self.time_left() - UPLOAD_RESERVE_SECS))
        self.save(name, generation)

        if send_problem:
            return SEND_FAILED, (
                f'Cloud digest: the Telegram send {send_problem}. The database was saved '
                'with every job that went out marked as sent. Safe to re-run, though a '
                'message sent just before the stop could repeat.')
        if self.search_problem:
            return SEARCH_FAILED, (
                f'Cloud digest: the job search {self.search_problem}. The send still ran '
                'on stored jobs and the database was saved. Safe to re-run.')
        return OK, ''

    # ── 1. Settings ──────────────────────────────────────────────────────────

    def prepare(self):
        if self.env.get('ENV_FILE_PATH'):
            added = 0
            for key, value in dotenv_values(self.mounted('ENV_FILE_PATH')).items():
                if not value:
                    continue  # a blank line is skipped, as core/config.py skips it
                self.secrets.add(value)
                if not self.env.get(key):
                    self.env[key] = value
                    added += 1
            say(f'[*] {added} settings added from the mounted env file')

        if self.env.get('CLOUD_RUN_TASK_ATTEMPT', '0').strip() not in ('', '0'):
            raise Stop(SETUP, (
                'Cloud digest stopped: Cloud Run retried the task by itself, and a retry '
                'could send the digest twice. Nothing ran. Set the job to max retries 0. '
                'A manual re-run is safe.'))
        for key in ('BUCKET', 'DB_OBJECT'):
            if not self.env.get(key, '').strip():
                raise Stop(SETUP, (
                    f'Cloud digest stopped before starting: {key} is not set on the job. '
                    'Nothing ran. Safe to re-run once it is set.'))

        self.place('CV_FILE_PATH', self.core / 'master-cv.yaml', 'MASTER_CV_PATH')
        self.place('COMPANIES_FILE_PATH', self.core / 'config' / 'companies.json')
        self.place('EXCLUDE_FILE_PATH', self.core / 'data' / 'digest_exclude.txt',
                   'DIGEST_EXCLUDE_FILE')

    def mounted(self, variable):
        path = Path(self.env[variable])
        if not path.is_file():
            raise Stop(SETUP, (
                f'Cloud digest stopped before starting: the file named by {variable} is '
                'missing. Nothing ran. Safe to re-run once the secret is mounted.'))
        return path

    def place(self, variable, target, setting=None):
        if not self.env.get(variable):
            return
        source = self.mounted(variable)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if setting:
            self.env[setting] = str(target)
        say(f'[*] {variable} copied to {target.relative_to(self.root).as_posix()}')

    # ── 2. Download ──────────────────────────────────────────────────────────

    def fetch(self, name):
        self.storage = Storage(self.env['BUCKET'].strip(), self.http, self.sleep, self.time_left)
        try:
            meta = self.storage.metadata(name)
            if meta is None:
                raise Stop(DB_MISSING, (
                    f'Cloud digest stopped: the database {name} is not in the bucket. The run '
                    'never starts from an empty database, which would resend old jobs. '
                    'Nothing was sent. Re-run once the database is restored.'))
            generation, md5 = meta
            if not md5:
                raise Stop(DOWNLOAD_CORRUPT, (
                    f'Cloud digest stopped: the database {name} has no MD5 checksum, so its '
                    'download cannot be checked. Nothing was sent. Upload it again as a '
                    'single file, then re-run.'))
            data = self.storage.download(name, generation)
        except StorageError as error:
            raise Stop(STORAGE_UNREACHABLE, (
                f'Cloud digest stopped: Cloud Storage failed while fetching the database '
                f'({error}). Nothing was sent and the stored database is unchanged. '
                'Safe to re-run.'))

        if md5_b64(data) != md5:
            raise Stop(DOWNLOAD_CORRUPT, (
                'Cloud digest stopped: the downloaded database did not match its MD5 '
                'checksum. Nothing was sent and the stored copy is unchanged. Safe to re-run.'))

        # A -wal or -shm file left beside the database belongs to another copy,
        # and SQLite would replay it into this one.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(f'{self.db_path}{suffix}').unlink(missing_ok=True)
        part = self.db_path.with_name(self.db_path.name + '.part')
        part.write_bytes(data)
        os.replace(part, self.db_path)

        problem = check_database(self.db_path)
        if problem:
            say(self.scrub(f'[!] Database check: {problem}'))
            raise Stop(DB_DAMAGED, (
                f'Cloud digest stopped: the stored database failed its check ({problem}). '
                'Nothing was sent. A re-run will stop the same way until a good copy is '
                'restored.'))
        say(f'[*] Database {name}, generation {generation}, '
            f'{len(data) // 1024} KB, downloaded and checked')
        return generation

    # ── 3. Search and send ───────────────────────────────────────────────────

    def step(self, script, label, limit):
        """Run one script as a child process. None if it finished cleanly, else why not."""
        limit = max(1, int(limit))
        say(f'[*] Running {script}, time limit {minutes(limit)}')
        try:
            result = self.runner([self.python, script], cwd=str(self.root),
                                 env=self.env, timeout=limit)
        except subprocess.TimeoutExpired:
            problem = f'timed out after {minutes(limit)}'
        except OSError as error:
            problem = f'could not start ({type(error).__name__})'
        else:
            if result.returncode == 0:
                say(f'[*] {script} finished')
                return None
            problem = f'failed with exit code {result.returncode}'
        say(f'[!] {label} {problem}')
        return problem

    def check_unchanged(self, name, generation):
        try:
            meta = self.storage.metadata(name)
        except StorageError as error:
            raise Stop(STORAGE_UNREACHABLE, (
                f'Cloud digest stopped before the send: Cloud Storage failed while '
                f're-checking the database ({error}). Nothing was sent or saved. '
                'Safe to re-run.'))
        if meta is None or meta[0] != generation:
            raise Stop(DB_CHANGED, (
                'Cloud digest skipped the send: the database in the bucket changed during '
                'the search, so another run wrote it. Nothing was sent and this run\'s copy '
                'was not saved. Safe to re-run.'))

    # ── 4. Save ──────────────────────────────────────────────────────────────

    def save(self, name, generation):
        problem = finish_database(self.db_path)
        if problem:
            say(self.scrub(f'[!] Database check after the send: {problem}'))
            raise Stop(NOT_SAVED, (
                f'Cloud digest: the send ran but the database was not saved, because it '
                f'failed its check ({problem}). {NOT_SAVED_TAIL}'))

        data = self.db_path.read_bytes()
        md5 = md5_b64(data)
        try:
            if self.storage.upload(name, data, md5, generation):
                say(f'[*] Database saved, {len(data) // 1024} KB')
                return
            say('[!] The database in the bucket changed during the run, not overwriting it')
            kept = self.keep_copy('conflicts', data, md5)
        except StorageError as error:
            # The container's disk goes when the run ends, and with it the
            # record of what was sent. A refusal such as a missing permission
            # to replace the object still allows a new name, so keep it there.
            say(f"[!] The upload failed ({error}), keeping this run's copy under unsaved/")
            kept = self.keep_copy('unsaved', data, md5)
            where = (f"This run's copy was kept as {kept}; restore it as {name} before "
                     "the next run." if kept else "This run's copy could not be kept either.")
            raise Stop(NOT_SAVED, (
                f'Cloud digest: the send ran but the database could not be saved ({error}). '
                f'{where} {NOT_SAVED_TAIL}'))

        where = (f"This run's copy was saved as {kept}." if kept
                 else "This run's copy could not be saved.")
        raise Stop(CONFLICT, (
            'Cloud digest: another run wrote the database while this one was sending, so '
            f'this run did not overwrite it. {where} Do not re-run until the two copies '
            "are reconciled, because a re-run would repeat today's jobs."))

    def keep_copy(self, folder, data, md5):
        """Save this run's database under folder/<UTC time>.db. Its name, or None."""
        copy_name = f"{folder}/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
        try:
            if self.storage.upload(copy_name, data, md5, 0):
                say(f"[*] This run's copy saved as {copy_name}")
                return copy_name
        except StorageError:
            pass
        return None

    # ── Failure ──────────────────────────────────────────────────────────────

    def unexpected(self, error):
        frame = traceback.extract_tb(error.__traceback__)[-1]
        say(f'[!] Unexpected {type(error).__name__} at {Path(frame.filename).name}:{frame.lineno}')
        if self.send_started:
            return (f'Cloud digest stopped on an unexpected error ({type(error).__name__}) '
                    'after the send started. The database may not be saved, so a re-run '
                    "could repeat today's jobs.")
        return (f'Cloud digest stopped on an unexpected error ({type(error).__name__}). '
                'Nothing was sent and the stored database is unchanged. Safe to re-run.')

    def scrub(self, text):
        for value in self.secrets:
            if len(value) >= 8:
                text = text.replace(value, '[hidden]')
        return ' '.join(text.split())

    def notify(self, text):
        """Send one line to Telegram. Never raises: the exit code already says it."""
        token = self.env.get('TELEGRAM_BOT_TOKEN', '').strip()
        chat = self.env.get('TELEGRAM_CHAT_ID', '').strip()
        if not token or not chat:
            say('[!] No Telegram settings, so no failure notice was sent')
            return
        label = self.env.get('DIGEST_LABEL', '').strip()
        if label:
            text = f'{label} {text}'
        body = urllib.parse.urlencode({'chat_id': chat, 'text': text}).encode('utf-8')
        try:
            status, _ = self.http('POST', f'{TELEGRAM_API}/bot{token}/sendMessage',
                                  {'Content-Type': 'application/x-www-form-urlencoded'},
                                  body, 15)
        except Exception as error:
            say(f'[!] Failure notice not sent ({type(error).__name__})')
            return
        if status != 200:
            say(f'[!] Failure notice not sent, HTTP {status}')
            return
        say('[*] Failure notice sent to Telegram')


def run(environ=None, http=urllib_http, runner=subprocess.run, clock=time.monotonic,
        sleep=time.sleep, root=ROOT, python=sys.executable):
    """Run the job once and return its exit code. Every argument is for tests."""
    env = dict(os.environ if environ is None else environ)
    started = clock()
    job = _Run(env, http, runner, sleep,
               lambda: RUN_DEADLINE_SECS - (clock() - started), Path(root), python)
    try:
        code, message = job.run()
    except Stop as stop:
        code, message = stop.code, stop.message
    except Exception as error:
        code, message = UNEXPECTED, job.unexpected(error)

    if code != OK:
        if job.search_problem and code != SEARCH_FAILED:
            message += ' The search had also failed.'
        message = job.scrub(message)
        say(f'[!] {message}')
        job.notify(message)
    say(f'[*] Exit code {code}')
    return code


if __name__ == '__main__':
    sys.exit(run())
