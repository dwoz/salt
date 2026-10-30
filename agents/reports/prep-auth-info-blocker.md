# `_prep_auth_info` Blocker — Investigation Report

**YES: Blocker confirmed.** The current `_prep_auth_info` implementations enforce a priority-based short-circuit that prevents detecting when both PKI (`"key"` field) and eauth credentials coexist in the same request.

## 1. Short-Circuit Logic (Confirmed)

### salt/master.py:5714–5730
```python
def _prep_auth_info(self, clear_load):
    sensitive_load_keys = []
    key = None
    if "token" in clear_load:
        auth_type = "token"
        ...
    elif "eauth" in clear_load:
        auth_type = "eauth"
        ...
    else:
        auth_type = "user"
        err_name = "UserAuthenticationError"
        key = self.key
    return auth_type, err_name, key, sensitive_load_keys
```

Priority: **`token` > `eauth` > `user`**. Eauth is never consulted when token is present; user-path (PKI) is never consulted when eauth is present.

### salt/daemons/masterapi.py:1648–1661
Identical logic, returns 3-tuple instead of 4 (no `sensitive_load_keys`). Same priority.

### salt/netapi/__init__.py:130–145
API-only variant; only supports `token` and `eauth`, raises error if neither present. Not exposed to PKI path.

## 2. Call Sites (9 Total)

### salt/master.py (6 call sites)
1. **5226** (runner): `auth_type, err_name, key, sensitive_load_keys = self._prep_auth_info(clear_load)`
2. **5302** (wheel): Same signature.
3. **5489** (publish): `auth_type, err_name, key, sensitive_load_keys = self._prep_auth_info(extra)` — passes `extra = clear_load.get("kwargs", {})`
4. **5958** (_sync_runner): Same as runner.
5. **6013** (_sync_wheel): Same as wheel.

### salt/daemons/masterapi.py (3 call sites)
1. **1227** (runner): `auth_type, err_name, key = self._prep_auth_info(load)`
2. **1324** (wheel): Same.
3. **1485** (publish): Same.

### salt/netapi/__init__.py (1 call site)
1. **148** (_authorize_ssh): `auth_type, err_name, key, sensitive_load_keys = self._prep_auth_info(low)`

## 3. Top 5 Downstream `auth_type` Checks

### salt/master.py:5534 (publish)
```python
if auth_type == "user" and auth_identity:  # Detects client (PKI) path
    try:
        client_key = self.key_cache.fetch("client_keys", auth_identity)
        if client_key:
            is_client = True
```
Branches on `auth_type == "user"` to distinguish client (PKI) from eauth/token.

### salt/master.py:5617–5618 (publish authorization gate)
```python
if not is_client and (
    auth_type != "user" or (auth_type == "user" and auth_list)
):
    # Authorize via ckminions.auth_check
```
Skips eauth authorization entirely when `is_client == True` (PKI path).

### salt/master.py:5635 (publish error logging)
```python
if (
    auth_type == "eauth"
    and not auth_list
    ...
):
    log.debug('Auth configuration for eauth "%s" and user "%s" is empty', ...)
```
Auth-type-specific debug logging.

### salt/master.py:5660–5666 (publish post-auth config)
```python
if auth_type == "token":
    username = auth_check.get("username")
    clear_load["user"] = username
elif auth_type == "eauth":
    clear_load["user"] = self.loadauth.load_name(extra)
```
Sets `clear_load["user"]` differently per auth-type; PKI path (`auth_type == "user"`) sets it at line 5606.

### salt/auth/__init__.py:674–723 (check_authentication branching)
```python
if auth_type == "token":
    token = self.authenticate_token(load)
    auth_list = self.get_auth_list(load, token=token)
elif auth_type == "eauth":
    if not self.authenticate_eauth(load):
        ret["error"] = {...}
elif auth_type == "user":
    auth_ret = self.authenticate_key(load, key)
    auth_list = salt.utils.master.get_values_of_matching_keys(...)
```
Dispatches to three independent authentication handlers based on single `auth_type`.

## 4. Minimal Fix: Recommended Shape

**Option A (recommended): Return secondary auth axes as new return fields.**

Expand `_prep_auth_info` to return a 6-tuple:
```python
(auth_type, err_name, key, sensitive_load_keys, stacked_possible, secondary_auth_type)
```

- `auth_type`: Primary auth method (today's priority: token > eauth > user).
- `secondary_auth_type`: Non-None if a secondary axis is available. `"user"` if PKI key present but not primary; `"eauth"` if eauth present but not primary.
- `stacked_possible`: Boolean flag indicating both axes are present.

**Blast radius:** Small.
- All 9 call sites unpack the tuple; Python unpacking is positional, so new fields are captured without code change.
- New fields are optional to consume; existing branches (`if auth_type == "user"`) remain unchanged.
- Callers that need stacking (publish/runner/wheel at both master and masterapi) can check `stacked_possible` flag and call `check_authentication` twice.

**Pythonic for Salt:** Tuple return with named fields is Salt's existing pattern (`check_authentication` returns a dict; tuple unpacking is used in `salt/auth/__init__.py` for token/eauth/user dispatch).

## 5. External Callers

**None detected outside the core monolith.**
- `_prep_auth_info` is private (leading underscore); not exported in `__all__`.
- grep results: only `salt/master.py`, `salt/daemons/masterapi.py`, `salt/netapi/__init__.py`, and tests.
- No plugins or external modules call it.

## 6. Client-Side (RemoteClient) Capability

### salt/client/__init__.py:2430–2434
```python
payload_kwargs = self._prep_pub(
    tgt, fun, arg, tgt_type, ret, jid, timeout, **kwargs
)
payload_kwargs["client_id"] = self.opts["id"]
```

### salt/client/__init__.py:2027–2091 (_prep_pub)
```python
payload_kwargs = {
    "cmd": "publish",
    "tgt": tgt,
    "fun": fun,
    "arg": arg,
    "key": self.self.key,       # PKI key always present
    "tgt_type": tgt_type,
    "ret": ret,
    "jid": jid,
}
if kwargs:
    payload_kwargs["kwargs"] = kwargs  # eauth/token passed here
payload_kwargs["user"] = self.salt_user
```

**Both axes are already supported:** RemoteClient always sends `"key"` (PKI), and `**kwargs` flows through to `"kwargs"` dict on the master, which becomes `extra` in the publish handler. Callers already invoke `client.pub(..., username='u', password='p', eauth='pam')` as shown in docstring (line 934–937).

**Client-side change: None required.** The payload structure already supports both. Only the server needs to detect and process both.

---

**Conclusion:** Fix is localized to `_prep_auth_info` return signature and its 9 call sites. Option A adds 2 new optional return fields; minimal code churn. Secondary auth can be dispatched via a new `_check_authentication_dual` or inline double-call in the publish handler.
