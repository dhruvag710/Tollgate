#!/usr/bin/env bash
# B3's failure mode, looked at directly: how long a gateway replica takes to
# resolve the providers' hostnames once the primary mock's container is killed.
# Docker's DNS drops a stopped container's name, and the lookup of a name it
# doesn't know goes to the host's resolver; B3 found failover slowed while
# those lookups ran. Times LOOKUPS lookups of each name from a gateway
# container (getent, the C library's resolver, as Python's socket module
# uses), with the primary up and then killed, and restarts it.
#
#   bash bench/b3_dns_check.sh [OUT]   # default: bench/results/b3-dns-<UTC timestamp>.txt
#
# Needs the benchmark stack up (bash bench/run_v2.sh brings it up; or
# docker compose -p tollgate-bench -f docker-compose.yml -f docker-compose.scale.yml
# -f bench/docker-compose.bench.yml up -d --wait).
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
LOOKUPS=${LOOKUPS:-5}
OUT=${1:-bench/results/b3-dns-$(date -u +%Y%m%dT%H%M%SZ).txt}
BASE=(docker compose -p tollgate-bench -f docker-compose.yml -f docker-compose.scale.yml
      -f bench/docker-compose.bench.yml)
gateway=$("${BASE[@]}" ps -q gateway | head -1)
primary=$("${BASE[@]}" ps -q mock_provider)
[ -n "$gateway" ] && [ -n "$primary" ] || { echo "the benchmark stack isn't up" >&2; exit 1; }

lookups() {  # state: time each name's lookups from the gateway container
  local name
  for name in mock_provider mock_provider_secondary; do
    docker exec "$gateway" sh -c '
      for i in $(seq 1 '"$LOOKUPS"'); do
        a=$(date +%s%N)
        r=$(getent ahostsv4 '"$name"' | head -1 | cut -d" " -f1)
        b=$(date +%s%N)
        echo "'"$1"' '"$name"' $(( (b - a) / 1000000 )) ms ${r:-not found}"
      done'
  done
}

{
  echo "started_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "gateway container: $(docker inspect --format '{{.Name}}' "$gateway")"
  echo "resolver: $(docker exec "$gateway" cat /etc/resolv.conf | grep -v '^#' | tr '\n' ' ')"
  lookups "primary-up"
  docker kill "$primary" >/dev/null
  sleep 2
  lookups "primary-killed"
  docker start "$primary" >/dev/null
  for _ in $(seq 1 60); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$primary")" = healthy ] && break
    sleep 1
  done
  lookups "primary-back"
} | tee "$OUT"
