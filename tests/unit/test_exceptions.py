"""Tests for op_core.exceptions."""

from __future__ import annotations

import pytest

from op_core.exceptions import (
    OpAuthError,
    OpError,
    OpNotFoundError,
    OpTimeoutError,
)

SUBCLASSES = [OpAuthError, OpNotFoundError, OpTimeoutError]


def test_op_error_is_an_exception():
    assert issubclass(OpError, Exception)


@pytest.mark.parametrize('cls', SUBCLASSES)
def test_subclasses_inherit_from_op_error(cls):
    assert issubclass(cls, OpError)


@pytest.mark.parametrize('cls', SUBCLASSES)
def test_subclasses_are_caught_by_op_error(cls):
    with pytest.raises(OpError):
        raise cls('boom')


@pytest.mark.parametrize('cls', SUBCLASSES)
def test_subclasses_are_distinct_types(cls):
    """Each subclass is its own type so callers can target them specifically."""
    others = [other for other in SUBCLASSES if other is not cls]
    for other in others:
        assert cls is not other
        assert not issubclass(cls, other)
