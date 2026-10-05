"""
Tests for cloud_run_job.py, the wrapper that runs the daily digest as a
Cloud Run job with the database kept in Cloud Storage between runs.

Nothing here touches the network or the real database. Cloud Storage, the
metadata server and the Telegram Bot API are one fake in memory, the two
child scripts are replaced by functions, and every database is a small
synthetic SQLite file made in a temporary folder.
"""

import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cloud_run_job as crj  # noqa: E402

BUCKET = 'test-bucket'
OBJECT = 'db/job_digest.db'
ACCESS_TOKEN = 'ya29.fake-access-token-value'

# Every value in the mounted env file counts as a secret. None of them may
# reach the Telegram notice or the printed log.
SECRET_ENV = {
    'TELEGRAM_BOT_TOKEN': '123456789:AAFakeBotTokenValueForTests',
    'TELEGRAM_CHAT_ID': '555000111',
    'ANTHROPIC_API_KEY': 'sk-ant-fake-key-value-for-tests',
    'GMAIL_APP_PASSWORD': 'fake-gmail-app-password',
    'ADZUNA_APP_KEY': 'fake-adzuna-key-value',
}


def md5_b64(data):
    return base64.b64encode(hashlib.md5(data).digest()).decode('ascii')


# ── Synthetic databases ──────────────────────────────────────────────────────

def make_db(path, tables=('jobs', 'telegram_sent_jobs'), page_size=None):
    """A small database in WAL mode, the way the agent keeps its own."""
    path = Path(path)
    with closing(sqlite3.connect(path)) as conn:
        if page_size:
            conn.execute(f'PRAGMA page_size = {page_size}')
        conn.execute('PRAGMA journal_mode = WAL')
        if 'jobs' in tables:
            conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_title TEXT, '
                         'company TEXT, link TEXT)')
            conn.execute("INSERT INTO jobs (job_title, company, link) "
                         "VALUES ('Seed Engineer', 'Seed Co', 'https://example.com/seed')")
        if 'telegram_sent_jobs' in tables:
            conn.execute('CREATE TABLE telegram_sent_jobs (job_id INTEGER PRIMARY KEY)')
        conn.commit()
    return path.read_bytes()


