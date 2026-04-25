"""Tests for op_core.opref."""

from __future__ import annotations

import pytest

from op_core.opref import OpRef, _split_uri_path


class TestSplitUriPath:
    def test_simple(self):
        assert _split_uri_path('Vault/Item/field') == ['Vault', 'Item', 'field']

    def test_quoted_item(self):
        assert _split_uri_path('Vault/"Item / Name"/field') == ['Vault', 'Item / Name', 'field']

    def test_quoted_no_field(self):
        assert _split_uri_path('Vault/"Item / Name"') == ['Vault', 'Item / Name']

    def test_url_encoded(self):
        assert _split_uri_path('Vault/Item %2F Name/field') == ['Vault', 'Item / Name', 'field']

    def test_url_encoded_no_field(self):
        assert _split_uri_path('Vault/Item %2F Name') == ['Vault', 'Item / Name']

    def test_multiple_encoded_slashes(self):
        assert _split_uri_path('V/A %2F B %2F C/f') == ['V', 'A / B / C', 'f']

    def test_self_ref(self):
        assert _split_uri_path('././field') == ['.', '.', 'field']

    def test_self_ref_with_section(self):
        assert _split_uri_path('././Section/field') == ['.', '.', 'Section', 'field']

    def test_same_vault_item(self):
        assert _split_uri_path('./Item') == ['.', 'Item']

    def test_same_vault_item_field(self):
        assert _split_uri_path('./Item/field') == ['.', 'Item', 'field']

    def test_no_slashes(self):
        assert _split_uri_path('JustAValue') == ['JustAValue']

    def test_trailing_slash_produces_no_extra(self):
        # Trailing slash: current is empty, not appended
        assert _split_uri_path('V/I/') == ['V', 'I']

    def test_quoted_vault(self):
        assert _split_uri_path('"My Vault"/Item/field') == ['My Vault', 'Item', 'field']

    def test_spaces_preserved(self):
        assert _split_uri_path('My Vault/My Item') == ['My Vault', 'My Item']


class TestOpRefParse:
    def test_full_reference(self):
        ref = OpRef.parse('op://Vault/Item/field')
        assert ref.vault == 'Vault'
        assert ref.item == 'Item'
        assert ref.field_path == 'field'
        assert ref.sensitive is False

    def test_item_level_reference(self):
        ref = OpRef.parse('op://Vault/Item')
        assert ref.vault == 'Vault'
        assert ref.item == 'Item'
        assert ref.field_path is None
        assert ref.is_complete is False

    def test_with_section(self):
        ref = OpRef.parse('op://Vault/Item/Section/field')
        assert ref.vault == 'Vault'
        assert ref.item == 'Item'
        assert ref.field_path == 'Section/field'

    def test_sensitive_ops(self):
        ref = OpRef.parse('ops://Vault/Item/field')
        assert ref.sensitive is True
        assert ref.vault == 'Vault'
        assert ref.item == 'Item'
        assert ref.field_path == 'field'

    def test_self_ref(self):
        ref = OpRef.parse('op://././password')
        assert ref.is_self_ref is True
        assert ref.vault == '.'
        assert ref.item == '.'
        assert ref.field_path == 'password'

    def test_self_ref_with_section(self):
        ref = OpRef.parse('op://././SSH Config/password')
        assert ref.is_self_ref is True
        assert ref.field_path == 'SSH Config/password'

    def test_sensitive_self_ref(self):
        ref = OpRef.parse('ops://././password')
        assert ref.is_self_ref is True
        assert ref.sensitive is True

    def test_vault_relative_item_ref(self):
        ref = OpRef.parse('op://./Item')
        assert ref.is_vault_relative is True
        assert ref.is_item_relative is False
        assert ref.is_self_ref is False
        assert ref.vault == '.'
        assert ref.item == 'Item'
        assert ref.field_path is None

    def test_vault_relative_field_ref(self):
        ref = OpRef.parse('op://./Item/field')
        assert ref.is_vault_relative is True
        assert ref.is_item_relative is False
        assert ref.is_self_ref is False
        assert ref.vault == '.'
        assert ref.item == 'Item'
        assert ref.field_path == 'field'

    def test_vault_relative_field_ref_with_section(self):
        ref = OpRef.parse('op://./Item/Section/field')
        assert ref.is_vault_relative is True
        assert ref.item == 'Item'
        assert ref.field_path == 'Section/field'

    def test_quoted_item_name(self):
        ref = OpRef.parse('op://Work/"Laptop / SN-001234 / john.doe"')
        assert ref.vault == 'Work'
        assert ref.item == 'Laptop / SN-001234 / john.doe'
        assert ref.field_path is None

    def test_quoted_item_with_field(self):
        ref = OpRef.parse('op://Work/"Laptop / SN-001234"/password')
        assert ref.vault == 'Work'
        assert ref.item == 'Laptop / SN-001234'
        assert ref.field_path == 'password'

    def test_url_encoded_item(self):
        ref = OpRef.parse('op://Work/Laptop %2F SN-001234 %2F john.doe')
        assert ref.vault == 'Work'
        assert ref.item == 'Laptop / SN-001234 / john.doe'
        assert ref.field_path is None

    def test_url_encoded_item_with_field(self):
        ref = OpRef.parse('op://Work/Laptop %2F SN-001234/password')
        assert ref.vault == 'Work'
        assert ref.item == 'Laptop / SN-001234'
        assert ref.field_path == 'password'

    def test_invalid_no_slash(self):
        with pytest.raises(ValueError):
            OpRef.parse('op://VaultItem')

    def test_invalid_empty_vault(self):
        with pytest.raises(ValueError):
            OpRef.parse('op:///Item')

    def test_invalid_empty_item(self):
        with pytest.raises(ValueError):
            OpRef.parse('op://Vault/')

    def test_invalid_empty(self):
        with pytest.raises(ValueError):
            OpRef.parse('op://')

    def test_spaces_in_names(self):
        ref = OpRef.parse('op://My Vault/Key Name')
        assert ref.vault == 'My Vault'
        assert ref.item == 'Key Name'

    def test_dot_in_item_requires_dot_in_vault(self):
        """op://Vault/./field is rejected — . in item requires . in vault."""
        with pytest.raises(ValueError, match=r'"." in item position'):
            OpRef.parse('op://Vault/./field')

    def test_dot_in_item_without_field_rejected(self):
        """op://Vault/. is rejected — . in item requires . in vault."""
        with pytest.raises(ValueError, match=r'"." in item position'):
            OpRef.parse('op://Vault/.')


