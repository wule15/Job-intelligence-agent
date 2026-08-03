"""
Shared HTTP session with retries.

Before this existed, config.py declared RETRY_ATTEMPTS and RETRY_BACKOFF and
nothing read either one. A source that hit a rate limit or a transient proxy
failure simply produced nothing for the day, and the run reported success.

The logs show this was not theoretical. DuckDuckGo returned one job from four
queries while being throttled, and the Telegram sender recorded repeated
"Tunnel connection failed: 403" proxy errors. Both are exactly the kind of
failure a retry recovers from.

Retries cover transient conditions only:

    408 request timeout
    429 too many requests
    500, 502, 503, 504 upstream failures
    connection errors and read timeouts

A 401, 403 or 404 is not retried. Those mean the request was wrong, and
repeating it wastes quota and delays the run.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import Config
from core.utils import setup_logging

logger = setup_logging('http_client')

# Transient by nature. Anything else is a problem retrying cannot fix.
RETRY_STATUSES = (408, 429, 500, 502, 503, 504)

# POST is included because the endpoints this project posts to are read-only
# queries. Workday's job listing is a POST that returns results and changes
# nothing. Do not add a POST endpoint here that creates or mutates anything.
RETRY_METHODS = frozenset(['GET', 'HEAD', 'POST'])

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
)


def build_retry(attempts=None, backoff=None):
    """
    Build the retry policy.

    backoff_factor produces delays of backoff * 2**(n-1) between attempts,
    so the default of 1 second gives 1s, 2s, 4s. respect_retry_after_header
    means a server that tells us how long to wait is obeyed instead of
    guessed at, which matters for the APIs that publish a quota.
    """
    return Retry(
        total=Config.RETRY_ATTEMPTS if attempts is None else attempts,
        backoff_factor=Config.RETRY_BACKOFF if backoff is None else backoff,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=RETRY_METHODS,
        respect_retry_after_header=True,
        # Return the final response rather than raising, so callers keep
        # their existing status_code handling and see the real status.
        raise_on_status=False,
    )


def build_session(attempts=None, backoff=None, user_agent=DEFAULT_USER_AGENT):
    """
    A requests.Session that retries transient failures on every request.

    Use this instead of the requests module directly. A bare requests.get
    has no retry policy, so one throttled response loses that source for
    the whole run.
    """
    session = requests.Session()

    adapter = HTTPAdapter(max_retries=build_retry(attempts, backoff))
    session.mount('https://', adapter)
    session.mount('http://', adapter)

    if user_agent:
        session.headers.update({'User-Agent': user_agent})

    return session


def request_json(session, method, url, **kwargs):
    """
    Make a request and return parsed JSON, or raise with a useful message.

    Never includes the response body or the request URL in the raised error.
    Both can carry credentials: a bot token lives in the Telegram request
    URL, and error bodies often echo the key that was sent.
    """
    kwargs.setdefault('timeout', Config.REQUEST_TIMEOUT)
    response = session.request(method, url, **kwargs)

    if response.status_code != 200:
        raise RuntimeError(f'HTTP {response.status_code}')

    return response.json()
