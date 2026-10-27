# Master-topology configuration-shape audit

Audit scope: topology-graph configurations and target-syntax edge cases
not exercised by the existing scenario suite under
`tests/pytests/scenarios/topology/`.

Target branch tip: `c842d5a1c26` (`dwoz/master-topology`).
Method: code inspection of `salt/topology/`, `salt/config/topology.py`,
`salt/matchers/cluster_match.py`, `salt/utils/minions.py`, and
`salt/cluster/consensus/raft/topology.py`, cross-checked with the RFC
at `rfcs/master-topology.md`.  Spot-checks executed via the venv314
Python interpreter against the live modules; the specific invocations
are documented inline per finding.

Classifications used: **BUG** (behavior wrong vs. RFC), **UNDOCUMENTED**
(behavior defined by implementation but not RFC), **GOTCHA** (handled
correctly but operator-surprising), **OK**.

---

## 1. Star (central hub H peered with N spokes; spokes not peered with each other)

Shape:

```
      S1
      |
S2 -- H -- S3
      |
      S4
```

**Expected per RFC.**  Publish from an operator at `S1`:
`salt '*' test.ping` floods; `H` relays it to `S2..S4` because `H` is a
peer of every spoke and the floodfill forwarder sends to every peer not
on the authenticated path.  Returns from `S2..S4` route back to `S1` via
breadcrumbs recorded at `H`.

**Actual (code inspection: `router.forward_publish`).**  `H` receives
with `path=[S1]`.  It appends itself (`path=[S1, H]`), writes a
breadcrumb keyed on `jid` pointing at the inbound link (`S1`), then
iterates its own `_links` and sends to every peer not in `path` and not
equal to the inbound peer.  So `H → S2, S3, S4` all receive one copy
each.  Each spoke's `forward_publish` sees `path=[S1, H]`, appends
itself, and tries to flood further — but each spoke only has `H` as a
peer, and `H` is already in `path`, so no further fan-out happens.

Returns walk back correctly: a return at `S2` finds its breadcrumb at
`H` (keyed on jid), gets forwarded to `S1` via the breadcrumb's
recorded inbound link (`S1`).

**Classification: OK.**  Expected shape works correctly.

**Note:** `H`'s publish-daemon process holds `len(spokes)` outbound
`PeerMasterLink` objects plus a breadcrumb MmapCache that holds one
entry per in-flight jid.  Resource amplification is linear, which is
in-spec per the "O(direct_peers + active_jobs_crossing)" invariant.

---

## 2. Pure chain (A ↔ B ↔ C ↔ ... ↔ Z, no shortcuts)

Shape:

```
A -- B -- C -- D -- ... -- Z
```

### 2a. Flood from A

**Expected per RFC.**  `salt '*' test.ping` from A reaches every cluster
via `transit_match=True` at each hop.

**Actual.**  Flood case works: `A → B → C → ...` each hop appends its
own `cluster_id` to the authenticated path, loop detection prevents
backtracking, and `transit_match=True` lets each cluster also publish
to its own minions.  **OK.**

### 2b. Scoped from A (`salt -C 'C@c and web-*' test.ping`), where `c` is not a direct peer of A

**Expected per RFC** (lines 85-87 of `rfcs/master-topology.md`):

> Nested peers reachable via graph routing: `salt -C 'C@east-sub1 and
> web-*'` works even if the originator peers only with `east` (routing
> table resolves `east-sub1 → via east`).

**Actual.**  Reproduced via

```python
from salt.topology.router import TopologyRouter
class FakeLink:
    def __init__(self, peer): self.peer_cluster_id = peer
r = TopologyRouter({"cluster_id": "a"}, None, None)
r._links = {"b": FakeLink("b")}  # A knows only B
load = {"tgt": "C@c and web-*", "jid": "00001"}
links, local = r.route_publish(load)
# → links=[]  local=False  envelope.destinations=['c']
```

`TopologyRouter.route_publish` filters outbound links by direct
`peer_cluster_id ∈ destinations`.  No direct link to `c` means no
dispatch, even though `b` is a valid transit hop to `c`.  The publish
is **silently dropped at the origin**.  Operator sees zero returns and
no error event.

