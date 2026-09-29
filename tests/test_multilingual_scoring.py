"""
Local-language adverts score on their meaning, not only on English words.

The scorer matches the CV's English skill words against the advert. A Serbian
or German advert for the same job matched almost nothing and sank below the
cutoff. The glossary adds the English equivalent of each local term it finds,
so the same job scores close to its English version.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.multilingual import english_equivalents  # noqa: E402

EN = ('Technical sales of industrial valves and pumps. Commissioning, '
      'preventive maintenance, hydraulics and project management.')
SR = ('Tehnička prodaja industrijskih ventila i pumpi. Puštanje u rad, '
      'preventivno održavanje, hidraulika i upravljanje projektima.')
DE = ('Technischer Vertrieb von Industriearmaturen und Pumpen. Inbetriebnahme, '
      'Instandhaltung, Hydraulik und Projektmanagement.')


class TestEnglishEquivalents:
    def test_serbian_terms_gain_english_equivalents(self):
        extra = english_equivalents(SR)
        for word in ('technical sales', 'valve', 'pump', 'commissioning',
                     'preventive maintenance', 'hydraulics', 'project management'):
            assert word in extra, word

    def test_german_terms_gain_english_equivalents(self):
        extra = english_equivalents(DE)
        for word in ('technical sales', 'valve', 'pump', 'commissioning',
                     'maintenance', 'hydraulics', 'project management'):
            assert word in extra, word

    def test_english_text_gains_nothing(self):
        assert english_equivalents(EN) == ''

    def test_ventilation_is_not_a_valve(self):
        assert 'valve' not in english_equivalents('Ventilacija i klimatizacija')


class TestScoringParity:
    def test_serbian_advert_scores_near_its_english_version(self, job_filter):
        en, _ = job_filter.score_job_with_cv('Sales Engineer', EN, 'Acme')
        sr, _ = job_filter.score_job_with_cv('Inženjer prodaje', SR, 'Acme')
        assert en > 0
        assert sr >= en * 0.6

    def test_german_advert_scores_near_its_english_version(self, job_filter):
        en, _ = job_filter.score_job_with_cv('Sales Engineer', EN, 'Acme')
        de, _ = job_filter.score_job_with_cv('Vertriebsingenieur', DE, 'Acme')
        assert de >= en * 0.6

    def test_english_score_is_unchanged(self, job_filter, monkeypatch):
        from core import job_filter as jf_module
        before, _ = job_filter.score_job_with_cv('Sales Engineer', EN, 'Acme')
        monkeypatch.setattr(jf_module, 'english_equivalents', lambda text: '')
        after, _ = job_filter.score_job_with_cv('Sales Engineer', EN, 'Acme')
        assert before == after
