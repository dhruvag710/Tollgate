#!/usr/bin/env bash
# SPEC §9's v2 load benchmarks, against the mock providers in Docker with the
# fixed limits of bench/docker-compose.bench.yml:
#
#   B5  cache impact: bench/workloads/make_workload.py's 20,000 requests
#       through one gateway at B5_RATE, the semantic layer off, then on
#       (bench/k6/cache_workload.js), each from an empty cache; B1's
#       non-cacheable request probes the gateway's overhead meanwhile. First,
#       the gateway's models are checked against B4's numbers
#       (bench/cache_eval/check_gateway_models.py).
#   B3  failover: B3_RUNS runs at each of B3_RATES (RATE, or RATE:RUNS for its own
#       number of runs) through nginx to 3 gateway replicas (bench/k6/failover.js);
#       B3_KILL_AT_S into each run's measured part the primary mock is killed
#       (docker kill), and restarted after.
#
# Everything lands in bench/results/v2-<UTC timestamp>/; bench/v2_report.py
# turns it into the tables in BENCHMARKS.md. With the defaults this takes about
# 75 minutes.
#
#   bash bench/run_v2.sh
#   PARTS=b5 B5_LIMIT=600 bash bench/run_v2.sh         # a quick B5 smoke run
#   PARTS=b3 B3_RATES=200 B3_RUNS=1 bash bench/run_v2.sh
#   PARTS=b3 B3_BY_IP=1 B3_LABEL=by-ip bash bench/run_v2.sh   # B3's control
#
# Needs Docker with compose, the B5 workload's text (python
# bench/workloads/make_workload.py --text) and ~4 GB free for the images.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash: leave /scripts and /results paths alone
# The repository as the Docker daemon sees it: a Windows path under Git Bash.
HOST_PWD=$(pwd -W 2>/dev/null || pwd)

PARTS=${PARTS:-"b5 b3"}
B5_RATE=${B5_RATE:-20}
B5_PROBE_RATE=${B5_PROBE_RATE:-20}
B5_IDLE_S=${B5_IDLE_S:-60}
B5_LIMIT=${B5_LIMIT:-0}
B5_MOCK_LATENCY_MS=${B5_MOCK_LATENCY_MS:-1000}
B5_OUTPUT_TOKENS=${B5_OUTPUT_TOKENS:-128}
B5_CONFIGS=${B5_CONFIGS:-"off on"}
B3_RATES=${B3_RATES:-"300 200"}
B3_RUNS=${B3_RUNS:-3}
B3_WARMUP_S=${B3_WARMUP_S:-10}
B3_DURATION_S=${B3_DURATION_S:-120}
B3_KILL_AT_S=${B3_KILL_AT_S:-40}
B3_PRE_VUS=${B3_PRE_VUS:-50}  # k6 VUs ready before the kill (up to 600 on demand)
# B3_BY_IP=1: the gateways reach the mocks by IP address, not by name
# (bench/docker-compose.b3-by-ip.yml), so a killed primary costs no name
# lookups: the control for what lookups of a vanished name do to failover.
B3_BY_IP=${B3_BY_IP:-0}
B3_LABEL=${B3_LABEL:-}  # added to each B3 run's file names
B4_RESULTS=${B4_RESULTS:-bench/results/b4-20261105T114809Z}
PYTHON=${PYTHON:-python3}

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
NAME=v2-$STAMP
RESULTS=bench/results/$NAME
BASE=(docker compose -p tollgate-bench -f docker-compose.yml -f docker-compose.scale.yml
      -f bench/docker-compose.bench.yml)
B5_OFF=("${BASE[@]}" -f bench/docker-compose.b5.yml)
B5_ON=("${B5_OFF[@]}" -f docker-compose.semantic.yml)

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

psql_() { "${BASE[@]}" exec -T postgres psql -U tollgate -d tollgate -qAt "$@"; }

counters() {  # cgroup CPU and memory counters of the stack's containers
  local id
  for id in $("${BASE[@]}" ps -q gateway mock_provider mock_provider_secondary postgres redis nginx); do
    echo "$(docker inspect --format '{{.Name}}' "$id")" \
      "$(docker exec "$id" cat /sys/fs/cgroup/cpu.stat | tr '\n' ' ')" \
      "memory_current $(docker exec "$id" cat /sys/fs/cgroup/memory.current)" \
      "memory_peak $(docker exec "$id" cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo 0)" \
      "$(docker exec "$id" grep -E '^(anon|file) ' /sys/fs/cgroup/memory.stat | sed 's/^/memory_/' | tr '\n' ' ')"
  done
}

