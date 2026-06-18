"""Semantic classification of declared tokens and DOM elements.

`tokens.py` maps declared CSS custom properties to semantic roles and usage priors;
`components.py` scores harvested DOM elements into component types. All weights and
vocabulary come from the config YAML — nothing is hard-coded here.
"""
