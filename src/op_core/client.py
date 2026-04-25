"""High-level 1Password client facades.

:class:`OnePassword` and :class:`AsyncOnePassword` compose a
:class:`~op_core.backends.base.Backend` (or ``AsyncBackend``) with the
:class:`~op_core.field.FieldValue` resolution helpers. They are intentionally
thin — caching, auth, and transport concerns belong to the backend layer.
If you want caching, wrap your backend in :class:`~op_core.backends.caching.CachingBackend`
before passing it in. If you want custom auth or binary, construct the
backend explicitly instead of relying on :func:`~op_core.backends.detect.detect_backend`.
"""

from __future__ import annotations

from collections.abc import Sequence

from op_core.backends.base import AsyncBackend, Backend
from op_core.backends.detect import detect_async_backend, detect_backend
from op_core.exceptions import OpNotFoundError
from op_core.field import FieldValue, async_resolve_chain, resolve_chain
from op_core.items import Item, ItemRef, ItemSummary
from op_core.opref import OpRef


def _to_ref(reference: str | OpRef) -> str:
    return reference.for_op() if isinstance(reference, OpRef) else reference


class OnePassword:
    """Synchronous 1Password client facade.

    Wraps any :class:`Backend`. When ``backend`` is omitted,
    :func:`detect_backend` picks one from the environment.

    >>> from op_core import InMemoryBackend, OnePassword
    >>> op = OnePassword(InMemoryBackend(refs={'op://v/i/f': 'hunter2'}))
    >>> op.read('op://v/i/f')
    'hunter2'
    """

    def __init__(self, backend: Backend | None = None) -> None:
        self._backend: Backend = backend if backend is not None else detect_backend()

    @property
    def backend(self) -> Backend:
        return self._backend

    def read(self, reference: str | OpRef, *, online: bool = True) -> str | None:
        """Resolve a single reference. Returns ``None`` when not found.

        Narrower than the backend ``read``: no ``default_value`` — miss
        translates to ``None``. Use :meth:`resolve` for chain/fallback semantics.

        ``online=False`` propagates to the backend: if the reference is not
        available from local state, :class:`OpOfflineError` is raised rather
        than silently returning ``None``. This lets callers distinguish
        "confirmed missing" from "I wouldn't let you check."

        >>> from op_core import InMemoryBackend, OnePassword
        >>> op = OnePassword(InMemoryBackend(refs={'op://v/i/f': 'hunter2'}))
        >>> op.read('op://v/i/f')
        'hunter2'
        >>> op.read('op://v/i/missing') is None
        True
        """
        try:
            return self._backend.read(_to_ref(reference), online=online)
        except OpNotFoundError:
            return None

    def resolve(self, field: FieldValue, *, online: bool = True) -> str | None:
        """Resolve a :class:`FieldValue` via its ``||`` fallback chain.

        Self-references in ``field.original`` are expected to be already
        expanded — :func:`op_core.field.normalize_original` is the place to
        do that before constructing the :class:`FieldValue`.

        ``online=`` is propagated to each backend read during chain walking.
        Note: under ``online=False``, an :class:`OpOfflineError` from any
        chain segment terminates the walk and propagates out — it is NOT
        caught and treated as "missing, try the next segment", because the
        caller explicitly forbade the network round-trip that would be
        needed to confirm the absence.

        >>> from op_core import FieldValue, InMemoryBackend, OnePassword
        >>> op = OnePassword(InMemoryBackend(refs={'op://v/i/backup': 'fallback'}))
        >>> fv = FieldValue.from_raw('op://v/i/primary||op://v/i/backup', 'token')
        >>> op.resolve(fv)
        'fallback'
        """
        return resolve_chain(field.original, lambda ref: self.read(ref, online=online))

    def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        """List items matching the given filters.

        >>> from op_core import InMemoryBackend, Item, OnePassword
        >>> item = Item(id='i1', title='T', vault_id='v1', vault_name='V',
        ...             category='LOGIN', tags=('dev',), sections=(), fields=())
        >>> op = OnePassword(InMemoryBackend(items=[item]))
        >>> [s.id for s in op.list_items(tags=['dev'])]
        ['i1']
        """
        return self._backend.list_items(vault=vault, tags=tags, categories=categories)

    def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        """Fetch a full item by id, summary, or item instance.

        >>> from op_core import InMemoryBackend, Item, OnePassword
        >>> item = Item(id='i1', title='T', vault_id='v1', vault_name='V',
        ...             category='LOGIN', tags=(), sections=(), fields=())
        >>> op = OnePassword(InMemoryBackend(items=[item]))
        >>> op.get_item('i1').title
        'T'
        """
        return self._backend.get_item(item, vault=vault)


class AsyncOnePassword:
    """Async 1Password client facade. Mirrors :class:`OnePassword`."""

    def __init__(self, backend: AsyncBackend | None = None) -> None:
        self._backend: AsyncBackend = backend if backend is not None else detect_async_backend()

    @property
    def backend(self) -> AsyncBackend:
        return self._backend

    async def read(self, reference: str | OpRef, *, online: bool = True) -> str | None:
        try:
            return await self._backend.read(_to_ref(reference), online=online)
        except OpNotFoundError:
            return None

    async def resolve(self, field: FieldValue, *, online: bool = True) -> str | None:
        """Resolve a :class:`FieldValue` via its ``||`` fallback chain."""

        async def reader(ref: str) -> str | None:
            return await self.read(ref, online=online)

        return await async_resolve_chain(field.original, reader)

    async def list_items(
        self,
        *,
        vault: str | None = None,
        tags: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
    ) -> list[ItemSummary]:
        return await self._backend.list_items(vault=vault, tags=tags, categories=categories)

    async def get_item(self, item: ItemRef, *, vault: str | None = None) -> Item:
        return await self._backend.get_item(item, vault=vault)