containers() {  # each running container's limits, image and the settings that shape it
  local id
  for id in $("${BASE[@]}" ps -q); do
    echo "$(docker inspect --format \
      '{{.Name}} image={{.Config.Image}} cpus={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}}' "$id")" \
      "cpu.max=$(docker exec "$id" cat /sys/fs/cgroup/cpu.max | tr ' ' '/')"
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$id" \
      | grep -E '^(LOG_LEVEL|WEB_CONCURRENCY|PROVIDERS_CONFIG|SERVER_TIMING|SEMANTIC_[A-Z_]+|MOCK_[A-Z_]+)=' \
      | sed 's/^/    /' || true
  done
}

watch_start() {  # out: the host watcher, and a docker stats sample every 30 s
  rm -f "$1.stop"
  "$PYTHON" bench/watch_host.py "$1.host.jsonl" "$1.stop" 2 &
  WATCHER=$!
  ( while [ ! -e "$1.stop" ]; do
      docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' \
        | grep tollgate-bench | sed "s/^/$(date -u +%H:%M:%S) /"
      sleep 30
    done > "$1.docker-stats.txt" ) &
  STATS=$!
}

watch_stop() {
  touch "$1.stop"
  wait "$WATCHER" "$STATS" || true
  rm -f "$1.stop"
}

host_memory() {  # what Windows can still commit (Docker's VM counts against it)
  "$PYTHON" bench/watch_host.py --commit
}

drop_caches() {  # out: empty the Docker VM's page cache before a measured part
  # Windows counts the VM's page cache as memory in use: on this laptop it
  # filled the VM's 7.6 GiB and took the host to its commit limit, and new
  # threads on the host failed. Dropping it (only clean, cached file data)
  # before every measured part gives the host room, and every part the same
  # cold start; each part's warm-up then reads back what it needs.
  { echo "before: $(host_memory)"
    docker run --rm --privileged alpine:3 sh -c 'sync && echo 3 > /proc/sys/vm/drop_caches'
    sleep 5
    echo "after: $(host_memory)"; } > "$1.memory.txt" 2>&1
}

reset_state() {  # an empty ledger and cache, and nothing in Redis
  psql_ -c 'TRUNCATE usage_events, cache_entries' >/dev/null
  "${BASE[@]}" exec -T redis redis-cli FLUSHALL >/dev/null
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
  echo "parameters: PARTS=\"$PARTS\" B5_RATE=$B5_RATE B5_PROBE_RATE=$B5_PROBE_RATE B5_IDLE_S=$B5_IDLE_S" \
       "B5_LIMIT=$B5_LIMIT B5_MOCK_LATENCY_MS=$B5_MOCK_LATENCY_MS B5_OUTPUT_TOKENS=$B5_OUTPUT_TOKENS" \
       "B5_CONFIGS=\"$B5_CONFIGS\" B3_RATES=\"$B3_RATES\" B3_RUNS=$B3_RUNS B3_WARMUP_S=$B3_WARMUP_S" \
       "B3_DURATION_S=$B3_DURATION_S B3_KILL_AT_S=$B3_KILL_AT_S B3_PRE_VUS=$B3_PRE_VUS"
  "$PYTHON" -c "import sys; sys.path.insert(0, 'bench/cache_eval'); import machine; print(machine.as_text(machine.describe()))" \
    | sed 's/^/host /'
  echo "host memory: $(host_memory)"
  echo "host disk free (the repository's drive): $(df -h . | awk 'NR == 2 {print $4}')"
}