class TestOpRefProperties:
    def test_is_self_ref_true(self):
        assert OpRef.parse('op://././pw').is_self_ref is True

    def test_is_self_ref_false_for_full_ref(self):
        assert OpRef.parse('op://V/I/f').is_self_ref is False

    def test_is_self_ref_false_for_vault_relative(self):
        """op://./Item/field has a relative vault but an explicit item."""
        assert OpRef.parse('op://./Item/field').is_self_ref is False

    def test_is_vault_relative_true_for_self_ref(self):
        assert OpRef.parse('op://././pw').is_vault_relative is True

    def test_is_vault_relative_true_for_cross_item(self):
        assert OpRef.parse('op://./Item/pw').is_vault_relative is True

    def test_is_vault_relative_false(self):
        assert OpRef.parse('op://V/I/f').is_vault_relative is False

    def test_is_item_relative_true_for_self_ref(self):
        assert OpRef.parse('op://././pw').is_item_relative is True

    def test_is_item_relative_false_for_vault_relative(self):
        """op://./Item/field has a relative vault but an explicit item."""
        assert OpRef.parse('op://./Item/field').is_item_relative is False

    def test_is_item_relative_false_for_full_ref(self):
        assert OpRef.parse('op://V/I/f').is_item_relative is False

    def test_is_complete_with_field(self):
        assert OpRef.parse('op://V/I/f').is_complete is True

    def test_is_complete_without_field(self):
        assert OpRef.parse('op://V/I').is_complete is False


class TestOpRefWithField:
    def test_adds_field(self):
        ref = OpRef.parse('op://V/I')
        completed = ref.with_field('password')
        assert completed.field_path == 'password'
        assert completed.vault == 'V'
        assert completed.item == 'I'
        assert completed.sensitive is ref.sensitive

    def test_replaces_field(self):
        ref = OpRef.parse('op://V/I/old')
        replaced = ref.with_field('new')
        assert replaced.field_path == 'new'


