- Feature Name: master_topology
- Start Date: 2026-10-25
- RFC PR: (leave this empty)
- Salt Issue: #64089 (Topology configuration for masters)
- Target Branch: master (Potassium 3009)

# Summary

Master topology extends the 3008.x master cluster primitive so that
clusters of masters can form a peer graph and coordinate — effectively
an integrated, scalable replacement for syndic. Jobs, returns, and
events route across the graph with loop detection and breadcrumb-based
return paths. The design is strictly scale-first: every cluster carries
O(direct_peers + active_jobs + total_clusters) state, never
O(descendant_minions).

# Motivation

Syndic is widely used but has load-bearing flaws that get worse at
scale: a single syndic daemon is a SPOF for its subtree, return
aggregation is single-process, event forwarding is lossy, and the
parent master has no idea how many minions actually sit behind a syndic
so completeness/timeout handling is heuristic. The 3008.x master
cluster primitive fixes all of these within one cluster; master
topology extends the same fixes across clusters.

Secondary motivation: operators want to run many smaller clusters
(per-site, per-tenant, per-blast-radius) and still get single-pane
operations. Doing this without hierarchy forces operators into
multi-master-plus-syndic hybrids that nobody wants to run.

# Design

## Terminology

- **master** — a logical salt-master, which may be a single process or
  a 3008.x cluster. Minions see "a master" regardless.
- **peer master** — another logical master connected via a topology
  link.
- **cluster peer** — a process that's a Raft member of the same
  cluster as this one. Pre-existing meaning, unchanged.
- **master identity key** — the Raft-replicated key that identifies
  this logical master to peer masters.
- **topology link** — the authenticated bidirectional connection
  between two peer masters.
- **cluster_id** — short string naming this logical master in the
  topology graph. Must be unique within the graph.
- **cluster_path** — ordered list of cluster_ids a message has
  traversed; carried on every topology-forwarded message for loop
  detection and return routing.
- **origin cluster** — the cluster whose master originally received an
  operator's publish.
- **transit cluster** — a cluster whose role on a given message is
  to forward it along toward another cluster, not to resolve locally.
- **destinations** — the explicit set of clusters named in a `C@`
  scope on a publish; absent means "flood by transit-default".

## Invariants

1. Each cluster carries state about its direct peers only, never about
   descendant clusters' minions. Resting state per cluster is
   O(direct_peers + active_jobs_crossing + total_clusters_in_graph).
2. All per-minion answers at any ancestor are *computed on query*, not
   *stored at rest*.
3. The topology is a peer graph, not a tree. Links are symmetric. No
   roles.
4. Routing is derived from graph state, not declared per-link.
5. Loop detection via path on every message. Any cluster that sees
   itself in the path drops the message.
6. Minion backward compatibility: unchanged minions must work against
   topology-enabled masters without any config change.

## User-facing behavior

**Target syntax (new `C@` compound matcher):**

- `salt '*' test.ping` — floods the reachable graph; each cluster
  resolves `*` locally. Transit by default.
- `salt -C 'C@east and web-*' test.ping` — routes to cluster `east`
  only; east resolves `web-*` locally.
- `salt -C 'C@east,west and web-*' test.ping` — union; routes to east
  and west in parallel.
- `salt -C 'not C@east and web-*' test.ping` — floods; `east` sees
  `not C@east` evaluate False on its own minions, drops local publish.
- Nested peers reachable via graph routing: `salt -C 'C@east-sub1 and
  web-*'` works even if the originator peers only with `east`
  (routing table resolves `east-sub1 → via east`).

**Trust (minion-flow parallel):**

- `salt-key -L --master` — list accepted/pending/rejected peer master
  keys
- `salt-key -a east --master` — accept pending peer master `east`
- `salt-key -d east --master` — delete/revoke
- `salt-key -F --master` — fingerprint
- `auto_accept_masters: True` — mirrors `auto_accept`

## Trust model: acceptance is directional

Key acceptance governs **inbound** traffic only, exactly like the
minion→master trust model:

* **`salt-key -a <peer> --master` on cluster A → A will accept
  publishes, returns, events, and summaries sent FROM `<peer>`.**
  It does NOT grant A permission to send anything TO `<peer>`.
* **`topology.peers:` in A's config lists clusters A will
  originate outbound connections to and send publishes to.** It
  is the local **send-list**; what the other side does with those
  publishes depends on whether the other side accepted A.

The two configurations are independent. Each cluster controls what
it will *send* via its own `topology.peers:` and what it will
*receive* via its own `pki_dir/masters/` acceptance set.

**Returns inherit the publish's acceptance.** When A sends a
publish to B (requires: B accepted A), returns from B's minions
flow back along the same connection without a separate acceptance
step. B is NOT "sending" to A --- it is responding to A's request.

**Resulting operating modes fall out from the acceptance set:**

* **Bidirectional peer:** A has B in its `topology.peers:` AND B
  has A in its `topology.peers:` AND both have accepted each
  other. Publishes flow both ways.
* **Observer / one-way:** A has B in `topology.peers:` and B
  accepted A, but A has not accepted B. Publishes flow A→B only.
  B's operator cannot target A's minions. (This is what the
  earlier RFC drafts called "observer links" --- it emerges from
  the acceptance set rather than requiring a special role.)
* **Transit chain:** For A → east → east-sub1 to work, east must
  have accepted A (so A can send through east), AND east-sub1 must
  have accepted east (so east can forward on east's authority to
  east-sub1). The trust chain is explicit per hop.
* **Pure receiver:** A accepts B but has no `topology.peers:`
  entry for B. B must initiate; A will accept inbound.

**Summary (gossip) traffic obeys the same rule.** B's summary
reaches A only if A has accepted B --- otherwise A rejects the
inbound handshake. This means routing-table learning only occurs
across accepted trust boundaries.

**Terminology clarification:** throughout this doc, "peer" refers
to a cluster in another cluster's topology.conf / masters/ dir;
it does not imply symmetry of trust. Bidirectional peering is a
specific configuration (both sides accept + both sides configure)
rather than the default.

**Diagnostics:**

- `salt-run topology.status` — tree view with per-peer counts
- `salt-run topology.links` — detailed link health
- `salt-run topology.find <minion_id>` — on-demand graph-wide lookup
- `salt-run manage.status` — automatically includes aggregate counts
  per peer when topology is enabled
