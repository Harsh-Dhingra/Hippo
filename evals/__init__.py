"""The eval harness.

STACK.md is written so that every graduation trigger is a measurement rather
than a preference — "provably insufficient on the eval harness, not on vibes".
This is that harness, and it is a product artifact rather than a test helper:
run it to get numbers, and read them before changing a stack decision.

    python -m evals.report                 # against the demo database
    python -m evals.report postgresql://…  # against another one

The pytest gates in tests/test_evals.py call into the same code, so the numbers
CI enforces and the numbers a person reads are the same numbers.
"""