The RFC description promises transit routing via a routing table
derived from summary gossip.  `salt.topology.router` has no routing
table — `_links` is the only hop set — and
`TopologyStateMachine.next_hop` in `salt/cluster/consensus/raft/topology.py:603-651`
explicitly notes:

```
2. Transit: not yet implemented at the SM layer; the live
   router owns multi-hop decisions off summary gossip.
   Returns ``None`` for non-direct destinations in Phase 3.
```

But the live router doesn't do it either.  **No path in the live code
turns a summary-learned non-adjacent `cluster_id` into a next-hop link
decision.**  The only mechanism `ingest_summary` provides is a
`registry.set_peer` write, which never feeds into `route_publish`.

**Classification: BUG.**

**Reproducer sketch.**  Chain A ↔ B ↔ C, operator runs
`salt -C 'C@c and web-*' test.ping` on A.  Expected: publish reaches
C's minions.  Actual: zero returns, zero events, no log line indicating
drop above DEBUG.

**Proposed fix location.**  `salt/topology/router.py:route_publish`
needs a routing-table consult: for each destination not in `_links`,
look up a learned next-hop via summary paths (summaries land in the
registry with their traversed `path` — the direct peer whose summary
carried that path is the next hop) and queue the publish onto that
link instead.  Alternative: close the RFC gap by updating the RFC to
restrict `C@` targets to direct peers in v1 and document this
limitation prominently.

### 2c. Return walk on chain

If 2b is fixed by widening scoped routing to transit via B, breadcrumbs
at B need to record the full inbound-path, not just the inbound peer,
so returns from C can traverse B → A.  Current breadcrumb value is
`{"link_cluster_id": inbound_peer_id, "expiry_at": ts}` which handles
single-hop return but is sufficient for chain return as long as each
transit cluster records its own breadcrumb.  The current
`forward_publish` already does `record_breadcrumb(jid, inbound_link)`
at every transit hop, so return walk would work once 2b is addressed.

---

## 3. Tree (hub-and-spoke-and-leaf): `H ↔ {A, B}`; `A ↔ {A1, A2}`; `B ↔ {B1, B2}`

Shape:

```
         H
        / \
       A   B
      /|   |\
    A1 A2  B1 B2
```

**Expected per RFC.**  Flood from H reaches every node.  Scoped
`C@a1` from H needs transit through A — same shape as finding 2b.

**Actual.**  Flood works per the forward-flood logic (same as star + an
additional hop).  Scoped `C@a1` from H fails for the same reason as
finding 2b.  Scoped `C@a` from H works (direct peer).

**Classification: BUG (subsumed by finding 2b).**

---

## 4. Disconnected forest (two separate topology components)

Shape: two independent graphs that share no edge.

**Expected per RFC.**  Fully implicit — if a cluster isn't in the peer
graph transitively, it's unreachable.  No RFC text promises
cross-component delivery.

**Actual.**  Component boundaries are respected because `_links` only
contains direct peers.  Floods never cross components; scoped targets
to the other component get no routing (same silent-drop issue as 2b,
but here the drop is correct per topology).

**Classification: OK** (handled correctly; the silent drop is correct
when the operator genuinely cannot reach the target).

**Note:** the silent-drop ergonomics are shared with 2b — operators
have no way to distinguish "target cluster is in another forest" from
"target cluster is 2 hops away but router can't resolve transit."
Addressed in finding 2b's proposed fix.

---

## 5. Self-loop (cluster lists itself as a peer in `topology.peers`)

**Expected per RFC.**  No explicit text, but the implicit expectation
is that this is rejected at config load.

**Actual.**  `salt.config.topology.validate_topology_config` has no
check comparing peer entries' hostnames to the local master's own
host.  Reproduced:

```python
from salt.config.topology import TopologyConfig, PeerRef, validate_topology_config
cfg = TopologyConfig(
    cluster_id="east",
    peers=(PeerRef(host="east-master-1", port=4521),),  # self!
    event_subscriptions=("salt/job/*/ret/*",),
    enabled=True,
)
validate_topology_config(cfg)  # → passes validation
```

