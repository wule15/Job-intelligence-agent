"""
Tests for reading CV variants from the master CV.

The scorer reads its skills from master-cv.yaml's `variants:` section now,
instead of extracting them from PDF and DOCX files. These tests pin the loader
and prove the scorer still picks the best-fit variant per job.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.cv_variants import load_variants  # noqa: E402
from core.job_filter import JobFilter  # noqa: E402


def _write(tmp_path, text):
    p = tmp_path / "master-cv.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class TestLoadVariants:
    def test_list_form_skills(self, tmp_path):
        p = _write(tmp_path, """
variants:
  sales:
    label: Sales Engineer
    skills:
      - valve sizing
      - technical sales
""")
        data = load_variants(p)
        assert set(data['cvs']) == {'sales'}
        assert data['cvs']['sales']['label'] == 'Sales Engineer'
        assert data['cvs']['sales']['skills'] == {'valve sizing': 1, 'technical sales': 1}

    def test_mapping_form_skills_keeps_weights(self, tmp_path):
        p = _write(tmp_path, """
variants:
  writer:
    skills:
      technical writing: 3
      seo: 2
""")
        data = load_variants(p)
        assert data['cvs']['writer']['skills'] == {'technical writing': 3, 'seo': 2}
        # label defaults to the key when not given
        assert data['cvs']['writer']['label'] == 'writer'

    def test_merged_skills_is_the_union(self, tmp_path):
        p = _write(tmp_path, """
variants:
  a:
    skills: [x, y]
  b:
    skills: [y, z]
""")
        data = load_variants(p)
        assert set(data['merged_skills']) == {'x', 'y', 'z'}

    def test_missing_file_returns_none(self, tmp_path):
        assert load_variants(tmp_path / "nope.yaml") is None

    def test_no_variants_section_returns_none(self, tmp_path):
        p = _write(tmp_path, "facts:\n  name: Someone\n")
        assert load_variants(p) is None

    def test_variant_with_no_skills_is_skipped(self, tmp_path):
        p = _write(tmp_path, """
variants:
  empty:
    label: Empty
  real:
    skills: [valve sizing]
""")
        data = load_variants(p)
        assert set(data['cvs']) == {'real'}

    def test_malformed_yaml_returns_none_not_raise(self, tmp_path):
        p = _write(tmp_path, "variants: [this: is, : broken")
        assert load_variants(p) is None


class TestScorerUsesVariants:
    """The scorer reads variants and still reports the best-fit one per job."""

    def test_best_variant_is_reported(self, tmp_path):
        p = _write(tmp_path, """
variants:
  sales_engineer:
    label: Sales Engineer
    skills: [valve sizing, kv calculation, technical sales, p&id]
  technical_writer:
    label: Technical Writer
    skills: [technical writing, seo, documentation]
""")
        jf = JobFilter()
        jf.skills_data = load_variants(p)
        jf.all_skills = {s for cv in jf.skills_data['cvs'].values() for s in cv['skills']}

        score, best = jf.score_job_with_cv(
            'Sales Engineer',
            'Valve sizing, Kv calculation and P&ID for a flow control vendor.',
            'Acme Fluid Systems',
        )
        assert best == 'sales_engineer'
        assert score > 0
