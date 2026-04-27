"""Integration tests for vault enumeration.

Marked ``integration`` — requires ``OP_SERVICE_ACCOUNT_TOKEN`` and network
access. Deselect with ``-m "not integration"``.

Each test exercises a real round-trip against 1Password to verify both
the wire format and the per-vault scoping use case that motivated the
``list_vaults`` API.
"""

from __future__ import annotations

import os

import pytest

from op_core.auth import ServiceAccountAuth
from op_core.backends.cli import CLIBackend
from op_core.client import OnePassword
from op_core.items import VaultSummary

pytestmark = pytest.mark.integration


def _has_service_account_token() -> bool:
    return bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"))


@pytest.fixture
def client() -> OnePassword:
    if not _has_service_account_token():
        pytest.skip("OP_SERVICE_ACCOUNT_TOKEN not set")
    return OnePassword(backend=CLIBackend(auth=ServiceAccountAuth(token=os.environ["OP_SERVICE_ACCOUNT_TOKEN"])))


def test_list_vaults_returns_real_vault_summaries(client: OnePassword) -> None:
    vaults = client.list_vaults()
    assert all(isinstance(v, VaultSummary) for v in vaults)
    # Service accounts always have at least one vault granted to them; if
    # this fails the test environment is mis-provisioned, not the code.
    assert len(vaults) >= 1
    assert all(v.id for v in vaults)


def test_round_trip_list_vaults_then_list_items(client: OnePassword) -> None:
    """The motivating use case: per-vault scoping.

    Enumerate vaults, then list items scoped to the first one. The scoped
    call must succeed and return only items belonging to that vault — the
    unscoped vs. scoped performance gap that prompted this API is *not*
    asserted (timing assertions are flaky); we only verify the contract.
    """
    vaults = client.list_vaults()
    assert vaults, "service account must have at least one vault"
    first = vaults[0]
    items = client.list_items(vault=first.id)
    # All returned items must belong to the requested vault.
    for item in items:
        assert item.vault_id == first.id
