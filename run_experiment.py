#!/usr/bin/env python3
import argparse
import csv
import json
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from string import Template

from monitoring import (
    MonitoringError,
    load_campaign_monitoring_config,
    publish_run_metrics,
    sanitize_text,
)
from vampire_integration import (
    ExternalMeasurementError,
    base_external_result,
    external_summary_fields,
    load_campaign_external_config,
    run_vampire_measurement,
)

BASE_RESULTS_DIR = Path("results")
BASE_RUNTIME_DIR = Path("runtime")
SLURM_DIR = Path("slurm")
SLURM_GENERATED_DIR = SLURM_DIR / "generated"
SLURM_TEMPLATE_FILE = SLURM_DIR / "templates" / "benchmark.sbatch"
DEFAULT_PUSHGATEWAY_URL = ""


def ensure_dirs(campaign):
    results_dir = BASE_RESULTS_DIR / campaign
    raw_results_dir = results_dir / "raw"
    logs_dir = results_dir / "logs"
    samples_rapl_dir = results_dir / "samples" / "rapl"
    samples_external_dir = results_dir / "samples" / "external"
    samples_vampire_dir = results_dir / "samples" / "vampire"
    logs_vampire_dir = logs_dir / "vampire"
    logs_monitoring_dir = logs_dir / "monitoring"
    sync_root_dir = BASE_RUNTIME_DIR / "sync"

    results_dir.mkdir(parents=True, exist_ok=True)
    raw_results_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    samples_rapl_dir.mkdir(parents=True, exist_ok=True)
    samples_external_dir.mkdir(parents=True, exist_ok=True)
    samples_vampire_dir.mkdir(parents=True, exist_ok=True)
    logs_vampire_dir.mkdir(parents=True, exist_ok=True)
    logs_monitoring_dir.mkdir(parents=True, exist_ok=True)
    sync_root_dir.mkdir(parents=True, exist_ok=True)
    SLURM_GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    return {
        "results_dir": results_dir,
        "raw_results_dir": raw_results_dir,
        "logs_dir": logs_dir,
        "samples_rapl_dir": samples_rapl_dir,
        "samples_external_dir": samples_external_dir,
        "samples_vampire_dir": samples_vampire_dir,
        "logs_vampire_dir": logs_vampire_dir,
        "logs_monitoring_dir": logs_monitoring_dir,
        "sync_root_dir": sync_root_dir,
        "summary_file": results_dir / "summary.csv",
    }


def run_cmd(cmd, check=True, input_text=None):
    return subprocess.run(
        cmd,
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=check,
    )


def normalize_pushgateway_url(url):
    url = (url or DEFAULT_PUSHGATEWAY_URL).strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://{}".format(url)
    return url


def parse_benchmark(stdout):
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except Exception:
            pass
    return {}


def read_file(path):
    if not path.exists():
        return ""
    return path.read_text()


def resolve_path(path, job_id):
    return Path(str(path).replace("%j", job_id))


def slurm_node_directives(nodelist):
    nodelist = (nodelist or "").strip()
    if not nodelist:
        return ""
    return "#SBATCH --nodelist={}".format(nodelist)


def render_slurm_script(args, run_id, dirs, external_config=None):
    template = Template(SLURM_TEMPLATE_FILE.read_text())

    job_name = args.label
    stdout_path = dirs["logs_dir"] / "{}_%j.out".format(run_id)
    stderr_path = dirs["logs_dir"] / "{}_%j.err".format(run_id)
    rapl_csv_path = dirs["samples_rapl_dir"] / "{}.csv".format(run_id)
    external_csv_path = dirs["samples_external_dir"] / "{}.csv".format(run_id)
    vampire_csv_path = dirs["samples_vampire_dir"] / "{}.csv".format(run_id)
    vampire_log_path = dirs["logs_vampire_dir"] / "{}.log".format(run_id)
    sync_dir = (dirs["sync_root_dir"] / run_id).resolve()
    script_path = SLURM_GENERATED_DIR / "{}.sbatch".format(run_id)
    external_enabled = bool(external_config and external_config.enabled)

    script = template.substitute(
        job_name=job_name,
        partition=args.partition,
        cpus=args.threads,
        node_directives=slurm_node_directives(args.nodelist),
        nodelist=args.nodelist or "",
        time_limit=args.time_limit,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        workdir=str(Path(args.workdir).resolve()),
        command=args.command,
        label=args.label,
        language=args.language,
        benchmark=args.benchmark,
        threads=args.threads,
        rep=args.rep,
        campaign=args.campaign,
        run_id=run_id,
        rapl_csv_path=str(rapl_csv_path),
        external_csv_path=str(external_csv_path),
        external_enabled="1" if external_enabled else "0",
        sync_dir=str(sync_dir),
        vampire_pre_capture_s=external_config.pre_capture_s if external_enabled else 0,
        vampire_sync_timeout_s=external_config.sync_timeout_s if external_enabled else 0,
        vampire_poll_interval_s=external_config.poll_interval_s if external_enabled else 0.5,
        pushgateway_url=args.pushgateway_url or "",
        rapl_interval=args.rapl_interval,
    )

    script_path.write_text(script)
    return (
        script_path,
        stdout_path,
        stderr_path,
        rapl_csv_path,
        external_csv_path,
        vampire_csv_path,
        vampire_log_path,
        sync_dir,
    )


