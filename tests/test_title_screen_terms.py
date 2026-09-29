"""
Local-language titles pass the title screen when the user lists their terms.

The title screen decides which jobs get their full advert fetched. It matches
the CV's skill words, which are English, so a local-language title such as
"Inženjer održavanja" never passed, and the job was scored on a two-line teaser.
The user lists local role words in TITLE_SCREEN_TERMS. Matching ignores
diacritics, because boards and users write the same word both ways.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import Config  # noqa: E402
from core.job_filter import title_prescreen, fold_diacritics  # noqa: E402

SKILLS = {'python', 'valve sizing'}


class TestFoldDiacritics:
    def test_strips_accents_and_maps_dj(self):
        assert fold_diacritics('Inženjer održavanja') == 'inzenjer odrzavanja'
        assert fold_diacritics('Građevinski ČŠĆ') == 'gradjevinski csc'


class TestTitleScreenTerms:
    def test_configured_term_passes_accented_title(self, monkeypatch):
        monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['inzenjer'], raising=False)
        assert title_prescreen('Inženjer održavanja', SKILLS)

    def test_accented_term_matches_plain_title(self, monkeypatch):
        monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['održavanj'], raising=False)
        assert title_prescreen('Tehnicar odrzavanja', SKILLS)

    def test_without_terms_local_title_still_fails(self, monkeypatch):
        monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', [], raising=False)
        assert not title_prescreen('Inženjer održavanja', SKILLS)

    def test_terms_do_not_let_unrelated_titles_through(self, monkeypatch):
        monkeypatch.setattr(Config, 'TITLE_SCREEN_TERMS', ['inzenjer'], raising=False)
        assert not title_prescreen('Pastry Chef', SKILLS)
