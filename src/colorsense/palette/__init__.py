"""Palette synthesis: fuse measured evidence and declared intent into the output views.

`inventory.py` establishes canonical color identities and extracts third-party-dominated
clusters; `fusion.py` accumulates per-``(color, role)`` evidence records; `detect.py`
runs detection-plus-ranking to produce the role-keyed view, color-keyed index, and
divergence report.
"""
