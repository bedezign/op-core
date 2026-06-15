"""Tests for :class:`op_core.backends.stack.AsyncResolverStack` (design section 3).

The async stack mirrors the sync one: same walk, back-fill, and ``default_value``
/ ``online`` semantics, with an :class:`asyncio.Lock` and ``await`` only on the
source. The sync layer doubles (``FakeLayer`` / ``FakeReadOnlyLayer``) are reused
verbatim — layers are synchronous in both stacks (design 5.1). The two
async-specific tests exercise the lock being released across the ``await``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING

import pytest

from op_core.backends import stack
from op_core.backends.caching import _NOT_FOUND
from op_core.backends.stack import AsyncResolverStack, MemoryLayer
from op_core.exceptions import OpAuthError, OpNotFoundError, OpOfflineError
from tests.unit.test_resolver_stack import FakeLayer, FakeReadOnlyLayer, _UNSET

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from op_core.items import Item, ItemRef, ItemSummary, VaultSummary

REF = "op://Vault/Item/field"
OTHER = "op://Vault/Item/other"


class AsyncRecordingSource:
    """An ``AsyncBackend`` double: counts calls, can raise, can run a hook mid-read."""

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
        self.read_count = 0
        self.get_item_count = 0
        self.list_items_count = 0
        self.list_vaults_count = 0
        # Records only kwargs *explicitly passed* by the caller (absent = not forwarded).
        self.last_read_kwargs: dict[str, object] | None = None

    async def read(
        self,
        reference: str,
        *,
        default_value: object = _UNSET,
        online: object = _UNSET,
    ) -> str:
        self.read_count += 1
        self.last_read_kwargs = {
            k: v
            for k, v in {"default_value": default_value, "online": online}.items()
            if v is not _UNSET
        }
        if self.on_read is not None:
            result = self.on_read()
            if inspect.isawaitable(result):
                await result
        if self.error is not None:
            raise self.error
        if reference in self.refs:
            return self.refs[reference]
        raise OpNotFoundError(reference)

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        self.get_item_count += 1
        raise OpNotFoundError("no item")

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        self.list_items_count += 1
        return []

    async def list_vaults(self) -> list[VaultSummary]:
        self.list_vaults_count += 1
        return []


class TestFirstHitWins:
    async def test_first_layer_with_a_live_entry_wins(self) -> None:
        top = FakeLayer("top", [], seed={REF: "from-top"})
        deep = FakeLayer("deep", [], seed={REF: "from-deep"})
        src = AsyncRecordingSource(refs={REF: "from-source"})
        assert await AsyncResolverStack([top, deep], src).read(REF) == "from-top"
        assert src.read_count == 0

    async def test_deeper_layer_wins_when_shallower_misses(self) -> None:
        top = FakeLayer("top", [])
        deep = FakeLayer("deep", [], seed={REF: "from-deep"})
        src = AsyncRecordingSource(refs={REF: "from-source"})
        assert await AsyncResolverStack([top, deep], src).read(REF) == "from-deep"
        assert src.read_count == 0


class TestHitShortCircuit:
    async def test_positive_hit_returns_value_without_source(self) -> None:
        src = AsyncRecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: "cached"})
        assert await AsyncResolverStack([layer], src).read(REF) == "cached"
        assert src.read_count == 0

    async def test_stored_miss_short_circuits_and_raises(self) -> None:
        src = AsyncRecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: _NOT_FOUND})
        with pytest.raises(OpNotFoundError):
            await AsyncResolverStack([layer], src).read(REF)
        assert src.read_count == 0

    async def test_stored_miss_with_default_returns_default(self) -> None:
        src = AsyncRecordingSource(refs={REF: "fresh"})
        layer = FakeLayer("w", [], seed={REF: _NOT_FOUND})
        assert await AsyncResolverStack([layer], src).read(REF, default_value="fallback") == "fallback"
        assert src.read_count == 0


class TestBackfill:
    async def test_warms_writable_layers_above_hit_deepest_first(self) -> None:
        log: list[tuple[str, str, object]] = []
        top = FakeLayer("top", log)
        mid = FakeLayer("mid", log)
        deep = FakeLayer("deep", log, seed={REF: "v"})
        await AsyncResolverStack([top, mid, deep], AsyncRecordingSource()).read(REF)
        assert log == [("mid", REF, "v"), ("top", REF, "v")]

    async def test_source_value_backfilled_into_all_writable_deepest_first(self) -> None:
        log: list[tuple[str, str, object]] = []
        top = FakeLayer("top", log)
        deep = FakeLayer("deep", log)
        src = AsyncRecordingSource(refs={REF: "v"})
        assert await AsyncResolverStack([top, deep], src).read(REF) == "v"
        assert log == [("deep", REF, "v"), ("top", REF, "v")]

    async def test_backfill_uses_fresh_stamp_not_inherited(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stack, "_monotonic", lambda: 5000.0)
        top = MemoryLayer(ttl=300.0)
        deep = FakeLayer("deep", [], seed={REF: "v"})
        await AsyncResolverStack([top, deep], AsyncRecordingSource()).read(REF)
        entry = top.lookup(REF)
        assert entry is not None
        assert entry.cached_at == 5000.0


class TestReadOnlyNeverWarmed:
    async def test_readonly_above_hit_not_warmed(self) -> None:
        log: list[tuple[str, str, object]] = []
        ro = FakeReadOnlyLayer("ro")
        deep = FakeLayer("deep", log, seed={REF: "v"})
        await AsyncResolverStack([ro, deep], AsyncRecordingSource()).read(REF)
        assert log == []

    async def test_readonly_not_warmed_on_source_result(self) -> None:
        log: list[tuple[str, str, object]] = []
        ro = FakeReadOnlyLayer("ro")
        writer = FakeLayer("w", log)
        src = AsyncRecordingSource(refs={REF: "v"})
        await AsyncResolverStack([ro, writer], src).read(REF)
        assert log == [("w", REF, "v")]


class TestOffline:
    async def test_offline_no_hit_raises_and_source_untouched(self) -> None:
        src = AsyncRecordingSource(refs={REF: "v"})
        with pytest.raises(OpOfflineError):
            await AsyncResolverStack([MemoryLayer(ttl=300.0)], src).read(REF, online=False)
        assert src.read_count == 0

    async def test_offline_served_from_layer(self) -> None:
        src = AsyncRecordingSource()
        layer = FakeLayer("w", [], seed={REF: "v"})
        assert await AsyncResolverStack([layer], src).read(REF, online=False) == "v"
        assert src.read_count == 0


class TestTerminalMiss:
    async def test_terminal_miss_backfills_miss_and_raises(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        src = AsyncRecordingSource()
        stack_obj = AsyncResolverStack([writer], src)
        with pytest.raises(OpNotFoundError):
            await stack_obj.read(REF)
        assert log == [("w", REF, _NOT_FOUND)]
        assert src.read_count == 1
        with pytest.raises(OpNotFoundError):
            await stack_obj.read(REF)
        assert src.read_count == 1  # back-filled miss short-circuits

    async def test_terminal_miss_with_default_backfills_miss_returns_default(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        result = await AsyncResolverStack([writer], AsyncRecordingSource()).read(REF, default_value="fallback")
        assert result == "fallback"
        assert log == [("w", REF, _NOT_FOUND)]


class TestNonNotFoundPropagates:
    async def test_auth_error_propagates_and_is_not_cached(self) -> None:
        log: list[tuple[str, str, object]] = []
        writer = FakeLayer("w", log)
        src = AsyncRecordingSource(error=OpAuthError("signed out"))
        stack_obj = AsyncResolverStack([writer], src)
        with pytest.raises(OpAuthError):
            await stack_obj.read(REF)
        assert log == []
        with pytest.raises(OpAuthError):
            await stack_obj.read(REF)
        assert src.read_count == 2


class TestNonReadOperationsDelegate:
    async def test_get_item_delegates_to_source(self) -> None:
        src = AsyncRecordingSource()
        with pytest.raises(OpNotFoundError):
            await AsyncResolverStack([FakeLayer("w", [])], src).get_item("id")
        assert src.get_item_count == 1

    async def test_list_items_delegates_to_source(self) -> None:
        src = AsyncRecordingSource()
        assert await AsyncResolverStack([], src).list_items() == []
        assert src.list_items_count == 1

    async def test_list_vaults_delegates_to_source(self) -> None:
        src = AsyncRecordingSource()
        assert await AsyncResolverStack([], src).list_vaults() == []
        assert src.list_vaults_count == 1


class TestSourceCalledBare:
    async def test_source_read_receives_no_forwarded_kwargs(self) -> None:
        # The stack owns default_value and online; neither is forwarded to the source.
        # last_read_kwargs records only what the caller explicitly passed, so both
        # keys must be absent -- not just set to the protocol defaults.
        src = AsyncRecordingSource(refs={REF: "v"})
        await AsyncResolverStack([], src).read(REF, default_value="x", online=True)
        assert src.last_read_kwargs is not None
        assert "default_value" not in src.last_read_kwargs
        assert "online" not in src.last_read_kwargs


class TestConstructionAndDelegation:
    async def test_empty_layers_is_a_pure_delegate(self) -> None:
        src = AsyncRecordingSource(refs={REF: "v"})
        stack_obj = AsyncResolverStack([], src)
        assert await stack_obj.read(REF) == "v"
        assert await stack_obj.read(REF) == "v"
        assert src.read_count == 2

    def test_constructor_rejects_backend_in_layers(self) -> None:
        src = AsyncRecordingSource()
        with pytest.raises(TypeError):
            AsyncResolverStack([AsyncRecordingSource()], src)  # type: ignore[list-item]

    def test_constructor_rejects_layer_as_source(self) -> None:
        with pytest.raises(TypeError):
            AsyncResolverStack([], FakeLayer("w", []))  # type: ignore[arg-type]


class TestStackClear:
    async def test_clear_fans_out_to_writable_layers(self) -> None:
        a = FakeLayer("a", [], seed={REF: "va"})
        b = FakeLayer("b", [], seed={"op://v/b": "vb"})
        stack_obj = AsyncResolverStack([a, b], AsyncRecordingSource())
        await stack_obj.clear()
        assert a.entries == {}
        assert b.entries == {}

    async def test_clear_misses_fans_out_to_writable_layers(self) -> None:
        a = FakeLayer("a", [], seed={REF: "value", "op://v/m": _NOT_FOUND})
        stack_obj = AsyncResolverStack([a], AsyncRecordingSource())
        await stack_obj.clear_misses()
        assert a.entries == {REF: "value"}


class TestAsyncLocking:
    async def test_lock_released_across_source_await(self) -> None:
        # While coroutine A is suspended awaiting the source, coroutine B's
        # layer-hit read must not block on the resolver lock (design 3.1).
        release = asyncio.Event()
        in_source = asyncio.Event()

        async def slow() -> None:
            in_source.set()
            await release.wait()

        src = AsyncRecordingSource(refs={REF: "v"}, on_read=slow)
        layer = FakeLayer("w", [], seed={OTHER: "cached"})
        stack_obj = AsyncResolverStack([layer], src)

        task_a = asyncio.create_task(stack_obj.read(REF))  # misses layer, suspends in source
        await asyncio.wait_for(in_source.wait(), timeout=5.0)
        result_b = await asyncio.wait_for(stack_obj.read(OTHER), timeout=2.0)
        assert result_b == "cached"
        release.set()
        assert await asyncio.wait_for(task_a, timeout=5.0) == "v"

    async def test_simultaneous_misses_both_reach_source(self) -> None:
        # Accepted benign race (design 3.2 / 11): two coroutines missing at once
        # both reach the source. If the lock were held across the await, the
        # second could not enter the source and the gate would never trip.
        count = 0
        proceed = asyncio.Event()

        async def gate() -> None:
            nonlocal count
            count += 1
            if count == 2:  # both concurrent readers have entered the source
                proceed.set()
            await asyncio.wait_for(proceed.wait(), timeout=5.0)

        src = AsyncRecordingSource(refs={REF: "v"}, on_read=gate)
        stack_obj = AsyncResolverStack([FakeLayer("shared", [])], src)
        results = await asyncio.wait_for(asyncio.gather(stack_obj.read(REF), stack_obj.read(REF)), timeout=5.0)
        assert results == ["v", "v"]
        assert src.read_count == 2
