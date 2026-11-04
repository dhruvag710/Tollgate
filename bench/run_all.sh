#!/usr/bin/env bash
# SPEC §9's v1 benchmarks, each run RUNS times, against the mock provider in
# Docker with fixed resource limits (bench/docker-compose.bench.yml):
#
#   B1   gateway overhead, non-streamed: the same load straight to the mock and
#        through one gateway replica; overhead = gateway latency - mock latency
#   B1b  the same for streams, timed to the first byte (the first SSE chunk)
#   B2   throughput ceiling, 1 and 3 replicas behind nginx: RPS steps up until
#        a step's p99 overhead passes 50 ms, over 0.1% of requests fail, or
#        k6 can't start requests on time; the last step that held is the ceiling
#
# Raw k6 output (end-of-test summary JSON; console log, which also lists every
# failed request), a docker stats sample, and the cgroup CPU counters of the
# gateways, mock and nginx around each step (CPU used, and the CFS-quota
# periods in which each was throttled) land in
# bench/results/<UTC timestamp>/; bench/report.py turns it into the
# tables in BENCHMARKS.md. With the defaults this takes about 80 minutes.
#
#   bash bench/run_all.sh
#   RUNS=1 B2_DURATION_S=10 bash bench/run_all.sh   # a quick smoke run
#
# Needs Docker with compose and ~1.5 GB free for images and the ledger.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash: leave /scripts and /results paths alone

RUNS=${RUNS:-3}
# B1 runs well below one replica's ceiling, where overhead is overhead and not
# queueing; SPEC asks for 500 RPS, which one 1-CPU replica can't sustain here
# (see BENCHMARKS.md).
B1_RATE=${B1_RATE:-100}
B1_WARMUP_S=${B1_WARMUP_S:-10}
B1_DURATION_S=${B1_DURATION_S:-60}
B2_START=${B2_START:-50}
B2_STEP=${B2_STEP:-25}
B2_MAX=${B2_MAX:-2000}
B2_WARMUP_S=${B2_WARMUP_S:-5}
B2_DURATION_S=${B2_DURATION_S:-20}
PYTHON=${PYTHON:-python3}

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
RESULTS=bench/results/$STAMP
COMPOSE=(docker compose -p tollgate-bench -f docker-compose.yml -f docker-compose.scale.yml
         -f bench/docker-compose.bench.yml)

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

containers() {  # each running container's limits and the settings that shape it
  local id
  for id in $("${COMPOSE[@]}" ps -q); do
    # cpu.max is the kernel's CFS quota behind --cpus: "<quota>/<period>" in µs.
    echo "$(docker inspect --format \
      '{{.Name}} cpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} cmd={{json .Config.Cmd}}' "$id")" \
      "cpu.max=$(docker exec "$id" cat /sys/fs/cgroup/cpu.max | tr ' ' '/')" \
      "healthcheck=$(docker inspect --format '{{if .Config.Healthcheck}}{{json .Config.Healthcheck.Test}}{{else}}none{{end}}' "$id")"
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$id" \
      | grep -E '^(LOG_LEVEL|WEB_CONCURRENCY|PROVIDERS_CONFIG|MOCK_[A-Z_]+)=' | sed 's/^/    /' || true
  done
}

record_environment() {
  echo "started_utc: $STAMP"
  local dirty
  dirty=$(git status --porcelain -- . ':!bench/results')
  echo "git: $(git rev-parse HEAD)${dirty:+ + uncommitted changes:}"
  [ -z "$dirty" ] || echo "$dirty" | sed 's/^/    /'
  echo "shell host: $(uname -srm)"
  docker version --format 'docker: client {{.Client.Version}}, server {{.Server.Version}}'
  docker info --format 'docker VM: {{.NCPU}} CPUs, {{.MemTotal}} bytes memory, {{.OperatingSystem}}, kernel {{.KernelVersion}}'
  echo "CPU (as the VM sees it):$("${COMPOSE[@]}" exec -T redis grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"
  echo "k6: $("${COMPOSE[@]}" --profile bench run --rm -T k6 version 2>/dev/null | head -1)"
  echo "parameters: RUNS=$RUNS B1_RATE=$B1_RATE B1_WARMUP_S=$B1_WARMUP_S B1_DURATION_S=$B1_DURATION_S" \
       "B2_START=$B2_START B2_STEP=$B2_STEP B2_MAX=$B2_MAX B2_WARMUP_S=$B2_WARMUP_S B2_DURATION_S=$B2_DURATION_S"
  echo "--- containers (one gateway replica)"
  containers
}

