"""
salt._logging.filters.pillar
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``PillarRedactingFilter``: a ``logging.Filter`` implementing the
boundary-redaction prototype that replaces the 3008.x
``MaskedDict``-at-rest design with scrubbing at the point where log
records leave the process.

Three redaction strategies are available and additive.  All three are
driven by opts and are no-ops when the feature is disabled::

    opts["pillar_mask_output"]        bool, default False  -- master switch
    opts["pillar_mask_log_keys"]      iterable[str]        -- key-name heuristic
    opts["pillar_mask_log_paths"]     iterable[str]        -- pillar-path denylist
    opts["pillar_mask_log_sources"]   iterable[str]        -- logger-name prefixes

Design intent:

* ``pillar_mask_output=False`` must be a *zero-cost* passthrough; we
  must not slow down the hot logging path for operators who have not
  opted in.
* ``filter(record)`` must never raise; a redaction bug must not take
  logging offline.
* Mutating ``record.msg`` / ``record.args`` is safe only because this
  filter runs per-handler (see ``install_on_handler``).  If the same
  record is formatted by multiple handlers, each handler's filter runs
  before that handler's formatter, so each handler sees the already
  scrubbed values -- which is what we want.
"""

from __future__ import annotations

import logging
import re
import threading

REDACT_PLACEHOLDER = "**********"

#: Default key-name heuristic.  Substring match, case-insensitive.
DEFAULT_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "private_key",
    "privkey",
    "auth",
)


