"""
Fixes from reading the 5 October digest advert by advert: a US job that
reached a Europe-only digest, a Dutch requirement that was missed, a closed
advert that was sent, senior sales titles, a years requirement hidden past a
2000-character cut, a year range written with a non-breaking hyphen, and a log
file that could not rotate.
"""

import logging

from core.job_filter import (
    is_senior_title, required_experience, requires_too_many_years,
    requires_unspoken_language,
)
from core import utils
from sources import ats
from sources.free_boards import DESCRIPTION_MAX_CHARS, clean_description
import telegram_sender


# ── Placeholder location on a SuccessFactors feed ─────────────────────────────

PAGE = ('<div><span class="joblayouttoken-label" role="heading">Job Location: </span> '
        '<span xml:lang="en-GB" class="rtltextaligneligible">Loves Park, IL, US </span></div>')


def test_page_location_read():
    assert ats.sf_page_location(PAGE) == 'Loves Park, IL, US'
    assert ats.sf_page_location('<p>no location here</p>') == ''


class _Page:
    status_code = 200
    text = PAGE


def test_placeholder_replaced_from_page(monkeypatch):
    monkeypatch.setattr(ats.detail_session, 'get', lambda *a, **k: _Page())
    jobs = [
        {'title': 'Project Manager (City-State-Country, City-State-Country)',
         'location': 'City-State-Country, City-State-Country', 'link': 'https://x/1'},
        {'title': 'Engineer', 'location': 'Kamnik, Slovenia', 'link': 'https://x/2'},
    ]
    ats._fill_placeholder_locations(jobs)
    assert jobs[0]['location'] == 'Loves Park, IL, United States'
    assert jobs[0]['title'] == 'Project Manager'
    assert jobs[1]['location'] == 'Kamnik, Slovenia'


def test_placeholder_kept_when_page_fails(monkeypatch):
    def boom(*a, **k):
        raise OSError('offline')
    monkeypatch.setattr(ats.detail_session, 'get', boom)
    jobs = [{'title': 'X', 'location': 'City-State-Country', 'link': 'https://x/1'}]
    ats._fill_placeholder_locations(jobs)
    assert jobs[0]['location'] == 'City-State-Country'


# ── Language level with "communication" in between ────────────────────────────

def test_fluent_communication_skills_in_dutch_drops():
    text = 'Fluent communication skills in Dutch and English (spoken and written)'
    assert requires_unspoken_language(text, ['dutch'])


def test_communication_skills_dutch_plus_kept():
    assert not requires_unspoken_language(
        'Excellent communication skills in English; Dutch is a plus', ['dutch'])


# ── Closed SuccessFactors advert ──────────────────────────────────────────────

class _Resp:
    status_code = 200
    headers = {}
    text = "<p>You can't view this job because it's not available at this time.</p>"


def test_successfactors_closed_page_is_closed(monkeypatch):
    telegram_sender.reset_liveness()
    monkeypatch.setattr(telegram_sender.requests, 'get', lambda *a, **k: _Resp())
    assert telegram_sender.check_link_live('https://jobs.example.com/job/1') is False


# ── Senior account titles ─────────────────────────────────────────────────────

def test_sized_account_titles_are_senior():
    for title in ('Major Account Executive, France', 'Major Account Manager, Sweden',
                  'Enterprise Account Executive', 'Strategic Account Manager'):
        assert is_senior_title(title), title


def test_plain_and_key_account_titles_kept():
    for title in ('Account Manager', 'Key Account Manager', 'Account Executive',
                  'Technical Key Account Manager'):
        assert not is_senior_title(title), title


# ── Advert text kept whole enough to read the requirements ────────────────────

def test_requirements_past_2000_characters_are_read():
    intro = '<p>' + 'We protect data across clouds. ' * 120 + '</p>'
    html = intro + '<ul><li>5+ years of experience in technical consulting</li></ul>'
    assert len(html) > 2000
    text = clean_description(html)
    assert requires_too_many_years(text, limit=4)


def test_clean_description_strips_tags_and_caps():
    assert clean_description('<p>A &amp; B</p><li>C</li>') == 'A & B\nC'
    assert len(clean_description('x' * (DESCRIPTION_MAX_CHARS + 50))) == DESCRIPTION_MAX_CHARS


def test_bullets_do_not_share_a_preferred_marker():
    text = clean_description('<li>5+ years of experience</li><li>Python is a plus</li>')
    assert required_experience(text) == (5, False)


# ── Year range with any dash ──────────────────────────────────────────────────

def test_year_range_with_non_breaking_hyphen():
    for dash in ('‐', '‑', '–', '—', '-'):
        assert required_experience(f'3{dash}7 years experience')[0] == 3, ascii(dash)


# ── One file handler for every logger ─────────────────────────────────────────

def test_loggers_share_one_file_handler(tmp_path):
    log_file = tmp_path / 'shared.log'
    a = utils.setup_logging('fix1010.a', log_file=log_file)
    b = utils.setup_logging('fix1010.b', log_file=log_file)
    files_a = [h for h in a.handlers if isinstance(h, logging.FileHandler)]
    files_b = [h for h in b.handlers if isinstance(h, logging.FileHandler)]
    assert files_a and files_a[0] is files_b[0]
    for handler in files_a:
        handler.close()


def test_second_setup_adds_no_handlers(tmp_path):
    log_file = tmp_path / 'again.log'
    first = len(utils.setup_logging('fix1010.c', log_file=log_file).handlers)
    second = len(utils.setup_logging('fix1010.c', log_file=log_file).handlers)
    assert first == second
