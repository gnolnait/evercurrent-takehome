"""Shared pytest configuration."""

from hypothesis import settings

# The Hypothesis deadline is a wall-clock limit, not part of any property. It fails
# tests at random when the full suite runs under load. This profile removes it and
# keeps every other setting of the profile that was active (the built-in "ci" profile
# in CI, else "default"). Per-test settings such as derandomize stay in force.
settings.register_profile("digest", settings(deadline=None))
settings.load_profile("digest")