class TestOpRefAsAbsolute:
    def test_self_ref_expanded(self):
        ref = OpRef.parse('op://././password')
        absolute = ref.as_absolute('vault-abc', 'item-123')
        assert absolute.vault == 'vault-abc'
        assert absolute.item == 'item-123'
        assert absolute.field_path == 'password'
        assert absolute.is_self_ref is False

    def test_self_ref_with_section(self):
        ref = OpRef.parse('op://././SSH Config/password')
        absolute = ref.as_absolute('v1', 'i1')
        assert absolute.field_path == 'SSH Config/password'

    def test_vault_relative_item_ref_expanded(self):
        ref = OpRef.parse('op://./Item')
        absolute = ref.as_absolute('v1')
        assert absolute.vault == 'v1'
        assert absolute.item == 'Item'
        assert absolute.field_path is None

    def test_vault_relative_cross_item_expanded(self):
        ref = OpRef.parse('op://./OtherItem/field')
        absolute = ref.as_absolute('v1', 'i1')
        assert absolute.vault == 'v1'
        assert absolute.item == 'OtherItem'  # item not replaced (not relative)
        assert absolute.field_path == 'field'

    def test_vault_relative_ignores_item_id_when_item_is_explicit(self):
        ref = OpRef.parse('op://./Item/field')
        absolute = ref.as_absolute('v1')
        assert absolute.vault == 'v1'
        assert absolute.item == 'Item'

    def test_absolute_ref_returns_self(self):
        ref = OpRef.parse('op://Vault/Item/field')
        assert ref.as_absolute('v1', 'i1') is ref

    def test_absolute_item_ref_returns_self(self):
        ref = OpRef.parse('op://Vault/Item')
        assert ref.as_absolute() is ref

    def test_sensitive_self_ref(self):
        ref = OpRef.parse('ops://././secret')
        absolute = ref.as_absolute('v1', 'i1')
        assert absolute.sensitive is True
        assert absolute.vault == 'v1'

    def test_expand_via_parent_ref(self):
        parent = OpRef.parse('op://Vault/Item/field')
        child = OpRef.parse('op://././password')
        absolute = child.as_absolute(parent.vault, parent.item)
        assert absolute.vault == 'Vault'
        assert absolute.item == 'Item'
        assert absolute.field_path == 'password'

    def test_raises_when_vault_relative_and_no_vault_id(self):
        ref = OpRef.parse('op://./Item/field')
        with pytest.raises(ValueError, match='vault_id required'):
            ref.as_absolute()

    def test_raises_when_self_ref_and_no_item_id(self):
        ref = OpRef.parse('op://././field')
        with pytest.raises(ValueError, match='item_id required'):
            ref.as_absolute('v1')

    def test_raises_when_self_ref_and_no_args(self):
        ref = OpRef.parse('op://././field')
        with pytest.raises(ValueError, match='vault_id required'):
            ref.as_absolute()

    def test_chained_for_storage(self):
        result = OpRef.parse('op://././password').as_absolute('v1', 'i1').for_storage()
        assert result == 'op://v1/i1/password'

    def test_chained_for_op_preserves_sensitivity_marker_drop(self):
        result = OpRef.parse('ops://././secret').as_absolute('v1', 'i1').for_op()
        assert result == 'op://v1/i1/secret'


class TestOpRefForOp:
    def test_simple(self):
        ref = OpRef.parse('op://Vault/Item/field')
        assert ref.for_op() == 'op://Vault/Item/field'

    def test_always_op_prefix(self):
        ref = OpRef.parse('ops://Vault/Item/field')
        assert ref.for_op() == 'op://Vault/Item/field'

    def test_encodes_slashes_in_item(self):
        ref = OpRef.parse('op://Work/"Laptop / SN-001234"/password')
        assert ref.for_op() == 'op://Work/Laptop %2F SN-001234/password'

    def test_encodes_slashes_in_vault(self):
        ref = OpRef(vault='My / Vault', item='Item', field_path='field', sensitive=False)
        assert ref.for_op() == 'op://My %2F Vault/Item/field'

    def test_roundtrip_url_encoded(self):
        ref = OpRef.parse('op://V/A %2F B/field')
        assert ref.for_op() == 'op://V/A %2F B/field'

    def test_roundtrip_quoted(self):
        ref = OpRef.parse('op://V/"A / B"/field')
        assert ref.for_op() == 'op://V/A %2F B/field'

    def test_self_ref(self):
        ref = OpRef.parse('op://././password')
        assert ref.for_op() == 'op://././password'

    def test_self_ref_with_section(self):
        ref = OpRef.parse('op://././Section/field')
        assert ref.for_op() == 'op://././Section/field'

    def test_same_vault_item_ref(self):
        ref = OpRef.parse('op://./Item')
        assert ref.for_op() == 'op://./Item'

    def test_same_vault_field_ref(self):
        ref = OpRef.parse('op://./Item/field')
        assert ref.for_op() == 'op://./Item/field'

    def test_item_level(self):
        ref = OpRef.parse('op://V/I')
        assert ref.for_op() == 'op://V/I'


class TestOpRefForStorage:
    def test_preserves_op_prefix(self):
        ref = OpRef.parse('op://V/I/f')
        assert ref.for_storage() == 'op://V/I/f'

    def test_preserves_ops_prefix(self):
        ref = OpRef.parse('ops://V/I/f')
        assert ref.for_storage() == 'ops://V/I/f'

    def test_encodes_slashes(self):
        ref = OpRef.parse('ops://Work/"Laptop / SN-001234"/password')
        assert ref.for_storage() == 'ops://Work/Laptop %2F SN-001234/password'

    def test_self_ref(self):
        ref = OpRef.parse('ops://././password')
        assert ref.for_storage() == 'ops://././password'

    def test_same_vault_ref(self):
        ref = OpRef.parse('op://./Item/field')
        assert ref.for_storage() == 'op://./Item/field'