def submit_job(script):
    out = run_cmd(["sbatch", str(script)]).stdout.strip()
    return out.split()[-1]


def wait_job(job_id):
    while True:
        out = subprocess.run(
            ["squeue", "-j", job_id, "-h"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()
        if not out:
            return
        time.sleep(2)


def is_job_active(job_id):
    result = subprocess.run(
        ["squeue", "-j", job_id, "-h"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return bool(result.stdout.strip())


def get_sacct_row(job_id):
    result = run_cmd(
        [
            "sacct",
            "-j",
            job_id,
            "--format=JobID,State,ElapsedRaw,NodeList,ExitCode",
            "-P",
            "-n",
        ],
        check=False,
    )

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        rows.append({
            "JobID": parts[0],
            "State": parts[1],
            "ElapsedRaw": parts[2],
            "NodeList": parts[3],
            "ExitCode": parts[4],
        })

    for row in rows:
        if row["JobID"] == job_id:
            return row

    for row in rows:
        if row["JobID"].startswith(job_id + "."):
            return row

    return {
        "JobID": job_id,
        "State": "",
        "ElapsedRaw": "",
        "NodeList": "",
        "ExitCode": "",
    }


def aggregate_rapl_csv(path):
    if not path.exists():
        return {
            "rapl_samples_count": 0,
            "rapl_energy_j": None,
            "rapl_avg_power_w": None,
            "rapl_peak_power_w": None,
        }

    count = 0
    last_total_energy_j = None
    power_values = []

    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            count += 1
            total_energy_j = row.get("total_energy_j", "")
            power_w = row.get("power_w", "")

            if total_energy_j not in ("", None):
                try:
                    last_total_energy_j = float(total_energy_j)
                except Exception:
                    pass

            if power_w not in ("", None):
                try:
                    power_values.append(float(power_w))
                except Exception:
                    pass

    avg_power = None
    peak_power = None
    if power_values:
        avg_power = sum(power_values) / float(len(power_values))
        peak_power = max(power_values)

    return {
        "rapl_samples_count": count,
        "rapl_energy_j": last_total_energy_j,
        "rapl_avg_power_w": avg_power,
        "rapl_peak_power_w": peak_power,
    }


def save_csv(row, summary_file):
    if not summary_file.exists():
        with summary_file.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            writer.writeheader()
            writer.writerow(row)
        return

    with summary_file.open("r", newline="") as f:
        reader = csv.DictReader(f)
        existing_fieldnames = reader.fieldnames or []
        existing_rows = list(reader)

    fieldnames = list(existing_fieldnames)
    for key in row.keys():
        if key not in fieldnames:
            fieldnames.append(key)

    if fieldnames == existing_fieldnames:
        with summary_file.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writerow(row)
        return

    with summary_file.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for existing_row in existing_rows:
            writer.writerow(existing_row)
        writer.writerow(row)


def run_slurm(args, run_id, dirs, external_config=None, monitoring_config=None):
    (
        script,
        out_t,
        err_t,
        rapl_csv_t,
        external_csv_t,
        vampire_csv_t,
        vampire_log_t,
        sync_dir,
    ) = render_slurm_script(args, run_id, dirs, external_config)

    job_id = submit_job(script)
    external_result = base_external_result(False)

    if external_config and external_config.enabled:
        external_result = run_vampire_measurement(
            external_config,
            run_id,
            job_id,
            sync_dir,
            vampire_csv_t,
            vampire_log_t,
            is_job_active=lambda: is_job_active(job_id),
        )

    wait_job(job_id)

    stdout_path = resolve_path(out_t, job_id)
    stderr_path = resolve_path(err_t, job_id)

    stdout = read_file(stdout_path)
    stderr = read_file(stderr_path)

    bench = parse_benchmark(stdout)
    if not bench:
        bench = parse_benchmark(stderr)
    if not bench:
        bench = parse_benchmark(stdout + "\n" + stderr)
    sacct = get_sacct_row(job_id)
    rapl_summary = aggregate_rapl_csv(rapl_csv_t)

    duration = bench.get("duration_real_s", 0)
    if not duration and sacct.get("ElapsedRaw"):
        try:
            duration = float(sacct["ElapsedRaw"])
        except Exception:
            duration = 0

    ops = bench.get("ops", 0)
    ops_per_sec = bench.get("ops_per_sec", "")
    energy_j = rapl_summary["rapl_energy_j"]
    avg_power_w = rapl_summary["rapl_avg_power_w"]
    peak_power_w = rapl_summary["rapl_peak_power_w"]

    ops_per_j = ""
    if energy_j not in (None, 0, 0.0) and ops not in (None, "", 0):
        try:
            ops_per_j = float(ops) / float(energy_j)
        except Exception:
            ops_per_j = ""

    row = {
        "campaign": args.campaign,
        "run_id": run_id,
        "timestamp": datetime.utcnow().isoformat(),
        "host": socket.gethostname(),
        "job_id": job_id,
        "job_state": sacct.get("State", ""),
        "node_list": sacct.get("NodeList", ""),
        "exit_code": sacct.get("ExitCode", ""),
        "language": args.language,
        "benchmark": args.benchmark,
        "profile": args.profile,
        "cores": args.cores,
        "threads": args.threads,
        "rep": args.rep,
        "duration_s": duration,
        "ops": ops,
        "ops_per_sec": ops_per_sec,
        "rapl_samples_count": rapl_summary["rapl_samples_count"],
        "energy_j": energy_j,
        "avg_power_w": avg_power_w,
        "peak_power_w": peak_power_w,
        "ops_per_j": ops_per_j,
        "rapl_samples_path": str(rapl_csv_t),
        "external_samples_path": external_result.get("csv_path") or str(external_csv_t),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }
    row.update(external_summary_fields(external_result))

    save_csv(row, dirs["summary_file"])

    monitoring_result = {"enabled": False, "status": "disabled", "error": "", "fail_run": False}
    if monitoring_config and monitoring_config.enabled:
        try:
            monitoring_result = publish_run_metrics(
                monitoring_config,
                row,
                debug_payload_path=dirs["logs_monitoring_dir"] / f"{run_id}-prometheus.txt",
            )
        except Exception as exc:
            monitoring_result = {
                "enabled": True,
                "status": "failed",
                "error": sanitize_text(exc),
                "fail_run": getattr(monitoring_config, "failure_policy", "continue") == "fail_run",
            }
            print(f"[WARN] monitoring push failed: {exc}")

    raw_path = dirs["raw_results_dir"] / "{}.json".format(run_id)
    with raw_path.open("w") as f:
        json.dump(
            {
                "row": row,
                "stdout": stdout,
                "stderr": stderr,
                "bench": bench,
                "sacct": sacct,
                "external_measurement": external_result,
                "monitoring": monitoring_result,
            },
            f,
            indent=2,
        )

    print(json.dumps(row, indent=2))
    return 1 if external_result.get("fail_run") or monitoring_result.get("fail_run") else 0


def install_signal_handlers():
    def _raise_keyboard_interrupt(signum, _frame):
        raise KeyboardInterrupt("received signal {}".format(signum))

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--label", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--profile", default="")
    parser.add_argument("--cores", type=int, default=0)
    parser.add_argument("--rep", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--command", required=True)

    parser.add_argument("--partition", default="guest")
    parser.add_argument("--nodelist", default="")
    parser.add_argument("--time-limit", default="00:10:00")
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--rapl-interval", type=float, default=1.0)
    parser.add_argument("--pushgateway-url", default=DEFAULT_PUSHGATEWAY_URL)
    parser.add_argument("--campaign-config", default=None)

    args = parser.parse_args()
    install_signal_handlers()

    try:
        external_config = load_campaign_external_config(args.campaign_config)
    except ExternalMeasurementError as exc:
        print("[ERROR] Configuracion de medicion externa invalida: {}".format(exc), file=sys.stderr)
        sys.exit(2)

    try:
        monitoring_config = load_campaign_monitoring_config(args.campaign_config, args.pushgateway_url)
    except MonitoringError as exc:
        print("[ERROR] Configuracion de monitoring invalida: {}".format(exc), file=sys.stderr)
        sys.exit(2)

    args.pushgateway_url = monitoring_config.pushgateway.url if monitoring_config.enabled else ""
    if not args.profile:
        args.profile = args.label
    if not args.cores:
        args.cores = args.threads

    dirs = ensure_dirs(args.campaign)
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + args.label

    sys.exit(run_slurm(args, run_id, dirs, external_config, monitoring_config))


if __name__ == "__main__":
    main()
