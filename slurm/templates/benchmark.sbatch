#!/bin/bash
#SBATCH -J ${job_name}
#SBATCH -p ${partition}
#SBATCH -n 1
#SBATCH -c ${cpus}
${node_directives}
#SBATCH --exclusive
#SBATCH --time=${time_limit}
#SBATCH -o ${stdout_path}
#SBATCH -e ${stderr_path}

set -euo pipefail

export BENCHMARK="${benchmark}"
export LANGUAGE="${language}"
export THREADS="${threads}"
export CAMPAIGN="${campaign}"
export RUN_ID="${run_id}"
export PUSHGATEWAY_URL="${pushgateway_url}"
export RAPL_CSV_PATH="${rapl_csv_path}"
export EXTERNAL_CSV_PATH="${external_csv_path}"
export EXTERNAL_MEASUREMENT_ENABLED="${external_enabled}"
export SYNC_DIR="${sync_dir}"
export VAMPIRE_PRE_CAPTURE_S="${vampire_pre_capture_s}"
export VAMPIRE_SYNC_TIMEOUT_S="${vampire_sync_timeout_s}"
export VAMPIRE_POLL_INTERVAL_S="${vampire_poll_interval_s}"

echo "JOB_ID=$$SLURM_JOB_ID"
echo "HOSTNAME=$$(hostname)"
echo "SLURMD_NODENAME=$${SLURMD_NODENAME:-}"
echo "START_TS=$$(date +%s)"
echo "REQUESTED_NODELIST=${nodelist}"
echo "LABEL=${label}"
echo "LANGUAGE=$$LANGUAGE"
echo "BENCHMARK=$$BENCHMARK"
echo "THREADS=$$THREADS"
echo "REP=${rep}"
echo "CAMPAIGN=$$CAMPAIGN"
echo "RUN_ID=$$RUN_ID"
echo "PUSHGATEWAY_URL=$$PUSHGATEWAY_URL"
echo "RAPL_CSV=$$RAPL_CSV_PATH"
echo "EXTERNAL_CSV=$$EXTERNAL_CSV_PATH"
echo "EXTERNAL_MEASUREMENT_ENABLED=$$EXTERNAL_MEASUREMENT_ENABLED"
echo "SYNC_DIR=$$SYNC_DIR"

cd ${workdir}

if [ "$$EXTERNAL_MEASUREMENT_ENABLED" = "1" ]; then
  mkdir -p "$$SYNC_DIR"
  JOB_HOSTNAME="$$(hostname)"
  JOB_STARTED_NS="$$(date +%s%N)"
  JOB_STARTED_ISO="$$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")"

  python3 - "$$SYNC_DIR/job_started.json" "$$RUN_ID" "$$SLURM_JOB_ID" "$$JOB_HOSTNAME" "$$JOB_STARTED_NS" "$$JOB_STARTED_ISO" <<'PY'
import json
import os
import sys

target, run_id, job_id, hostname, timestamp_ns, timestamp_iso = sys.argv[1:7]
payload = {
    "run_id": run_id,
    "slurm_job_id": job_id,
    "hostname": hostname,
    "timestamp_ns": int(timestamp_ns),
    "timestamp_iso": timestamp_iso,
}
tmp = "{}.tmp.{}".format(target, os.getpid())
with open(tmp, "w") as handle:
    json.dump(payload, handle, indent=2)
os.replace(tmp, target)
PY

  SYNC_TIMEOUT_INT="$${VAMPIRE_SYNC_TIMEOUT_S%.*}"
  if ! [ "$$SYNC_TIMEOUT_INT" -gt 0 ] 2>/dev/null; then
    SYNC_TIMEOUT_INT=120
  fi
  SYNC_DEADLINE=$$(($$(date +%s) + SYNC_TIMEOUT_INT))

  while [ ! -f "$$SYNC_DIR/vampire_ready" ]; do
    if [ -f "$$SYNC_DIR/vampire_abort.json" ]; then
      echo "Vampire abort requested by orchestrator:"
      cat "$$SYNC_DIR/vampire_abort.json" || true
      exit 97
    fi
    if [ "$$(date +%s)" -ge "$$SYNC_DEADLINE" ]; then
      echo "Timeout waiting for vampire_ready in $$SYNC_DIR"
      exit 98
    fi
    sleep "$$VAMPIRE_POLL_INTERVAL_S"
  done

  sleep "$$VAMPIRE_PRE_CAPTURE_S"
fi

python3 rapl_sampler.py \
  --output "$$RAPL_CSV_PATH" \
  --job-id "$$SLURM_JOB_ID" \
  --benchmark "$$BENCHMARK" \
  --language "$$LANGUAGE" \
  --threads "$$THREADS" \
  --campaign "$$CAMPAIGN" \
  --node-name "$${SLURMD_NODENAME:-$$(hostname)}" \
  --pushgateway-url "$$PUSHGATEWAY_URL" \
  --interval ${rapl_interval} \
  --domain-mode package &
RAPL_PID=$$!

set +e
echo "CPU AFFINITY:"
taskset -cp $$$$
numactl --show
BENCHMARK_START_NS="$$(date +%s%N)"
BENCHMARK_START_ISO="$$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")"
${command}
CMD_RC=$$?
BENCHMARK_END_NS="$$(date +%s%N)"
BENCHMARK_END_ISO="$$(date -u +"%Y-%m-%dT%H:%M:%S.%NZ")"
set -e

kill "$$RAPL_PID" 2>/dev/null || true
wait "$$RAPL_PID" 2>/dev/null || true

if [ "$$EXTERNAL_MEASUREMENT_ENABLED" = "1" ]; then
  python3 - "$$SYNC_DIR/benchmark_finished.json" "$$RUN_ID" "$$SLURM_JOB_ID" "$$(hostname)" "$$BENCHMARK_START_NS" "$$BENCHMARK_START_ISO" "$$BENCHMARK_END_NS" "$$BENCHMARK_END_ISO" "$$CMD_RC" <<'PY'
import json
import os
import sys

(
    target,
    run_id,
    job_id,
    hostname,
    benchmark_start_ns,
    benchmark_start_iso,
    benchmark_end_ns,
    benchmark_end_iso,
    return_code,
) = sys.argv[1:10]
payload = {
    "run_id": run_id,
    "slurm_job_id": job_id,
    "hostname": hostname,
    "benchmark_start_ns": int(benchmark_start_ns),
    "benchmark_start_iso": benchmark_start_iso,
    "benchmark_end_ns": int(benchmark_end_ns),
    "benchmark_end_iso": benchmark_end_iso,
    "return_code": int(return_code),
}
tmp = "{}.tmp.{}".format(target, os.getpid())
with open(tmp, "w") as handle:
    json.dump(payload, handle, indent=2)
os.replace(tmp, target)
PY
fi

echo "END_TS=$$(date +%s)"
exit "$$CMD_RC"
