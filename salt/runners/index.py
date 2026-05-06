"""
Salt runner for on-demand maintenance of mmap-backed master indexes.

.. versionadded:: 3009.0

Each index uses :class:`~salt.utils.mmap_cache.MmapCache` compaction
(``atomic_rebuild`` with sorted placement) where applicable. The **pki** index
is rebuilt from the PKI directory layout; the **resources** index is compacted
from its existing primary contents (tombstone reclamation / load relief).

CLI examples:

.. code-block:: bash

    salt-run index.status name=pki
    salt-run index.compact name=pki
    salt-run index.compact name=resources dry_run=True
"""

import logging

from salt.exceptions import SaltInvocationError

log = logging.getLogger(__name__)

# Aliases map user-facing names to canonical handlers.
_INDEX_ALIASES = {
    "pki": "pki",
    "minion_keys": "pki",
    "keys": "pki",
    "resources": "resources",
    "resource_registry": "resources",
}

_CANONICAL = ("pki", "resources")


def _canonical_index(name):
    if name is None or (isinstance(name, str) and not name.strip()):
        raise SaltInvocationError(
            "Missing index name. Example: salt-run index.compact name=pki. "
            f"Valid names: {', '.join(_CANONICAL)} (see ``index.list_indexes``)."
        )
    key = str(name).strip().lower().replace("-", "_")
    canonical = _INDEX_ALIASES.get(key, key)
    if canonical not in _CANONICAL:
        raise SaltInvocationError(
            f"Unknown index {name!r}. Expected one of: {', '.join(_CANONICAL)} "
            "(aliases: minion_keys, keys → pki; resource_registry → resources)."
        )
    return canonical


def list_indexes():
    """
    Return supported canonical index names and short descriptions.

    CLI Example:

    .. code-block:: bash

        salt-run index.list_indexes
    """
    return {
        "pki": "Minion authentication key mmap index (requires ``pki_index_enabled``).",
        "resources": "Master resource registry SRN primary mmap under ``cachedir``.",
    }


def _pki_index_stats(opts):
    """
    Return ``MmapCache.get_stats()`` for the PKI ``keys`` bank, or ``None``
    if the index file does not exist yet (no ``rebuild`` has run).
    """
    import os  # pylint: disable=import-outside-toplevel

    import salt.utils.mmap_cache  # pylint: disable=import-outside-toplevel
    from salt.cache import mmap_key  # pylint: disable=import-outside-toplevel

    if opts.get("cluster_id"):
        cachedir = opts.get("cluster_pki_dir", "")
    else:
        cachedir = opts.get("pki_dir", "")
    if not cachedir:
        return None
    index_path = os.path.join(cachedir, mmap_key._BANK_INDEX_NAME["keys"])
    if not os.path.exists(index_path):
        return None
    cache = salt.utils.mmap_cache.MmapCache(
        path=index_path,
        size=opts.get("mmap_key_size", mmap_key._DEFAULT_SIZE),
        slot_size=opts.get("mmap_key_slot_size", mmap_key._DEFAULT_SLOT_SIZE),
        key_size=opts.get("mmap_key_id_size", mmap_key._DEFAULT_ID_SIZE),
    )
    try:
        return cache.get_stats()
    finally:
        cache.close()


def _compact_pki(opts, dry_run):
    from salt.cache import mmap_key  # pylint: disable=import-outside-toplevel

    stats_before = _pki_index_stats(opts)

    if dry_run:
        if not stats_before:
            return (
                "PKI Index Status:\n"
                "  Index file not present (no migration has run yet).\n"
                "  Occupied: 0\n"
            )

        occ = stats_before["occupied"]
        deleted = stats_before["deleted"]
        denom = occ + deleted
        pct_tombstones = (deleted / denom * 100) if denom > 0 else 0.0

        return (
            f"PKI Index Status:\n"
            f"  Total slots: {stats_before['total']:,}\n"
            f"  Occupied: {occ:,}\n"
            f"  Deleted (tombstones): {deleted:,}\n"
            f"  Empty: {stats_before['empty']:,}\n"
            f"  Load factor: {stats_before['load_factor']:.1%}\n"
            f"  Tombstone ratio: {pct_tombstones:.1f}%\n"
            f"\n"
            f"Rebuild recommended: {'Yes' if pct_tombstones > 25 else 'No'}"
        )

    log.info("Starting PKI mmap index rebuild")
    # ``mmap_key`` reads its module-level ``__opts__`` to size the MmapCache
    # under ``store()``; since we're calling it from a runner (no loader
    # injection on this import), point it at our opts before invoking.
    mmap_key.__opts__ = opts
    counts = mmap_key.rebuild_from_localfs(opts)
    if counts is None:
        return "PKI index rebuild failed. Check logs for details."

    total = sum(counts.values())
    deleted_before = stats_before["deleted"] if stats_before else 0
    return (
        f"PKI index rebuilt successfully.\n"
        f"  Keys: {total:,}\n"
        f"    accepted: {counts['accepted']:,}\n"
        f"    pending:  {counts['pending']:,}\n"
        f"    rejected: {counts['rejected']:,}\n"
        f"    denied:   {counts['denied']:,}\n"
        f"  Tombstones removed: {deleted_before:,}\n"
    )


def _compact_resources(opts, dry_run):
    import salt.utils.resource_registry as rr  # pylint: disable=import-outside-toplevel

    reg = rr.get_registry(opts)

    if dry_run:
        try:
            stats = reg.stats()
        except Exception as exc:  # pylint: disable=broad-except
            log.exception("resource index status failed")
            return f"Resource index stats unavailable: {exc}"

        primary = stats.get("primary") or {}
        occ = primary.get("occupied", 0)
        deleted = primary.get("deleted", 0)
        total = primary.get("total", 0) or 1
        load_factor = primary.get("load_factor", (occ + deleted) / total)
        denom = occ + deleted
        pct_tombstones = (deleted / denom * 100) if denom > 0 else 0.0

        return (
            f"Resource index status:\n"
            f"  Path: {stats.get('path')}\n"
            f"  Total slots: {primary.get('total', 0):,}\n"
            f"  Occupied: {occ:,}\n"
            f"  Deleted (tombstones): {deleted:,}\n"
            f"  Load factor: {load_factor:.1%}\n"
            f"  Tombstone ratio: {pct_tombstones:.1f}%\n"
            f"  Derived types: {stats.get('derived_by_type_count', 0)}\n"
            f"  Derived minions: {stats.get('derived_by_minion_count', 0)}\n"
        )

    log.info("Starting resource registry primary compaction")
    before, after = reg.compact()
    return (
        f"Resource index compacted successfully.\n"
        f"  Occupied slots: {before:,} -> {after:,}\n"
    )


def compact(name, dry_run=False):
    """
    Rebuild or compact the named mmap-backed master index.

    :param str name: Index to operate on. See :func:`list_indexes`.
    :param bool dry_run: When ``True``, print statistics only (no writes).

    CLI Examples:

    .. code-block:: bash

        salt-run index.compact name=pki
        salt-run index.compact name=resources
        salt-run index.compact name=pki dry_run=True
    """
    which = _canonical_index(name)
    if which == "pki":
        return _compact_pki(__opts__, dry_run)
    return _compact_resources(__opts__, dry_run)


def status(name):
    """
    Show statistics for the named index (same as ``compact(..., dry_run=True)``).

    CLI Example:

    .. code-block:: bash

        salt-run index.status name=resources
    """
    return compact(name, dry_run=True)
