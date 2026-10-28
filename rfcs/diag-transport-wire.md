# Diagnosis: topology transport wire gap

Branch `dwoz/master-topology`.
Target: `tests/pytests/scenarios/topology/test_cli_e2e_scenarios.py::test_scenario_cli_c_at_peer_publish_round_trip_with_peered_topology`.

## 1. Actual error

```
AssertionError: salt CLI exited 2; stdout='{}\n'
                stderr='ERROR: No return received\n'
```

East's publish daemon publishes `C@west test.ping` but nothing comes back; the CLI reports no returns.

## 2. There are two bugs on this path, not one

Instrumenting `link_manager.start`, `PeerMasterLink.connect`, and `_handle_inbound_channel` with WARNING-level probes revealed two separate failures. Fixing the first makes the second visible; prior to the first fix, the second was invisible because no code reached it.

### Bug A — the coroutine body never ran *(FIXED)*

`MasterPubServerChannel._publish_daemon` (`salt/channel/server.py`) was scheduling the topology bring-up with:

```python
self.io_loop.spawn_callback(link_manager.start, opts, io_loop)
```

`spawn_callback` on a tornado `IOLoop` hands tornado a bare coroutine object and never actually drives the body — the `async def` is scheduled but never stepped. Fixed by switching to the asyncio-native pattern already used ~100 lines above for `self.transport.publisher(...)`:

```python
aio_loop = salt.utils.asynchronous.aioloop(self.io_loop)
aio_loop.create_task(link_manager.start(self.opts, self.io_loop))
```

Verified by DIAG probe: `start()`'s body now enters, step 1 loads cfg (`enabled=True`, peers list correctly populated after restart), step 7 opens `_LISTENER`, step 8 schedules `_connect_and_register` for each peer, `PeerMasterLink.connect()` is entered, and the handshake request is handed off to `AsyncReqChannel.send()`.

### Bug B — the inbound listener never binds to `topology_link_port` *(unresolved)*

With Bug A fixed, east now actually sends a `topology/_master_auth` handshake toward west's `topology_link_port`. The request produces no error on the client side (no `handshake transport error` WARNING fires); it just awaits a reply that never comes. West's `_handle_inbound_channel` DIAG never fires, so the request is not reaching the dispatcher.

Root cause: `_open_inbound_listener` creates a `ReqServerChannel` whose transport is the stock zmq `RequestServer` (`salt/transport/zeromq.py:492`). Its `post_fork` (line 901) **does not bind** `ret_port` — it `connect()`s the server's REP socket to the master's existing `workers.ipc`:

```python
self.w_uri = self.get_worker_uri(pool_name=pool_name)
log.info("Worker binding to socket %s", self.w_uri)
self._socket.connect(self.w_uri)
```

The external `tcp://interface:ret_port` is bound by the separate `MWorkerQueue` process running `zmq_device()` (line 519), which we never start for the topology link port. Result:

1. No process is listening on `topology_link_port`, so east's connect sits in TCP retry.
2. Worse, our in-process `ReqServerChannel` **connected to the master's regular `workers.ipc`** — if traffic routed to it, our `_handle_inbound_channel` would receive non-topology master commands (publish, auth, etc.) and respond with an empty cleartext ret. The dispatcher's `cmd` whitelist (`_DISPATCH_TABLE`) would drop them, but we'd still be competing with real MWorkers for the master's own command stream.

The `_LISTENER=<salt.channel.server.ReqServerChannel object>` DIAG at the end of step 7 is misleading: a `ReqServerChannel` is returned, but it has no bound socket on `topology_link_port`.

### Why the original diagnosis missed Bug B

The first-pass diagnosis stopped at Bug A because no code reached the point where Bug B would manifest. The suggested fix (`call_soon_threadsafe(ensure_future(...))`) also happened to not drive the body, so a second attempt to validate didn't surface Bug B either. Only once `aio_loop.create_task(...)` actually drove `start()` to step 7 and beyond did the real wire gap become observable.

## 3. What's needed to close Bug B

The pooled-master architecture assumes an `MWorkerQueue` process in front of every `ret_port`. For topology we need one of:

1. **Dedicated MWorkerQueue for topology.** Add a second `MWorkerQueue` subprocess started from topology `pre_fork`, binding `topology_link_port` and routing to a topology-specific worker IPC. Minimum code surface, maximum process-count cost (one extra process per master).
2. **Direct TCP server for topology.** Replace `ReqServerChannel.factory` with a hand-rolled `TCPServer`/`TCPPuller`-style listener that owns the port, decrypts cleartext topology commands, and feeds them to `_handle_inbound`. Smaller runtime footprint; duplicates some crypto/envelope handling.
3. **Direct-bind mode on `RequestServer`.** Add a flag that makes `zeromq.RequestServer.post_fork` bind `tcp://interface:ret_port` directly (same shape as `MWorkerQueue`'s `zmq_device`) instead of connecting to `workers.ipc`. Fits the "non-pooled plain ReqServerChannel" story the current `_open_inbound_listener` already paints; biggest cross-cutting surface.

(2) is probably the cleanest for the topology surface since the handshake payload is tiny, strictly clear-text, and uses its own crypto on top. (3) is the "correct" fix for the zmq transport abstraction but touches a load-bearing path shared with the regular master.

## 4. Status

- Bug A: fixed in `salt/channel/server.py:3187-3213` (`spawn_callback` → `aio_loop.create_task`). Verified by DIAG probe that `start()` now runs end-to-end.
- Bug B: fixed. Took option 3 — added a `direct_bind` kwarg to `salt.transport.zeromq.RequestServer.post_fork` and opted into it from `salt.topology.link_manager._open_inbound_listener`. The `RequestServer` now binds the REP socket directly to `tcp://interface:ret_port` when `direct_bind=True`, instead of connecting to an upstream `MWorkerQueue`'s `workers.ipc`. The pre-fork TCP reservation in `link_manager.pre_fork` was dropped — it held a bound fd in the parent process that caused the publish daemon's zmq bind to fail with EADDRINUSE.
- Bug C: fixed. `_handle_master_auth` now registers a server-side `_InboundLink(peer_cluster_id, session_generation, session_aes)` into `_INBOUND[cluster_id]` before returning `ok`. Previously the handshake replied with the session AES but never populated the server-side link record, so every follow-on `topology/pub|ret|evt|sum` frame failed `_verify_session`.
- Bug D: fixed. `TopologyRouter.__init__` reads `self.cluster_id` from `opts["cluster_id"]`, which is unset on a solo (non-3008.x cluster) master. Without this projection, every outbound envelope went on the wire with `origin=None, path=[None]` and the peer rejected it as a tampered path. `start()` now projects `cfg.cluster_id` into a `projected_opts` dict and uses it for both the router and the per-peer `PeerMasterLink` construction.
- Bug E: fixed. `PeerMasterLink.connect` returned immediately on transport exception (no retry), so a peer master briefly unreachable at connect time (restart window, listener still binding, network blip) left the link stuck in `DISCONNECTED` forever. The transport-error branch now applies the same backoff+retry cycle as the `status == "retry"` branch, and re-opens the `AsyncReqChannel` between attempts (zmq REQ sockets go into a bad state after a failed send; rebuilding resets the socket FSM).
- Bug F: fixed. zmq REQ-REP's client-side socket silently reconnects when the peer master restarts — the queued send is delivered to the fresh REP server, and no transport error signals the lost session back to the sender. The sender never re-triggers `_master_auth`, and the receiver's fresh `_INBOUND` dict rejects everything until re-handshake. `_handle_inbound` now lazy-registers `_INBOUND[origin]` when (a) the frame is non-handshake, (b) the peer key is accepted on disk (`masters/<cluster_id>.pub`), (c) no in-process record exists. The accepted-key check is the auth boundary — a stranger with no accepted key cannot reach `topology_link_port` in the first place.
- Bug G: fixed, four pieces:
  1. `salt.topology.link_manager._dispatch_pub` now detects return-shaped loads (`cmd='_return'` with `jid`/`id`/`return`) and routes them to `salt.utils.job.store_job` on the origin, rather than fanning them out to local minions as a fake publish. West currently forwards minion-return events as `topology/pub` frames through the event-forwarding mechanism (the `topology/ret` path would require an event-bus subscriber on the publish daemon that doesn't exist yet — tracked separately); this short-circuit covers the shape in play.
  2. `_store_forwarded_return` also calls the returner's `save_minions(jid, [minion_id])` so `<jid>/.minions.p` includes the returning minion. Without this, east's `.minions.p` stays empty (east has no local minion matching `C@west`) and `local_cache` lookup can't key-match the stored return.
  3. `salt.client.LocalClient._check_pub_data` no longer short-circuits with "No minions matched the target" when `topology_enabled` is set — the originating master can't know peer-cluster minion ids at pub-time, so empty-local-match is the normal case for a topology-forward-only publish.
  4. `salt.client.LocalClient.get_iter_returns` no longer early-exits on the `len(found) >= len(minions)` (`0 >= 0 → True`) check when `topology_enabled` AND `minions` is empty. The loop now waits the full timeout window so cross-cluster returns streaming in asynchronously have a chance to arrive.
- End-to-end verified: `salt -C 'C@west' test.ping` on east returns `{"web-west": True}`; the previously-xfail test now passes with the xfail marker removed.
- Known cost: cross-cluster CLI publishes with no local-match minions wait the full `timeout` window (default 5s for `test.ping`) because the CLI can't know when the last peer-cluster return has arrived. Follow-up: fire a `salt/job/<jid>` event with the expected minion set when the first forwarded return arrives, so `get_iter_returns`' built-in all-minions-returned early-exit can fire.