At runtime, `link_manager.start` creates a `PeerMasterLink` for the
self-entry and attempts `connect()`.  The handshake eventually comes
back around to this master's own inbound listener; `_handle_master_auth`
does NOT compare `load["cluster_id"]` against `opts["cluster_id"]`
(line 1016 of `link_manager.py`), so the self-auth proceeds through
the usual pending → accepted flow.  If `auto_accept_masters: True`,
the local master accepts its own key and brings up a link whose
`peer_cluster_id == self.cluster_id`.

`TopologyRouter.route_publish` and `forward_publish` both skip links
whose `peer_cluster_id == self.cluster_id` (`router.py:593` and `694`),
so publishes don't fan out to self.  Returns via breadcrumb lookup to
`peer == self.cluster_id` would resolve back to the self-link but the
link's `send_return` is only invoked for breadcrumbs recorded with the
self-link as inbound — which `record_breadcrumb` would reject if it
checked (it doesn't; `peer = getattr(inbound_link, "peer_cluster_id",
None)` would be `self.cluster_id`, and the breadcrumb value would
record `self.cluster_id` as the return-destination peer).

Net effect: configuration anomaly consumes one `PeerMasterLink` plus
one entry in each pki registry; routing-level guards prevent actual
traffic loops.  No crash, no observable publish misbehavior.

**Classification: GOTCHA.**  Should be rejected at config load with a
descriptive error.

**Proposed addition.**  In `validate_topology_config`, iterate `peers`
and reject any entry whose `(host, port)` resolves to `opts["interface"]`
(or `127.0.0.1` / `0.0.0.0`) + `opts["topology_link_port"]`.  Also add
a defense-in-depth check in `_handle_master_auth`: reject when
`load["cluster_id"] == opts["cluster_id"]`.

---

## 6. Asymmetric trust (A has B in `topology.peers` and B accepted A, but A has NOT accepted B)

**Expected per RFC** (lines 98-146):

> `salt-key -a <peer> --master` on cluster A → A will accept
> publishes, returns, events, and summaries sent FROM `<peer>`.
> It does NOT grant A permission to send anything TO `<peer>`.

So A→B publish traffic works; B→A publish/return/summary traffic is
rejected at A's inbound handshake because A has not accepted B.

**Actual.**  `_handle_master_auth` in `link_manager.py` checks
`os.path.exists(accepted)` and responds with `{"status": "retry"}` or
`{"status": "rejected"}` otherwise.  Match: inbound summaries from B
are dropped at A's listener because B's inbound handshake stays in
PENDING_ACCEPT.  A→B outbound works because B accepted A, so B's
listener returns `{"status": "ok"}` to A's handshake.

Return handling is more subtle.  A publish initiated at A arrives at
B; B's minion returns go back on the same breadcrumb-recorded link to
A.  B's `send_return` on its `PeerMasterLink(peer=A)` — but B's
`PeerMasterLink` is the outbound side B initiated TO A.  B has A in
its peers config?  The scenario says only A has B in `topology.peers`.
So B has no outbound link to A, so B cannot `send_return` back.
Instead, B's return path is: the return arrives at B's MWorker, which
looks up the breadcrumb (recorded at B when A→B publish arrived), gets
`inbound_link_cluster_id=A`, and dispatches a return event the publish
daemon catches.  The publish daemon then calls `link.send_return`.
`get_link("a")` on B returns the INBOUND link (the one B accepted
during A's handshake), which has a live session AES.  So the return
flows back along the inbound-direction link.

That matches the RFC: "Returns inherit the publish's acceptance.  When
A sends a publish to B (requires: B accepted A), returns from B's
minions flow back along the same connection without a separate
acceptance step."

**Classification: OK.**  Behavior matches RFC.

**Verification caveat.**  I did not execute a live scenario because the
scenario tests already exercise this (`test_scenario_directional_trust_east_accepts_west_not_vice_versa`
in `test_core_scenarios.py:677`).  Code inspection matches the
scenario's assertions.

---

## 7. Mutual-but-one-side-only-config: A has B in `topology.peers`; B has A in `pki_dir/masters/` only (no `topology.peers` entry)

**Expected per RFC** (line 136):

> Pure receiver: A accepts B but has no `topology.peers:` entry for B.
> B must initiate; A will accept inbound.

The symmetric case: A initiates to B; B accepts; link comes up as
A-initiated.  B has no outbound config, but the inbound-accepted link
provides the return path.  B can also publish TO A over the inbound
link if it needs to? That's unclear.

**Actual.**  B's `_INBOUND[A]` entry contains the link object once
handshake completes.  B's publish daemon calling `on_publish` from a
locally-originated publish iterates `_links` (outbound only) in
`route_publish`.  So B cannot initiate a publish toward A — B's
operator typing `salt -C 'C@a and web-*' test.ping` on B will have
`destinations={a}`, no outbound link to `a` in `_links` (only in
`_INBOUND`), so no fan-out.  Flood works similarly: `_links` is
iterated, not `_INBOUND` or `get_all_links`.

In contrast, A's operator can target B (A has outbound).  The link is
*effectively one-way for publish-origination* even though returns and
inbound traffic work both ways.

The RFC's "Pure receiver" mode (A accepts B, no topology.peers entry
for B) describes the ONE-WAY case correctly: A accepts and receives
from B; A does not originate to B.  But the symmetric "Mutual with
one-side config" case is not addressed by the RFC and the semantics
are quietly: B cannot originate to A, even though a link exists.

**Classification: UNDOCUMENTED.**

**Proposed doc addition.**  In the "Resulting operating modes" section
of the RFC (lines 120-146), add:

> **Receive-only initiator:** A has B in `topology.peers:` and both
> sides accepted.  B has NO `topology.peers:` entry for A.  Publishes
> flow A→B.  B's operator cannot originate publishes to A — the live
> link is only consulted in A's outbound registry.  Returns, events,
> and summaries from B to A traverse the inbound-accepted link
> correctly.  Operators who want fully symmetric peering must list the
> peer on both sides.

---

## 8. Partial-transitive (A ↔ B, B ↔ C; A has no entry for C)

Covered by finding 2b.  **BUG.**

---

## 9. Dumbbell (two triangles connected by a single bridge)

Shape:

```
A --- B
 \   /
  \ /
   X
   |
   Y
  / \
 /   \
