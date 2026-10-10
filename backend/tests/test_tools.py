"""Unit tests for tools.py"""

from tools import MAX_FIELD_LENGTH, _truncate


def test_truncate_keeps_short_text():
    # Arrange
    text = "Hello"
    # Act
    result = _truncate(text)
    # Assert
    assert result == "Hello"


def test_truncate_cuts_long_text_and_adds_ellipsis():
    long_text = "a" * (MAX_FIELD_LENGTH + 100)

    result = _truncate(long_text)

    assert result == "a" * MAX_FIELD_LENGTH + "…"


def test_truncate_strips_spaces_and_handles_none():
    assert _truncate("  hi  ") == "hi"
    assert _truncate(None) == ""
