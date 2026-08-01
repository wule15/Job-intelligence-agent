"""
Tests for the shared helpers.

format_cv_label replaced six near identical copies of the same string
mangling, each of which had a personal filename prefix hardcoded into it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from utils import format_cv_label, sanitize_filename, is_remote_job  # noqa: E402


@pytest.fixture(autouse=True)
def prefix(monkeypatch):
    monkeypatch.setattr(config.Config, 'CV_FILENAME_PREFIX', 'Jane_Doe_CV_')


class TestFormatCvLabel:
    def test_strips_prefix_and_extension(self):
        assert format_cv_label('Jane_Doe_CV_Sales_Engineer.pdf') == 'Sales Engineer'

    def test_strips_trailing_version_number(self):
        assert format_cv_label('Jane_Doe_CV_AI_Updated_2.pdf') == 'AI Updated'

    def test_works_without_extension(self):
        assert format_cv_label('Jane_Doe_CV_Engineering') == 'Engineering'

    def test_leaves_unprefixed_names_alone(self):
        assert format_cv_label('Some_Other_CV.pdf') == 'Some Other CV'

    def test_none_returns_none(self):
        assert format_cv_label(None) is None

    def test_empty_returns_none(self):
        assert format_cv_label('') is None

    def test_prefix_only_returns_none(self):
        assert format_cv_label('Jane_Doe_CV_') is None

    def test_no_configured_prefix_is_harmless(self, monkeypatch):
        monkeypatch.setattr(config.Config, 'CV_FILENAME_PREFIX', '')
        assert format_cv_label('Sales_Engineer.pdf') == 'Sales Engineer'


class TestSanitizeFilename:
    def test_removes_path_separators(self):
        assert '/' not in sanitize_filename('a/b')
        assert '\\' not in sanitize_filename('a\\b')

    def test_removes_windows_reserved_characters(self):
        cleaned = sanitize_filename('a:b*c?d"e<f>g|h')
        assert not any(c in cleaned for c in ':*?"<>|')

    def test_keeps_ordinary_text(self):
        assert sanitize_filename('Sales Engineer 2026') == 'Sales Engineer 2026'


class TestIsRemoteJob:
    def test_detects_remote(self):
        assert is_remote_job('This is a fully remote position.')

    def test_detects_work_from_home(self):
        assert is_remote_job('Work from home, flexible hours.')

    def test_rejects_onsite(self):
        assert not is_remote_job('Based in our Munich office, five days a week.')

    def test_empty_is_not_remote(self):
        assert not is_remote_job('')

    def test_none_is_not_remote(self):
        assert not is_remote_job(None)
