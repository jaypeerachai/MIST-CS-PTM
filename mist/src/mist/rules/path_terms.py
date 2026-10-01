"""Path-token terms shared by seed extraction and later FP handling."""

from __future__ import annotations


EXAMPLE_DEMO_TOKENS = frozenset({
    "example",
    "examples",
    "demo",
    "demos",
    "tutorial",
    "tutorials",
    "sample",
    "samples",
})

# Filename-level `sample` is often a normal eval/config name, so do not use it
# as an early-exclusion path signal unless it appears as a directory component.
FILENAME_EXAMPLE_DEMO_TOKENS = EXAMPLE_DEMO_TOKENS - frozenset({"sample"})

THIRD_PARTY_TOKENS = frozenset({
    ".venv",
    "venv",
    "env",
    "envs",
    "environment",
    "environments",
    "site-packages",
    "dist-packages",
    "vendor",
    "vendored",
    "third_party",
    "third-party",
})
