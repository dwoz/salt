# Boundary-redaction pillar masking (prototype)

_Status: prototype, pr-70413-alt. Branch: `pr-70413-alt-boundary-redaction`
off `origin/3008.x`. Local-only, not pushed. Audience: a maintainer
reviewing an alternative to PR #70413 and the
`MaskedDict`-at-rest 3008.x design._

## Why this exists

Issue [#69599](https://github.com/saltstack/salt/issues/69599) raised
that Salt's pillar data regularly leaks into logs: states print pillar
values when they fail, debug logging renders the compiled pillar dict,
and `pillar.items` / `pillar.get` default to real values. The
accepted-direction response in 3008.x
([PR #70413](https://github.com/saltstack/salt/pull/70413)) was to make
pillar data _mask itself at rest_: `RemotePillar.compile_pillar` wraps
the compiled dict in a `MaskedDict`/`MaskedList` whose `__repr__` /
`__str__` consult a `mask_pillar` `ContextVar` and redact every string
leaf. Explicit unmask points (`pillar.get(..., unmask=True)`, Jinja
renderer context, `pillar.raw`, etc.) call `expose()` to recover
real values.

That approach is invasive: `__pillar__` is no longer a plain dict,
every pillar func grew an `unmask=` kwarg with per-func default
semantics, third-party pillar consumers that formatted the dict got
silently masked, and the audit of leak points kept growing (VCOPS-98852
caught a `pillar.items` regression; 3008.x carries at least two
follow-ups to tune when masking applies). _This branch is an
alternative._ Instead of making the data lie about itself, we leave
`__pillar__` a plain dict (as it was in 3007.x and earlier) and move
redaction to a `logging.Filter` installed on each log handler. Masking
becomes an _output-boundary_ concern, not a type-system concern.

## The two approaches side by side

| Dimension                                | `MaskedDict` at rest (3008.x)                           | Boundary redaction (this prototype)                          |
|------------------------------------------|---------------------------------------------------------|--------------------------------------------------------------|
| What a contributor has to remember       | Pillar is a special kind of dict; call `expose()` at every real boundary | Pillar is a plain dict; operator declares what to redact     |
| Default behavior                         | Masked (operator opts out)                              | Unmasked (operator opts in via `pillar_mask_output=True`)    |
| Third-party pillar consumers             | Silently masked if they `str()` / `repr()` the dict     | Unchanged (plain dict in, plain dict out)                    |
| Kwarg churn                              | Nine pillar funcs grew `unmask=`                        | Nine pillar funcs' `unmask=` is deprecated / no-op           |
| Audit surface                            | Every stringification site is a potential leak point    | One logging filter at every handler is the surface           |
| Operator tunability                      | On/off; contextvar toggles                              | Three knobs: key-heuristic, path denylist, logger scope      |
| Upgrade path from 3007.x                 | Transparent only if code never stringifies pillar       | Transparent; operators flip one opt when they're ready       |
| Non-logging leak paths (e.g. return data)| Covered (dict-level masking)                            | **Not** covered -- see "Known limits" below                  |

## Prototype wiring

Eight commits on `pr-70413-alt-boundary-redaction`, one per phase:

1. **`salt/_logging/filters/pillar.py`** -- `PillarRedactingFilter`,
   a `logging.Filter` with three additive redaction strategies:
   - _Key-name heuristic_: dict values whose keys match
     `opts["pillar_mask_log_keys"]` (substring, case-insensitive) are
     replaced with `**********` in `record.args`.
   - _Path denylist_: `opts["pillar_mask_log_paths"]` lists pillar
     paths like `"database:root_password"`. The filter resolves them
     via `opts["pillar"]`, compiles a regex of the concrete values,
     and `re.sub`s both `record.msg` (if str) and string leaves of
     `record.args`.
   - _Logger scope_: `opts["pillar_mask_log_sources"]` is a tuple of
     logger-name prefixes the filter applies to. Default `("",)` means
     all loggers; set to e.g. `("salt.states.",)` to scope more tightly.
   Fast path: `pillar_mask_output=False` returns `True` immediately.
   `filter()` is wrapped in `try/except` so a bug in the scrubber can
   never silence logging.
   `install_on_handler(handler, opts)` attaches the filter idempotently.

2. **`salt/_logging/impl.py`** -- `setup_pillar_redacting_filter(opts)`
   iterates `logging.root.handlers` and installs the filter on each.
   `setup_logging()` calls it after console, file, and extended
   handlers are set up. Import is lazy so early-bootstrap logging never
   imports the filter module.

3. **`salt/pillar/__init__.py`** --
   `RemotePillar.compile_pillar` / `AsyncRemotePillar.compile_pillar`
   return a plain dict. The `salt.utils.secret.hide(ret_pillar)` wrap
   is gone; a one-line comment points here. `MaskedDict`,
   `MaskedList`, and the `mask_pillar` `ContextVar` stay in
   `salt/utils/secret.py` for callers that still construct them
   explicitly (CLI output formatter, tests).

4. **`salt/modules/pillar.py`** -- `get`, `items`, `item`, `data`,
   `obfuscate`, `ls`, `raw`, `ext`, and `keys` all emit a one-shot
   `DeprecationWarning` when the caller passes a non-None `unmask=`,
   and ignore the value. A process-wide `_UNMASK_DEPRECATION_EMITTED`
   set prevents log spam. Return values are always plain.

5. **`salt/state.py`** -- `_NoLogScrubFilter` + `_no_log_scope(low)`
   context manager. During a state body with `no_log: True`, a scoped
   filter is attached to every root handler that scrubs records whose
   logger name starts with `salt.states.*` or `salt.state`. The
   existing `salt.utils.secret.no_log_mask(ret)` post-call behavior is
   preserved -- this just widens the scrub to debug logging _inside_
   the state body.

6. **`salt/config/__init__.py`** -- adds `pillar_mask_log_keys`,
   `pillar_mask_log_paths`, `pillar_mask_log_sources` opts to
   `VALID_OPTS`, `DEFAULT_MINION_OPTS`, and `DEFAULT_MASTER_OPTS`.
   Flips the default of `pillar_mask_output` from `True` to `False`
   (restoring 3007.x semantics for operators who do not opt in).

7. **Tests** -- 40 pytest cases across four files; see commit
   `ce28cd75609`.

8. **This design doc.**

## Known limits

This prototype is deliberately narrower than the `MaskedDict` design:

- **Bypasses non-Salt logging.** `print()` statements, `sys.stderr.write`,
  tracebacks outside the Salt logger tree, and stdlib modules that
  format records before Salt's filter sees them are all out of scope.
  An operator who wires `logging.basicConfig(...)` with their own
  handler must also attach `PillarRedactingFilter` to that handler.
- **Non-logging leak paths.** State returns, orchestration events, the
  event bus, `salt.cli.highstate` stdout, `salt-call --output=json`,
  and salt-ssh roster stdout are _not_ covered. The 3008.x design
  caught these because the dict itself lied in `__repr__`; this one
  doesn't.
- **Key-heuristic false positives.** `"authorized_keys"` matches
  `"auth"` and gets scrubbed. Operators who need `authorized_keys` in
  logs must either narrow the heuristic or add negative patterns (not
  implemented in this prototype).
- **Operator must declare paths.** The path denylist is fully explicit.
  An operator who forgets to add `web:api_key` to
  `pillar_mask_log_paths` gets no protection for that value. The
  key-name heuristic is the belt; the path denylist is the
  suspenders; neither alone covers a careless pillar schema.
- **Pillar refresh race.** The filter caches the regex per
  `id(opts["pillar"])`. If opts["pillar"] is mutated in-place rather
  than rebound, the filter won't re-compile until the next log
  record -- which is fine, but means for exactly one window between
  rebind and next-record, concrete values from the _new_ pillar are
  not yet in the regex.

## Migration

- **`unmask=` kwarg.** Retained on all nine pillar funcs; now a no-op.
  First call per func per process emits a `DeprecationWarning`. In a
  future release the kwarg can be removed entirely; callers that
  passed `unmask=True` already get the real value today.
- **`MaskedDict` / `MaskedList` / `mask_pillar` `ContextVar`.** Kept
  in `salt/utils/secret.py` because the CLI output formatter and some
  test fixtures still reference them. They are no longer produced by
  the pillar compile path. A follow-up PR can delete them once their
  last call site is converted to explicit boundary redaction.
- **`salt.utils.secret.expose()`.** The call in
  `salt.modules.pillar.filter_by` is now a no-op (its input is already
  plain) but was retained for parity with 3008.x. It's safe to drop.
- **`pillar_mask_output` default.** Flipped `True` -> `False`.
  Operators who have a 3008.x deployment with pillar-masking turned
  on will see pillar values in logs again after upgrading to this
  prototype unless they set the opt explicitly. This is intentional:
  the new design pushes the decision into opt-in territory because
  the surface of what gets masked has changed (loggers, not objects).

## Open questions for reviewers

1. **Opt-in or opt-out default?** This prototype defaults
   `pillar_mask_output=False` so the migration is transparent. The
   3008.x default is `True` (masked). Which do we want shipped?
2. **State-body scope widening.** `_no_log_scope` catches records from
   `salt.states.*` and `salt.loaded.int.states.*`. Should it also
   scope to `salt.modules.*` logs emitted during a state body (e.g.
   `cmd.run` debug output)? The decision has perf implications --
   wider scope means more records go through the scrubber.
3. **Key-heuristic defaults.** The default list
   (`password`, `secret`, `token`, `credential`, `auth`, ...) is
   deliberately conservative. Should it be empty (operator must
   declare) to match the strictness of `pillar_mask_log_paths`?
4. **`MaskedDict` retirement timeline.** Is this prototype positioned
   as a replacement (delete `MaskedDict` after shipping) or as a
   parallel option (keep both, let the operator pick via opts)? The
   parallel-option route avoids the breakage in known limits above
   but doubles the surface to maintain.
5. **salt-ssh thin.** salt-ssh ships a trimmed tree; does the thin
   include `salt._logging.filters.pillar`? If not, salt-ssh runs don't
   get boundary redaction. Is that acceptable given salt-ssh output is
   usually local to an operator?
