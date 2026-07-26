"""Deployment artifacts, and the demo world that ships with them.

A package rather than a bare directory so `python -m deploy.demo.seed` resolves
the same way everywhere — as a namespace package it worked at runtime and gave
mypy two names for one file.
"""
