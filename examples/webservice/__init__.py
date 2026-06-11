"""A FastAPI palette service: reference implementation of the SECURITY.md controls.

A flat mini-package: ``main`` assembles the app (start here — its docstring maps every
control to its SECURITY.md section), ``settings`` reads the environment knobs, ``policy``
holds the shared :class:`~colorsense.PolitenessPolicy`, ``url_guard`` is the hand-rolled
SSRF pre-call gate (pure stdlib, unit-testable without FastAPI or a browser), ``schemas``
the API models and response trimming, and ``routes`` the ``/analyze`` endpoint wiring it
all around :func:`colorsense.analyze`.
"""
