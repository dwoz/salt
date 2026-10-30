# RemoteRunnerClient / RemoteWheelClient — Design

Branch: `wip-remote-cli-rebased`.

## Recommendation
**Option (a): new sibling classes `RemoteRunnerClient` and `RemoteWheelClient`.** Mirror the `RemoteClient(LocalClient)` precedent at `salt/client/__init__.py:2341`. Make them standalone (duck-typed to the same public method shape as `RunnerClient`/`WheelClient`), **not** subclasses — avoid inheriting local-master setup they don't want.

**Also revert the half-done option (b) already in the branch:** commits `077cf07e4f4`, `6ab13f3fb82`, `17991800043` added `_use_network_events` and `__role=client` branches inside `Runner.__init__`, `RunnerClient.__init__`, and `WheelClient.__init__`. That approach is already causing the fragility we want to avoid — `cmd_sync` in `mixins.py:170` has a `hasattr(self.event, "connect")` type-sniff; `Runner.__init__` late-imports `salt.config` to self-rewrite its own opts. Decide now, delete those branches, consolidate the remote path in one place.

## Why not (b)
- The current `_use_network_events` detection is partial option (b) and it's already drifting the local path. Any `if remote:` branch mid-init of a class whose primary production use is the local case is a regression risk.
- The LocalClient/RemoteClient precedent for the `salt` CLI is cleaner — grep `RemoteClient` and you see every remote-only line.
- Local-path test transfer: existing `RunnerClient`/`WheelClient` unit tests remain intact and test the master-local path. New tests target the remote classes. Zero overlap.

## Existing scaffolding we reuse
Agent 1's verification confirmed:
- **`WheelClient.master_call` is already remote-capable** — `salt/wheel/__init__.py:198-207` uses `ReqChannel.factory(crypt="clear", cmd="wheel")`. The `RemoteWheelClient` is mostly a wrapper around existing code.
- **`SyncClientMixin.cmd_sync`** already does `master_call` → event-wait → unwrap. Can inherit this unchanged for the runner path.

## Class surface — `RemoteRunnerClient` (new; location: `salt/runner.py` or `salt/runner/remote.py`)

Instantiated when `opts["__role"] == "client"`. **Does not** load `salt.loader.runner()` — there is no local runner module dict to introspect.

