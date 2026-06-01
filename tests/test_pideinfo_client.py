"""Unit tests for response handling in client.pideinfo.

The CTBG webhook returns ``created`` as a list of per-document dicts, while
legacy sources (transparencia_age, redsara_rec…) return an int. Mixing the
two raised TypeError in production (agent.log 2026-05-11), so the parsing
helpers must tolerate both shapes.
"""

from client.pideinfo import _created_count


def test_created_count_with_ctbg_list():
    assert _created_count({"created": [{"filename": "a.pdf"}, {"filename": "b.pdf"}]}) == 2


def test_created_count_with_empty_list():
    assert _created_count({"created": []}) == 0


def test_created_count_with_legacy_int():
    assert _created_count({"created": 3}) == 3


def test_created_count_with_zero():
    assert _created_count({"created": 0}) == 0


def test_created_count_with_none():
    assert _created_count({"created": None}) == 0


def test_created_count_with_missing_key():
    assert _created_count({}) == 0
