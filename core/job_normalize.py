"""
Normalisation for deduplication.

The same job reaches this system several times: once per source, often with a
different tracking URL and a slightly different title. Comparing raw strings
treats every variant as a new job, so the digest repeats itself and the
seen-link table never fires.

Everything here is standard library. No dependency solves this better than
twenty lines of regex, and the rules need to stay readable because they are
the ones that will need tuning.
"""

import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Query parameters that identify the referrer, not the job. Two URLs that
# differ only in these point at the same posting.
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'utm_source_platform', 'utm_creative_format',
    'ref', 'refid', 'referrer', 'source', 'src',
    'gh_src', 'gh_jid_src',
    'trackingid', 'trk', 'trkinfo', 'originaltrackingid',
    'lipi', 'licu', 'position', 'pagenum',
    'fbclid', 'gclid', 'msclkid',
    'sessionid', 'sid', 'cid', 'mc_cid', 'mc_eid',
    'from', 'campaignid', 'jobid_src',
}

# Bracketed or trailing noise that boards append to titles.
# "Sales Engineer (Remote, m/w/d)" and "Sales Engineer" are one job.
_TITLE_NOISE_PATTERNS = [
    r'\((?:[^()]*)\)',                  # anything parenthesised
    r'\[[^\[\]]*\]',                    # anything bracketed
    r'\bm\s*/\s*[wf]\s*/\s*[dx]\b',     # m/w/d, m/f/d, m/w/x
    r'\b[wf]\s*/\s*m\s*/\s*[dx]\b',     # w/m/d
    r'\bm\s*/\s*[wf]\b',                # m/w
    r'\b(?:fully\s+)?remote\b',
    r'\bhybrid\b',
    r'\bon[\s-]?site\b',
    r'\bfull[\s-]?time\b',
    r'\bpart[\s-]?time\b',
    r'\bcontract\b',
    r'\bpermanent\b',
    r'\bfreelance\b',
    r'\bintern(?:ship)?\b',
    r'\burgent(?:ly)?\s+hiring\b',
    r'\bnew\b\s*$',
    r'\bh/f\b',                         # French
    r'\bw/m/d\b',
]

# Legal suffixes. "Acme Inc." and "Acme" are one company.
_COMPANY_SUFFIXES = [
    'incorporated', 'inc', 'llc', 'l.l.c', 'ltd', 'limited', 'plc',
    'corporation', 'corp', 'company', 'co',
    'gmbh', 'mbh', 'ag', 'kg', 'gmbh & co kg',
    'b.v', 'bv', 'n.v', 'nv',
    's.a', 'sa', 's.a.s', 'sas', 's.r.l', 'srl', 's.p.a', 'spa',
    'a/s', 'as', 'ab', 'oy', 'aps',
    'd.o.o', 'doo', 'a.d', 'ad',
    'pty', 'pty ltd', 'sp z o.o', 'sp zoo', 'z o.o',
    'group', 'holding', 'holdings', 'international',
]

_PUNCT = re.compile(r'[^\w\s]', flags=re.UNICODE)
_WHITESPACE = re.compile(r'\s+')


def canonical_url(url):
    """
    Strip tracking noise from a URL so the same posting compares equal.

    Lowercases the host, drops tracking query parameters, removes the
    fragment, and drops a trailing slash. Path case is preserved because
    some boards use case sensitive job ids.

    >>> canonical_url('https://WWW.Example.com/jobs/12?utm_source=x&id=7#top')
    'https://www.example.com/jobs/12?id=7'
    """
    if not url:
        return ''

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    host = parts.netloc.lower()

    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept))

    path = parts.path.rstrip('/') or '/'

    return urlunsplit((parts.scheme.lower(), host, path, query, ''))


def normalize_title(title):
    """
    Reduce a job title to its comparable core.

    Removes bracketed content, employment type words, gender markers and
    punctuation, then collapses whitespace.

    >>> normalize_title('Senior Sales Engineer (Remote, m/w/d) - Full-time')
    'senior sales engineer'
    """
    if not title:
        return ''

    text = str(title).lower()

    for pattern in _TITLE_NOISE_PATTERNS:
        text = re.sub(pattern, ' ', text, flags=re.IGNORECASE)

    # Drop a trailing location or qualifier after a dash or pipe.
    text = re.split(r'\s+[-–—|]\s+', text)[0]

    text = _PUNCT.sub(' ', text)
    return _WHITESPACE.sub(' ', text).strip()


def normalize_company(company):
    """
    Reduce a company name to its comparable core.

    Strips legal suffixes and punctuation.

    >>> normalize_company('Acme Fluid Systems B.V.')
    'acme fluid systems'
    """
    if not company:
        return ''

    text = _PUNCT.sub(' ', str(company).lower())
    text = _WHITESPACE.sub(' ', text).strip()

    # Strip suffixes from the end, repeatedly, so "Acme Group Ltd" reduces.
    changed = True
    while changed and text:
        changed = False
        for suffix in sorted(_COMPANY_SUFFIXES, key=len, reverse=True):
            token = _WHITESPACE.sub(' ', _PUNCT.sub(' ', suffix)).strip()
            if token and text.endswith(' ' + token):
                text = text[: -(len(token) + 1)].strip()
                changed = True
                break

    return text


def dedup_key(title, company):
    """
    The value stored and uniquely indexed per job.

    Two postings sharing this key are the same job as far as the digest is
    concerned, whatever URL or title decoration they arrived with.
    """
    return f"{normalize_title(title)}|{normalize_company(company)}"


def title_similarity(a, b):
    """
    Jaccard similarity over normalised title tokens, 0.0 to 1.0.

    Used for the near-duplicate pass, where two titles survive
    normalisation but still describe one job, for example
    "Sales Engineer Industrial" and "Industrial Sales Engineer".
    """
    tokens_a = set(normalize_title(a).split())
    tokens_b = set(normalize_title(b).split())

    if not tokens_a or not tokens_b:
        return 0.0

    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return intersection / union


def find_near_duplicates(jobs, threshold=0.85):
    """
    Return the indices of jobs that duplicate an earlier job in the list.

    Only compares jobs at the same normalised company, which keeps this
    close to linear in practice. Order matters: the first occurrence is
    kept, later ones are reported.

    Args:
        jobs: list of dicts with 'title' and 'company'
        threshold: Jaccard score at or above which two titles are one job

    Returns:
        set of indices into `jobs` that should be dropped
    """
    by_company = {}
    duplicates = set()

    for index, job in enumerate(jobs):
        company = normalize_company(job.get('company', ''))
        title = job.get('title', '')

        seen = by_company.setdefault(company, [])
        if any(title_similarity(title, other) >= threshold for other in seen):
            duplicates.add(index)
        else:
            seen.append(title)

    return duplicates