- `salt-call grains.get master_cluster_id` — new grain on
  topology-aware minions

## Config file: `/etc/salt/topology.conf`

Per-master, identical on every peer in a cluster. Schema:

```yaml
# Required when topology is enabled: short, unique within the graph.
cluster_id: east

# Zero or more peer entries. Each entry is a seed address for first
# connect; the full peer list is learned from Raft after handshake.
peers:
  - host: hq-master-1.example.com
    port: 4521             # topology_link_port; optional, defaults to
                           # the master's topology_link_port config

  - host: west-master-1.example.com
    # Optional per-peer fingerprint pin for TOFU avoidance
    finger: 00:11:22:33:...

# Optional: tag patterns to subscribe-forward across each peer link.
# Default is job events only; add more here for presence/full-bus.
event_subscriptions:
  - 'salt/job/*/ret/*'       # default
  - 'salt/job/*/new'         # default
  - 'salt/presence/change'   # opt-in
```

No `role:` / `parent:` / `children:` keys. Everything is a peer; the
graph shape emerges from each master's local peer list.

## New config options (`salt/config/__init__.py`)

### `VALID_OPTS` additions

```python
"topology_enabled": bool,            # Master key for topology feature
"topology_config_file": str,         # Default: /etc/salt/topology.conf
"topology_link_port": int,           # Default: 4521
"topology_summary_interval": int,    # Default: 30 (seconds)
"topology_breadcrumb_grace": int,    # Default: 30 (seconds)
"topology_breadcrumb_max_age": int,  # Default: 600 (seconds)
"topology_push_cluster_grain": bool, # Default: True; master injects
                                     # cluster_id into auth response
"auto_accept_masters": bool,         # Mirrors auto_accept for minions

# MmapCache sizing (see salt/utils/mmap_cache.py for shape)
"topology_breadcrumb_cache_size": int,   # Hash-table slot count
                                         # Default: 1_000_000
"topology_breadcrumb_cache_key_size": int,  # Bytes per key
                                            # Default: 64 (fits
                                            # <cluster_id>:<jid>)
"topology_registry_cache_size": int,     # Default: 10_000
"topology_registry_cache_key_size": int, # Default: 64
```

### `DEFAULT_MASTER_OPTS` additions

```python
"topology_enabled": False,
"topology_config_file": "/etc/salt/topology.conf",
"topology_link_port": 4521,
"topology_summary_interval": 30,
"topology_breadcrumb_grace": 30,
"topology_breadcrumb_max_age": 600,
"topology_push_cluster_grain": True,
"auto_accept_masters": False,
"topology_breadcrumb_cache_size": 1_000_000,
"topology_breadcrumb_cache_key_size": 64,
"topology_registry_cache_size": 10_000,
"topology_registry_cache_key_size": 64,
```

## PKI layout

**Non-clustered master in topology (2 keys):**
- `pki_dir/master/master.pub` + `master.pem` — unchanged, signs
  publishes to minions
- `pki_dir/master/master_identity.pub` + `master_identity.pem` —
  topology identity, generated lazily on first topology-enabled start

**Clustered master in topology (3 keys):**
- `pki_dir/master/master.pub` + `master.pem` — unchanged
- `pki_dir/master/cluster.pub` + `cluster.pem` — unchanged (3008.x)
- `pki_dir/master/master_identity.pub` + `master_identity.pem` —
  Raft-replicated via `MASTER_IDENTITY_KEY` log entry

**Accepted peer master keys (parallels `minions*/`):**
- `pki_dir/masters/` — accepted
- `pki_dir/masters_pre/` — pending
- `pki_dir/masters_rejected/` — rejected
- `pki_dir/masters_denied/` — denied

Each file is named `<cluster_id>.pub`.

## New Raft log entry types

Added to `LogEntryType` in `salt/cluster/consensus/raft/log.py`:

```python
MASTER_IDENTITY_KEY = <next_value>       # Written at bootstrap and
                                         # on identity rotation
MASTER_PEER_ATTACH = <next_value>        # Operator accepted a peer
MASTER_PEER_DETACH = <next_value>        # Operator revoked a peer
```

`MASTER_PEER_CATALOG` is reserved for the v1.1 bloom catalog feature.

**NOT replicated via Raft:**

* ``MASTER_PEER_SUMMARY`` --- summaries are soft state kept in
  the registry MmapCache and in-process routing table.
  Replicating them to Raft at the gossip rate (seconds per
  peer per cycle) causes unbounded commit pressure and blocks
  real propositions behind soft-state updates.  Each cluster
  peer receives summaries directly on its own inbound links.
* ``MASTER_PEER_ROUTE`` --- routing table is derived from the
  summary stream and recomputed locally in the publish daemon
  as summaries arrive.  No log entry needed; this was in a
  prior design draft and was removed to resolve a documented
  contradiction (snapshot replay vs live summary ingest would
  have produced divergent routing tables).

### Rolling-upgrade protocol-version gate

Rolling upgrades of a cluster where some peers are
pre-topology-release and some are topology-release would
silently diverge: new peers commit ``MASTER_*`` entries and
mutate state, old peers see unknown entry types and no-op them.
Prevent this with a cluster-wide **protocol version** flag that
all cluster peers advertise during Raft join:

* Pre-topology peers advertise protocol version N.
* Topology-aware peers advertise protocol version N+1.
* No ``MASTER_*`` log entry can be committed while any cluster
  peer still advertises N.
* ``salt-key -a --master`` on a mixed-version cluster fails
  cleanly with an operator-visible error telling them to
  finish the rolling upgrade first.

Implementation: register the protocol version as a cluster
opt propagated via ``MembershipStateMachine`` at join time;
``RaftService.propose`` for ``MASTER_*`` types checks the
cluster-wide minimum before proposing.

### State-machine interface

The topology state machine must match
:class:`salt.cluster.consensus.raft.log.BaseStateMachine`
exactly, or committed entries silently never apply:

```python
class TopologyStateMachine(BaseStateMachine):
    def apply(self, cmd, client_id, sequence_num, index=-1): ...
    def get_snapshot(self): ...
    def restore_snapshot(self, data): ...
```

