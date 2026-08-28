"""Sketch closure pass — makes drawn loops topologically closed.

Every sketch-entity tool appends this so profiles form reliably (the hexagon
drawn as 6 lines must become a CLOSED loop or extrude silently produces no
body — observed live 2026-08-28). Endpoints within EPS cm get an explicit
coincident constraint; the profile count is reported so callers know the
sketch is extrudable.
"""
from __future__ import annotations

_EPS = 1e-6

_CLOSE_SKETCH = """
# ---- closure pass ----
_eps = 1e-6
_pts = []
_segs = []
for _c in sk.sketchCurves:
    if hasattr(_c, 'startSketchPoint') and hasattr(_c, 'endSketchPoint'):
        _segs.append((_c.startSketchPoint, _c.endSketchPoint))
for _s, _e in _segs:
    _pts.append(_s)
    _pts.append(_e)
_cons = sk.geometricConstraints
for i in range(len(_pts)):
    for j in range(i + 1, len(_pts)):
        try:
            _d = _pts[i].geometry.vectorTo(_pts[j].geometry).length
            if _d < _eps:
                _cons.addCoincident(_pts[i], _pts[j])
        except Exception:
            pass
print('PROFILES', sk.profiles.count)
if sk.profiles.count == 0:
    print('WARN_NO_PROFILE')
"""


def _profile_report(count: int) -> str:
    if count == 0:
        return "print('WARN_NO_PROFILE')"
    return f"print('PROFILES {count}')"
