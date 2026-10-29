#!/usr/bin/env bash
# Run a few smoke-test publishes to verify the sandbox round-trips.
# Each check prints PASS/FAIL and a one-line summary.
set -uo pipefail
cd "$(dirname "$0")"

pass=0
fail=0

check() {
  local label="$1" cmd="$2" expect="$3"
  local out
  out=$(eval "$cmd" 2>&1 || true)
  if echo "$out" | grep -q "$expect"; then
    echo "PASS: $label"
    pass=$((pass+1))
  else
    echo "FAIL: $label"
    echo "    cmd:    $cmd"
    echo "    expect: $expect"
    echo "    got:    $(echo "$out" | head -5 | sed 's/^/      /')"
    fail=$((fail+1))
  fi
}

check_only() {
  # Like `check` but asserts the output does NOT contain some string.
  local label="$1" cmd="$2" negate="$3"
  local out
  out=$(eval "$cmd" 2>&1 || true)
  if echo "$out" | grep -q "$negate"; then
    echo "FAIL: $label"
    echo "    cmd:                  $cmd"
    echo "    must NOT contain:     $negate"
    echo "    got:                  $(echo "$out" | head -5 | sed 's/^/      /')"
    fail=$((fail+1))
  else
    echo "PASS: $label"
    pass=$((pass+1))
  fi
}

echo "=== global * publishes (expect all 4 minions, mesh-wide) ==="
check "east -> * includes west-minion-1 (cross-cluster)" \
  "docker exec topo-east salt --timeout=15 '*' test.ping --out=json" \
  '"west-minion-1": true'
check "east -> * includes west-minion-2 (cross-cluster)" \
  "docker exec topo-east salt --timeout=15 '*' test.ping --out=json" \
  '"west-minion-2": true'
check "west -> * includes east-minion-1 (cross-cluster)" \
  "docker exec topo-west salt --timeout=15 '*' test.ping --out=json" \
  '"east-minion-1": true'

echo
echo "=== scoped C@<peer> publishes (expect only peer's minions) ==="
check "east -> C@west includes west-minion-1" \
  "docker exec topo-east salt --timeout=15 -C 'C@west' test.ping --out=json" \
  '"west-minion-1": true'
check "east -> C@west includes west-minion-2" \
  "docker exec topo-east salt --timeout=15 -C 'C@west' test.ping --out=json" \
  '"west-minion-2": true'
check_only "east -> C@west MUST NOT include east-minion-1" \
  "docker exec topo-east salt --timeout=15 -C 'C@west' test.ping --out=json" \
  '"east-minion-1"'
check "west -> C@east includes east-minion-1" \
  "docker exec topo-west salt --timeout=15 -C 'C@east' test.ping --out=json" \
  '"east-minion-1": true'
check_only "west -> C@east MUST NOT include west-minion-1" \
  "docker exec topo-west salt --timeout=15 -C 'C@east' test.ping --out=json" \
  '"west-minion-1"'

echo
echo "=== self-cluster compound (expect only local minions) ==="
check "east -> C@east includes east-minion-1" \
  "docker exec topo-east salt --timeout=10 -C 'C@east' test.ping --out=json" \
  '"east-minion-1": true'
check_only "east -> C@east MUST NOT include west-minion-1" \
  "docker exec topo-east salt --timeout=15 -C 'C@east' test.ping --out=json" \
  '"west-minion-1"'

echo
echo "=== adjacent sub-cluster (one-hop) ==="
check "east -> C@east-sub includes east-sub-minion-1" \
  "docker exec topo-east salt --timeout=15 -C 'C@east-sub' test.ping --out=json" \
  '"east-sub-minion-1": true'
check_only "east -> C@east-sub MUST NOT include east-minion-1" \
  "docker exec topo-east salt --timeout=15 -C 'C@east-sub' test.ping --out=json" \
  '"east-minion-1"'

echo
echo "=== transit sub-cluster (two-hop: west -> east -> east-sub) ==="
check "west -> C@east-sub includes east-sub-minion-1 (transit through east)" \
  "docker exec topo-west salt --timeout=20 -C 'C@east-sub' test.ping --out=json" \
  '"east-sub-minion-1": true'
check "east -> C@west-sub includes west-sub-minion-1 (transit through west)" \
  "docker exec topo-east salt --timeout=20 -C 'C@west-sub' test.ping --out=json" \
  '"west-sub-minion-1": true'

echo
echo "=== mesh-wide * includes all 6 minions ==="
check "east -> * includes east-sub-minion-1" \
  "docker exec topo-east salt --timeout=20 '*' test.ping --out=json" \
  '"east-sub-minion-1": true'
check "east -> * includes west-sub-minion-1" \
  "docker exec topo-east salt --timeout=20 '*' test.ping --out=json" \
  '"west-sub-minion-1": true'

echo
echo "---------------------------"
echo "pass: $pass    fail: $fail"
[[ $fail -eq 0 ]] && exit 0 || exit 1