b5() {
  mkdir -p "$RESULTS/b5"
  log "B5: building the semantic gateway image"
  "${B5_ON[@]}" build -q gateway
  "${BASE[@]}" build -q
  log "B5: the gateway's models against B4's numbers"
  docker run --rm -e HF_HUB_DISABLE_PROGRESS_BARS=1 -e TRANSFORMERS_VERBOSITY=error \
    --mount "type=bind,source=$HOST_PWD/bench,target=/bench,readonly" \
    --mount "type=bind,source=$HOST_PWD/$RESULTS/b5,target=/out" \
    $(sed -n 's/^ *\(SEMANTIC_CACHE_[A-Z_]*\): *"\{0,1\}\([^"]*\)"\{0,1\}$/-e \1=\2/p' docker-compose.semantic.yml) \
    tollgate-gateway-semantic python -W ignore /bench/cache_eval/check_gateway_models.py \
    "/bench/${B4_RESULTS#bench/}" --out /out/model-parity.json > "$RESULTS/b5/model-parity.log" 2>&1
  tail -12 "$RESULTS/b5/model-parity.log" | sed 's/^/    /'
  local config compose
  for config in $B5_CONFIGS; do
    if [ "$config" = on ]; then compose=("${B5_ON[@]}"); else compose=("${B5_OFF[@]}"); fi
    log "B5: layer $config: starting one gateway"
    "${compose[@]}" up -d --no-build --wait --force-recreate --scale gateway=1 gateway >/dev/null 2>&1
    reset_state
    containers > "$RESULTS/b5/$config.containers.txt"
    local out="$RESULTS/b5/$config" tenant="b5-$config-$STAMP"
    drop_caches "$out"
    counters > "$out.counters-before.txt"
    watch_start "$out"
    log "B5: layer $config: replaying (rate $B5_RATE/s)"
    "${compose[@]}" --profile bench run --rm -T \
      -e TARGET=http://gateway:8000 -e RATE="$B5_RATE" -e PROBE_RATE="$B5_PROBE_RATE" \
      -e IDLE_S="$B5_IDLE_S" -e LIMIT="$B5_LIMIT" -e MOCK_LATENCY_MS="$B5_MOCK_LATENCY_MS" \
      -e OUTPUT_TOKENS="$B5_OUTPUT_TOKENS" -e TENANT="$tenant" -e OUT="/results/$NAME/b5/$config" \
      k6 run --quiet --no-color --log-format=raw \
      --log-output="file=/results/$NAME/b5/$config.records.jsonl" /scripts/cache_workload.js \
      > "$out.log" 2>&1
    watch_stop "$out"
    sleep 3  # the ledger's last batch, and the cache's last writes
    counters > "$out.counters-after.txt"
    psql_ -F, -c "SELECT e.cache_status, e.status, count(*), sum(e.prompt_tokens),
                    sum(e.completion_tokens), sum(e.cost_usd), sum(e.saved_cost_usd)
                  FROM usage_events e JOIN tenants t ON t.id = e.tenant_id
                  WHERE t.name = '$tenant' GROUP BY 1, 2 ORDER BY 1, 2" > "$out.ledger.csv"
    psql_ -c "SELECT count(*) FROM cache_entries" > "$out.cache-entries.txt"
    "${compose[@]}" exec -T gateway python -c \
      "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())" \
      > "$out.metrics.txt"
    docker logs "$("${compose[@]}" ps -q gateway)" > "$out.gateway.log" 2>&1 || true
    tail -6 "$out.log" | sed 's/^/    /'
  done
}

b3_scale() {  # three gateway replicas behind nginx (bench/run_all.sh's scale)
  "${B3C[@]}" up -d --wait --scale gateway=3 >/dev/null 2>&1
  "${BASE[@]}" restart nginx >/dev/null 2>&1
  local i
  for i in $(seq 1 60); do
    "${BASE[@]}" exec -T nginx wget -qO- http://127.0.0.1:8000/healthz >/dev/null 2>&1 && return
    sleep 1
  done
  echo "nginx never came up" >&2
  exit 1
}

primary_up() {  # start the primary mock if it is down, and wait for it to be healthy
  local id
  id=$("${BASE[@]}" ps -aq mock_provider)
  docker start "$id" >/dev/null
  for _ in $(seq 1 60); do
    [ "$(docker inspect --format '{{.State.Health.Status}}' "$id")" = healthy ] && return
    sleep 1
  done
  echo "the primary mock never came back" >&2
  exit 1
}

by_ip() {  # out: the providers file with each mock's current address in place of its name
  local primary secondary
  primary=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    "$("${BASE[@]}" ps -q mock_provider)")
  secondary=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
    "$("${BASE[@]}" ps -q mock_provider_secondary)")
  sed -e "s#//mock_provider:8001#//$primary:8001#" \
      -e "s#//mock_provider_secondary:8001#//$secondary:8001#" config/providers.test.yaml > "$1"
}

