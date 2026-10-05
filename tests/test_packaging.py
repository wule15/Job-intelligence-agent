"""
What goes into an install and into a container image.

requirements.txt lists only what the code imports, so an install carries no
package the project does not use and the README cannot name a parser that is
not there.

.dockerignore decides what reaches a Docker build. Docker is not run here, so
the rules are read by a small copy of Docker's own matcher: a pattern is
matched against the path and against each of its parent folders, `**` spans
any number of folders, `*` and `?` stay inside one, and the last rule that
matches decides. The private paths below are the layout of a real deployment
folder, with invented file names.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Distribution name on PyPI -> the name the code imports.
IMPORT_NAMES = {
    'python-dotenv': 'dotenv',
    'beautifulsoup4': 'bs4',
    'apify-client': 'apify_client',
    'pyyaml': 'yaml',
    'python-docx': 'docx',
    'flask': 'flask',
}


def code_files():
    files = list(ROOT.glob('*.py'))
    for folder in ('core', 'sources'):
        files += (ROOT / folder).glob('*.py')
    return files


def imported_modules():
    names = set()
    for path in code_files():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split('.')[0])
    return names


def requirements():
    names = []
    for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if line and not line.startswith('-'):
            names.append(re.split(r'[<>=!~\[ ]', line, maxsplit=1)[0].lower())
    return names


class TestRequirements:
    def test_every_listed_package_is_imported_by_the_code(self):
        imported = imported_modules()
        unused = [
            name for name in requirements()
            if IMPORT_NAMES.get(name, name.replace('-', '_')) not in imported
        ]

        assert unused == []

    def test_no_pdf_parser_is_listed_or_claimed(self):
        for name in ('requirements.txt', 'requirements-dev.txt', 'README.md',
                     'Dockerfile', '.env.example'):
            text = (ROOT / name).read_text(encoding='utf-8').lower()
            assert 'pdfplumber' not in text, name


# â”€â”€ .dockerignore â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def docker_rules():
    rules = []
    for line in (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        keep = line.startswith('!')
        pattern = line[1:].strip() if keep else line
        pattern = pattern.strip('/')
        rules.append((keep, re.compile('^' + to_regex(pattern) + '$')))
    return rules


def to_regex(pattern):
    """Docker's pattern syntax as a regular expression."""
    out, i = '', 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith('**/', i):
            out += '(.*/)?'
            i += 3
        elif pattern.startswith('**', i):
            out += '.*'
            i += 2
        elif char == '*':
            out += '[^/]*'
            i += 1
        elif char == '?':
            out += '[^/]'
            i += 1
        else:
            out += re.escape(char)
            i += 1
    return out


def excluded(path, rules):
    parts = path.split('/')
    candidates = ['/'.join(parts[:n]) for n in range(1, len(parts) + 1)]
    result = False
    for keep, regex in rules:
        if any(regex.match(candidate) for candidate in candidates):
            result = not keep
    return result


PRIVATE = [
    '.env',
    'core/.env',
    'core/.env.bak-1',
    'core/data/job_digest.db',
    'core/data/job_digest.db-wal',
    'core/data/digest_exclude.txt',
    'core/data/env.bak-1',
    'core/data/companies.json.bak-1',
    'core/config/companies.json',
    'config/companies.json',
    'core/master-cv.yaml',
    'master-cv.yaml',
    'core/master-cv.sales.yaml',
    'core/keywords.json',
    'core/logs/job_search.log',
    'logs/job_search.log',
    'core/output/docx cover letters/cover_letter_1_Sales Engineer.docx',
    'core/cache/skills.json',
    'core/resumes/cv.pdf',
    'core/linkedin profile/profile.pdf',
    'core/credentials.json',
    'core/token.json',
    'core/client_secret_1.json',
    'git.txt',
    'core/git.txt',
    'backup/history.bundle',
    'old/notes.py',
    'archive/run.py',
    'local/notes.py',
    'graphify-out/graph.json',
    '.git/config',
    '.venv/lib/site.py',
    'core/__pycache__/config.cpython-312.pyc',
    'tests/test_end_to_end.py',
    'docs/dashboard-demo.png',
]


def runtime_files():
    files = [p.relative_to(ROOT).as_posix() for p in code_files()]
    files += ['templates/dashboard.html', 'requirements.txt']
    return files


class TestDockerignore:
    @pytest.mark.parametrize('path', PRIVATE)
    def test_private_path_never_reaches_the_build(self, path):
        assert excluded(path, docker_rules())

    def test_everything_the_agent_runs_on_reaches_the_build(self):
        rules = docker_rules()
        missing = [path for path in runtime_files() if excluded(path, rules)]

        assert missing == []

    def test_the_matcher_reads_docker_patterns_as_docker_does(self):
        # A guard on the guard: the rules above are only as good as this.
        rules = [(False, re.compile('^' + to_regex('**/.env') + '$'))]
        assert excluded('.env', rules)
        assert excluded('core/.env', rules)
        assert not excluded('core/.env.example', rules)

        rules = [(False, re.compile('^' + to_regex('.env') + '$'))]
        assert not excluded('core/.env', rules)

        rules = [(False, re.compile('^' + to_regex('*') + '$')),
                 (True, re.compile('^' + to_regex('core') + '$')),
                 (False, re.compile('^' + to_regex('**/data') + '$'))]
        assert excluded('README.md', rules)
        assert not excluded('core/config.py', rules)
        assert excluded('core/data/job_digest.db', rules)