C --- D
```

Where X-Y is the single bridge between triangles `{A, B, X}` and
`{Y, C, D}`.

**Expected per RFC.**  Flood from A reaches every cluster via mesh +
single-bridge transit; scoped from A to `C@c` needs transit through
X and Y (two hops of non-direct resolution).

**Actual.**  Flood: works, each hop's `transit_match=True` and
loop-detection via path handles the single-bridge case cleanly.
Dedup via jid breadcrumb drops duplicate arrivals from mesh paths.

Scoped: `C@c` from A — same silent-drop issue as finding 2b.

**Classification: BUG (subsumed by 2b).**

---

## 10. Petersen-graph-shaped (10 clusters, 3-regular, girth 5)

**Expected per RFC.**  Mesh dedup via jid breadcrumb handles the
density; summary dedup via `_last_origin_seq` handles the fan-in on
summaries.

**Actual.**  Both dedup mechanisms are in place.  I did not execute a
scaled test; existing mesh-dedup scenarios cover a 3-triangle, which
exercises the hot path.  Petersen graph adds girth-5 cycles which
means a message traverses more hops before loop-detection kicks in —
still bounded by path length.

Resource amplification per cluster: `_last_origin_seq` grows to 10
entries (one per origin); breadcrumb cache sized by default to 1M.
No concern.

**Classification: OK.**  Mesh correctness properties generalize.

**Caveat.**  The duplicate-return LRU (`_FORWARDED_RETURN_LRU_SIZE =
1000` in router.py:67) could become a bottleneck under heavy
cross-cluster return bursts in dense graphs; one return `(jid, id)`
retained for only 1000 recent entries.  Not a correctness bug — only
affects whether a stale-mmap duplicate forward is dropped.

---

## 11. Dangling peer (A has B in `topology.peers` but B's host is unreachable)

**Expected per RFC.**  Not specified.  Implicit: startup should not
block indefinitely; link should park in DISCONNECTED and retry.

**Actual.**  `PeerMasterLink.connect` catches transport exceptions and
sets `_health = LinkHealth.DISCONNECTED`.  Returns from `connect`
without raising, so `start`'s task-scheduling loop completes.  There
is no automatic reconnect timer in the stubbed Phase-2 implementation
visible in `link.py` — once DISCONNECTED, the link stays that way
until a future `connect()` call.  I did not find a periodic retry
scheduler in `link_manager.py`.

Startup does not block.  `salt-run topology.status` should report the
link as DISCONNECTED.

**Classification: UNDOCUMENTED.**  The retry policy is not stated in
the RFC, and the current implementation has no retry loop — once a
link enters DISCONNECTED due to transport failure, the operator must
restart or trigger a manual reconnect.

**Proposed doc addition.**  Specify retry behavior for DISCONNECTED
links.  Either (a) add a periodic retry task in `link_manager` with a
documented backoff, or (b) explicitly document that operators must
restart the publish daemon to retry a dead link.  Current implicit
behavior is (b) by omission.

---

## 12. Rotation during traffic (identity key rotation on A with in-flight jids)

**Expected per RFC** (line 666-677):

> `MasterIdentityKey`.  Any peer in the cluster can present the key
> during peer-master handshakes.

Rotation path: `MASTER_IDENTITY_KEY` log entry commits a new keypair;
state machine's apply handler writes it to disk and the MmapCache.
In-flight jids: breadcrumbs are jid-keyed and don't carry the identity
key.  Session AES on each link is tied to `_session_generation`, so an
identity rotation doesn't invalidate existing sessions unless the
rotation triggers a re-handshake.

**Actual.**  Identity rotation is only invoked at bootstrap by
`TopologyStateMachine._bootstrap_identity_key` (per
`test_identity_rotation_overwrites_disk_keypair_in_place` in
`test_adversarial_statemachine.py`).  No code path triggers a
mid-flight rotation.  If an operator forced one (e.g., `salt-run
topology.rotate_identity` — doesn't exist today), existing sessions
would continue with their current AES until the next re-handshake.
Peer masters pin to this master's identity key via
`pki_dir/masters/<cluster_id>.pub`, so a rotated public key would
cause their next inbound handshake to fail acceptance (the stored
public key no longer matches), parking B in PENDING_ACCEPT.

**Classification: UNDOCUMENTED.**

**Proposed doc addition.**  Specify that identity rotation requires
operator action on every peer to re-accept the new public key, or
describe an automatic re-pin mechanism.  Current behavior is operator
confusion risk.

---

## 13. Target syntax edges

Executed probe:

```python
from salt.topology.router import extract_cluster_scope, strip_cluster_scope
for tgt in [
    "C@", "C@,,", "C@east,east", "C@EAST",
    "C@east and C@west", "C@east and not C@east",
    "C@east or web-*", "C@east and C@west and web-*",
    "( C@east )", "not C@east",
]:
    print(tgt, extract_cluster_scope(tgt), strip_cluster_scope(tgt))