Earlier drafts of this RFC used ``on_apply`` / ``snapshot`` /
``restore``; those names do not match what the Raft Node calls.

## Process model and shared state

The salt-master is multi-process. Topology code lives in one place
(the publish daemon) but needs to coordinate with MWorkers for the
return path. Shared state uses :mod:`salt.utils.mmap_cache` (the
existing cross-process hash table that backs
:mod:`salt.utils.resource_registry` and the key caches), not bespoke
IPC.

### Which process owns what

* **`_publish_daemon`** (one process): sole owner of
  :mod:`salt.topology.link_manager`, every
  :class:`~salt.topology.link.PeerMasterLink`, the in-process
  :class:`~salt.topology.router.TopologyRouter` object, and the
  routing table. Only writer to the breadcrumb and registry
  MmapCaches.
* **MWorker × N**: `AESFuncs._return` reads the breadcrumb
  MmapCache to decide whether an incoming minion return needs
  upstream forwarding. If so, MWorker fires one event
  (``salt/topology/return/<jid>``) that the publish daemon
  subscribes to; publish daemon does the actual link send.
  MWorkers never hold a PeerMasterLink.
* **EventPublisher**: unchanged. Topology forward-events pass
  through it like any other event.
* **salt-key CLI** (out-of-process): touches ``pki_dir/masters*/``
  files only. Master daemon's PKI watcher reconciles into the
  registry MmapCache (solo) or proposes Raft entries (clustered).

### Shared caches

Two MmapCaches, both under ``pki_dir/master/``:

* ``topology_breadcrumbs.mmap`` --- hot path. Keys are
  ``<cluster_id>:<jid>`` strings. Values hold the inbound-link
  identifier and an expiry timestamp. Writes on publish forwarding
  (publish daemon); reads on every minion return (MWorker, O(1)
  lock-free fast path). Entry mtime (``get_mtime``) drives TTL.
* ``topology_registry.mmap`` --- warm path. Keys are
  ``<cluster_id>`` or reserved names (``__identity_pub__``,
  ``__identity_priv__``). Values hold msgpack-serialized
  :class:`PeerState` entries. Writes are infrequent (peer attach,
  summary update, route change). Reads from the publish daemon's
  router hot path.

Breadcrumb survival across publish-daemon restart is a direct
consequence of this choice --- the mmap file persists on disk, so
a restart just re-opens it. Strictly better than syndic's
restart-drops-everything behavior.

### Write-path split (solo vs clustered)

Both modes read from the same MmapCaches. The writer differs:

* **Solo master**: publish daemon writes directly to the
  MmapCache under the cache's exclusive flock.
* **Clustered master**: write goes through Raft (operator action
  or peer traffic proposes a ``MASTER_*`` log entry), state
  machine applies the committed entry, apply handler writes to
  the MmapCache. Every peer in the cluster converges to the same
  cache contents on commit; readers everywhere see a consistent
  view via the standard MmapCache ``_cache_id`` remap cycle.

A migration from solo to clustered is one-shot: on first start
after setting ``cluster_peers``, the publish daemon proposes
Raft entries from the current MmapCache contents. From that
commit onward, writes go through Raft. See
:mod:`salt.topology.migration`.

### Fork safety

``MmapCache`` uses ``fcntl.flock``, which is per open-file-
description. The existing :mod:`salt.utils.resource_registry`
pattern already works through master pre_fork, so topology
inherits the same tested approach: defer the cache ``open`` to
after fork in each process that needs it. The master process
opens one instance; MWorkers open their own instances pointing
at the same path after fork.

## New package: `salt/topology/`

### Package module map

| File | Who owns it (process) | Purpose |
|---|---|---|
| ``salt/topology/__init__.py`` | --- | package docstring; architecture overview |
| ``salt/topology/registry.py`` | publish daemon (writer); any process (reader via MmapCache) | identity key, accepted-peer records |
| ``salt/topology/breadcrumbs.py`` | publish daemon (writer); MWorker (reader via MmapCache) | ``jid → inbound-link`` map for return routing |
| ``salt/topology/link.py`` | publish daemon only | ``PeerMasterLink``, ``LinkHealth``, ``LinkUnavailable`` |
| ``salt/topology/link_manager.py`` | publish daemon only | process-wide link registry (``ring_membership`` pattern) |
| ``salt/topology/router.py`` | publish daemon only | ``TopologyRouter``, routing table, breadcrumb lifecycle; writes to the two MmapCaches |
| ``salt/topology/summary.py`` | publish daemon only | ``ClusterSummary`` data type |
| ``salt/topology/migration.py`` | publish daemon only | one-shot solo→clustered promotion of the registry contents into Raft |
| ``salt/cluster/consensus/raft/topology.py`` | publish daemon (via Raft service) | ``TopologyStateMachine`` --- the Raft-committed write path that applies into the shared MmapCaches |

The runtime package ``salt/topology/`` does not import
``salt.cluster.consensus.*`` directly; the state machine is loaded
by the Raft service when ``cluster_peers`` is non-empty, and writes
through the same MmapCache surfaces that the solo-master direct-
write path uses.

Full signatures and docstrings for each file live in the stub
files themselves; browse them under ``salt/topology/``.

### `salt/config/topology.py`

Lives alongside ``salt/config/worker_pools.py`` --- the convention
for subsystem config loading in salt. The runtime package
``salt/topology/`` imports :class:`TopologyConfig` and
:class:`PeerRef` from here; it does not have its own ``config.py``.

Module shape mirrors ``worker_pools.py``: a defaults constant,
dataclass(es) for the parsed shape, a ``validate_*`` function, and
a ``load_*`` function as the public entry point. See the actual
stub at ``salt/config/topology.py`` for signatures and docstrings.

Public surface:

* ``DEFAULT_EVENT_SUBSCRIPTIONS: tuple[str, ...]`` --- default
  per-link event forwarding set (job events only).
* ``PeerRef`` --- frozen dataclass ``(host, port, finger)``.
* ``TopologyConfig`` --- frozen dataclass
  ``(cluster_id, peers, event_subscriptions, enabled)``.
* ``validate_topology_config(cfg: TopologyConfig) -> None`` ---
  invariant checks; raises ``ValueError`` on violation.