def _as_tuple(value):
    """Normalize an opts value that may be str, list, tuple, or None."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


def _resolve_pillar_path(pillar, path, delimiter=":"):
    """Return ``pillar[a][b][c]`` for a path like ``"a:b:c"``, or ``None``."""
    if not pillar or not path:
        return None
    node = pillar
    for part in path.split(delimiter):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _flatten_scalars(value, out):
    """Collect str/bytes/int/float leaf values into ``out`` (a list)."""
    if isinstance(value, dict):
        for v in value.values():
            _flatten_scalars(v, out)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            _flatten_scalars(v, out)
    elif isinstance(value, (str, bytes)):
        if value:
            out.append(value)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        out.append(str(value))


class PillarRedactingFilter(logging.Filter):
    """Boundary-redaction of pillar values on outbound log records.

    Install with ``install_on_handler(handler, opts)`` or by constructing
    the filter directly and calling ``handler.addFilter(filter)``.  The
    filter reads its configuration from ``opts`` at construction time;
    opts changes (e.g. a pillar refresh adding a new sensitive path) are
    picked up lazily via the ``refresh()`` method.
    """

    def __init__(self, opts=None):
        super().__init__()
        self._lock = threading.Lock()
        self._opts = opts or {}
        self._enabled = False
        self._sensitive_keys = ()
        self._paths = ()
        self._sources = ("",)
        self._value_regex = None
        self._pillar_snapshot_id = None
        self.refresh()

    # ---- configuration --------------------------------------------------

    def refresh(self):
        """Re-read opts.  Cheap; called by install_on_handler at setup."""
        with self._lock:
            self._enabled = bool(self._opts.get("pillar_mask_output", False))
            self._sensitive_keys = tuple(
                k.lower() for k in _as_tuple(self._opts.get("pillar_mask_log_keys"))
            )
            self._paths = _as_tuple(self._opts.get("pillar_mask_log_paths"))
            sources = _as_tuple(self._opts.get("pillar_mask_log_sources")) or ("",)
            self._sources = sources
            self._compile_value_regex()

    def _compile_value_regex(self):
        """Build a regex of concrete pillar values from the path denylist."""
        pillar = self._opts.get("pillar") or {}
        values = []
        for path in self._paths:
            node = _resolve_pillar_path(pillar, path)
            if node is None:
                continue
            _flatten_scalars(node, values)
        # De-dup; keep longer values first so overlapping matches prefer
        # the longer one (e.g. "hunter2hunter" before "hunter2").
        uniq = sorted(
            {v.decode(errors="replace") if isinstance(v, bytes) else v for v in values},
            key=len,
            reverse=True,
        )
        if uniq:
            self._value_regex = re.compile("|".join(re.escape(v) for v in uniq))
        else:
            self._value_regex = None
        self._pillar_snapshot_id = id(pillar)

    # ---- the hot path ---------------------------------------------------

    def filter(self, record):
        # Fast-path: feature disabled -> let the record through untouched.
        # This branch must stay O(1).
        if not self._enabled:
            return True

        # Logger-scope: only scrub records whose logger name starts with
        # one of the configured prefixes.  Empty-string prefix matches
        # everything.
        name = record.name or ""
        if not any(name.startswith(prefix) for prefix in self._sources):
            return True

        try:
            # Opportunistic refresh if pillar was swapped under us
            # (e.g. refresh_pillar() rebuilt opts["pillar"]).
            if id(self._opts.get("pillar")) != self._pillar_snapshot_id:
                self._compile_value_regex()

            # Strategy 1: pillar-path denylist -- regex-sub concrete
            # values in msg/args.
            if self._value_regex is not None:
                if isinstance(record.msg, str):
                    record.msg = self._value_regex.sub(REDACT_PLACEHOLDER, record.msg)
                record.args = self._scrub_args_values(record.args)

            # Strategy 2: key-name heuristic -- redact dict values whose
            # keys look sensitive.
            if self._sensitive_keys:
                record.args = self._scrub_args_keys(record.args)
        except Exception:  # pylint: disable=broad-except
            # A bug in the filter must never silence logging.  Fall back
            # to letting the record through unchanged.
            return True
        return True

    # ---- internals ------------------------------------------------------

    def _looks_sensitive(self, key):
        if not isinstance(key, str):
            return False
        k = key.lower()
        return any(needle in k for needle in self._sensitive_keys)

    def _scrub_dict_keys(self, value):
        """Return a copy of *value* with sensitive keys' values redacted.

        Does not recurse into containers whose own keys are safe -- a
        shallow walk is cheap and mirrors how operators actually write
        pillar structures: ``{"db": {"root_password": "..."}}`` is
        redacted because ``root_password`` matches, not because ``db``
        does.
        """
        if isinstance(value, dict):
            new = {}
            for k, v in value.items():
                if self._looks_sensitive(k):
                    new[k] = REDACT_PLACEHOLDER
                else:
                    new[k] = self._scrub_dict_keys(v)
            return new
        if isinstance(value, (list, tuple)):
            cls = type(value)
            return cls(self._scrub_dict_keys(v) for v in value)
        return value

    def _scrub_args_keys(self, args):
        if args is None:
            return args
        if isinstance(args, dict):
            return self._scrub_dict_keys(args)
        if isinstance(args, tuple):
            return tuple(self._scrub_dict_keys(a) for a in args)
        return args

    def _scrub_scalar_values(self, value):
        """Apply the path-value regex to any str leaves inside *value*."""
        if self._value_regex is None:
            return value
        if isinstance(value, str):
            return self._value_regex.sub(REDACT_PLACEHOLDER, value)
        if isinstance(value, dict):
            return {k: self._scrub_scalar_values(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            cls = type(value)
            return cls(self._scrub_scalar_values(v) for v in value)
        return value

    def _scrub_args_values(self, args):
        if args is None:
            return args
        if isinstance(args, dict):
            return self._scrub_scalar_values(args)
        if isinstance(args, tuple):
            return tuple(self._scrub_scalar_values(a) for a in args)
        return args


def install_on_handler(handler, opts):
    """Attach a ``PillarRedactingFilter`` bound to *opts* to *handler*.

    Idempotent: if a ``PillarRedactingFilter`` is already attached to
    *handler*, its opts snapshot is refreshed in place instead of
    double-installing.
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
