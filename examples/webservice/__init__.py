"""A FastAPI palette service: reference implementation of the SECURITY.md controls.

``url_guard`` holds the SSRF policy (pure stdlib, unit-testable without FastAPI or a
browser); ``app`` wires it into a small HTTP service around :func:`colorsense.analyze`.
"""
