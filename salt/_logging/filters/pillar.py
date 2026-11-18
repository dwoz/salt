"""
salt._logging.filters.pillar
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``PillarRedactingFilter`` — a ``logging.Filter`` that redacts every
pillar leaf value from outbound log records.

The operator opts in with a single config option::

    pillar_mask_output: True

That is the entire configuration surface. When False (the default),
the filter is a zero-cost passthrough. When True, every string,
bytes, int, and float leaf of the minion's pillar that is at least
``MIN_VALUE_LENGTH`` characters long is substituted with
``**********`` wherever it appears in ``record.msg`` or
``record.args``. The snapshot is rebuilt lazily when ``opts["pillar"]``
is reassigned (which happens on every pillar refresh).

The 4-character floor is a fixed internal limit, not a knob.
Redacting shorter values would corrupt too much log text to be
useful.
"""

from __future__ import annotations

import logging
import re
import threading

REDACT_PLACEHOLDER = "**********"
MIN_VALUE_LENGTH = 4


def _flatten_scalars(value, out):
    """Collect pillar leaf scalars (len >= MIN_VALUE_LENGTH) into ``out``."""
    if isinstance(value, bool):
        # bool is a subclass of int; skip — "True"/"False" collisions are
        # worse than useless.
        return
    if isinstance(value, dict):
        for v in value.values():
            _flatten_scalars(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _flatten_scalars(v, out)
    elif isinstance(value, (str, bytes)):
        if not value:
            return
        s = value.decode(errors="replace") if isinstance(value, bytes) else value
        if len(s) >= MIN_VALUE_LENGTH:
            out.append(s)
    elif isinstance(value, (int, float)):
        s = str(value)
        if len(s) >= MIN_VALUE_LENGTH:
            out.append(s)


class PillarRedactingFilter(logging.Filter):
    """Redact every pillar leaf value from outbound log records.

    Install with ``install_on_handler(handler, opts)``. The filter
    reads ``opts["pillar_mask_output"]`` and ``opts["pillar"]`` at
    construction time; the pillar snapshot is refreshed lazily when
    ``id(opts["pillar"])`` changes (which occurs on every
    ``saltutil.refresh_pillar``).
    """

    def __init__(self, opts=None):
        super().__init__()
        self._lock = threading.Lock()
        self._opts = opts or {}
        self._enabled = bool(self._opts.get("pillar_mask_output", False))
        self._regex = None
        self._snapshot_id = None
        self._compile()

    def refresh(self):
        """Re-read opts and rebuild the pillar-value regex."""
        with self._lock:
            self._enabled = bool(self._opts.get("pillar_mask_output", False))
            self._compile()

    def _compile(self):
        pillar = self._opts.get("pillar") or {}
        values = []
        _flatten_scalars(pillar, values)
        # De-dup; order longest-first so compound values redact before
        # their own prefixes would.
        uniq = sorted(set(values), key=len, reverse=True)
        if uniq:
            self._regex = re.compile("|".join(re.escape(v) for v in uniq))
        else:
            self._regex = None
        self._snapshot_id = id(pillar)

    def filter(self, record):
        # Fast path: feature off.
        if not self._enabled:
            return True
        try:
            # Opportunistic refresh if opts["pillar"] was rebound.
            if id(self._opts.get("pillar")) != self._snapshot_id:
                self._compile()
            if self._regex is None:
                return True
            if isinstance(record.msg, str):
                record.msg = self._regex.sub(REDACT_PLACEHOLDER, record.msg)
            record.args = self._scrub(record.args)
        except Exception:  # pylint: disable=broad-except
            # A bug in redaction must never silence logging.
            return True
        return True

    def _scrub(self, obj):
        if self._regex is None or obj is None:
            return obj
        if isinstance(obj, str):
            return self._regex.sub(REDACT_PLACEHOLDER, obj)
        if isinstance(obj, bytes):
            return self._regex.sub(REDACT_PLACEHOLDER, obj.decode(errors="replace"))
        if isinstance(obj, dict):
            return {k: self._scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self._scrub(v) for v in obj)
        return obj


def install_on_handler(handler, opts):
    """Attach a ``PillarRedactingFilter`` to *handler*.

    Idempotent: if one is already attached, its opts snapshot is
    refreshed in place rather than double-installing.
    """
    if handler is None:
        return None
    for existing in handler.filters:
        if isinstance(existing, PillarRedactingFilter):
            existing._opts = opts or {}  # pylint: disable=protected-access
            existing.refresh()
            return existing
    flt = PillarRedactingFilter(opts)
    handler.addFilter(flt)
    return flt
