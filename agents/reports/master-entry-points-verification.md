# Master-Side Entry Points for Remote Runner/Wheel — Verification

Branch: `wip-remote-cli-rebased`.

## Dispatcher routes (confirmed)
- `salt/master.py:5150-5158` — `ClearFuncs.get_method()` dispatches on `clear_load["cmd"]`.
  - `cmd="runner"` → `ClearFuncs.runner()` (`master.py:5272`)
  - `cmd="wheel"` → `ClearFuncs.wheel()` (`master.py:5376`)

## Load shape (both)
Transport envelope (set automatically by `ReqChannel._package_load`, `salt/channel/client.py:188-197`):
```
{"enc": "clear", "load": {...inner_load}, "version": 3}
```
Inner load:
- `cmd`: `"runner"` or `"wheel"` (set by `master_call()`, `salt/client/mixins.py:159`)
- `fun`: function name
- `kwarg`: dict (shaped as `{"arg": [...], "kwarg": {...}}`)
- One of:
  - `eauth` + `username` + `password`
  - `token`
  - `user` + `key`

## Reply shape
- **Runner**: `{"tag": "salt/run/<jid>", "jid": "<jid>"}` from `master.py:5272`. Actual return arrives on the event bus as `salt/run/<jid>/ret` — sync requires event wait.
- **Wheel**: `{"tag": "salt/wheel/<jid>", "data": {"return": <result>, "success": bool, ...}}` — **synchronous**; master blocks on `wheel_.call_func` (`master.py:5347`, `masterapi.py:1416`). No event wait needed for sync wheel.
- Error: `{"error": {"name": "...", "message": "..."}}`.

## Salt-api `/run` driver (confirmed)
Trace: `netapi/__init__.py:173-211` → `NetapiClient.runner()`/`.wheel()` → `RunnerClient.cmd_sync()`/`WheelClient.cmd_sync()` → `_reformat_low()` → `master_call()` (`mixins.py:209`) → `ReqChannel.send({"cmd": "runner"|"wheel", ...})`. Same `cmd` keys we need.

## Existing remote code paths
- **`WheelClient` ALREADY has a remote path** — `salt/wheel/__init__.py:198-207` uses `ReqChannel.factory(crypt="clear")` to send to master's `ret_port` with `load["cmd"] = self.client`. For the `RemoteWheelClient` design this is largely a wrapper around existing code.
- **`RunnerClient` does NOT have a remote path.** Everything calls `RunnerClient.asynchronous()` in-process on the master (`master.py:5272`, `5997`). Build from scratch.

## Gotchas
| Item | Status |
|---|---|
| Clear port TLS | Configurable via `ssl:` opts; same socket for AES and clear; no per-command TLS selection |
| `enc` field | ReqChannel sets `enc: "clear"` automatically — client doesn't manage it |
| Token caching | Isolated per `eauth_tokens.cache_driver`; no per-request side effects |
| Sensitive field stripping | Sync and async both pop auth creds from load before execution (`master.py:5258-5260, 5334-5336, 5985-5986, 6040-6041`) |

**Bottom line:** no mystery here. The server-side is exactly what salt-api already drives. Only the client-side `RunnerClient` needs net-new network transport code.
