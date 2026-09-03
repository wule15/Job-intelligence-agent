"""
Tests for two fixes from code review:
  - _pick_regional_queries: use configured REGIONAL_QUERIES (capped), else the
    CV-query fallback. Guards a quota blowout from an unbounded configured list.
  - force_utf8_streams: a diagnostic print of Serbian job titles must not crash a
    run under a cp1252-encoded stream (the daily-run crash that shipped).
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.free_boards import _pick_regional_queries, REGIONAL_QUERY_CAP  # noqa: E402
from core.utils import force_utf8_streams  # noqa: E402


class TestPickRegionalQueries:
    def test_uses_configured_when_set(self):
        cfg = ['engineer', 'process engineer', 'inzenjer']
        assert _pick_regional_queries(cfg, ['cv1', 'cv2']) == cfg

    def test_caps_a_long_configured_list(self):
        big = [f'q{i}' for i in range(50)]
        picked = _pick_regional_queries(big, ['cv1'])
        assert len(picked) == REGIONAL_QUERY_CAP
        assert picked == big[:REGIONAL_QUERY_CAP]

    def test_falls_back_to_cv_queries_when_empty(self):
        cv = ['a', 'b', 'c', 'd', 'e', 'f']
        assert _pick_regional_queries([], cv) == cv[:4]

    def test_fallback_accepts_a_set_deterministically(self):
        # queries can arrive as any iterable; list() must not raise on a set.
        assert len(_pick_regional_queries([], {'x', 'y', 'z', 'w', 'q'})) == 4


class TestForceUtf8Streams:
    def test_serbian_print_does_not_crash_under_cp1252(self, monkeypatch):
        # Simulate the Windows cp1252 stream that crashed the scheduled run.
        buf = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        monkeypatch.setattr(sys, 'stdout', buf)
        force_utf8_streams()
        # Would raise UnicodeEncodeError without the fix; must be silent now.
        print('Inženjer održavanja | Bačka Palanka | Nikšić')
        buf.flush()

    def test_is_a_noop_on_streams_without_reconfigure(self, monkeypatch):
        class Dummy:  # no reconfigure attribute
            def write(self, s):
                return len(s)
        monkeypatch.setattr(sys, 'stdout', Dummy())
        force_utf8_streams()  # must not raise
