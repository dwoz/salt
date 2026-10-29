#!/usr/bin/env bash
# Bring up the topology sandbox: 2 clusters (east/west) + 2 minions each.
#
# Idempotent: safe to re-run.  Pre-generates master_identity keypairs on
# first run and cross-populates each master's accepted-peer directory so
# the topology handshake succeeds on first boot.
set -euo pipefail

cd "$(dirname "$0")"

HOSTS=(east west east-sub west-sub east-sub-sub west-sub-sub)

# minion count per cluster (indexed to match HOSTS)
declare -A MINIONS_PER_CLUSTER=(
  [east]=2
  [west]=2
  [east-sub]=1
  [west-sub]=1
  [east-sub-sub]=1
  [west-sub-sub]=1
)

# Peering adjacency: each entry is "<local>:<peer>".  Mirrors
# topology.conf -- east<->west, east<->east-sub, west<->west-sub,
# east-sub<->east-sub-sub, west-sub<->west-sub-sub.
PEERINGS=(
  "east:west"
  "west:east"
  "east:east-sub"
  "east-sub:east"
  "west:west-sub"
  "west-sub:west"
  "east-sub:east-sub-sub"
  "east-sub-sub:east-sub"
  "west-sub:west-sub-sub"
  "west-sub-sub:west-sub"
)

echo ">>> ensuring PKI structure"
for h in "${HOSTS[@]}"; do
  # salt-master in the container runs as root and chmods ``pki/${h}``
  # to 0700 (root-owned) once it boots.  Subsequent host-side ``mkdir
  # -p`` into those subdirs fails with EACCES.  Skip the whole mkdir
  # block when the master dir already exists so re-runs are idempotent
  # against post-boot ownership changes.
  if [[ ! -d "pki/${h}/master" ]]; then
    mkdir -p "pki/${h}"/{master,masters,minions,minions_pre,minions_rejected,minions_denied,masters_pre,masters_rejected,masters_denied}
  fi
  n="${MINIONS_PER_CLUSTER[$h]}"
  for i in $(seq 1 "$n"); do
    [[ -d "pki/${h}-minion-${i}" ]] || mkdir -p "pki/${h}-minion-${i}"
  done
done

echo ">>> generating master_identity keypairs (idempotent)"
for h in "${HOSTS[@]}"; do
  priv="pki/${h}/master/master_identity.pem"
  pub="pki/${h}/master/master_identity.pub"
  if [[ ! -f "$priv" ]]; then
    openssl genrsa -out "$priv" 2048 2>/dev/null
    chmod 0400 "$priv"
    openssl rsa -in "$priv" -pubout -out "$pub" 2>/dev/null
    chmod 0444 "$pub"
    echo "    generated $priv"
  else
    echo "    exists    $priv"
  fi
done

echo ">>> cross-populating accepted peer keys"
# For each (local, peer) adjacency, copy peer's master_identity.pub
# into local's masters/<peer>.pub -- same as `salt-key -a <peer> --master`.
for pair in "${PEERINGS[@]}"; do
  local_host="${pair%%:*}"
  peer_host="${pair##*:}"
  cp -f "pki/${peer_host}/master/master_identity.pub" \
        "pki/${local_host}/masters/${peer_host}.pub"
done

# Salt's master subdir needs master.pem/master.pub too, pre-generate so the
# container doesn't race on first start (the bind-mount means we provide them).
echo ">>> generating master signing keys (idempotent)"
for h in "${HOSTS[@]}"; do
  # salt-master may rewrite master.pem/pub as a symlink pointing into
  # the container filesystem (``/etc/salt/pki/master/topo-*.pem``) when
  # a cluster_id is seen; that leaves a dangling symlink on the host
  # between runs.  Treat symlinks as "regenerate".
  for f in master.pem master.pub; do
    if [[ -L "pki/${h}/${f}" ]]; then
      rm -f "pki/${h}/${f}"
    fi
  done
  if [[ ! -f "pki/${h}/master.pem" ]]; then
    openssl genrsa -out "pki/${h}/master.pem" 2048 2>/dev/null
    chmod 0400 "pki/${h}/master.pem"
    openssl rsa -in "pki/${h}/master.pem" -pubout -out "pki/${h}/master.pub" 2>/dev/null
    chmod 0444 "pki/${h}/master.pub"
  fi
done

echo ">>> docker build (legacy builder with host network -- buildkit has pypi reach issues in this env)"
# Only (re)build if the image is missing; subsequent runs reuse the layer cache.
if ! docker image inspect topo-sandbox-salt >/dev/null 2>&1; then
  DOCKER_BUILDKIT=0 docker build --network=host \
    -f Dockerfile.salt -t topo-sandbox-salt ../../..
fi

echo ">>> docker compose up"
docker compose up -d

echo ">>> waiting for masters to be ready"
for h in "${HOSTS[@]}"; do
  for i in $(seq 1 30); do
    if docker exec "topo-${h}" test -S /var/run/salt/master/publish_pull.ipc 2>/dev/null; then
      echo "    topo-${h}: up"
      break
    fi
    sleep 1
    [[ $i -eq 30 ]] && { echo "    topo-${h}: TIMED OUT"; exit 1; }
  done
done

echo ">>> waiting for minions to auth (auto_accept)"
expected_total=0
for h in "${HOSTS[@]}"; do
  expected_total=$((expected_total + ${MINIONS_PER_CLUSTER[$h]}))
done
for i in $(seq 1 30); do
  total=0
  for h in "${HOSTS[@]}"; do
    cnt=$(docker exec "topo-${h}" salt-key -L --out=json 2>/dev/null | \
      python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d.get("minions",[])))' 2>/dev/null || echo 0)
    total=$((total + cnt))
  done
  if [[ "$total" == "$expected_total" ]]; then
    echo "    all $expected_total minions authenticated"
    break
  fi
  sleep 1
  [[ $i -eq 30 ]] && { echo "    only $total of $expected_total minions attached after 30s"; }
done

echo
echo ">>> state:"
docker compose ps --format "    {{.Name}}\t{{.Status}}"
echo
echo "ready.  try:"
echo "    docker exec topo-east salt --timeout=15 '*' test.ping           # mesh-wide (6 minions)"
echo "    docker exec topo-east salt --timeout=15 -C 'C@west' test.ping   # scoped to west"
echo "    docker exec topo-east salt --timeout=15 -C 'C@east-sub' test.ping   # adjacent sub-cluster"
echo "    docker exec topo-west salt --timeout=15 -C 'C@east-sub' test.ping   # transit via east"
echo
echo "tear down with:  ./down.sh"
