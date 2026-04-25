"""Flat-export contract for op_core's package root.

These tests guard the recommended import shape (``from op_core import ...``)
against accidental drift — re-export blocks tend to silently disconnect from
their submodule origin if someone refactors a constant or function and forgets
to update the package root.
"""

from __future__ import annotations

import op_core


class TestAllResolves:
    def test_every_name_in_all_resolves(self):
        # Catches `__all__` entries that name a symbol the package root does
        # not actually expose — e.g. a typo or a forgotten re-export edit.
        missing = [name for name in op_core.__all__ if not hasattr(op_core, name)]
        assert missing == []


class TestFlatExportIdentity:
    def test_field_helpers_are_submodule_originals(self):
        # Identity check: the flat-imported object is the same object as the
        # one defined in op_core.field. A re-export that accidentally rebinds
        # (e.g. by re-defining the constant in __init__.py) would fail here.
        from op_core import (
            TEMPLATE_CLOSE,
            TEMPLATE_OPEN,
            classify_type,
            complete_field_refs,
            is_sensitive,
            normalize_original,
        )
        from op_core import field as field_module

        assert TEMPLATE_OPEN is field_module.TEMPLATE_OPEN
        assert TEMPLATE_CLOSE is field_module.TEMPLATE_CLOSE
        assert complete_field_refs is field_module.complete_field_refs
        assert classify_type is field_module.classify_type
        assert is_sensitive is field_module.is_sensitive
        assert normalize_original is field_module.normalize_original

    def test_template_delimiter_values(self):
        # Smoke test pinning the actual literal values — protects downstream
        # consumers that recognize op-core's template markers (e.g. for
        # validation that a literal isn't accidentally a template).
        from op_core import TEMPLATE_CLOSE, TEMPLATE_OPEN

        assert TEMPLATE_OPEN == "{{"
        assert TEMPLATE_CLOSE == "}}"
