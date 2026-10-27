# Diagnosis: topology transport wire gap

Branch `dwoz/master-topology` @ `3b2a28fdaf0`.
Target: `tests/pytests/scenarios/topology/test_cli_e2e_scenarios.py::test_scenario_cli_c_at_peer_publish_round_trip_with_peered_topology`.

## 1. Actual error

```
AssertionError: salt CLI exited 2; stdout='{}\n'
                stderr='ERROR: No return received\n'
```

East's publish daemon publishes `C@west test.ping` but nothing comes back; `salt` CLI reports no returns. The salt log shows east repeatedly logging:

```
WARNING  salt.transport.tcp:tcp.py:2939 [SaltMaster(id='topology-east')]
   TCP Message Client encountered an exception while connecting to
   127.0.0.1:41867: StreamClosedError('Stream is closed'),
   will reconnect in 1 seconds
```

41867 is west's `topology_link_port`.

## 2. Which side is failing

Both ends miss, but the root cause is **server-side**. Instrumented `log.warning` statements in `AsyncReqChannel.send` (client), `ReqServerChannel.handle_message` (server), and `_handle_inbound_channel` (link_manager): **none fired**. More importantly, nothing from `link_manager.start` ever fired at DEBUG — not the "topology disabled; start is a no-op" branch, not the "called twice" warning, not the per-peer link schedule logs. The hook in `MasterPubServerChannel._publish_daemon` (`salt/channel/server.py:3190-3196` — `spawn_callback(link_manager.start, opts, io_loop)`) is scheduled but the coroutine body **never executes**. No inbound ReqServerChannel ever binds `topology_link_port`, so east's outbound TCP client slams into nothing and `StreamClosedError`'s forever.

## 3. The mismatch

Not an envelope/crypt/payload shape issue — it's a **port-binding issue upstream of the wire**. `spawn_callback` on a tornado `IOLoop` with a plain `async def` coroutine (asyncio-native) does not drive the awaitable; `link_manager.start` is scheduled but never stepped. The listener is never opened, the handshake never leaves east's `AsyncReqChannel._uncrypted_transfer` as a completed send.

## 4. Fix sketch (cheapest first)

1. Wrap the schedule in `asyncio.ensure_future(link_manager.start(...))` via `aioloop(self.io_loop).call_soon_threadsafe` instead of tornado `spawn_callback` (~5 lines, mirrors existing `aioloop(io_loop)` pattern used inside `start` itself).
2. Convert `link_manager.start` + helpers to a tornado-native entrypoint using `tornado.gen.coroutine` / `IOLoop.add_callback(functools.partial(...))` (larger refactor).
3. Spawn a dedicated asyncio event loop thread for topology (biggest surface, worst isolation story).
