"""
Load CV variants from the master CV, the single source of truth.

The scorer used to extract skills from PDF and DOCX files in resumes/, one skill
set per file. That text extraction is fragile, and the skills then lived in two
places, the master CV and the generated documents, which drift apart.

This module reads the variants straight from master-cv.yaml instead. A variant
is one way you present yourself, Sales Engineer, Mechanical Engineer and so on,
with the skills that matter for that kind of role. The scorer matches each job
against every variant and reports which one fits best, so the per-job "apply
with this CV" signal is preserved exactly as before.

The master file holds real employer names and is never published. Only
master-cv.example.yaml, with placeholders, is committed. See it for the format.
"""

from __future__ import annotations

from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a declared dependency
    yaml = None

from core.utils import setup_logging

logger = setup_logging('cv_variants')


def _skills_to_dict(skills) -> dict:
    """Normalise a variant's skills to a dict of skill -> weight.

    Accepts either a plain list, where every skill counts equally, or a mapping
    of skill -> weight when some skills should count for more.
    """
    if isinstance(skills, dict):
        return {str(k): v for k, v in skills.items() if k}
    if isinstance(skills, (list, tuple)):
        return {str(s): 1 for s in skills if s}
    return {}


def load_variants(master_path):
    """Load CV variants from the master CV file.

    Returns a skills-data dict in the shape the scorer already expects:

        {'cvs': {variant: {'label': str, 'skills': {skill: weight}}},
         'linkedin': {}, 'merged_skills': {skill: weight}}

    Returns None if the file is absent, unreadable, or defines no usable
    variants, so the caller can fall back to the old keyword cache.
    """
    path = Path(master_path)
    if yaml is None or not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Could not read master CV at {path}: {type(e).__name__}")
        return None

    raw = data.get('variants') or {}
    if not isinstance(raw, dict) or not raw:
        return None

    cvs = {}
    merged = {}
    for key, entry in raw.items():
        entry = entry or {}
        skills = _skills_to_dict(entry.get('skills'))
        if not skills:
            continue
        cvs[str(key)] = {'label': entry.get('label', str(key)), 'skills': skills}
        merged.update(skills)

    if not cvs:
        return None

    logger.info(f"Loaded {len(cvs)} CV variants from {path.name}")
    return {'cvs': cvs, 'linkedin': {}, 'merged_skills': merged}