* ``load_topology_config(opts: dict) -> TopologyConfig`` ---
  entry point used from master configuration processing; always
  returns an instance (never ``None``).

### Per-file signatures

The authoritative source for each new file's signatures and
docstrings is the stub itself under ``salt/topology/``,
``salt/matchers/cluster_match.py``, and
``salt/runners/topology.py``. Browse those directly rather than
duplicating them here --- the RFC used to carry inline copies;
they were removed when the stubs landed to prevent drift.


## Modifications to existing files

### `salt/utils/minions.py:35`

Add ``C`` to the ``TARGET_REX`` engine character class:

```python
TARGET_REX = re.compile(
    r"""(?x)
        (
            (?P<engine>G|P|I|J|L|N|S|E|R|T|M|C)  # ADD C
            (?P<delimiter>(?<=G|P|I|J).)?
        @)?
        (?P<pattern>.+)$"""
)
```

### `salt/matchers/compound_match.py:44`

Add ``"C": "cluster"`` to the ``ref`` dict:

```python
ref = {
    "G": "grain",
    "P": "grain_pcre",
    "I": "pillar",
    "J": "pillar_pcre",
    "L": "list",
    "N": None,
    "S": "ipcidr",
    "E": "pcre",
    "T": "resource",
    "M": "managing_minion",
    "C": "cluster",   # NEW
}
```

### `salt/utils/minions.py:821` --- `_check_compound_minions`

**Critical companion to the compound_match.py change.**
``_check_compound_minions`` has its own ``ref`` dict that is
distinct from the one in ``compound_match.py``.  It runs at
``ClearFuncs.publish`` time via ``ckminions.check_minions``
*before* the publish reaches the router.  Without the ``C``
entry here, a ``C@east and web-*`` publish on origin returns
zero matched minions and silently never dispatches.

Add ``"C"`` to the ref dict with semantics "always-match-local
when this cluster is in the C@ target set; no-match otherwise."
The actual routing fan-out is done later by
:func:`salt.topology.router.on_publish`; this check is only
about whether the origin cluster itself has any local matching
minions to publish to.

### `salt/channel/server.py` --- modifications to `MasterPubServerChannel`

Three small hooks; no new class, no refactor.  All topology
business logic lives in :mod:`salt.topology`; this file just
starts and stops it.

```python
# In pre_fork, after Raft service is up:
if self.opts.get("topology_enabled"):
    import salt.topology.link_manager
    salt.topology.link_manager.pre_fork(self.opts, process_manager)

# In _publish_daemon, after existing setup:
if self.opts.get("topology_enabled"):
    import salt.topology.link_manager
    self.io_loop.create_task(
        salt.topology.link_manager.start(self.opts, self.io_loop)
    )

# In close:
if self.opts.get("topology_enabled"):
    import salt.topology.link_manager
    self.io_loop.run_until_complete(salt.topology.link_manager.stop())
```

Topology fan-out hooks into ``publish_payload`` so a locally-
originated publish also goes out on peer-master links:

```python
async def publish_payload(self, load, *args):
    # ... existing wrap_payload / minion broadcast ...
    if self.opts.get("topology_enabled"):
        import salt.topology.router
        await salt.topology.router.on_publish(load, self.io_loop)
```

The ``on_publish`` helper owns the full fan-out: route decision,
per-link task scheduling, exception gathering, failure event
emission.  ``MasterPubServerChannel`` doesn't need to know any
of that.

### `salt/channel/client.py` --- unchanged

:class:`salt.topology.link.PeerMasterLink` consumes the existing
:class:`AsyncReqChannel` / :class:`AsyncPubChannel` directly as
library primitives.  There is no topology-specific channel
class.  Session AES, path-stamped envelopes, batched return
aggregation, and tag dispatch all live in
:mod:`salt.topology.link` --- they are protocol concerns, not
channel plumbing.

### Inbound topology listener

Lives in :mod:`salt.topology.link_manager`, which already owns
outbound-link lifecycle.  The manager opens the inbound listener
on ``topology_link_port`` using existing channel/transport
primitives (:class:`salt.channel.server.ReqServerChannel` with a
topology-specific payload handler, or
:class:`salt.transport.tcp.RequestServer` directly if we need to
bypass the minion-auth-shaped dispatch).  Dispatch by tag is
done in-module; verification + handoff to the router is topology
business logic.

Inbound topology RPCs arrive on ``topology_link_port`` and are
dispatched by a new ``_topology_dispatcher`` on
``MasterPubServerChannel`` (parallel to ``_raft_dispatcher``) ---
publishes, returns, events, and summaries all handled here, in
the publish daemon's io loop. No MWorker ever receives peer-master
traffic directly.

Event-bus subscription (also in the publish daemon) picks up
MWorker-stored returns that are flagged for upstream forwarding
(see MWorker ``_return`` below).

### `salt/master.py` --- MWorker-side modifications

MWorkers do **not** hold topology state; they consult the
breadcrumb MmapCache instead.

#### `AESFuncs._return`

Extended to:

1. If ``load["jid"]`` carries a topology prefix
   (``<cluster_id>:<jid>``), look up the breadcrumb MmapCache for
   this ``jid``. On hit, this is a topology return the publish
   daemon needs to forward upstream.
2. Store locally as today (always) --- the local ``jid`` keying is
   also used for diagnostics and ``jobs.lookup_jid`` on this
   cluster.
3. On breadcrumb hit, fire
   ``salt/topology/return/<origin>/<jid>`` on the event bus with
   the return load; the publish daemon's topology event
   subscriber picks it up and dispatches on the recorded link.

```python
async def _return(self, load):
    # ... existing signature verify / resource_id remap ...

    # Store locally regardless (keyed by cluster_path when provided
    # via load["topology"]["cluster_path"] from _topology_pub).
    await loop.run_in_executor(None, store_job, self.opts, load, ...)

    # Topology-return detection via envelope, NOT jid-string parsing.
    # jids stay in their native Salt format; `is_jid`,
    # `unique_jid: True`, and shipped returner schemas all keep
    # working.
    topology = load.get("topology") or {}
    origin = topology.get("origin")
    if origin and origin != self.opts.get("cluster_id"):
        # Hand off to publish daemon for upstream forwarding.
        bc = salt.topology.breadcrumbs.open_reader(self.opts)
        if bc.lookup(load["jid"]) is not None:
            await self.event.fire_master(
                {"load": load},
                f"salt/topology/return/{origin}/{load['jid']}",
            )
```