scale() {  # replicas: run that many gateways, and point nginx at them
  "${COMPOSE[@]}" up -d --wait --scale gateway="$1" >/dev/null 2>&1
  "${COMPOSE[@]}" restart nginx >/dev/null 2>&1  # nginx resolves "gateway" at startup
  local i
  for i in $(seq 1 60); do
    "${COMPOSE[@]}" exec -T nginx wget -qO- http://127.0.0.1:8000/healthz >/dev/null 2>&1 && return
    sleep 1
  done
  echo "nginx never came up" >&2
  exit 1
}

service_cpu() {  # cgroup CPU counters (usage, throttling) of the gateways, mock and nginx
  local id
  for id in $("${COMPOSE[@]}" ps -q gateway mock_provider nginx); do
    echo "$(docker inspect --format '{{.Name}}' "$id") $(docker exec "$id" cat /sys/fs/cgroup/cpu.stat | tr '\n' ' ')"
  done
}

step() {  # name target rate warmup_s duration_s stream
  local name=$1 target=$2 rate=$3 warmup=$4 duration=$5 stream=$6
  mkdir -p "$(dirname "$RESULTS/$name")"
  # Same conditions every step: an empty ledger table.
  "${COMPOSE[@]}" exec -T postgres psql -U tollgate -d tollgate -qc 'TRUNCATE usage_events' >/dev/null
  # A docker stats sample halfway through the measured gateway phase.
  local at=$((warmup + duration + 3 + warmup + duration / 2 + 4))
  (sleep "$at"; docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
     | grep tollgate-bench > "$RESULTS/$name.docker-stats.txt") &
  service_cpu > "$RESULTS/$name.cpu-before.txt"
  "${COMPOSE[@]}" --profile bench run --rm -T \
    -e TARGET="$target" -e RATE="$rate" -e WARMUP_S="$warmup" -e DURATION_S="$duration" \
    -e STREAM="$stream" -e OUT="/results/$STAMP/$name" \
    k6 run --quiet --no-color /scripts/chat.js > "$RESULTS/$name.log" 2>&1
  service_cpu > "$RESULTS/$name.cpu-after.txt"
  wait
  tail -3 "$RESULTS/$name.log" | sed 's/^/    /'
}

mkdir -p "$RESULTS"
log "building and starting the stack (results: $RESULTS)"
"${COMPOSE[@]}" build -q
scale 1
record_environment > "$RESULTS/environment.txt"

for run in $(seq 1 "$RUNS"); do
  scale 1
  for stream in false true; do
    kind=$([ "$stream" = true ] && echo stream || echo plain)
    log "B1 run $run/$RUNS, $kind, $B1_RATE RPS"
    step "b1/run-$run/$kind" http://gateway:8000 "$B1_RATE" "$B1_WARMUP_S" "$B1_DURATION_S" "$stream"
  done
done

for run in $(seq 1 "$RUNS"); do
  for replicas in 1 3; do
    scale "$replicas"
    containers > "$RESULTS/b2-run-$run-replicas-$replicas.containers.txt"
    rate=$B2_START
    while (( rate <= B2_MAX )); do
      log "B2 run $run/$RUNS, $replicas replica(s), $rate RPS"
      name="b2/run-$run/replicas-$replicas/rate-$rate"
      step "$name" http://nginx:8000 "$rate" "$B2_WARMUP_S" "$B2_DURATION_S" false
      grep -q '"pass": true' "$RESULTS/$name.verdict.json" || break
      rate=$((rate + B2_STEP))
    done
  done
done

"${COMPOSE[@]}" down -v >/dev/null 2>&1  # the ledger volume is only for this run
log "done; report:"
"$PYTHON" bench/report.py "$RESULTS" | tee "$RESULTS/report.md"
