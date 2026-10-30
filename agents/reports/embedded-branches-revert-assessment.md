# Embedded `_use_network_events` Branches — Revert Assessment

Branch: `wip-remote-cli-rebased`.

## UNSAFE to revert in isolation
Reverting the three commits without replacement sibling classes breaks the remote CLI path and fails 14 test assertions. **Land `RemoteRunnerClient` + `RemoteWheelClient` + CLI dispatch first, then revert.**

## The three commits (what they do)

| Commit | File(s) | Net LOC | Purpose |
|---|---|---|---|
| `077cf07e4f4` | `salt/client/mixins.py:170-237` | +19 | Polymorphic event dispatch in `cmd_sync()`; `hasattr(self.event, "connect")` sniff |
| `6ab13f3fb82` | `salt/runner.py:46-98`, `salt/wheel/__init__.py:47-119` | +95 | `RunnerClient`/`WheelClient.__init__` branches on `_use_network_events`; new `_should_use_network_events()` |
| `17991800043` | `salt/utils/client_event.py:412-423` | +13 | `ClientEventSubscriber.destroy()` uses `linger=0` + `context.destroy(linger=0)` (the hang fix) |

**Total removed by revert: +127 net lines.** New sibling classes: ~330 net new. Net final: **+200** (matches Agent 4's ~250/−80 estimate).

**⚠️ One of the three is NOT embedded-branch logic**: `17991800043` is the ZMQ teardown-hang fix we debugged with py-spy. **Keep that one** — don't revert the destroy() change. The design agent lumped them together; separating them: `077cf07e4f4` + `6ab13f3fb82` are the branching logic to revert; `17991800043` is pure bugfix and should stay.

## Classified references

### (R) To revert
- `salt/runner.py:50-98` — `RunnerClient` init branch + `_should_use_network_events()`.
- `salt/wheel/__init__.py:71-119` — `WheelClient` init branch + `_should_use_network_events()`.
- `salt/client/mixins.py:186-237` — `cmd_sync()` polymorphic branch + `hasattr` sniff.

### (K) Load-bearing, keep
- `salt/client/__init__.py:2359` — `RemoteClient.__init__` sets `__role="client"` on its own opts. Not touched by the three commits; this is RemoteClient's own path.
- `salt/client/__init__.py:2371` — `self._use_network_events = True` hardcoded on `RemoteClient`. Not conditional. Keep.
- `salt/client/__init__.py:2446, 2494` — `RemoteClient.pub()` branches on `self._use_network_events` and `_use_network_events and listen`. Keep.
- `salt/crypt.py:1112, 1569, 1583` — `__role == "client"` selects `client.pem` vs `minion.pem`. Keep.

### (D) Dead after revert
- `salt/client/mixins.py` `hasattr(self.event, "connect")` sniff — after revert, only `RemoteClient` reaches this with a `ClientEventSubscriber`, so the True branch is only taken by one caller. Keep as a guard; simplify to polymorphic `_get_event()` in a follow-up.

## Test fallout
**14 assertions across 3 files depend on `_use_network_events`:**
- `tests/pytests/scenarios/client/test_remote_cli_containers.py` — 8 assertions (lines 275, 344, 409, 1575, 1608, …).
- `tests/pytests/scenarios/client/test_remote_cli_cluster.py` — 4 assertions.
- `tests/pytests/integration/client/test_remote_cli_execution.py` — 6 assertions + a test method named `test_all_remote_clients_use_network_events`.

Example pattern: `assert runner._use_network_events is True`.

**Migration**: replace flag checks with `isinstance(client, RemoteRunnerClient)` / `isinstance(event, ClientEventSubscriber)` after the sibling classes land. Zero coverage lost.

## The `hasattr(self.event, "connect")` sniff
`salt/client/mixins.py:189-193`. Compensates for the fact that an in-process event (`salt.utils.event.MasterEvent`) doesn't have a `.connect()` method, but `ClientEventSubscriber` does. After the revert, only `RemoteClient` ever reaches this with a network event. Keep as-is until follow-up:

```python
class ClientEventSubscriber:
    # ...  has .connect(), .cpub, .connect_pub, .close_pub, .subscribe, .unsubscribe

class RunnerClient(SyncClientMixin, AsyncClientMixin):
    # cmd_sync uses self.event   ← IPC
class WheelClient(...):
    # cmd_sync uses self.event   ← IPC
class RemoteClient(LocalClient):
    # overrides _setup_network_events to swap self.event for ClientEventSubscriber   ← network
```

Collapse to polymorphism: give the mixin a `_get_event()` hook; `RemoteClient` overrides to return the network subscriber. Not urgent.

## Risk by scenario

| Scenario | Outcome |
|---|---|
| **A — Revert `077cf07e4f4` + `6ab13f3fb82` in isolation** | ❌ Remote `salt-run`/`salt-key` break. 14 test assertions fail. |
| **B — Sibling classes land first, then revert** | ✅ Zero intermediate breakage. Sibling classes coexist during transition. |
| **C — Full flip in one PR** | ⚠️ Doable but review-heavy. Easier to split as (B). |

## Recommended sequence
1. **Implement `RemoteRunnerClient` + `RemoteRunner`** in `salt/runner.py` (ClientEventSubscriber in `__init__`, inherit `cmd_sync` from `SyncClientMixin`, override `_proc_function_remote` to drop the fork, override `master_call` to use `opts["master"]` URI).
2. **Implement `RemoteWheelClient`** in `salt/wheel/__init__.py` (same pattern; `cmd_sync` returns `ret["data"]["return"]` directly — wheel is synchronous on the master).
3. **CLI dispatch**: one-line change in `salt/cli/run.py:23` and `salt/cli/key.py:24` keyed on `opts["__role"] == "client"`.
4. **Test migration**: replace `_use_network_events` assertions with isinstance checks against the new classes. Rename `test_all_remote_clients_use_network_events`.
5. **Revert `077cf07e4f4` + `6ab13f3fb82`** (NOT `17991800043` — that's the destroy() hang fix, keep it).
6. **Optional follow-up**: collapse `mixins.py` `hasattr` sniff via polymorphic `_get_event()`.

## LOC impact
- Revert: **−108 net** (just the two branching commits; destroy() fix stays).
- Sibling classes: **+330 net** new.
- Final: **+222 net** on the branch; **−40** if the follow-up simplification lands → **+182** final.
