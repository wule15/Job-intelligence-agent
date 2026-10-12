"""
12 October: the direct and regional digests went empty. The dedup key cut a
title at " - ", so different openings at one company shared a key and were
dropped, merged or held back as repeats. And an empty digest sent nothing, so
the chat could not tell a quiet section from a broken one.
"""

import sqlite3

from core.database import Database
from core.job_normalize import dedup_key, find_near_duplicates, normalize_title


# ── The dedup key keeps what tells two openings apart ─────────────────────────

def test_qualifier_after_a_dash_is_kept_in_the_key():
    a = dedup_key('Project Engineer - Calculation (Antwerp, Belgium)', 'Atlas Copco Group')
    b = dedup_key('Project Engineer - Mechanical (Antwerp, Belgium)', 'Atlas Copco Group')
    assert a != b
    assert a == 'project engineer calculation|atlas copco'


def test_a_place_or_remote_label_after_a_dash_is_still_dropped():
    assert dedup_key('Sales Engineer - Germany', 'Acme') == dedup_key('Sales Engineer', 'Acme')
    assert dedup_key('Sales Engineer - Remote', 'Acme') == dedup_key('Sales Engineer', 'Acme')
    assert dedup_key('Area Sales Manager - UK North', 'Acme') == dedup_key('Area Sales Manager', 'Acme')


def test_plain_normalize_title_is_unchanged():
    """The exclusion list still matches on the title before the dash."""
    assert normalize_title('Project Engineer - Calculation') == 'project engineer'


def test_different_openings_are_not_near_duplicates():
    jobs = [
        {'title': 'Project Engineer - Calculation', 'company': 'Atlas Copco'},
        {'title': 'Project Engineer - Mechanical', 'company': 'Atlas Copco'},
        {'title': 'Project Engineer - Belgium', 'company': 'Atlas Copco'},
    ]
    assert find_near_duplicates(jobs) == set()


def test_same_opening_with_a_location_suffix_is_a_near_duplicate():
    jobs = [
        {'title': 'Project Engineer - Calculation', 'company': 'Atlas Copco'},
        {'title': 'Project Engineer - Calculation - Belgium', 'company': 'Atlas Copco'},
    ]
    assert find_near_duplicates(jobs) == {1}


# ── Seen links are forgotten once, so the hidden openings are scored again ────

def _seen_count(path):
    with sqlite3.connect(path) as conn:
        return conn.execute('SELECT COUNT(*) FROM job_links_seen').fetchone()[0]


def test_seen_links_are_cleared_once(tmp_path):
    path = str(tmp_path / 'jobs.db')
    db = Database(path)
    db.mark_job_link_seen('https://example.com/a')
    db.close()
    # The first open already ran the reset, so this link survives a reopen.
    db = Database(path)
    db.close()
    assert _seen_count(path) == 1


def test_seen_links_from_before_the_fix_are_cleared(tmp_path):
    path = str(tmp_path / 'jobs.db')
    db = Database(path)
    db.mark_job_link_seen('https://example.com/a')
    db.connection.execute('DELETE FROM migrations')
    db.connection.commit()
    db.close()
    db = Database(path)
    db.close()
    assert _seen_count(path) == 0