The MWorker **never** calls ``PeerMasterLink.send_return``
directly --- that would require a live link object, which lives
in the publish daemon.

#### No ``AESFuncs._topology_*`` handlers on MWorkers

These were listed in an earlier draft of this RFC as MWorker
handlers; that was wrong. Inbound topology RPCs arrive on
``topology_link_port`` and are dispatched by
``_topology_dispatcher`` on ``MasterPubServerChannel`` (publish
daemon). The semantic handlers (verify link signature, apply
loop detection, record breadcrumb, forward or dispatch locally,
etc.) live in :class:`~salt.topology.router.TopologyRouter`.

### `salt/utils/job.py` — `store_job`

```python
def store_job(opts, load, event=None, mminion=None, cluster_path=None):
    """
    (existing docstring preserved; add:)

    :param cluster_path: Optional tuple of cluster_ids identifying
        the originating topology path for this return. When present,
        the job cache key becomes
        ``(jid, cluster_path, id)`` so duplicate minion ids
        across clusters are disambiguated. Returners that key by
        a flat path receive the ``cluster_path`` joined with ``/``
        as a path prefix on the id (e.g. ``east/web-01``).
    """
```

### `salt/crypt.py` — master identity key

```python
class MasterIdentityKey:
    """
    The topology master-identity keypair.

    * On a non-clustered master, generated lazily on first
      ``topology_enabled`` start and persisted to
      ``pki_dir/master/master_identity.{pub,pem}``.
    * On a clustered master, the keypair is written at Raft
      bootstrap time via ``MASTER_IDENTITY_KEY``; local persistence
      happens inside the Raft SM's apply handler. Any peer in the
      cluster can present the key during peer-master handshakes.

    This class is a thin reader/writer; the lifecycle is owned by
    ``TopologyStateMachine``.
    """

    def __init__(self, opts: dict) -> None:
        ...

    def exists(self) -> bool:
        """Return True if a keypair is present on disk."""

    def load(self) -> tuple[bytes, bytes]:
        """Return (pub_pem, priv_pem) bytes. Raises if absent."""

    def write(self, pub_pem: bytes, priv_pem: bytes) -> None:
        """
        Persist to disk with strict permissions (0400 priv, 0444 pub).
        Idempotent: no-op if the on-disk content matches.
        """

    def fingerprint(self) -> str:
        """Return the pub-key fingerprint using opts["hash_type"]."""
```

Also augment `AuthenticationError` paths in the minion auth response
builder (around line 1538): if `opts["topology_enabled"]` and
`opts["topology_push_cluster_grain"]`, add
`auth["cluster_id"] = opts["cluster_id"]` to the response payload.

### `salt/key.py` — `--master` scope

Add a `KeyCLI` branch handling `--master`:

```python
class Key:
    """
    (existing class; add new methods parallel to the minion-scope
    ones, parameterized by the key scope.)
    """

    def list_master(self) -> dict:
        """Return accepted/pending/rejected peer-master cluster_ids."""

    def accept_master(self, cluster_id: str) -> dict:
        """
        Accept a peer master.

        **Propose-first ordering** (critical for cluster-peer
        consistency): on clustered masters, propose
        ``MASTER_PEER_ATTACH`` carrying the pub key bytes to
        Raft, then let the state machine's apply handler on every
        peer do the file-move from ``masters_pre/`` to
        ``masters/`` atomically.  salt-key does NOT rename the
        file first --- a crash between rename and propose would
        leave the file in ``masters/`` on this master with no
        Raft commit, and other cluster peers would never see the
        attach.

        On solo masters, salt-key only touches ``masters_pre/``
        (operator-visible move) and signals the publish daemon's
        PKI watcher, which performs the registry update.
        """

    def reject_master(self, cluster_id: str) -> dict:
        """Move the key from ``masters_pre/`` to ``masters_rejected/``."""

    def delete_master(self, cluster_id: str) -> dict:
        """
        Remove the key.  Same propose-first ordering as
        :meth:`accept_master`: proposes ``MASTER_PEER_DETACH``
        and lets the state machine's apply handler on every peer
        tear down the file atomically.
        """

    def finger_master(self, cluster_id: str) -> dict:
        """Return the fingerprint of the stored key for ``cluster_id``."""
```

CLI parser (`salt/utils/parsers.py`): add a `--master` scope flag to
`SaltKeyOptionParser` that dispatches to the `*_master` methods.

### Minion-side: `master_cluster_id` grain

In `salt/grains/core.py` (or a new `salt/grains/topology.py`), add:

```python
def master_cluster_id():
    """
    Return the ``master_cluster_id`` grain, populated from the
    master's auth response (``opts["master_cluster_id"]``).

    Returns an empty dict (no grain) when the field is absent — i.e.
    when the master is not topology-enabled or does not set
    ``topology_push_cluster_grain``.
    """
```

In `salt/minion.py` auth-success path, store the response's
`cluster_id` into `self.opts["master_cluster_id"]` so the grain
module can read it.

## Wire protocol summary

### `topology.conf` RPC tags on topology_link_port

* ``topology/_master_auth`` — initial handshake (clear-channel)
* ``topology/pub`` — forwarded publish; AES-signed by the link
* ``topology/ret`` — aggregated return batch; AES-signed
* ``topology/evt`` — single selectively-forwarded event; AES-signed
* ``topology/sum`` — ``ClusterSummary`` push; AES-signed

### Publish envelope (`load["topology"]`)

```python
{
    "origin": "<cluster_id of original operator cluster>",
    "destinations": ["<cluster_id>", ...] | None,
    "transit_match": bool,          # see below
    "path": ["<cluster_id>", ...],
}
```

* ``origin`` identifies the operator's cluster.  Replaces the
  earlier ``jid_prefix``-in-the-jid-string mechanism (which broke
  ``is_jid`` validation, ``unique_jid``, and returner schemas).
  The jid itself stays in its native Salt format.
* ``destinations`` None means flood by transit-default.  When
  non-None, lists the clusters that must *match locally*; any
  cluster not in the list is pure transit and does not publish to
  its own minions.