def corrupt_db_bytes(tmp_path):
    """A database that opens but fails PRAGMA quick_check."""
    path = tmp_path / 'corrupt.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('PRAGMA page_size = 1024')
        conn.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, t TEXT)')
        conn.execute('CREATE TABLE telegram_sent_jobs (job_id INTEGER PRIMARY KEY)')
        conn.executemany('INSERT INTO jobs (t) VALUES (?)', [('x' * 200,) for _ in range(50)])
        conn.commit()
    data = bytearray(path.read_bytes())
    # Overwrite the cell pointers of the last table leaf page (type byte 13).
    leaves = [page for page in range(1, len(data) // 1024)
              if data[page * 1024] == 13]
    offset = leaves[-1] * 1024
    data[offset + 8:offset + 40] = b'\xff' * 32
    return bytes(data)


def rows(data_or_path, sql, tmp_path=None):
    """Query a database given as a path, or as bytes copied to a temp file."""
    if isinstance(data_or_path, bytes):
        path = tmp_path / 'inspect.db'
        for suffix in ('', '-wal', '-shm'):
            Path(f'{path}{suffix}').unlink(missing_ok=True)
        path.write_bytes(data_or_path)
    else:
        path = data_or_path
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def leave_rows_in_wal(db_path, work_dir, sql):
    """
    Write rows that only the -wal file holds, as a process stopped before its
    last checkpoint leaves them. The main file is unchanged.
    """
    work = work_dir / 'wal-work.db'
    for suffix in ('', '-wal', '-shm'):
        Path(f'{work}{suffix}').unlink(missing_ok=True)
    shutil.copyfile(db_path, work)
    conn = sqlite3.connect(work)
    try:
        conn.execute('PRAGMA journal_mode = WAL')
        conn.execute('PRAGMA wal_autocheckpoint = 0')
        conn.execute(sql)
        conn.commit()
        shutil.copyfile(work, db_path)
        shutil.copyfile(f'{work}-wal', f'{db_path}-wal')
    finally:
        conn.close()


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeCloud:
    """Cloud Storage, the metadata server and Telegram, in memory."""

    def __init__(self, bucket=BUCKET):
        self.bucket = bucket
        self.objects = {}
        self.generation = 1000
        self.log = []
        self.uploads = []
        self.telegram = []
        self.faults = []

    def store(self, name, data, md5=True):
        self.generation += 1
        self.objects[name] = {
            'data': data,
            'generation': self.generation,
            'md5Hash': md5_b64(data) if md5 is True else md5,
        }
        return self.generation

    def fault(self, match, action):
        """One-shot: the next request matching match(method, url) gets action."""
        self.faults.append((match, action))

    def __call__(self, method, url, headers=None, body=None, timeout=None):
        headers = dict(headers or {})
        self.log.append((method, url))
        for i, (match, action) in enumerate(self.faults):
            if match(method, url):
                del self.faults[i]
                if isinstance(action, BaseException):
                    raise action
                if callable(action):
                    return action(self, method, url, headers, body)
                return action
        return self.handle(method, url, headers, body)

    def handle(self, method, url, headers, body):
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if url == crj.TOKEN_URL:
            assert headers.get('Metadata-Flavor') == 'Google'
            return 200, json.dumps({'access_token': ACCESS_TOKEN, 'expires_in': 3599,
                                    'token_type': 'Bearer'}).encode()
        if parts.netloc == 'api.telegram.org':
            form = urllib.parse.parse_qs(body.decode('utf-8'))
            self.telegram.append(form['text'][0])
            return 200, b'{"ok": true}'
        assert parts.netloc == 'storage.googleapis.com', url
        assert headers.get('Authorization') == f'Bearer {ACCESS_TOKEN}'
        if method == 'POST' and parts.path == f'/upload/storage/v1/b/{self.bucket}/o':
            return self._upload(query, headers, body)
        prefix = f'/storage/v1/b/{self.bucket}/o/'
        if method == 'GET' and parts.path.startswith(prefix):
            name = urllib.parse.unquote(parts.path[len(prefix):])
            obj = self.objects.get(name)
            if obj is None:
                return 404, b'{"error": {"code": 404}}'
            if query.get('alt') == 'media':
                if int(query['generation']) != obj['generation']:
                    return 404, b'{"error": {"code": 404}}'
                return 200, obj['data']
            meta = {'name': name, 'generation': str(obj['generation'])}
            if obj['md5Hash']:
                meta['md5Hash'] = obj['md5Hash']
            return 200, json.dumps(meta).encode()
        raise AssertionError(f'unexpected request {method} {parts.path}')

    def _upload(self, query, headers, body):
        name = query['name']
        want = int(query['ifGenerationMatch'])
        live = self.objects.get(name)
        if (want == 0 and live) or (want and (not live or live['generation'] != want)):
            status = 412
        elif headers.get('X-Goog-Hash') != f'md5={md5_b64(body)}':
            status = 400
        else:
            status = 200
        self.uploads.append((name, want, status))
        if status != 200:
            return status, b'{"error": {}}'
        generation = self.store(name, body)
        return 200, json.dumps({'name': name, 'generation': str(generation),
                                'md5Hash': md5_b64(body)}).encode()

    def storage_requests(self):
        return [url for _, url in self.log if 'storage.googleapis.com' in url]


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRunner:
    """Stands in for subprocess.run. Each script maps to a function."""

    def __init__(self):
        self.calls = []
        self.actions = {}

    def __call__(self, args, cwd=None, env=None, timeout=None, **kwargs):
        script = Path(args[-1]).name
        self.calls.append({'script': script, 'timeout': timeout, 'env': dict(env), 'cwd': cwd})
        action = self.actions.get(script)
        code = action(timeout) if action else 0
        return subprocess.CompletedProcess(args, code or 0)

    @property
    def scripts(self):
        return [call['script'] for call in self.calls]


class Ctx:
    """One run's world: the app folder, the secrets, the cloud and the clock."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.root = tmp_path / 'app'
        (self.root / 'core').mkdir(parents=True)
        self.db_path = self.root / 'core' / 'data' / 'job_digest.db'

        secrets = tmp_path / 'secrets'
        secrets.mkdir()
        env_lines = [f'{key}={value}' for key, value in SECRET_ENV.items()]
        env_lines += [
            r'MASTER_CV_PATH=C:\Users\someone\master-cv.yaml',
            'DIGEST_EXCLUDE_FILE=data/old_exclude.txt',
            'DIGEST_LABEL=[FROM FILE]',
            'SERPAPI_BUDGET=',
        ]
        (secrets / 'agent.env').write_text('\n'.join(env_lines) + '\n', encoding='utf-8')
        (secrets / 'master-cv.yaml').write_text('variants: {}\n', encoding='utf-8')
        (secrets / 'companies.json').write_text('[]\n', encoding='utf-8')
        (secrets / 'exclude.txt').write_text('Acme | Engineer\n', encoding='utf-8')
        self.secrets = secrets

        self.env = {
            'BUCKET': BUCKET,
            'DB_OBJECT': OBJECT,
            'ENV_FILE_PATH': str(secrets / 'agent.env'),
            'CV_FILE_PATH': str(secrets / 'master-cv.yaml'),
            'COMPANIES_FILE_PATH': str(secrets / 'companies.json'),
            'EXCLUDE_FILE_PATH': str(secrets / 'exclude.txt'),
            'CLOUD_RUN_TASK_ATTEMPT': '0',
            'DIGEST_LABEL': '[SHADOW]',
        }
        self.cloud = FakeCloud()
        self.seed = make_db(tmp_path / 'seed.db')
        self.cloud.store(OBJECT, self.seed)
        self.generation = self.cloud.objects[OBJECT]['generation']
        self.clock = Clock()
        self.runner = FakeRunner()
        self.runner.actions['job_search_smart.py'] = self.search_adds_a_job
        self.runner.actions['telegram_sender.py'] = self.send_marks_it_sent

    def search_adds_a_job(self, timeout):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO jobs (job_title, company, link) "
                         "VALUES ('New Engineer', 'New Co', 'https://example.com/new')")
            conn.commit()
        return 0

    def send_marks_it_sent(self, timeout):
        # The sender's last write sits only in the -wal file, so the save
        # must checkpoint before it uploads.
        leave_rows_in_wal(self.db_path, self.tmp,
                          'INSERT INTO telegram_sent_jobs (job_id) VALUES (2)')
        return 0

    def run(self):
        return crj.run(environ=self.env, http=self.cloud, runner=self.runner,
                       clock=self.clock, sleep=self.clock.sleep, root=self.root,
                       python='python')

    def saved(self):
        return self.cloud.objects[OBJECT]['data']


@pytest.fixture
def ctx(tmp_path):
    return Ctx(tmp_path)


def is_upload(method, url):
    return method == 'POST' and '/upload/storage/' in url


def is_metadata(method, url):
    return method == 'GET' and '/storage/v1/b/' in url and 'alt=media' not in url


def is_download(method, url):
    return method == 'GET' and 'alt=media' in url


# ── Happy path ───────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_runs_search_then_send_and_saves_against_the_downloaded_generation(self, ctx):
        code = ctx.run()

        assert code == crj.OK
        assert ctx.runner.scripts == ['job_search_smart.py', 'telegram_sender.py']
        assert ctx.cloud.uploads == [(OBJECT, ctx.generation, 200)]
        assert ctx.cloud.telegram == []

    def test_the_saved_database_holds_the_search_and_the_send(self, ctx):
        ctx.run()

        saved = ctx.saved()
        assert rows(saved, 'SELECT job_title FROM jobs ORDER BY id', ctx.tmp) == [
            ('Seed Engineer',), ('New Engineer',)]
        # This row was only in the -wal file when the sender finished.
        assert rows(saved, 'SELECT job_id FROM telegram_sent_jobs', ctx.tmp) == [(2,)]

    def test_children_run_from_the_app_folder_with_the_merged_settings(self, ctx):
        ctx.run()

        core = ctx.root / 'core'
        for call in ctx.runner.calls:
            env = call['env']
            assert call['cwd'] == str(ctx.root)
            assert env['TELEGRAM_BOT_TOKEN'] == SECRET_ENV['TELEGRAM_BOT_TOKEN']
            # A setting on the job beats the same key in the mounted file.
            assert env['DIGEST_LABEL'] == '[SHADOW]'
            # The files this script placed beat any path in the mounted file.
            assert env['MASTER_CV_PATH'] == str(core / 'master-cv.yaml')
            assert env['DIGEST_EXCLUDE_FILE'] == str(core / 'data' / 'digest_exclude.txt')
            # A blank line is skipped, as core/config.py skips it.
            assert 'SERPAPI_BUDGET' not in env

    def test_mounted_files_are_copied_where_config_reads_them(self, ctx):
        ctx.run()

        core = ctx.root / 'core'
        assert (core / 'master-cv.yaml').read_text(encoding='utf-8') == 'variants: {}\n'
        assert (core / 'config' / 'companies.json').read_text(encoding='utf-8') == '[]\n'
        assert (core / 'data' / 'digest_exclude.txt').read_text(
            encoding='utf-8') == 'Acme | Engineer\n'
        # No core/.env is written: core/config.py lets that file override the
        # environment, which would undo the job's own settings.
        assert not (core / '.env').exists()

    def test_a_stale_wal_beside_the_download_is_removed_first(self, ctx):
        # A -wal file from another database would be replayed into the
        # downloaded one when it is opened.
        ctx.db_path.parent.mkdir(parents=True)
        other = ctx.tmp / 'other.db'
        make_db(other)
        shutil.copyfile(other, ctx.db_path)
        leave_rows_in_wal(ctx.db_path, ctx.tmp,
                          "INSERT INTO jobs (job_title, company) VALUES ('Stale', 'Stale Co')")
        seen = []

        def search(timeout):
            seen.extend(rows(ctx.db_path, 'SELECT job_title FROM jobs'))
            return 0
        ctx.runner.actions['job_search_smart.py'] = search

        assert ctx.run() == crj.OK
        assert ('Stale',) not in seen

    def test_the_image_build_includes_the_script(self):
        from tests.test_packaging import docker_rules, excluded
        assert not excluded('cloud_run_job.py', docker_rules())


# ── Stops before anything runs ───────────────────────────────────────────────

class TestStopsBeforeTheSearch:
    def test_missing_object_stops_before_the_search(self, ctx):
        del ctx.cloud.objects[OBJECT]

        assert ctx.run() == crj.DB_MISSING
        assert ctx.runner.calls == []
        assert ctx.cloud.uploads == []
        assert not ctx.db_path.exists()

    def test_md5_mismatch_stops(self, ctx):
        ctx.cloud.objects[OBJECT]['md5Hash'] = md5_b64(b'something else')

        assert ctx.run() == crj.DOWNLOAD_CORRUPT
        assert ctx.runner.calls == []
        assert ctx.cloud.uploads == []
        assert not ctx.db_path.exists()

    def test_an_object_without_md5_stops(self, ctx):
        ctx.cloud.store(OBJECT, ctx.seed, md5=None)

        assert ctx.run() == crj.DOWNLOAD_CORRUPT
        assert ctx.runner.calls == []

    def test_quick_check_failure_stops(self, ctx):
        ctx.cloud.store(OBJECT, corrupt_db_bytes(ctx.tmp))

        assert ctx.run() == crj.DB_DAMAGED
        assert ctx.runner.calls == []
        assert ctx.cloud.uploads == []

    def test_a_file_that_is_not_a_database_stops(self, ctx):
        ctx.cloud.store(OBJECT, b'not a database at all' * 100)

        assert ctx.run() == crj.DB_DAMAGED
        assert ctx.runner.calls == []

    def test_a_database_without_the_sent_table_stops(self, ctx):
        ctx.cloud.store(OBJECT, make_db(ctx.tmp / 'nosent.db', tables=('jobs',)))

        assert ctx.run() == crj.DB_DAMAGED
        assert ctx.runner.calls == []

    def test_unreachable_storage_stops_with_nothing_changed(self, ctx):
        for _ in range(crj.HTTP_ATTEMPTS):
            ctx.cloud.fault(is_metadata, crj.NetworkError('ConnectionResetError'))

        assert ctx.run() == crj.STORAGE_UNREACHABLE
        assert ctx.runner.calls == []
        assert len(ctx.clock.sleeps) == crj.HTTP_ATTEMPTS - 1

    def test_a_missing_setting_stops_before_any_request(self, ctx):
        del ctx.env['BUCKET']

        assert ctx.run() == crj.SETUP
        assert ctx.cloud.storage_requests() == []
        assert ctx.runner.calls == []

    def test_a_named_secret_file_that_is_missing_stops(self, ctx):
        ctx.env['CV_FILE_PATH'] = str(ctx.secrets / 'missing.yaml')

        assert ctx.run() == crj.SETUP
        assert ctx.cloud.storage_requests() == []

    def test_an_automatic_retry_of_the_task_is_refused(self, ctx):
        ctx.env['CLOUD_RUN_TASK_ATTEMPT'] = '1'

        assert ctx.run() == crj.SETUP
        assert ctx.cloud.storage_requests() == []
        assert ctx.runner.calls == []


# ── Between the search and the send ──────────────────────────────────────────

class TestBeforeTheSend:
    def test_generation_changed_before_send_skips_send(self, ctx):
        def search(timeout):
            ctx.cloud.store(OBJECT, make_db(ctx.tmp / 'other.db'))
            return 0
        ctx.runner.actions['job_search_smart.py'] = search

        assert ctx.run() == crj.DB_CHANGED
        assert ctx.runner.scripts == ['job_search_smart.py']
        assert ctx.cloud.uploads == []

    def test_a_deleted_object_before_send_skips_send(self, ctx):
        def search(timeout):
            del ctx.cloud.objects[OBJECT]
            return 0
        ctx.runner.actions['job_search_smart.py'] = search

        assert ctx.run() == crj.DB_CHANGED
        assert ctx.runner.scripts == ['job_search_smart.py']

    def test_the_send_runs_even_when_the_search_fails(self, ctx):
        ctx.runner.actions['job_search_smart.py'] = lambda timeout: 1

        assert ctx.run() == crj.SEARCH_FAILED
        assert ctx.runner.scripts == ['job_search_smart.py', 'telegram_sender.py']
        assert ctx.cloud.uploads == [(OBJECT, ctx.generation, 200)]


# ── Saving ───────────────────────────────────────────────────────────────────

class TestSaving:
    def test_412_on_upload_with_matching_md5_is_success(self, ctx):
        # The first upload lands, but its answer is lost on the way back. The
        # retry is refused because the generation moved, and the live object
        # is this run's own bytes.
        def lands_then_drops(cloud, method, url, headers, body):
            cloud.handle(method, url, headers, body)
            raise crj.NetworkError('ConnectionResetError')
        ctx.cloud.fault(is_upload, lands_then_drops)

        assert ctx.run() == crj.OK
        assert [status for _, _, status in ctx.cloud.uploads] == [200, 412]
        assert [name for name in ctx.cloud.objects if name.startswith('conflicts/')] == []
        assert ctx.cloud.telegram == []

    def test_real_412_goes_to_conflicts(self, ctx):
        theirs = make_db(ctx.tmp / 'theirs.db')

        def send(timeout):
            ctx.send_marks_it_sent(timeout)
            ctx.cloud.store(OBJECT, theirs)  # another writer, during the send
            return 0
        ctx.runner.actions['telegram_sender.py'] = send

        assert ctx.run() == crj.CONFLICT
        assert ctx.saved() == theirs
        conflicts = [name for name in ctx.cloud.objects if name.startswith('conflicts/')]
        assert len(conflicts) == 1 and conflicts[0].endswith('.db')
        assert ctx.cloud.uploads[-1] == (conflicts[0], 0, 200)
        ours = ctx.cloud.objects[conflicts[0]]['data']
        assert rows(ours, 'SELECT job_id FROM telegram_sent_jobs', ctx.tmp) == [(2,)]
        assert len(ctx.cloud.telegram) == 1
        assert conflicts[0] in ctx.cloud.telegram[0]

    def test_upload_retries_are_bounded(self, ctx):
        for _ in range(crj.HTTP_ATTEMPTS + 5):
            ctx.cloud.fault(is_upload, crj.NetworkError('TimeoutError'))

        assert ctx.run() == crj.NOT_SAVED
        assert len([1 for method, url in ctx.cloud.log if is_upload(method, url)]) \
            == crj.HTTP_ATTEMPTS
        assert ctx.saved() == ctx.seed

    def test_upload_retries_stop_at_the_deadline(self, ctx):
        for _ in range(crj.HTTP_ATTEMPTS):
            ctx.cloud.fault(is_upload, crj.NetworkError('TimeoutError'))

        def send(timeout):
            ctx.clock.now = crj.RUN_DEADLINE_SECS - 1
            return 0
        ctx.runner.actions['telegram_sender.py'] = send

        assert ctx.run() == crj.NOT_SAVED
        assert len([1 for method, url in ctx.cloud.log if is_upload(method, url)]) == 1

    def test_a_server_error_on_upload_is_retried(self, ctx):
        ctx.cloud.fault(is_upload, (503, b'{}'))

        assert ctx.run() == crj.OK
        assert ctx.cloud.uploads == [(OBJECT, ctx.generation, 200)]

    def test_a_database_damaged_by_the_send_is_not_saved(self, ctx):
        def send(timeout):
            ctx.db_path.write_bytes(corrupt_db_bytes(ctx.tmp))
            return 0
        ctx.runner.actions['telegram_sender.py'] = send

        assert ctx.run() == crj.NOT_SAVED
        assert ctx.cloud.uploads == []
        assert ctx.saved() == ctx.seed


# ── Child process time limits ────────────────────────────────────────────────

class TestTimeLimits:
    def test_limits_fit_inside_the_job_timeout(self, ctx):
        ctx.run()

        search, send = ctx.runner.calls
        assert search['timeout'] <= crj.SEARCH_LIMIT_SECS
        assert send['timeout'] <= crj.SEND_LIMIT_SECS
        assert crj.RUN_DEADLINE_SECS < 30 * 60
        assert (crj.SEARCH_LIMIT_SECS + crj.SEND_LIMIT_SECS + crj.UPLOAD_RESERVE_SECS
                <= crj.RUN_DEADLINE_SECS)

    def test_a_search_that_times_out_still_lets_the_send_run(self, ctx):
        def hangs(timeout):
            ctx.clock.advance(timeout)
            raise subprocess.TimeoutExpired(['python', 'job_search_smart.py'], timeout)
        ctx.runner.actions['job_search_smart.py'] = hangs

        assert ctx.run() == crj.SEARCH_FAILED
        assert ctx.runner.scripts == ['job_search_smart.py', 'telegram_sender.py']
        assert ctx.runner.calls[1]['timeout'] <= crj.SEND_LIMIT_SECS
        assert ctx.clock.now + crj.UPLOAD_RESERVE_SECS <= crj.RUN_DEADLINE_SECS + 1
        assert ctx.cloud.uploads == [(OBJECT, ctx.generation, 200)]

    def test_a_slow_start_shortens_the_search(self, ctx):
        def slow_download(cloud, method, url, headers, body):
            ctx.clock.advance(600)
            return cloud.handle(method, url, headers, body)
        ctx.cloud.fault(is_download, slow_download)

        ctx.run()

        search = ctx.runner.calls[0]
        assert search['timeout'] == (crj.RUN_DEADLINE_SECS - 600
                                     - crj.SEND_LIMIT_SECS - crj.UPLOAD_RESERVE_SECS)

    def test_a_send_that_times_out_still_saves_the_database(self, ctx):
        def hangs(timeout):
            ctx.clock.advance(timeout)
            raise subprocess.TimeoutExpired(['python', 'telegram_sender.py'], timeout)
        ctx.runner.actions['telegram_sender.py'] = hangs

        assert ctx.run() == crj.SEND_FAILED
        assert ctx.cloud.uploads == [(OBJECT, ctx.generation, 200)]


# ── The failure notice ───────────────────────────────────────────────────────

def missing_object(ctx):
    del ctx.cloud.objects[OBJECT]


def md5_mismatch(ctx):
    ctx.cloud.objects[OBJECT]['md5Hash'] = md5_b64(b'x')


def search_fails(ctx):
    ctx.runner.actions['job_search_smart.py'] = lambda timeout: 1


def changed_before_send(ctx):
    def search(timeout):
        ctx.cloud.store(OBJECT, make_db(ctx.tmp / 'other.db'))
        return 1  # and the search failed too: still one line
    ctx.runner.actions['job_search_smart.py'] = search


def conflict(ctx):
    def send(timeout):
        ctx.cloud.store(OBJECT, make_db(ctx.tmp / 'theirs.db'))
        return 1
    ctx.runner.actions['telegram_sender.py'] = send


def upload_fails(ctx):
    for _ in range(crj.HTTP_ATTEMPTS):
        ctx.cloud.fault(is_upload, crj.NetworkError('TimeoutError'))


def unexpected(ctx):
    ctx.cloud.fault(lambda method, url: url == crj.TOKEN_URL, ValueError(ACCESS_TOKEN))


FAILURES = {
    'missing_object': (missing_object, crj.DB_MISSING),
    'md5_mismatch': (md5_mismatch, crj.DOWNLOAD_CORRUPT),
    'search_fails': (search_fails, crj.SEARCH_FAILED),
    'changed_before_send': (changed_before_send, crj.DB_CHANGED),
    'conflict': (conflict, crj.CONFLICT),
    'upload_fails': (upload_fails, crj.NOT_SAVED),
    'unexpected': (unexpected, crj.UNEXPECTED),
}


class TestFailureNotice:
    @pytest.mark.parametrize('name', FAILURES)
    def test_a_failure_sends_exactly_one_telegram_line_with_no_secret(self, ctx, capsys, name):
        arrange, expected = FAILURES[name]
        arrange(ctx)

        assert ctx.run() == expected

        assert len(ctx.cloud.telegram) == 1
        line = ctx.cloud.telegram[0]
        printed = capsys.readouterr().out
        assert '\n' not in line
        assert line.startswith('[SHADOW] ')
        assert 're-run' in line.lower()
        for text in (line, printed):
            for secret in list(SECRET_ENV.values()) + [ACCESS_TOKEN, BUCKET]:
                assert secret not in text
            assert 'http' not in text.lower()

    def test_success_sends_no_notice(self, ctx):
        assert ctx.run() == crj.OK
        assert ctx.cloud.telegram == []

    def test_no_telegram_settings_means_no_notice_and_no_crash(self, ctx):
        del ctx.env['ENV_FILE_PATH']
        del ctx.cloud.objects[OBJECT]

        assert ctx.run() == crj.DB_MISSING
        assert ctx.cloud.telegram == []

    def test_a_notice_that_cannot_be_sent_does_not_change_the_exit_code(self, ctx):
        del ctx.cloud.objects[OBJECT]
        ctx.cloud.fault(lambda method, url: 'api.telegram.org' in url,
                        crj.NetworkError('TimeoutError'))

        assert ctx.run() == crj.DB_MISSING

    def test_the_real_transport_returns_error_statuses_and_hides_the_url(self, monkeypatch):
        # urlopen is replaced, so no socket opens. An error status comes back
        # as a value, and a request with no answer raises without the URL,
        # which can hold the bot token.
        import io
        import urllib.error
        import urllib.request
        secret_url = f"https://api.telegram.org/bot{SECRET_ENV['TELEGRAM_BOT_TOKEN']}/x"

        class Response(io.BytesIO):
            status = 200

        answers = [
            Response(b'ok'),
            urllib.error.HTTPError(secret_url, 412, 'Precondition Failed', {}, io.BytesIO(b'{}')),
            urllib.error.URLError(ConnectionRefusedError(f'refused {secret_url}')),
            TimeoutError(f'timed out {secret_url}'),
        ]

        def urlopen(request, timeout=None):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        monkeypatch.setattr(urllib.request, 'urlopen', urlopen)

        assert crj.urllib_http('GET', secret_url) == (200, b'ok')
        assert crj.urllib_http('POST', secret_url, body=b'x') == (412, b'{}')
        for _ in range(2):
            with pytest.raises(crj.NetworkError) as raised:
                crj.urllib_http('GET', secret_url)
            assert SECRET_ENV['TELEGRAM_BOT_TOKEN'] not in str(raised.value)
            assert raised.value.__suppress_context__

    def test_exit_codes_are_distinct_and_documented(self):
        codes = [crj.OK, crj.UNEXPECTED, crj.SETUP, crj.STORAGE_UNREACHABLE,
                 crj.DB_MISSING, crj.DOWNLOAD_CORRUPT, crj.DB_DAMAGED, crj.DB_CHANGED,
                 crj.SEARCH_FAILED, crj.SEND_FAILED, crj.NOT_SAVED, crj.CONFLICT]
        assert len(set(codes)) == len(codes)
        for code in codes:
            assert f'\n  {code:>2}  ' in crj.__doc__
