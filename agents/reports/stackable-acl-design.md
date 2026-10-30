# Stackable PKI + Eauth Authorization — Design

Branch: `wip-remote-cli-rebased`.

## Recommendation
**Policy (a) — both-must-pass (stacked AND) for case 3**, opt-in via a new master config `stacked_auth: true` (default `false` to preserve current case-1/case-2 behavior).

- Case 1 (PKI only) → `ClientACL` gates. Unchanged.
- Case 2 (eauth only) → `auth_list` + `ckminions.auth_check` gates. Unchanged.
- Case 3 (both) → **both** must authorize. **New.** Enabled only when `stacked_auth: true`.
- Case 4 (neither) → deny. Unchanged.

Rationale: policy (a) is the only one that upholds the mTLS+OAuth invariant — workstation posture (PKI) *and* human identity (eauth) each independently entitled. Policies (b) either-sufficient and (c) PKI-first-override create privilege-escalation paths that defeat the stated enterprise goal.

## 🚨 Blocker that must land first

`_prep_auth_info` (`salt/master.py:5714`; sibling in `masterapi.py`) currently returns a **single** `auth_type` chosen by priority `token > eauth > user`. If both PKI (`"key"`) and `"eauth"` are present in the same load, the current code picks `eauth` and never consults the user path.

Options to unblock:
1. Teach `_prep_auth_info` to return both axes when both are present: `(auth_type, err_name, key, sensitive, stacked, secondary_auth_type)`.
2. Have the client set `extra["stacked_auth"] = True` explicitly, and have `_prep_auth_info` branch on that.

Recommend (1) — server-side detection; no CLI knob required.

## Code-shape change (identical at all three dispatch sites)

Today each site is `if is_client: ClientACL-gate else: auth_list-gate`. Replace with a three-way:

```python
is_client, client_id = _detect_client(auth_check, auth_type, self.key_cache)
has_eauth = _detect_eauth(clear_load, extra)  # auth_type in ("eauth", "token")

if is_client and has_eauth and self.opts.get("stacked_auth"):
    client_decision = self.client_acl.authorize_publish(client_id, minions, fun)
    eauth_decision  = _eauth_authorize_publish(auth_list, minions, fun, ...)
    ok, err = salt.acl.stacked_authorize(client_decision, eauth_decision)
    if not ok:
        return _deny(err)
    clear_load["client_id"] = client_id
    clear_load["user"]      = self.loadauth.load_name(extra)   # human
elif is_client:
    # case 1 — unchanged
elif has_eauth or auth_type != "user":
    # case 2 — unchanged
else:
    return _deny_unauth()
```

Dispatch sites:
- `salt/master.py:5529-5606` (publish) — the big one, line-numbers above.
- `salt/daemons/masterapi.py:1237-1302` (runner).
- `salt/daemons/masterapi.py:1337-1402` (wheel).

## Composition helper

New in `salt/acl/__init__.py`:

```python
def stacked_authorize(client_decision, eauth_decision):
    """Both must be truthy. Returns (bool, error_dict)."""
```

Both existing `ClientACL.client_can_publish` (`client_can_runner_wheel`-style additions needed for the other two sites) and the eauth paths return the same `{allowed: bool, error: dict}` shape.

New thin wrappers on `ClientACL` for symmetry:
- `authorize_runner(client_id, fun) -> {allowed, error}` — wraps `client_is_blacklisted` + `client_is_allowed`.
- `authorize_wheel(client_id, fun) -> {allowed, error}` — same shape.

## Audit log, returners, event tags

Today `clear_load["user"] = client_id` on `master.py:5606` is the TODO leak. In stacked mode we have two identities:

- `clear_load["user"]` → **human username** (eauth principal). Preserves backwards-compat with returners, event tags (`salt/job/<jid>/new`, `salt/wheel/<jid>/new`), job cache.
- `clear_load["client_id"]` → **workstation identity** (PKI). New field; aware consumers can log both.

Where no eauth is present (case 1), `clear_load["user"] = clear_load["client_id"] = client_id` — today's behavior preserved.

Audit log (currently `master.py:5607`): expand to `authorized user=%s via client=%s for fun=%s` when `stacked`.

Error paths that `fire_event` (lines 5522-5524, 5594-5598, 5652-5656) should include both identities in the error payload so denials are attributable to the right policy.

Returners: `runner_client.asynchronous(fun, kwarg, username)` on `masterapi.py:1308` — pass **human username** as the identity; thread `client_id` via `load.setdefault("__client_id__", client_id)` so returners can read it without a function-signature change.

## Precedent in Salt
`publisher_acl_blacklist` is a layered pattern — veto *in addition to* the main auth. It runs **before** `check_authentication` and is purely subtractive. The stacked design here introduces the **first genuine two-axis AND** of two whitelists. No existing symmetric precedent.

Mirror suggestion: implement `ClientACL` as a veto-posture layer where eauth is primary — the composition helper enforces both must agree, but neither owns the dispatch flow.

## Policy comparison

| Dimension | (a) Both must pass | (b) Either sufficient | (c) PKI-first eauth override |
|---|---|---|---|
| Fail mode | Fail-closed — eauth misconfig denies, can't escalate | Fail-open — bad `client_acl: '*'` defeats eauth | Fail-open in override direction |
| UX ("forgot LDAP password") | Can't publish; must fix eauth. Mitigation: drop eauth creds → case 1 fallback | Works, but eauth never gates | Depends on override direction; surprising |
| Audit | Two independent allow decisions, trivially explained | Hard to answer "which policy let this through?" | Logs must indicate override happened |
| Matches enterprise mTLS+OAuth invariant | Yes | No | No |

## Opt-in flag naming
`stacked_auth: true` on master config. Default `false` to preserve existing deployments.

Alternative: per-client-acl entry `require_eauth: true`. Strictly more flexible — lets ops require eauth for *specific* workstation identities while leaving others on PKI-only. Recommend adding both: `stacked_auth: true` means "always try both axes when both are present"; `require_eauth: true` on a `client_acl` entry means "this client MUST present eauth, deny otherwise."

## Files to touch
- `salt/master.py:5714` — expand `_prep_auth_info` return.
- `salt/master.py:5529-5606` — publish dispatch.
- `salt/daemons/masterapi.py:1222,1317` — runner/wheel dispatch.
- `salt/acl/__init__.py` — new `stacked_authorize` + `authorize_runner`/`authorize_wheel`.
- `salt/config/__init__.py` — add `stacked_auth` default.
- `salt/auth/__init__.py` — no changes to auth modules themselves.
