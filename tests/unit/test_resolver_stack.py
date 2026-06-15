"""Tests for :class:`op_core.backends.stack.ResolverStack` (design section 3).

Each test cites the design rule it pins so a future port cannot quietly drift.
The stack is driven with lightweight doubles: ``FakeLayer`` (a writable layer
that records its ``store`` calls into a shared log), ``FakeReadOnlyLayer`` (a
``lookup``-only layer), and ``RecordingSource`` (a ``Backend`` that counts calls
and can be told to fail or to run a hook mid-read).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest

from op_core.backends import stack
from op_core.backends.caching import _NOT_FOUND, CacheEntry
from op_core.backends.stack import MemoryLayer, ResolverStack
from op_core.exceptions import OpAuthError, OpNotFoundError, OpOfflineError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

REF = "op://Vault/Item/field"


class FakeLayer:
    """A writable layer double. Records every ``store`` into a shared log."""

    def __init__(
        self, label: str, log: list[tuple[str, str, object]], *, seed: dict[str, object] | None = None
    ) -> None:
        self.label = label
        self.log = log
        self.entries: dict[str, object] = dict(seed or {})

    def lookup(self, reference: str) -> CacheEntry | None:
        if reference in self.entries:
            return CacheEntry(key=reference, value=self.entries[reference], cached_at=0.0, metadata={})
        return None

    def store(self, reference: str, value: object) -> None:
        self.entries[reference] = value
        self.log.append((self.label, reference, value))

    def clear(self) -> None:
        self.entries.clear()

    def clear_misses(self) -> None:
        self.entries = {k: v for k, v in self.entries.items() if v is not _NOT_FOUND}


class FakeReadOnlyLayer:
    """A read-only layer double: ``lookup`` only, never written to."""

    def __init__(self, label: str, *, seed: dict[str, object] | None = None) -> None:
        self.label = label
        self.entries: dict[str, object] = dict(seed or {})

    def lookup(self, reference: str) -> CacheEntry | None:
        if reference in self.entries:
            return CacheEntry(key=reference, value=self.entries[reference], cached_at=0.0, metadata={})
        return None


_UNSET = object()


class RecordingSource:
    """A ``Backend`` double: counts calls, can raise a chosen error, can run a hook mid-read."""

    def __init__(
        self,
        *,
        refs: dict[str, str] | None = None,
        error: Exception | None = None,
        on_read: Callable[[], object] | None = None,
    ) -> None:
        self.refs = dict(refs or {})
        self.error = error
        self.on_read = on_read
        self._lock = threading.Lock()
        self.read_count = 0
        self.get_item_count = 0
        self.list_items_count = 0
        self.list_vaults_count = 0
        # Records only kwargs *explicitly passed* by the caller (absent = not forwarded).
        self.last_read_kwargs: dict[str, object] | None = None

    def read(
        self,
        reference: str,
        *,
        default_value: object = _UNSET,
        online: object = _UNSET,
    ) -> str:
        with self._lock:
            self.read_count += 1
            self.last_read_kwargs = {
                k: v
                for k, v in {"default_value": default_value, "online": online}.items()
                if v is not _UNSET
            }
        # Resolve to real defaults for the rest of the method body.
        _default_value = None if default_value is _UNSET else default_value
        _online = True if online is _UNSET else online
        if self.on_read is not None:
            self.on_read()
        if self.error is not None:
            raise self.error
        if reference in self.refs:
            return self.refs[reference]
        raise OpNotFoundError(reference)

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        self.get_item_count += 1
        raise OpNotFoundError("no item")

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        self.list_items_count += 1
        return []

    def list_vaults(self) -> list[VaultSummary]:
        self.list_vaults_count += 1
        return []


# ---------------------------------------------------------------------------
# 3.2 rule 1 — walk in order, first live hit wins
# ---------------------------------------------------------------------------


class TestFirstHitWins:
    def test_first_layer_with_a_live_entry_wins(self) -> None:
        top = FakeLayer("top", [], seed={REF: "from-top"})
        deep = FakeLayer("deep", [], seed={REF: "from-deep"})
        src = RecordingSource(refs={REF: "from-source"})
        assert ResolverStack([top, deep], src).read(REF) == "from-top"
        assert src.read_count == 0

    def test_deeper_layer_wins_when_shallower_misses(self) -> None:
        top = FakeLayer("top", [])  # empty
        deep = FakeLayer("deep", [], seed={REF: "from-deep"})
        src = RecordingSource(refs={REF: "from-source"})
        assert ResolverStack([top, deep], src).read(REF) == "from-deep"
        assert src.read_count == 0


# ---------------------------------------------------------------------------
# 3.2 — a hit is positive (a value) OR negative (a stored miss); both short-circuit
# ---------------------------------------------------------------------------


class TestHitShortCircuit:
    def test_positive_hit_returns_value_without_source(self) -> None:
        src = RecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: "cached"})
        assert ResolverStack([layer], src).read(REF) == "cached"
        assert src.read_count == 0

    def test_stored_miss_short_circuits_and_raises(self) -> None:
        src = RecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: _NOT_FOUND})
        with pytest.raises(OpNotFoundError):
            ResolverStack([layer], src).read(REF)
        assert src.read_count == 0

    def test_stored_miss_with_default_returns_default_without_source(self) -> None:
        src = RecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: _NOT_FOUND})
        assert ResolverStack([layer], src).read(REF, default_value="fallback") == "fallback"
        assert src.read_count == 0


# ---------------------------------------------------------------------------
# 3.3 — back-fill: who gets warmed, what, order (deepest-first), fresh stamp
# ---------------------------------------------------------------------------


class TestBackfillOnLayerHit:
    def test_warms_writable_layers_strictly_above_hit_deepest_first(self) -> None:
        log: list[tuple[str, str, object]] = []
        top = FakeLayer("top", log)  # idx 0 (empty)
        mid = FakeLayer("mid", log)  # idx 1 (empty)
        deep = FakeLayer("deep", log, seed={REF: "v"})  # idx 2 (hit)
        ResolverStack([top, mid, deep], RecordingSource()).read(REF)
        # Warm idx1 then idx0 (deepest-first); the hit layer (idx2) is not re-warmed.
        assert log == [("mid", REF, "v"), ("top", REF, "v")]

    def test_warmed_layer_serves_the_next_read(self) -> None:
        top = FakeLayer("top", [])
        deep = FakeLayer("deep", [], seed={REF: "v"})
        src = RecordingSource()
        stack_obj = ResolverStack([top, deep], src)
        stack_obj.read(REF)  # warms top
        assert top.lookup(REF) is not None  # top now holds it

    def test_backfill_uses_fresh_stamp_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The resolver back-fills the payload (value), not the source entry; the
        # warmed layer re-stamps with its own clock — staleness is not inherited.
        monkeypatch.setattr(stack, "_monotonic", lambda: 5000.0)
        top = MemoryLayer(ttl=300.0)
        deep = FakeLayer("deep", [], seed={REF: "v"})  # FakeLayer stamps cached_at=0.0
        ResolverStack([top, deep], RecordingSource()).read(REF)
        entry = top.lookup(REF)
        assert entry is not None
        assert entry.cached_at == 5000.0


class TestBackfillOnSourceResult:
    def test_source_value_backfilled_into_all_writable_deepest_first(self) -> None:
        log: list[tuple[str, str, object]] = []
        top = FakeLayer("top", log)
        deep = FakeLayer("deep", log)
        src = RecordingSource(refs={REF: "v"})
        assert ResolverStack([top, deep], src).read(REF) == "v"
        assert log == [("deep", REF, "v"), ("top", REF, "v")]
        assert src.read_count == 1


# ---------------------------------------------------------------------------
# 3.3 — read-only layers are never warmed
# ---------------------------------------------------------------------------


class TestReadOnlyNeverWarmed:
    def test_readonly_above_hit_not_warmed(self) -> None:
        log: list[tuple[str, str, object]] = []
        ro = FakeReadOnlyLayer("ro")  # idx 0, miss
        deep = FakeLayer("deep", log, seed={REF: "v"})  # idx 1, hit
        ResolverStack([ro, deep], RecordingSource()).read(REF)
        assert log == []  # nothing writable above the hit

    def test_readonly_not_warmed_on_source_result(self) -> None:
        log: list[tuple[str, str, object]] = []
        ro = FakeReadOnlyLayer("ro")  # idx 0
        writer = FakeLayer("w", log)  # idx 1
        src = RecordingSource(refs={REF: "v"})
        ResolverStack([ro, writer], src).read(REF)
        assert log == [("w", REF, "v")]  # only the writable layer warmed


# ---------------------------------------------------------------------------
# 3.2 rule 2 — online=False: raise OpOfflineError, never consult the source
# ---------------------------------------------------------------------------


class TestOffline:
    def test_offline_no_hit_raises_and_source_untouched(self) -> None:
        src = RecordingSource(refs={REF: "v"})
        with pytest.raises(OpOfflineError):
            ResolverStack([MemoryLayer(ttl=300.0)], src).read(REF, online=False)
        assert src.read_count == 0

    def test_offline_served_from_layer(self) -> None:
        src = RecordingSource()
        layer = FakeLayer("w", [], seed={REF: "v"})
        assert ResolverStack([layer], src).read(REF, online=False) == "v"
        assert src.read_count == 0


# ---------------------------------------------------------------------------
# 3.2 rule 3 — online=True terminal miss: back-fill the miss into ALL writable,
# then raise OpNotFoundError or return default
# ---------------------------------------------------------------------------


class TestTerminalMiss:
    def test_terminal_miss_backfills_miss_and_raises(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        src = RecordingSource()
        stack_obj = ResolverStack([writer], src)
        with pytest.raises(OpNotFoundError):
            stack_obj.read(REF)
        assert log == [("w", REF, _NOT_FOUND)]
        assert src.read_count == 1
        # The back-filled miss now short-circuits — the source is not consulted again.
        with pytest.raises(OpNotFoundError):
            stack_obj.read(REF)
        assert src.read_count == 1

    def test_terminal_miss_with_default_backfills_miss_returns_default(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        src = RecordingSource()
        result = ResolverStack([writer], src).read(REF, default_value="fallback")
        assert result == "fallback"
        # The MISS is back-filled, not the default value.
        assert log == [("w", REF, _NOT_FOUND)]


# ---------------------------------------------------------------------------
# 3.2 rule 3 — any other OpError propagates unchanged and is never cached
# ---------------------------------------------------------------------------


class TestNonNotFoundPropagates:
    def test_auth_error_propagates_and_is_not_cached(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        src = RecordingSource(error=OpAuthError("signed out"))
        stack_obj = ResolverStack([writer], src)
        with pytest.raises(OpAuthError):
            stack_obj.read(REF)
        assert log == []  # failures are never cached
        with pytest.raises(OpAuthError):
            stack_obj.read(REF)
        assert src.read_count == 2  # retry still reaches the source


# ---------------------------------------------------------------------------
# 3.4 — get_item / list_items / list_vaults route straight to the source
# ---------------------------------------------------------------------------


class TestNonReadOperationsDelegate:
    def test_get_item_delegates_to_source(self) -> None:
        src = RecordingSource()
        with pytest.raises(OpNotFoundError):
            ResolverStack([FakeLayer("w", [])], src).get_item("id")
        assert src.get_item_count == 1

    def test_list_items_delegates_to_source(self) -> None:
        src = RecordingSource()
        assert ResolverStack([], src).list_items() == []
        assert src.list_items_count == 1

    def test_list_vaults_delegates_to_source(self) -> None:
        src = RecordingSource()
        assert ResolverStack([], src).list_vaults() == []
        assert src.list_vaults_count == 1


# ---------------------------------------------------------------------------
# 3.2 step 3 — source is called bare (no default_value / online forwarded)
# ---------------------------------------------------------------------------


class TestSourceCalledBare:
    def test_source_read_receives_no_forwarded_kwargs(self) -> None:
        # The stack owns default_value and online; neither is forwarded to the source.
        # last_read_kwargs records only what the caller explicitly passed, so both
        # keys must be absent -- not just set to the protocol defaults.
        src = RecordingSource(refs={REF: "v"})
        ResolverStack([], src).read(REF, default_value="x", online=True)
        assert src.last_read_kwargs is not None
        assert "default_value" not in src.last_read_kwargs
        assert "online" not in src.last_read_kwargs


# ---------------------------------------------------------------------------
# 3.1 / 5.1 — locking: one resolver lock, released across the source call
# ---------------------------------------------------------------------------


class TestLocking:
    def test_lock_released_across_source_call(self) -> None:
        probe: dict[str, bool] = {}
        src = RecordingSource(refs={REF: "v"})
        stack_obj = ResolverStack([FakeLayer("w", [])], src)

        def on_read() -> None:
            acquired = stack_obj._lock.acquire(blocking=False)
            probe["free_during_source"] = acquired
            if acquired:
                stack_obj._lock.release()

        src.on_read = on_read
        stack_obj.read(REF)
        assert probe["free_during_source"] is True

    def test_simultaneous_misses_both_reach_source(self) -> None:
        # Accepted benign race (design section 3.2 / 11): two threads missing at
        # once both reach the source. If the lock were held across the source
        # call, the barrier could never trip and the test would time out.
        barrier = threading.Barrier(2, timeout=5.0)
        src = RecordingSource(refs={REF: "v"}, on_read=barrier.wait)
        stack_obj = ResolverStack([FakeLayer("shared", [])], src)
        results: list[str] = []
        errors: list[Exception] = []

        def worker() -> None:
            try:
                results.append(stack_obj.read(REF))
            except Exception as exc:  # test records any failure for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert not errors
        assert results == ["v", "v"]
        assert src.read_count == 2


# ---------------------------------------------------------------------------
# 5.1 — layers=[] is a pure delegate; constructor validation is strict
# ---------------------------------------------------------------------------


class TestConstructionAndDelegation:
    def test_empty_layers_is_a_pure_delegate(self) -> None:
        src = RecordingSource(refs={REF: "v"})
        stack_obj = ResolverStack([], src)
        assert stack_obj.read(REF) == "v"
        assert src.read_count == 1
        assert stack_obj.read(REF) == "v"
        assert src.read_count == 2  # nothing to cache — source consulted each time

    def test_constructor_rejects_backend_in_layers(self) -> None:
        src = RecordingSource()
        with pytest.raises(TypeError):
            ResolverStack([RecordingSource()], src)  # type: ignore[list-item]

    def test_constructor_rejects_layer_as_source(self) -> None:
        with pytest.raises(TypeError):
            ResolverStack([], FakeLayer("w", []))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 5.1 — stack-level clear() / clear_misses() fan out to writable layers
# ---------------------------------------------------------------------------


class TestStackClear:
    def test_clear_fans_out_to_writable_layers(self) -> None:
        a = FakeLayer("a", [], seed={REF: "va"})
        b = FakeLayer("b", [], seed={"op://v/b": "vb"})
        stack_obj = ResolverStack([a, b], RecordingSource())
        stack_obj.clear()
        assert a.entries == {}
        assert b.entries == {}

    def test_clear_misses_fans_out_to_writable_layers(self) -> None:
        a = FakeLayer("a", [], seed={REF: "value", "op://v/m": _NOT_FOUND})
        stack_obj = ResolverStack([a], RecordingSource())
        stack_obj.clear_misses()
        assert a.entries == {REF: "value"}

    def test_clear_skips_readonly_layers(self) -> None:
        ro = FakeReadOnlyLayer("ro", seed={REF: "v"})
        stack_obj = ResolverStack([ro], RecordingSource())
        stack_obj.clear()  # must not raise on a layer with no clear()
        assert ro.entries == {REF: "v"}  # untouched


# ---------------------------------------------------------------------------
# Back-fill partial failure: a writable layer whose store() raises
# ---------------------------------------------------------------------------


class FaultyLayer:
    """A writable layer whose store() always raises OSError."""

    def __init__(self, label: str, log: list[tuple[str, str, object]]) -> None:
        self.label = label
        self.log = log
        self.entries: dict[str, object] = {}

    def lookup(self, reference: str) -> CacheEntry | None:
        return None

    def store(self, reference: str, value: object) -> None:
        raise OSError("disk full")

    def clear(self) -> None:
        self.entries.clear()

    def clear_misses(self) -> None:
        pass


class TestBackfillPartialFailure:
    def test_store_exception_in_backfill_propagates_and_source_value_lost(self) -> None:
        # _backfill calls layer.store() directly with no exception guard (design 3.3).
        # If a layer's store() raises, the exception propagates out of read().
        # This test documents the actual behavior: the exception is NOT swallowed.
        faulty = FaultyLayer("faulty", [])
        src = RecordingSource(refs={REF: "v"})
        stack_obj = ResolverStack([faulty], src)
        # The stack calls _backfill after the source read succeeds; the faulty
        # store raises, so read() propagates the OSError.
        with pytest.raises(OSError, match="disk full"):
            stack_obj.read(REF)
        assert src.read_count == 1  # source was reached before the failure


# ---------------------------------------------------------------------------
# Unicode reference key round-trip
# ---------------------------------------------------------------------------


class TestUnicodeReferenceKey:
    def test_non_ascii_reference_key_round_trips_through_read_and_backfill(self) -> None:
        unicode_ref = "op://Vault/Item/éphémère"
        layer = FakeLayer("w", [])
        src = RecordingSource(refs={unicode_ref: "secret-value"})
        stack_obj = ResolverStack([layer], src)

        result = stack_obj.read(unicode_ref)
        assert result == "secret-value"
        # Back-fill must have stored the unicode key in the layer.
        assert layer.lookup(unicode_ref) is not None
        assert layer.lookup(unicode_ref).value == "secret-value"  # type: ignore[union-attr]
        # Second read must be served from the layer (source not consulted again).
        result2 = stack_obj.read(unicode_ref)
        assert result2 == "secret-value"
        assert src.read_count == 1
