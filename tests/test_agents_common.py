"""Tests for the shared agent helpers: parse_json + format_changed_files.

These need no LLM and no network — pure logic.
"""

from __future__ import annotations

import pytest

from app.agents.common import format_changed_files, parse_json


def test_parse_json_raw():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_fenced():
    text = 'Here is the result:\n```json\n{"risk_score": "low"}\n```\n'
    assert parse_json(text) == {"risk_score": "low"}


def test_parse_json_fenced_no_lang():
    text = '```\n{"x": [1, 2, 3]}\n```'
    assert parse_json(text) == {"x": [1, 2, 3]}


def test_parse_json_embedded_in_prose():
    text = 'The findings are {"findings": [], "risk_score": "trivial"} as shown.'
    assert parse_json(text) == {"findings": [], "risk_score": "trivial"}


def test_parse_json_array():
    assert parse_json("[1, 2, 3]") == [1, 2, 3]


def test_parse_json_raises_on_garbage():
    with pytest.raises(ValueError):
        parse_json("this has no json at all")


def test_format_changed_files_basic():
    files = [{"path": "a.py", "patch": "+print(1)"}]
    out = format_changed_files(files)
    assert "a.py" in out
    assert "+print(1)" in out


def test_format_changed_files_truncates():
    big = "x" * 20000
    files = [{"path": "big.py", "patch": big}]
    out = format_changed_files(files, max_chars=1000)
    assert "truncated" in out
    assert len(out) < 20000