b3() {
  mkdir -p "$RESULTS/b3"
  B3C=("${BASE[@]}")
  if [ "$B3_BY_IP" = 1 ]; then
    export B3_PROVIDERS_FILE="./$RESULTS/b3/providers.by-ip.yaml"
    cp config/providers.test.yaml "$B3_PROVIDERS_FILE"  # until the mocks have addresses
    B3C+=(-f bench/docker-compose.b3-by-ip.yml)
  fi
  log "B3: building and starting 3 replicas"
  "${BASE[@]}" build -q
  "${B3C[@]}" up -d --wait --force-recreate --scale gateway=3 >/dev/null 2>&1
  b3_scale
  containers > "$RESULTS/b3/containers.txt"
  local spec rate runs run
  for spec in $B3_RATES; do
    rate=${spec%%:*}
    runs=$B3_RUNS
    [ "$spec" = "$rate" ] || runs=${spec#*:}
    for run in $(seq 1 "$runs"); do
      local name="rate-$rate${B3_LABEL:+-$B3_LABEL}-run-$run"
      local out="$RESULTS/b3/$name"
      primary_up
      if [ "$B3_BY_IP" = 1 ]; then
        by_ip "$B3_PROVIDERS_FILE"
        cp "$B3_PROVIDERS_FILE" "$out.providers.yaml"
      fi
      # Fresh replicas every run: no run inherits another's state, and each
      # replica's counters (replicas.txt) are that run's alone.
      "${B3C[@]}" up -d --wait --force-recreate --scale gateway=3 gateway >/dev/null 2>&1
      b3_scale
      reset_state
      drop_caches "$out"
      counters > "$out.counters-before.txt"
      watch_start "$out"
      log "B3: $rate RPS, run $run/$runs: killing the primary at ${B3_KILL_AT_S}s"
      "${BASE[@]}" --profile bench run --rm -T \
        -e TARGET=http://nginx:8000 -e RATE="$rate" -e WARMUP_S="$B3_WARMUP_S" \
        -e DURATION_S="$B3_DURATION_S" -e PRE_VUS="$B3_PRE_VUS" -e OUT="/results/$NAME/b3/$name" \
        k6 run --quiet --no-color --log-format=raw \
        --log-output="file=/results/$NAME/b3/$name.records.jsonl" /scripts/failover.js \
        > "$out.log" 2>&1 &
      local k6=$!
      sleep $((B3_WARMUP_S + B3_KILL_AT_S + 3))  # k6 takes ~3 s to start and set up
      local primary
      primary=$("${BASE[@]}" ps -q mock_provider)
      docker kill "$primary" >/dev/null
      docker inspect --format '{{.State.FinishedAt}}' "$primary" > "$out.killed-at.txt"
      wait "$k6" || true
      watch_stop "$out"
      sleep 2
      counters > "$out.counters-after.txt"
      local tenant
      tenant=$("$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['tenant'])" "$out.summary.json")
      psql_ -F, -c "SELECT status, provider, attempts, count(*) FROM usage_events
                    WHERE tenant_id = '$tenant' GROUP BY 1, 2, 3 ORDER BY 1, 2, 3" > "$out.ledger.csv"
      local gw  # each replica's own counts: which of them failed, and how
      for gw in $("${BASE[@]}" ps -q gateway); do
        echo "== $(docker inspect --format '{{.Name}}' "$gw")"
        docker exec "$gw" python -c \
          "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())" \
          | grep -E '^tollgate_(requests_total|provider_attempts_total|breaker_transitions_total)' || true
        docker logs --since "$(cat "$out.killed-at.txt" | cut -c1-19)Z" "$gw" 2>&1 | tail -20 | sed 's/^/    log: /'
      done > "$out.replicas.txt"
      tail -2 "$out.log" | sed 's/^/    /'
    done
  done
  primary_up
}

mkdir -p "$RESULTS"
log "results: $RESULTS"
record_environment > "$RESULTS/environment.txt"
for part in $PARTS; do
  "$part"
done
"${BASE[@]}" down -v >/dev/null 2>&1  # the volumes are only for this run
log "done; report:"
"$PYTHON" bench/v2_report.py "$RESULTS" | tee "$RESULTS/report.md"
