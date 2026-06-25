"""Unit tests for the interactive-confirmation matcher helpers.

These cover the fix for the awkward case where answering a ``(y/n)`` prompt forced
callers to hand-write a regex like ``\\(y/n\\)`` - which, single-escaped, is invalid
JSON over the MCP wire. Now an ``answer`` alone selects a built-in matcher, and any
explicit pattern is made literal-safe. Pure functions, no sockets.
"""

from __future__ import annotations

import re

from device_mcp.connection import (
    _DEFAULT_CONFIRM_RE,
    _resolve_confirm,
    _safe_expect,
)


def test_answer_alone_selects_the_default_matcher():
    # The common case: caller says only what to answer, not how to spot the prompt.
    assert _resolve_confirm(None, "n") == _DEFAULT_CONFIRM_RE
    assert _resolve_confirm(None, "y") == _DEFAULT_CONFIRM_RE


def test_no_answer_and_no_pattern_is_none():
    assert _resolve_confirm(None, None) is None


def test_explicit_regex_is_honored():
    # A real regex (the internal monitor-reboot caller still passes this) is unchanged.
    assert _resolve_confirm(r"\(y/n\)", "y") == r"\(y/n\)"


def test_plain_substring_is_accepted_as_a_pattern():
    # A literal '(y/n)' is a valid regex (a group), so it is used verbatim and search
    # still finds it inside the device's "...(y/n)?".
    pat = _resolve_confirm("(y/n)", "n")
    assert re.search(pat, "Do you want to reboot the Switch(y/n)?")


def test_invalid_regex_falls_back_to_literal_match():
    # An unbalanced paren is not a valid regex; match it literally instead of raising.
    pat = _safe_expect("(y/n")
    assert re.search(pat, "reboot? (y/n")


def test_default_matches_common_confirmation_prompts():
    rx = re.compile(_DEFAULT_CONFIRM_RE)
    for prompt in (
        "Continue? (y/n)",
        "reboot the Switch(y/n)?",
        "Are you sure? [yes/no]:",
        "Proceed with reload? [confirm]",
        "Overwrite file? (yes/no)",
    ):
        assert rx.search(prompt), prompt