```

### 13a. `C@` (empty cluster-id list)

`extract_cluster_scope("C@") → None`.  `strip_cluster_scope("C@") → "*"`.
Treated as flood (destinations=None) and local target becomes `*`.
Operator who typed `C@` as a typo gets a global flood with `*` target.

**Classification: GOTCHA.**  Should either reject at CLI parsing or
treat as empty-destinations → no-op.

### 13b. `C@,,` (empty cluster_ids via commas)

Identical to 13a.  **GOTCHA.**

### 13c. `C@east,east` (duplicate cluster_id in a C@ token)

`extract_cluster_scope → {"east"}`.  Deduplication by set semantics.
**OK.**

### 13d. `C@EAST` (uppercase)

`extract_cluster_scope → {"EAST"}`.  `CLUSTER_ID_RE` in
`config/topology.py` rejects uppercase, so no valid cluster will ever
have `cluster_id="EAST"`.  The router will try to fan out to a peer
whose cluster_id is `EAST`, find none, drop (same silent-drop issue
as finding 2b).

**Classification: GOTCHA.**  Case sensitivity of `C@` is not
documented in the RFC, and the silent-drop behavior means operators
who accidentally caps-lock will see no returns and no error.

**Proposed doc addition.**  Document that `cluster_id` matching is
case-sensitive and must use lowercase per `CLUSTER_ID_RE`.

### 13e. `C@east and C@west` (AND of two positive C@ tokens)

`extract_cluster_scope → {"east", "west"}`.  Both clusters are in the
destination set.  At each receiving cluster, `strip_cluster_scope`
drops both `C@` tokens, so the local target becomes `*` (or just the
rest of the compound).

Operator probably meant intersection ("minions that are on BOTH east
AND west") which is a nonsensical ask (a minion is on one cluster).
Current semantics is UNION — every minion on east and every minion on
west gets the publish.  Not what the operator intended.

**Classification: UNDOCUMENTED.**  The RFC's compound-target examples
only show `C@east,west` for union; the AND form is operationally a
union-with-wrong-syntax and should be flagged.

**Proposed doc addition.**  Document that `C@a and C@b` is semantically
equivalent to `C@a,b` (union, not intersection) because any single
minion can only be in one cluster at a time, and the current
implementation reflects this.  Alternatively, reject `and`-joined
positive `C@` tokens at parse time.

### 13f. `C@east and not C@east` (contradiction)

`extract_cluster_scope → None` (any `not` present → flood fallback).
`strip_cluster_scope → "C@east and not C@east"` (skip-strip because
of `not`).  Floods the entire graph; each receiver evaluates the
full compound.  On east: `C@east → True; not C@east → False;
True AND False → False`.  On other clusters: `C@east → False;
False AND ... → False`.  So the compound evaluates to False
everywhere — zero minions matched.

**Classification: OK.**  Floods are expensive but the correctness
holds.

### 13g. `C@east or web-*` (C@ in disjunction)

`extract_cluster_scope → {"east"}`.  `strip_cluster_scope` sees `or`
adjacent to `C@` and skip-strips (line 274-278 of router.py).  So
destinations={east}, but the receive-side target still contains
`C@east` which the minion-side cluster matcher evaluates — on east it
matches all minions; off east the `C@east` evaluates False and the
disjunction becomes just `web-*`.

But: destinations=`{east}` means the publish ONLY goes to east.
`web-*` minions on OTHER clusters never see the publish because the
router doesn't route there.  Operator probably intended "east OR any
web-* minion anywhere" (flood).

**Classification: BUG.**

**Reproducer.**  Operator on cluster A runs
`salt -C 'C@east or web-*' test.ping`.  Expectation: all east minions
PLUS web-* minions on every reachable cluster.  Actual: only east
minions receive it.

**Proposed fix.**  `extract_cluster_scope` must recognize `or`-joined
`C@` tokens as "flood + match C@ on top" OR reject the pattern at
parse time.  The simplest safe fix: `or`-adjacency on a `C@` token
means flood (`destinations=None`), matching the `not` fallback already
implemented.

### 13h. `C@east and C@west and web-*`

`extract_cluster_scope → {"east", "west"}`.  `strip_cluster_scope →
"web-*"`.  Routes to east and west; each matches `web-*` locally.
Union semantics, same as 13e but with an additional glob constraint.

**Classification: UNDOCUMENTED** (same as 13e).

### 13i. `( C@east )`

`extract_cluster_scope → {"east"}`.  `strip_cluster_scope → "*"`
(empty-group cleanup produces residual-empty → `*`).  **OK.**

### 13j. `not C@east`

`extract_cluster_scope → None` (any `not` → flood).
`strip_cluster_scope → "not C@east"` (skip-strip).  Floods, every
cluster evaluates `not C@east` locally — east drops, every other
cluster matches all minions.  **OK.**

---

## 14. Minion attached to multiple masters in the same cluster — `master_cluster_id` grain on failover

**Expected per RFC** (lines 155-156, 755-770).

> `salt-call grains.get master_cluster_id` — new grain on
> topology-aware minions

The grain is populated from `opts["master_cluster_id"]`, written by
`AsyncAuth.handle_signin_response` from the master's auth-response
`cluster_id`.  Every master in a cluster has the same `cluster_id`
(it's a Raft-replicated opt), so failover across masters within a
cluster produces the same grain value.  No divergence expected.

**Actual.**  `salt/master.py` populates `cluster_id` in the auth
response from `opts["cluster_id"]` (which is Raft-replicated).  On
minion failover between A1 and A2 in the same cluster, the grain
value stays the same.  **OK.**

**Scenario covered.**  `test_master_cluster_grain.py` (unit +
functional) exists.

---

## 15. One cluster missing `opts["cluster_id"]` on the master side (topology.conf exists but `cluster_id` is empty)

**Expected per RFC.**  Config load should fail validation.

**Actual.**  Executed:

```python
from salt.config.topology import TopologyConfig, PeerRef, validate_topology_config
cfg = TopologyConfig(cluster_id="", peers=(), event_subscriptions=("salt/job/*/ret/*",), enabled=True)
validate_topology_config(cfg)
# → ValueError: cluster_id is required when topology is enabled
```

**OK.**  Validation rejects.

Fallback: a master with `topology_enabled: False` has no `cluster_id`,
so the `C@` matcher returns False.  `_check_cluster_minions` returns
`{"minions": [], "missing": []}`.  Verified via unit probe:

```python
from salt.matchers.cluster_match import match
match("east", opts={"cluster_id": ""}) → False
```

**Classification: OK.**

---

## 16. Different masters in the same cluster running different `topology.conf`

**Expected per RFC** (line 161):

> Per-master, identical on every peer in a cluster.

**Actual.**  No code enforces this.  Each cluster peer loads its own
`topology.conf` from `/etc/salt/topology.conf` at startup.
`TopologyStateMachine` only replicates the accepted-peers set via
`MASTER_PEER_ATTACH` Raft entries — not the raw `topology.peers`
config.  The RFC acknowledges this: config is "per-master" and the
"full peer list is learned from Raft after handshake."

So divergent `topology.conf` on cluster peers results in:
- Different seed peer lists at startup; only the Raft leader runs the
  publish daemon, so only its seeds get connected.
- On leadership failover, the new leader may use a different initial
  seed set, affecting first-connect bootstrap time.
- Accepted-peer convergence happens via Raft replication of
  `MASTER_PEER_ATTACH`, so steady-state is consistent regardless of
  initial config divergence.

**Classification: GOTCHA.**  Config divergence works until a
leadership change exposes a seed-list mismatch.

**Proposed addition.**  Document in the "Config file" section of the
RFC that divergent `topology.conf` across cluster peers is tolerated
in steady state but can produce surprising first-connect behavior on
leadership failover.  Add a hash-check at Raft bootstrap that WARN
logs if `topology.conf` hashes differ across voters.

---

## 17. Transit-cluster return breadcrumb failover (RFC §"Unresolved questions")

**Expected per RFC.**  Unresolved; three options documented, decision
pending.  Not yet implemented in any form.

**Actual.**  Breadcrumbs are per-process on the publish-daemon-owning
peer.  Leader failover in a cluster loses in-flight breadcrumbs → returns
land locally at the transit cluster and never route back to origin.

**Classification: UNDOCUMENTED** (acknowledged in RFC, no decision).

**Proposed note.**  Already captured by the RFC's "Unresolved
questions" section; audit flags that the audit team saw this and
confirms the implementation matches option (mC) — transit failover is
syndic-parity.

---

## 18. Inbound-only acceptance (A accepts B inbound but has no outbound link, B initiates publishes)

Partially covered by finding 7.  Same mechanism; `_INBOUND` dict has
the link, `_links` does not.  **UNDOCUMENTED** (same as 7).

---

## 19. Peer with conflicting fingerprints at the same `(host, port)`

**Expected per RFC.**  Rejected at config load.

**Actual.**  `validate_topology_config` catches this:

```python
cfg = TopologyConfig("east", (PeerRef("h", 4521, "aa"), PeerRef("h", 4521, "bb")), ..., True)
validate_topology_config(cfg)
# → ValueError: peers[1] duplicate (host='h', port=4521) with conflicting fingerprints
```

**OK.**

---

## 20. Empty event_subscriptions and glob edge cases

Executed:

```python
validate_topology_config(TopologyConfig("east", (), ("[",), True))  # → ACCEPTED
```

`fnmatch.translate("[")` doesn't raise (fnmatch is permissive), so an
unbalanced bracket glob slips through validation.  At match time,
`fnmatch.fnmatchcase(tag, "[")` returns False for every tag — the
subscription never fires.  Benign but silent-no-op.

**Classification: GOTCHA.**

**Proposed addition.**  In `validate_topology_config`, additionally
compile the fnmatch-translated regex via `re.compile` to catch
malformed brackets.

---

# Summary

| # | Config | Classification |
|---|---|---|
| 1 | Star (hub-and-spoke) | OK |
| 2a | Chain, flood | OK |
| 2b | Chain, scoped `C@c` from A where c is non-adjacent | **BUG** |
| 3 | Tree (3-level, hub-spoke-leaf), scoped non-adjacent | **BUG** (subsumed by 2b) |
| 4 | Disconnected forest | OK |
| 5 | Self-loop (cluster lists self in peers) | GOTCHA |
| 6 | Asymmetric trust | OK |
| 7 | Mutual-but-one-side-only-config | UNDOCUMENTED |
| 8 | Partial-transitive | **BUG** (same as 2b) |
| 9 | Dumbbell, scoped non-adjacent | **BUG** (same as 2b) |
| 10 | Petersen-graph-shaped | OK |
| 11 | Dangling peer (unreachable host) | UNDOCUMENTED |
| 12 | Identity rotation during traffic | UNDOCUMENTED |
| 13a | `C@` empty target | GOTCHA |
| 13b | `C@,,` empty via commas | GOTCHA |
| 13c | `C@east,east` duplicate | OK |
| 13d | `C@EAST` uppercase | GOTCHA |
| 13e | `C@east and C@west` AND of positive C@ | UNDOCUMENTED |
| 13f | `C@east and not C@east` contradiction | OK |
| 13g | `C@east or web-*` C@ in disjunction | **BUG** |
| 13h | `C@east and C@west and web-*` | UNDOCUMENTED (same as 13e) |
| 13i | `(C@east)` parens | OK |
| 13j | `not C@east` bare | OK |
| 14 | Minion failover grain | OK |
| 15 | Missing `cluster_id` opts | OK |
| 16 | Divergent `topology.conf` across cluster peers | GOTCHA |
| 17 | Transit breadcrumb failover (acknowledged open issue) | UNDOCUMENTED |
| 18 | Inbound-only acceptance | UNDOCUMENTED (same as 7) |
| 19 | Conflicting fingerprints | OK |
| 20 | Malformed glob in `event_subscriptions` | GOTCHA |

**Counts (distinct findings; subsumed-by-same-root-cause ones counted
once):**
- BUGs: 2 (`#2b` non-adjacent scoped routing; `#13g` `C@` in `or` scope collapse)
- UNDOCUMENTEDs: 5 (`#7` inbound-only initiator; `#11` reconnect; `#12` identity rotation; `#13e` AND-of-positive-C@; `#17` transit breadcrumb failover)
- GOTCHAs: 6 (`#5` self-peer; `#13a`/`#13b` empty C@; `#13d` uppercase; `#16` divergent conf; `#20` malformed glob)
- OKs: the remainder — explicitly traced and confirmed.
