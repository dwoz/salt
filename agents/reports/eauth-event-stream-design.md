# Event-Stream Authentication for Remote Eauth Clients — Design

Branch: `wip-remote-cli-rebased`. Primary file: `salt/utils/client_event.py`.

## Recommendation
**Option (a): add an `action="auth_token"` branch to the existing REQ/REP auth socket.** Reuse `LoadAuth.authenticate_token`; key subscription filter by a **per-session synthetic ID** (not token value, not username).

~40–80 LOC in `client_event.py` + ~30 in subscriber. Preserves the streaming UX we built the whole feature around. Stacks cleanly on PKI.

Reject option (b) — polling `jobs.lookup_jid` loses `salt/run/*` sub-events and worse latency. Reject option (c) — tunneling through salt-api means owning two subscribers in the CLI.

## Why it works

### Token cache is transport-agnostic and reachable from the publisher
- Mint: `LoadAuth.mk_token` → `localfs` driver stores msgpack dict under `opts["token_dir"]` (= `cachedir/tokens`).
- Validate: `authenticate_token` → `get_tok(load["token"])` → `localfs.get_token` reads the file. Checks `expire`.
- The publisher runs as a child of the master via `process_manager.add_process` (`master.py:1243`), inherits opts and uid. Instantiating `LoadAuth(opts)` in the publisher is symmetric with how `MWorker` does it.

### Subscription filter choice
- **Token** (bad): bearer credential as the SUB frame; the ZMQ pub-sub prefix is sent in the clear to the publisher.
- **Eauth username** (bad): collides across concurrent sessions, logout doesn't revoke the other.
- **Per-session synthetic ID** (recommended): server mints `session_id = secrets.token_urlsafe(16)` on `auth_token` success, returns it, client uses it as SUB prefix. Clean revocation on expire.

### ACL — mirror the master's publish gate
On `auth_token` success:
1. Call `self.loadauth.get_auth_list(load, token=tdata)` — same code path master uses on publish.
2. Cache `clients[session_id] = {"authorized": True, "auth_mode": "token", "username": ret["username"], "auth_list": ret["auth_list"], "jobs": set(), "expire": tdata["expire"]}`.
3. In `_publish_filtered_event`, in addition to the existing per-JID check, evaluate the event's `fun`/`tgt`/`tgt_type` against `auth_list` via `self.loadauth.ckminions.auth_check(...)`. Same gate the master applies before enqueueing a job.
4. PKI clients continue through the existing `ClientACL` branch — the two coexist by setting `auth_mode: "pki"|"token"` in `clients[id]` and branching in the filter.

## Protocol
Auth request: `{"action": "auth_token", "token": <salt-token>, "eauth": <driver>}`.

Server flow: `load = {"token": token, "eauth": eauth}` → `check_authentication(load, "token")` → mint `session_id` → return `{"authorized": True, "session_id": session_id}`.

Subscriber: use `session_id` as SUB filter instead of `client_id`. Carry `session_id` on `subscribe_job`.

Publisher: check `expire` on each publish; evict expired sessions.

## Open items / assumptions
- `keep_acl_in_token=False` deployments: `get_auth_list` already handles both cases.
- The REP auth socket is already trusted to carry bearer tokens; if the deployment requires wire-level encryption, the auth port needs the same TLS/CurveZMQ treatment the PKI path gets. Out of scope here — flag for the TLS story.
- Token revocation (`rm_token`): best-effort via `expire` on next publish. Dedicated watcher is follow-up.

## Comparison table

| Axis | (a) token branch | (b) polling | (c) salt-api SSE |
|---|---|---|---|
| LoC | 70–110 total | 20 CLI, 0 server | 200+ (new HTTP dep, TLS cert mgmt) |
| UX for `salt-run --eauth state.orchestrate` | real-time, matches PKI | polling delay, no sub-events | real-time, separate port + cert |
| Security | reuses `authenticate_token` + eauth `auth_list` | smallest surface | adds HTTP TLS surface |
| Stacking w/ PKI+eauth | one method, two branches | diverges — eauth has no stream | parallel subsystem |

## Files to touch
- `salt/utils/client_event.py` — add `auth_token` action branch in publisher; add token-auth path in subscriber.
- `salt/auth/__init__.py` — no changes.
- `salt/master.py` — no changes.
- `salt/tokens/localfs.py` — no changes.