* ``transit_match`` is True when this cluster should treat
  itself as a destination (match locally).  Explicitly set per-
  hop by the forwarder so flood vs transit is unambiguous.
* ``path`` is the ordered list of clusters that have handled
  this publish.  **Authenticated per hop**: a receiver appends
  its own authenticated ``cluster_id`` (not what the sender
  wrote) and asserts ``path[-1]`` on receipt matches the
  inbound link's peer identity.  Prevents path tampering by
  compromised peers.

### Return envelope (`load["topology"]`)

```python
{
    "origin": "<cluster_id of the originating operator cluster>",
    "cluster_path": ["<cluster_id>", ...],
    "path": ["<cluster_id>", ...],
}
```

* ``origin`` lets MWorker and router identify "is this return
  for a job I originated" via a dict lookup, not jid string
  parsing.  Setting ``origin`` on returns matches the publish
  envelope so breadcrumb lookups and job-cache keying stay
  symmetric.
* ``cluster_path`` identifies where the return originated for
  disambiguation at the origin's job cache.
* ``path`` walks back via breadcrumbs; receivers forward until
  ``path`` is empty, meaning the return has reached the origin.
  Path authentication rule from the publish envelope applies
  symmetrically here.

## Backward compatibility

* **Minions:** fully compatible. The ``topology`` envelope key is
  additive and ignored by old msgpack decoders. ``C@`` targets are
  stripped at the publish-to-minions boundary; minions see standard
  compound targets only. The ``master_cluster_id`` grain is
  additive; old minions simply do not have it.
* **Jid format:** unchanged.  An earlier RFC draft proposed
  prefixing topology-forwarded jids with ``<cluster_id>:``; that
  broke :func:`salt.utils.jid.is_jid`, ``unique_jid``,
  ``SyndicManager`` event filtering, and shipped returner schemas
  (postgres ``varchar(20)``).  Replaced with an ``origin`` field
  in the ``load["topology"]`` envelope.  Jids stay in their
  native Salt format in every code path.
* **Mixed-fleet caveat on pillar/SLS targeting:** operators who
  use the new ``master_cluster_id`` grain in ``top.sls`` matches
  or pillar top rules should be aware that pre-topology minions
  do not have the grain and will silently fail to match.  In
  mixed-version deployments, prefer master-side ``C@`` targeting
  until all minions are upgraded.
* **Existing clusters:** fully compatible. All topology opts default
  to disabled; a 3008.x cluster upgraded to the master release
  continues to run exactly as before unless the operator sets
  ``topology_enabled: True`` and provides a ``topology.conf``.
* **Clients (CLI, salt-run, salt-api):** compatible for all
  pre-topology operations. New operations (``C@``, ``topology.*``
  runners, ``salt-key --master``) require updated clients.
* **Syndic:** continues to function unchanged for the deprecation
  window; see below.

## Syndic deprecation plan

* **Potassium (3009), topology ships:** syndic unchanged, fully
  functional. Syndic docs gain a "topology is the recommended
  replacement; syndic will be removed in <future release>" banner.
  Topology docs include a migration guide.
* **Next release after Potassium:** syndic emits a
  ``DeprecationWarning`` at daemon start and in log output on every
  reconnect. Config loader logs INFO when it detects both
  ``syndic_master`` and ``topology:`` set on the same master.
* **Release N+2 (removal):** ``syndic_master`` and related opts
  become hard errors on config load, with a message pointing at the
  migration doc. ``salt-syndic`` daemon is removed from the package.

Interop during the window:

* A syndic can continue connecting to a topology-enabled master; the
  minion-shaped ``_syndic_return`` / ``_minion_event`` paths are
  unchanged.
* A topology peer whose child subtree uses syndic sees the syndic'd
  minions via the child's local resolution; no special handling at
  the topology layer.
* Running both syndic and topology on the same master is permitted
  (with INFO log) but discouraged.

## Testing

* **Unit tests:** per-module, covering ``extract_cluster_scope``,
  ``strip_cluster_scope``, ``ClusterSummary`` merge/serialize,
  ``TopologyStateMachine`` apply paths, routing table updates,
  ``breadcrumbs`` TTL via ``get_mtime``, ``registry`` round-trip
  through ``MmapCache``.
* **Functional tests:** mock-link harness exercising the end-to-end
  publish → forward → return path with 2-, 3-, and 4-cluster graphs
  including a cycle. ``MmapCache`` fork-safety test: parent writes
  a breadcrumb, forks, child reads the breadcrumb.
* **Integration tests:** multi-cluster scenario using the existing
  salt-factories framework, with real masters forming a peer graph
  and real minions attached to each. Scenarios: ``salt '*'`` flood,
  ``C@east`` scoped, duplicate minion ids across clusters, peer
  disconnect/reconnect, breadcrumb expiry, publish-daemon restart
  mid-job (verifies breadcrumbs survive via ``MmapCache``).
* **Backward-compat tests:** 3008.x minion against
  topology-enabled master; verify auth, pub/sub, returns, no
  surprise behavior on compound targets after C@ stripping.
* **Syndic coexistence tests:** syndic subtree under a topology
  peer; verify syndic'd minions reachable via graph flood and via
  scoped target.
* **Scale tests:** publish-daemon ``MmapCache`` write throughput
  with sustained publish rate; MWorker breadcrumb lookup latency
  under contention from publish-daemon writes; publish fan-out to
  N peer links with a mix of healthy and DEGRADED links.

## Alternatives

* **Replicating syndic as-is with new transport.** Rejected: the
  SPOF and single-process-aggregation problems are structural to
  the syndic design.
* **Minion-side cluster awareness required.** Rejected for v1: a
  schema bump on the minion is a compat liability. ``C@`` stripping
  at the publish boundary gives us correctness without touching
  minions. The additive ``master_cluster_id`` grain gives us the
  ergonomics with zero compat risk.
* **Global tree declared in a single config.** Rejected: breaks the
  "O(direct_peers) resting state" invariant and makes any topology
  change a multi-master config rollout.
* **Full BGP-style path-vector routing with route selection
  policy.** Rejected for v1 as over-engineered; a single ordered
  next-hop list per destination handles failover without needing a
  policy layer.
* **MinionCatalog bloom filter in v1.** Deferred to v1.1: not
  needed for correctness; only an optimization for wide trees with
  frequent single-minion lookups.