| Method | Behavior |
|---|---|
| `__init__(opts, context=None)` | Store `opts`, instantiate `ClientEventSubscriber(opts)` as `self.event`. No loaders, no `MasterMinion`. |
| `cmd(fun, arg, ..., full_return=False)` | Build low, delegate to `cmd_sync`. |
| `cmd_sync(low, timeout=None, full_return=False)` | `master_call(cmd="runner", **low)` → listen on `self.event` for `salt/run/<jid>/ret`. Inherits from `SyncClientMixin.cmd_sync`. |
| `cmd_async(low)` | `master_call(cmd="runner", **low)`; return `{jid, tag}`. |
| `master_call(**kwargs)` | `ReqChannel.factory(opts, crypt="clear", master_uri=...).send({"cmd": "runner", **kwargs})`. |
| `asynchronous(fun, low, user, pub=None)` | Call `cmd_async`. Drops the forking path (that's master-side). |
| `get_docs(arg=None)` | Remote via `fun="doc.runner"` through `cmd_sync`. Local introspection impossible without the loader. |
| `destroy()` | `self.event.destroy()`. |
| `functions` property | Raise `NotImplementedError("remote client has no local runner modules; use cmd_sync")`. |

### `RemoteRunner(RemoteRunnerClient)` — CLI entry
`run(full_return=False)` mirrors `Runner.run()` at `salt/runner.py:348`, drops eauth-prompting (that happens via `Resolver.cli()` before entering `run()`), builds `low = {"fun": opts["fun"], "arg": args, "kwarg": kwargs}`, calls `cmd_sync(low)`, prints via `display_output`.

## Class surface — `RemoteWheelClient` (new; location: `salt/wheel/__init__.py` or `salt/wheel/remote.py`)

| Method | Behavior |
|---|---|
| `__init__(opts, context=None)` | Store opts, `ClientEventSubscriber(opts)` as `self.event`. No `salt.loader.wheels()`. |
| `cmd(...)` | Build low, delegate to `cmd_sync`. |
| `cmd_sync(low, timeout=None, full_return=False)` | `master_call(cmd="wheel", **low)` — master returns `{tag, data}` **synchronously**; return `ret["data"]["return"]` directly. No event wait needed. |
| `cmd_async(low)` | `master_call(cmd="wheel", **low)`; return `{jid, tag}`. |
| `master_call(**kwargs)` | Same shape as existing `WheelClient.master_call` (which is already remote-capable). Only diff: use `opts["master"]` for the URI, not `opts["interface"]`. |
| `call_func(fun, **kwargs)` | Thin wrapper for backwards-compat; same as local. |
| `destroy()` | Teardown event subscriber. |

## Reply-shape alignment
- **Local `RunnerClient.cmd`** returns the direct Python return value of the runner.
- **`ClearFuncs.runner`** returns `{tag, jid}` only. Result arrives on `salt/run/<jid>/ret`. So `RemoteRunnerClient.cmd_sync` must send → get `{tag, jid}` → `subscribe_job(jid)` → wait for `salt/run/<jid>/ret` → unwrap `event["data"]["return"]`. **This logic already exists in `SyncClientMixin.cmd_sync` lines 208-234** — inherit it unchanged.
- **Wheel is different**: `ClearFuncs.wheel` returns `{tag, data: {return, success, jid}}` **synchronously**. `RemoteWheelClient.cmd_sync` returns `ret["data"]["return"]` directly, no event wait. `_run_cmd` in `salt/key.py:277-279` already does exactly this for the local wheel path.

## Event stream (`salt-run state.orchestrate` and friends)
The `ClientEventSubscriber` already supports streaming per-state events — `connect()`, `subscribe_job(jid)`, loop on `get_event(wait=..., full=True)` until the terminal `salt/run/<jid>/ret` arrives. Server-side `ClientEventPublisher` already filters `salt/run/`, `salt/wheel/`, `salt/job/` prefixes. **No new server code needed.**

**Timing gotcha**: `subscribe_job` must land before the first event is fired. Options:
1. **Speculative subscribe by tag prefix** before `master_call` — need the publisher to accept wildcard tag subscriptions.
2. **Subscribe to all jobs for this session**, filter client-side — simplest, matches how `LocalClient` listens.
3. **Replay buffer on publisher** — N recent events kept, replayed on subscribe. Overkill.

Recommend (2) initially — the per-session filter means a client only sees its own job events anyway; filtering jid client-side is trivial.

## CLI dispatch
`salt/cli/run.py:23`:
```python
import salt.runner
if self.config.get("__role") == "client":
    runner = salt.runner.RemoteRunner(self.config)
else:
    runner = salt.runner.Runner(self.config)
```

`salt/cli/key.py:24` — inside `KeyCLI.__init__`:
```python
if opts.get("__role") == "client":
    self.client = salt.wheel.RemoteWheelClient(opts)
else:
    self.client = salt.wheel.WheelClient(opts)
```

Everything downstream goes through `self.client.cmd_sync(low)` — no further CLI changes needed.

## Implementation sequencing
1. **`RemoteWheelClient` first** — simpler (no event loop needed in the common sync path). Validate end-to-end with `salt-key -L`.
2. **`RemoteRunnerClient` + `RemoteRunner`**. Reuse `SyncClientMixin.cmd_sync` for event-wait.
3. **Switch CLI dispatch** in `salt/cli/run.py` and `salt/cli/key.py`.
4. **Revert `__init__` branches** in `salt/runner.py` (lines 50-98, 306-321) and `salt/wheel/__init__.py` (lines 47-119). `_use_network_events` detection becomes dead code.
5. **Simplify `salt/client/mixins.py:188-227`** — collapse the `hasattr(event, "connect")` sniffing. Dispatch polymorphically by subclass overriding `_get_event()`.

## Option (a) vs (b) comparison

| | (a) sibling classes | (b) branch inside existing |
|---|---|---|
| LoC | ~250 new, ~80 removed (revert embedded branches) | ~60-100 kept (what's already there) |
| Test transfer | Existing tests intact for local path; new tests target remote. Zero overlap, high isolation | Existing tests must run under both roles via parametrization; mocking is harder |
| Future readability | Name tells you what it does; grep finds the remote path | Reader must trace `_use_network_events` + `__role` through init, event helpers, mixin |
| Blast radius on local | Zero — local path literally unchanged | Non-zero — local salt-run is production hot path; any mis-typed `if` regresses it |
| Precedent fit | Matches `RemoteClient(LocalClient)` | Diverges |

## Uncertainties
- `doc.runner` under `__role=client`: need to verify callable without eauth; if not, either add a no-ACL endpoint or accept `salt-run --doc` unavailable for remote.
- `_proc_function_remote` in `AsyncClientMixin:526` is the forking variant used by `WheelClient.cmd_async`. Remote classes **must not fork** — override `cmd_async` to just `master_call` (don't inherit the mixin's version).
- LTS sync-wheel path check: `master.py:5211` is the common case; async opt-in branch returns differently. `RemoteWheelClient.cmd_sync` should fall back to event-wait if the response is `{tag, jid}` without `data`.

## Files to touch
- `salt/runner.py` — new `RemoteRunnerClient` + `RemoteRunner`; revert embedded `_use_network_events` branches in `Runner`/`RunnerClient`.
- `salt/wheel/__init__.py` — new `RemoteWheelClient`; revert embedded branches in `WheelClient`.
- `salt/client/mixins.py:188-227` — simplify.
- `salt/cli/run.py:23` — dispatch.
- `salt/cli/key.py:24` — dispatch.
