"""Tests for op_core.field."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from op_core.field import (
    FieldValue,
    classify_type,
    complete_field_refs,
    is_sensitive,
    normalize_original,
    resolve_chain,
)


class TestClassifyType:
    def test_literal_ip(self):
        assert classify_type('10.0.0.1') == 'literal'

    def test_literal_plain_text(self):
        assert classify_type('deploy') == 'literal'

    def test_reference_op(self):
        assert classify_type('op://Vault/Item/field') == 'reference'

    def test_reference_ops(self):
        assert classify_type('ops://Vault/Item/field') == 'reference'

    def test_reference_self(self):
        assert classify_type('op://././password') == 'reference'

    def test_reference_with_fallback(self):
        assert classify_type('op://././pw||fallback') == 'reference'

    def test_template(self):
        assert classify_type('{{alias}}.example.com') == 'template'

    def test_template_without_reference(self):
        assert classify_type('{{alias}}') == 'template'

    def test_reference_takes_precedence_over_template(self):
        # If both :// and {{ are present, reference wins (checked first)
        assert classify_type('op://././{{alias}}') == 'reference'


class TestIsSensitive:
    def test_password_field_name(self):
        assert is_sensitive('anything', 'password') is True

    def test_passwd_field_name(self):
        assert is_sensitive('anything', 'passwd') is True

    def test_pass_field_name(self):
        assert is_sensitive('anything', 'pass') is True

    def test_secret_field_name(self):
        assert is_sensitive('anything', 'secret') is True

    def test_token_field_name(self):
        assert is_sensitive('anything', 'token') is True

    def test_otp_field_name(self):
        assert is_sensitive('anything', 'otp') is True

    def test_case_insensitive_field_name(self):
        assert is_sensitive('anything', 'Password') is True
        assert is_sensitive('anything', 'TOKEN') is True

    def test_substring_match(self):
        assert is_sensitive('anything', 'sudo_password') is True
        assert is_sensitive('anything', 'api_token') is True
        assert is_sensitive('anything', 'my_secret_field') is True

    def test_ops_prefix(self):
        assert is_sensitive('ops://Vault/Item/field', 'hostname') is True

    def test_ops_in_chain(self):
        assert is_sensitive('op://././pw||ops://Vault/Backup/pw', 'api_key') is True

    def test_non_sensitive_field(self):
        assert is_sensitive('10.0.0.1', 'hostname') is False

    def test_op_reference_non_sensitive_name(self):
        assert is_sensitive('op://Vault/Item/hostname', 'hostname') is False

    def test_plain_literal_non_sensitive(self):
        assert is_sensitive('deploy', 'user') is False


class TestCompleteFieldRefs:
    def test_complete_ref_unchanged(self):
        assert complete_field_refs('op://Vault/Item/password', 'password') == 'op://Vault/Item/password'

    def test_literal_unchanged(self):
        assert complete_field_refs('hunter2', 'password') == 'hunter2'

    def test_password_auto_completed(self):
        assert complete_field_refs('op://Vault/Item', 'password') == 'op://Vault/Item/password'

    def test_password_auto_completed_ops(self):
        assert complete_field_refs('ops://Vault/Item', 'password') == 'ops://Vault/Item/password'

    def test_password_auto_completed_same_vault(self):
        assert complete_field_refs('op://./Item', 'password') == 'op://./Item/password'

    def test_password_auto_completed_in_chain(self):
        assert complete_field_refs('op://Vault/Item||fallback', 'password') == 'op://Vault/Item/password||fallback'

    def test_incomplete_ref_unknown_field_raises(self):
        with pytest.raises(ValueError, match='incomplete reference'):
            complete_field_refs('op://Vault/Item', 'hostname')

    def test_incomplete_ref_in_chain_unknown_field_raises(self):
        with pytest.raises(ValueError, match='incomplete reference'):
            complete_field_refs('op://Vault/Item||10.0.0.1', 'hostname')

    def test_non_reference_segments_passthrough(self):
        assert complete_field_refs('10.0.0.1', 'hostname') == '10.0.0.1'


class TestNormalizeOriginal:
    def test_self_ref_expanded(self):
        assert normalize_original('op://././password', 'v1', 'i1') == 'op://v1/i1/password'

    def test_self_ref_with_section(self):
        assert normalize_original('op://././SSH Config/password', 'v1', 'i1') == 'op://v1/i1/SSH Config/password'

    def test_ops_self_ref_expanded(self):
        assert normalize_original('ops://././password', 'v1', 'i1') == 'ops://v1/i1/password'

    def test_same_vault_cross_item_expanded(self):
        assert normalize_original('op://./OtherItem/field', 'v1', 'i1') == 'op://v1/OtherItem/field'

    def test_full_ref_unchanged(self):
        assert normalize_original('op://Vault/Item/field', 'v1', 'i1') == 'op://Vault/Item/field'

    def test_literal_unchanged(self):
        assert normalize_original('10.0.0.1', 'v1', 'i1') == '10.0.0.1'

    def test_chain_with_self_ref(self):
        assert normalize_original('op://././pw||op://Vault/Backup/pw', 'v1', 'i1') == 'op://v1/i1/pw||op://Vault/Backup/pw'

    def test_chain_with_literal_fallback(self):
        assert normalize_original('op://././hostname||10.0.0.1', 'v1', 'i1') == 'op://v1/i1/hostname||10.0.0.1'

    def test_item_level_ref_unchanged(self):
        """Item-level refs (no field) are not modified — no auto-append."""
        assert normalize_original('op://Vault/Item', 'v1', 'i1') == 'op://Vault/Item'

    def test_ops_self_ref_in_chain(self):
        assert normalize_original('ops://././secret||ops://././backup_secret', 'v1', 'i1') == 'ops://v1/i1/secret||ops://v1/i1/backup_secret'


class TestResolveChain:
    def test_single_reference_success(self):
        reader = MagicMock(return_value='resolved-value')
        assert resolve_chain('op://Vault/Item/field', reader) == 'resolved-value'

    def test_single_reference_failure_returns_none(self):
        reader = MagicMock(return_value=None)
        assert resolve_chain('op://Vault/Item/field', reader) is None

    def test_literal_value(self):
        reader = MagicMock()
        assert resolve_chain('10.0.0.1', reader) == '10.0.0.1'
        reader.assert_not_called()

    def test_fallback_to_literal(self):
        reader = MagicMock(return_value=None)
        assert resolve_chain('op://Vault/Item/field||10.0.0.1', reader) == '10.0.0.1'

    def test_fallback_chain_first_wins(self):
        reader = MagicMock(return_value='first-value')
        assert resolve_chain('op://Vault/Item/field||fallback', reader) == 'first-value'
        reader.assert_called_once()

    def test_fallback_chain_second_ref(self):
        reader = MagicMock(side_effect=[None, 'backup-value'])
        result = resolve_chain('op://Vault/Item/field||op://Vault/Backup/field', reader)
        assert result == 'backup-value'
        assert reader.call_count == 2

    def test_all_segments_fail(self):
        reader = MagicMock(return_value=None)
        assert resolve_chain('op://V/I/f1||op://V/I/f2', reader) is None

    def test_empty_segments_skipped(self):
        reader = MagicMock(return_value=None)
        assert resolve_chain('op://V/I/f||  ||fallback', reader) == 'fallback'

    def test_self_ref_expanded(self):
        reader = MagicMock(return_value='pw')
        result = resolve_chain('op://././password', reader, vault_id='v1', item_id='i1')
        reader.assert_called_once_with('op://v1/i1/password')
        assert result == 'pw'

    def test_same_vault_cross_item_expanded(self):
        reader = MagicMock(return_value='value')
        result = resolve_chain('op://./OtherItem/field', reader, vault_id='v1', item_id='i1')
        reader.assert_called_once_with('op://v1/OtherItem/field')
        assert result == 'value'

    def test_ops_normalized(self):
        reader = MagicMock(return_value='secret')
        result = resolve_chain('ops://Vault/Item/field', reader)
        reader.assert_called_once_with('op://Vault/Item/field')
        assert result == 'secret'

    def test_item_level_ref_passed_through(self):
        """Item-level refs (no field_path) are passed as-is to the reader."""
        reader = MagicMock(return_value='value')
        resolve_chain('op://Vault/Item', reader)
        reader.assert_called_once_with('op://Vault/Item')


class TestFieldValue:
    def test_from_raw_literal(self):
        fv = FieldValue.from_raw('10.0.0.1', 'hostname')
        assert fv.original == '10.0.0.1'
        assert fv.resolved is None
        assert fv.sensitive is False
        assert fv.field_type == 'literal'

    def test_from_raw_reference(self):
        fv = FieldValue.from_raw('op://Vault/Item/hostname', 'hostname')
        assert fv.field_type == 'reference'
        assert fv.sensitive is False

    def test_from_raw_sensitive_by_name(self):
        fv = FieldValue.from_raw('op://Vault/Item/password', 'password')
        assert fv.sensitive is True

    def test_from_raw_sensitive_by_ops_prefix(self):
        fv = FieldValue.from_raw('ops://Vault/Item/field', 'hostname')
        assert fv.sensitive is True

    def test_from_raw_otp_sensitive_by_name(self):
        fv = FieldValue.from_raw('op://Vault/Item/one-time password', 'otp')
        assert fv.sensitive is True

    def test_from_raw_template(self):
        fv = FieldValue.from_raw('{{alias}}.example.com', 'hostname')
        assert fv.field_type == 'template'
        assert fv.sensitive is False

    def test_with_resolved_sets_value(self):
        fv = FieldValue.from_raw('op://V/I/hostname', 'hostname')
        assert fv.with_resolved('10.0.0.1').resolved == '10.0.0.1'

    def test_with_resolved_preserves_other_fields(self):
        fv = FieldValue.from_raw('op://V/I/hostname', 'hostname')
        updated = fv.with_resolved('10.0.0.1')
        assert updated.original == fv.original
        assert updated.sensitive == fv.sensitive
        assert updated.field_type == fv.field_type

    def test_with_resolved_none_clears(self):
        fv = FieldValue(original='op://V/I/f', resolved='10.0.0.1', sensitive=False, field_type='reference')
        assert fv.with_resolved(None).resolved is None

    def test_frozen_dataclass(self):
        fv = FieldValue.from_raw('10.0.0.1', 'hostname')
        with pytest.raises(FrozenInstanceError):
            fv.original = 'changed'  # type: ignore[misc]


class TestFieldValueSerialization:
    def test_to_dict_literal(self):
        fv = FieldValue.from_raw('10.0.0.1', 'hostname')
        assert fv.to_dict() == {
            'original': '10.0.0.1',
            'resolved': None,
            'sensitive': False,
        }

    def test_to_dict_with_resolved(self):
        fv = FieldValue.from_raw('op://V/I/hostname', 'hostname').with_resolved('10.0.0.1')
        assert fv.to_dict() == {
            'original': 'op://V/I/hostname',
            'resolved': '10.0.0.1',
            'sensitive': False,
        }

    def test_to_dict_sensitive(self):
        fv = FieldValue.from_raw('op://V/I/password', 'password')
        assert fv.to_dict()['sensitive'] is True

    def test_to_dict_omits_field_type(self):
        fv = FieldValue.from_raw('{{alias}}.example.com', 'hostname')
        assert 'field_type' not in fv.to_dict()

    def test_from_dict_literal(self):
        fv = FieldValue.from_dict({'original': '10.0.0.1', 'resolved': None, 'sensitive': False})
        assert fv.original == '10.0.0.1'
        assert fv.field_type == 'literal'  # derived via classify_type
        assert fv.sensitive is False

    def test_from_dict_reference_derives_type(self):
        fv = FieldValue.from_dict({
            'original': 'op://V/I/f',
            'resolved': 'cached',
            'sensitive': True,
        })
        assert fv.field_type == 'reference'
        assert fv.resolved == 'cached'

    def test_from_dict_template_derives_type(self):
        fv = FieldValue.from_dict({'original': '{{alias}}.host', 'resolved': None, 'sensitive': False})
        assert fv.field_type == 'template'

    def test_from_dict_missing_sensitive_defaults_false(self):
        fv = FieldValue.from_dict({'original': '10.0.0.1', 'resolved': None})
        assert fv.sensitive is False

    def test_round_trip(self):
        original = FieldValue.from_raw('op://V/I/password', 'password').with_resolved('hunter2')
        restored = FieldValue.from_dict(original.to_dict())
        assert restored == original

    def test_from_dict_missing_original_raises_value_error(self):
        with pytest.raises(ValueError, match='original'):
            FieldValue.from_dict({'resolved': None, 'sensitive': False})

    def test_from_dict_rejects_non_string_original(self):
        with pytest.raises(ValueError, match='original'):
            FieldValue.from_dict({'original': 42, 'resolved': None, 'sensitive': False})

    def test_from_dict_rejects_non_string_resolved(self):
        with pytest.raises(ValueError, match='resolved'):
            FieldValue.from_dict({'original': 'x', 'resolved': 42, 'sensitive': False})

    def test_from_dict_rejects_non_bool_sensitive(self):
        with pytest.raises(ValueError, match='sensitive'):
            FieldValue.from_dict({'original': 'x', 'resolved': None, 'sensitive': 'yes'})