## Unresolved questions

### Transit-cluster breadcrumb failover (DECISION NEEDED)

**Problem.** Breadcrumbs live in a per-process ``MmapCache`` on
the publish daemon owning dispatch for a given cluster peer. For
**origin** and **terminal** clusters this is fine --- origin
identifies "did I originate this jid?" via the jid prefix with no
state lookup, and terminal clusters just store returns locally.
For **transit** clusters, however, if the publish-daemon-owning
peer dies with in-flight jobs routing through it, the failover
peer has an empty breadcrumb cache: inbound returns from
downstream are stored locally at the transit cluster and never
forwarded to the origin.

This is **syndic parity** --- the same observable failure happens
when a syndic daemon is restarted mid-job today. The agent review
flagged this as blocking the "strictly better than syndic" claim
for the transit case specifically; the claim remains honest for
origin and terminal.

**Three options:**

* **(mA) Batched Raft breadcrumbs on transit.** Transit
  publish daemons write to local ``MmapCache`` (hot path
  unchanged) AND enqueue to a batched Raft propose. Every 100ms
  or every 50 entries, flush a ``MASTER_PEER_BREADCRUMB`` batch.
  On failover the new owner replays committed entries into its
  cache; up to 100ms of breadcrumbs may be lost in the gap.
  Entries coalesce naturally in the log because they're
  overwrites of the same key.
* **(mB) Peer-shared breadcrumb mmap via file_sync.**
  Breadcrumbs stay per-process writer; the file itself is
  replicated via :mod:`salt.cluster.file_sync` on an interval,
  or is naturally visible for ``cluster_isolated_filesystem:
  False`` deployments. On failover, new owner opens the most
  recent synced copy.
* **(mC) Document as known limitation.** Accept transit-
  failover parity with syndic. Operator can retrieve returns
  that landed at the transit cluster via
  ``salt-run jobs.lookup_jid`` on that cluster, or retry the
  publish.

**Trade-offs:**

* (mA) adds one new LogEntryType, a batched flush timer on the
  router, and tiny Raft log growth bounded by in-flight job
  count. No change to the hot MWorker read path or the
  breadcrumbs module API.
* (mB) reuses existing cluster plumbing but mixes two
  primitives (``MmapCache`` + ``file_sync``) with a window
  during which it is ambiguous which copy is authoritative. Not
  recommended for v1.
* (mC) is zero code --- just a note.

**Recommendation:** pick (mA) if we want to beat syndic in all
cases. Pick (mC) if syndic parity on transit failover is
acceptable and we want to minimize v1 surface. (mB) is deferred.

**Decision owner:** project. Pending as of RFC writing.

### Other deferred items

* **Observer links** (upward-only, no job routing, event+summary
  subscription): deferred until a concrete user asks. Trivial to
  add on top of the current design (new ``role: observer`` on a
  link).
* **Per-link transit flag** (``transit: false`` on specific peer
  links to prevent flood relay across them): deferred until
  concrete need. Current design floods transit-by-default.
* **Multi-tenancy / ACLs** on cross-cluster targeting: v1 trusts
  every attached peer symmetrically. Fine-grained "peer A may
  publish to minions matching X, but not Y" is out of scope.
* **``MinionCatalog`` bloom filter:** deferred to v1.1; see
  Alternatives section.
* **jid format migration to UUID7:** scoped out of topology;
  worth tackling as an independent Potassium-era cleanup. The
  current ``<cluster_id>:<jid>`` prefix on topology-forwarded
  jids is sufficient without changing the base format.

## Implementation follow-ups

Audit items that are too small to re-litigate at the RFC level
but need to be addressed as the stubs grow bodies.  Captured
here so they are not forgotten.  Each has a known shape; none
require further design decisions.

* **Fuzz ``strip_cluster_scope`` against the compound grammar.**
  Property-test: for every well-formed compound expression
  containing ``C@``, after stripping, the residual must either
  (a) parse via :func:`salt.utils.minions.parse_target` and
  evaluate correctly, or (b) match the documented
  "``not C@...``, skip strip" fallback.  Residuals must never
  reach :mod:`salt.matchers.compound_match` in a syntactically
  invalid state.

* ~~**Expand nodegroups before ``extract_cluster_scope``.**  A
  nodegroup definition can contain ``C@`` tokens; the router's
  scope extraction currently runs before compound_match's
  in-place nodegroup expansion.  Pre-expand nodegroups once at
  the origin so scope extraction sees the final token set.
  Code change in :func:`salt.topology.router.extract_cluster_scope`:
  call ``salt.utils.minions.nodegroup_comp`` on any ``N@``
  token up-front.~~ **RESOLVED** --- Final-cleanup commit:
  :meth:`TopologyRouter.route_publish` now calls
  :func:`extract_cluster_scope` with ``opts["nodegroups"]``, so
  ``N@<name>`` tokens that embed ``C@`` are scope-visible.  The
  pre-existing unit tests for the expansion helper
  (``TestExtractClusterScopeNodegroups``) always passed; the
  bug was that ``route_publish`` ignored the ``nodegroups``
  kwarg.  Both ``TestRoutePublishWithNodegroups`` integration
  tests now pass.

* **``LinkHealth.QUIESCING`` drain window specifics.**  Spec
  the exact drain semantics: duration =
  ``opts["topology_detach_quiesce"]`` (default:
  ``opts["publish_timeout"]``); drain exit condition = "no
  active breadcrumbs reference this link" OR timer elapsed,
  whichever comes first; after exit, link transitions to
  DISCONNECTED and any still-in-flight returns are stored
  locally at the terminal cluster with WARNING log.  Also
  specify behavior when the quiesce timer fires mid-``send_return``:
  let the in-flight send complete, then transition.

* ~~**MWorker->publish-daemon return hand-off durability.**
  Current design fires ``salt/topology/return/<jid>`` on the
  event bus, which is pub-sub (unordered, lossy under
  backpressure).  If load testing shows return loss on burst,
  replace with a persistent bounded queue (either a dedicated
  MmapCache keyed by jid, or a filesystem spool), reading in
  the publish daemon's subscriber loop.  Interim: keep the
  event-bus hand-off but ensure the publish-daemon subscriber
  can't fall behind indefinitely (bounded consumer window with
  back-pressure to MWorker's ``fire_master``).~~ **MEASURED, NO
  ACTION NEEDED** --- Final-cleanup commit: added
  ``tests/pytests/stress/master_subprocess/eventpublisher/
  test_topology_return_handoff.py`` which drives the exact
  wire path (puller + subscriber against the real
  ``EventPublisher`` subprocess) at 1000 topology-return
  payloads/sec sustained for 10 seconds.  Measured sustained
  rate: **29,083 returns/sec, zero drops, zero duplicates,
  zero reordering**.  Drop rate < 0.0001 %; the event-bus
  hand-off is more than two orders of magnitude above the
  target burst and does not require a persistent queue.  The
  stress test is parked under the explicit stress-test gate so
  it is a measurement tool (not CI pressure) and will catch any
  future regression that erodes this margin.

* **``next_hop`` on non-leader cluster peers.**  Only the
  publish-daemon-owning peer has a live
  :mod:`salt.topology.link_manager`.
  :meth:`TopologyStateMachine.next_hop` on a follower returns
  ``None`` for every non-direct destination (link_manager is
  empty).  Decision: either (a) document that routing
  decisions are leader-only and have followers refuse to
  resolve, or (b) decouple health signaling from link_manager
  so followers can at least compute paths based on
  last-known-good summaries.  Follower-visible routing isn't
  needed by any current call site; option (a) is cheapest and
  can be revisited if a diagnostic runner wants it.

* **PeerState NamedTuple serialization schema version.**
  NamedTuples msgpack-serialize to positional tuples --- a
  reordering of fields across releases silently breaks
  snapshot restore.  Add a schema-version field or encode as
  a dict with named keys when serializing for Raft log entries
  and snapshots.

* **``atomic_rebuild`` grace period on breadcrumbs heap.**
  When ``cleanup`` triggers a rebuild, the old heap segment is
  unlinked once the new file is swapped in.  MWorkers that
  hold an mmap pointing at the old inode continue to read it
  (Linux allows unlinked-but-mapped files to stay valid), but
  a stale breadcrumb may still appear live for one lookup.
  Mitigation: delay heap-segment unlink by one
  staleness-check-interval; also make the publish daemon's
  return-forwarding path idempotent on duplicate return
  delivery (second delivery of same jid+id lands on a
  no-breadcrumb path and is dropped silently).

* ~~**Signal-safety documentation.**  Document that writers must
  not spawn subprocesses that inherit the registry /
  breadcrumbs lock fds while a lock is held --- current
  ``MmapCache`` code preserves lock fds across
  ``_close_mmaps_and_fds`` for this reason, but the topology
  stubs should state the invariant explicitly for future
  maintainers.~~ **RESOLVED** --- Final-cleanup commit: added
  "Signal-safety invariant" sections to module docstrings of
  :mod:`salt.topology.registry`, :mod:`salt.topology.breadcrumbs`,
  and :mod:`salt.utils.mmap_cache` explicitly spelling out the
  "no subprocess spawn while lock held" rule and cross-referencing
  :meth:`MmapCache._close_mmaps_and_fds`'s lock-fd preservation.

* **Returner schemas accept ``cluster_path``.**  Even though
  jids remain unprefixed (we abandoned the prefix mechanism),
  returns from topology-forwarded publishes carry a
  ``cluster_path`` field.  Shipped returners that key solely
  by ``(jid, id)`` lose the cluster_path annotation on store
  --- benign for single-cluster deploys, but
  :func:`salt-run jobs.lookup_jid` on topology deploys cannot
  disambiguate duplicate minion ids across clusters.  Audit
  each shipped returner (``local_cache``, ``postgres``,
  ``redis``, ``elasticsearch``, etc.) and extend the schema /
  key layout to accept ``cluster_path`` where ``opts["topology_enabled"]``.

### Also resolved during the final-cleanup pass

Production bugs the test harness surfaced during the
final-cleanup pass; all three fixed in the same commit:

* **``MasterPubServerChannel.pre_fork`` ordering.**
  ``process_manager.add_process`` forks the EventPublisher
  synchronously, so running ``link_manager.pre_fork`` *after*
  ``add_process`` races the child's ``open_writer`` against the
  parent's ``_init_index_file``.  The child saw partially-written
  MmapCache files (observed sizes: 7,704,576 / 23,998,464 /
  27,123,712 bytes against an expected 96,000,000) and died with
  ``RuntimeError("failed to open topology breadcrumbs writer at
  ... (did pre_create run?)")``.  Fix: swap the two blocks so
  ``link_manager.pre_fork`` finishes materialising all four
  MmapCache files BEFORE the EventPublisher subprocess forks.
* **Minion-side cluster_id lift gated on master-side opts.**
  :meth:`AsyncAuth.handle_signin_response` gated the
  ``cluster_id`` lift on
  ``self.opts.get("topology_enabled") and
  self.opts.get("topology_push_cluster_grain")`` --- but both
  opts are *master-side* config schema entries with no minion
  default, so both read as falsy on every minion in every real
  deployment and the grain was never lifted.  Fix: drop the
  minion-side gate; the policy decision lives on the master in
  :func:`salt.master._topology_cluster_id_for_auth`.
* **``TopologyRouter.route_publish`` ignored ``opts["nodegroups"]``.**
  Called :func:`extract_cluster_scope` with no ``nodegroups``
  kwarg, so a target like ``N@east_web`` where ``east_web`` is
  defined as ``C@east and web-*`` extracted no cluster scope and
  defaulted to flood.  Fix: pass ``self.opts.get("nodegroups")``
  through.

# Drawbacks

* **Implementation surface.** ~2000 lines of new code across 9 new
  files plus modifications to 8 existing files. Non-trivial.
* **Operational complexity.** Operators now have one more subsystem
  with its own PKI directory, config file, salt-key scope, and
  runner module. Documentation is critical.
* **Debugging multi-hop issues** is harder than single-cluster. A
  publish that silently drops at some transit cluster is harder to
  trace than a syndic forward failure, which was always visible at
  the one syndic daemon.
* **Mental model shift from syndic.** Operators who know syndic will
  initially expect hierarchical semantics (parent/child); the
  peer-graph model is simpler but requires explanation.
