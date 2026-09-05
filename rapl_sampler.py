#!/usr/bin/env python3
import argparse
import csv
import os
import signal
import sys
import time
import subprocess
from typing import Dict, Optional
from datetime import datetime

DEFAULT_PUSHGATEWAY_URL = ""
RUN = True


def handle_signal(signum, frame):
    global RUN
    RUN = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def _format_labels(labels: Dict[str, str]) -> str:
    parts = []
    for k, v in labels.items():
        safe_v = str(v).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{k}="{safe_v}"')
    return "{" + ",".join(parts) + "}"


def normalize_pushgateway_url(url: Optional[str]) -> str:
    url = (url or DEFAULT_PUSHGATEWAY_URL).strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://{}".format(url)
    return url


def push_live_metrics(
    pushgateway_url: str,
    labels: Dict[str, str],
    metrics: Dict[str, float],
) -> None:
    label_str = _format_labels(labels)
    lines = []
    for metric_name, metric_value in metrics.items():
        lines.append(f"{metric_name}{label_str} {metric_value}")
    payload = "\n".join(lines) + "\n"

    subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--data-binary",
            "@-",
            f"{pushgateway_url.rstrip('/')}/metrics/job/tfg_live_rapl",
        ],
        input=payload,
        universal_newlines=True,
        check=True,
    )


def safe_push_live_metrics(
    pushgateway_url: Optional[str],
    labels: Dict[str, str],
    metrics: Dict[str, float],
) -> None:
    if not pushgateway_url:
        return

    try:
        push_live_metrics(pushgateway_url, labels, metrics)
    except Exception as exc:
        print(f"[WARN] live push failed: {exc}", file=sys.stderr)


def read_text(path):
    with open(path, "r") as f:
        return f.read().strip()


def discover_domains(base_dir=None, mode="package"):
    candidates = []
    if base_dir:
        candidates.append(base_dir)

    candidates.extend([
        "/sys/devices/virtual/powercap/intel-rapl",
        "/sys/class/powercap",
    ])

    for candidate in candidates:
        domains = []
        if not os.path.isdir(candidate):
            continue

        for root, dirs, files in os.walk(candidate):
            if "energy_uj" not in files:
                continue

            energy_path = os.path.join(root, "energy_uj")
            name_path = os.path.join(root, "name")
            max_path = os.path.join(root, "max_energy_range_uj")

            name = os.path.basename(root)
            if os.path.isfile(name_path):
                try:
                    name = read_text(name_path).strip().lower()
                except Exception:
                    pass
            else:
                name = name.lower()

            max_range = None
            if os.path.isfile(max_path):
                try:
                    max_range = int(read_text(max_path))
                except Exception:
                    max_range = None

            domains.append({
                "path": root,
                "name": name,
                "energy_path": energy_path,
                "max_range_uj": max_range,
            })

        if not domains:
            continue

        domains.sort(key=lambda d: d["path"])

        if mode == "all":
            return domains

        if mode == "package":
            package_domains = []
            for d in domains:
                tail = os.path.basename(d["path"])
                if tail.count(":") == 1:
                    package_domains.append(d)
            if package_domains:
                return package_domains

        if mode == "dram":
            dram_domains = [d for d in domains if d["name"] == "dram"]
            if dram_domains:
                return dram_domains

        if mode == "package_and_dram":
            selected = []
            for d in domains:
                tail = os.path.basename(d["path"])
                if tail.count(":") == 1:
                    selected.append(d)
                elif d["name"] == "dram":
                    selected.append(d)
            if selected:
                return selected

    return []


def read_domain_energy_uj(domain):
    return int(read_text(domain["energy_path"]))


def read_total_energy_uj(domains, prev_map):
    total_delta = 0
    current_map = {}

    for d in domains:
        path = d["path"]
        cur = read_domain_energy_uj(d)
        current_map[path] = cur

        if path not in prev_map:
            continue

        prev = prev_map[path]
        delta = cur - prev

        if delta < 0:
            max_range = d.get("max_range_uj")
            if max_range:
                delta = (max_range - prev) + cur
            else:
                delta = cur

        total_delta += delta

    return total_delta, current_map


def iso_utc_now():
    return datetime.utcnow().isoformat() + "Z"


def main():
    parser = argparse.ArgumentParser(description="Periodic RAPL sampler")
    parser.add_argument("--output", required=True, help="CSV output path")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--threads", required=True)
    parser.add_argument("--campaign", required=False, default="unknown")
    parser.add_argument("--node-name", required=False, default="")
    parser.add_argument("--pushgateway-url", required=False, default=DEFAULT_PUSHGATEWAY_URL)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--powercap-base", default="/sys/devices/virtual/powercap/intel-rapl")
    parser.add_argument("--domain-mode", default="package", choices=["package", "dram", "package_and_dram", "all"])
    args = parser.parse_args()
    args.pushgateway_url = normalize_pushgateway_url(args.pushgateway_url)

    domains = discover_domains(args.powercap_base, args.domain_mode)
    if not domains:
        print("No RAPL domains found", file=sys.stderr)
        sys.exit(1)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    fieldnames = [
        "timestamp_utc",
        "epoch_s",
        "node",
        "job_id",
        "benchmark",
        "language",
        "threads",
        "sample_idx",
        "interval_s",
        "delta_energy_j",
        "power_w",
        "total_energy_j",
    ]

    node = args.node_name or os.uname()[1]
    prev_epoch = time.time()
    sample_idx = 0
    cumulative_energy_j = 0.0

    prev_energy_map = {}
    for d in domains:
        prev_energy_map[d["path"]] = read_domain_energy_uj(d)

    live_labels = {
        "campaign": str(args.campaign),
        "benchmark": str(args.benchmark),
        "language": str(args.language),
        "threads": str(args.threads),
        "node": str(node),
    }

    # Estado inicial visible en Grafana
    safe_push_live_metrics(
        args.pushgateway_url,
        live_labels,
        {
            "tfg_live_power_watts": 0.0,
            "tfg_live_delta_energy_joules": 0.0,
            "tfg_live_total_energy_joules": 0.0,
            "tfg_live_job_running": 1.0,
        },
    )

    with open(args.output, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        while RUN:
            time.sleep(args.interval)

            now_epoch = time.time()
            delta_uj, current_map = read_total_energy_uj(domains, prev_energy_map)
            prev_energy_map = current_map

            delta_j = float(delta_uj) / 1000000.0
            interval_s = now_epoch - prev_epoch if prev_epoch else 0.0
            prev_epoch = now_epoch

            cumulative_energy_j += delta_j
            power_w = (delta_j / interval_s) if interval_s > 0 else 0.0

            writer.writerow({
                "timestamp_utc": iso_utc_now(),
                "epoch_s": "{:.6f}".format(now_epoch),
                "node": node,
                "job_id": args.job_id,
                "benchmark": args.benchmark,
                "language": args.language,
                "threads": args.threads,
                "sample_idx": sample_idx,
                "interval_s": "{:.6f}".format(interval_s),
                "delta_energy_j": "{:.6f}".format(delta_j),
                "power_w": "{:.6f}".format(power_w),
                "total_energy_j": "{:.6f}".format(cumulative_energy_j),
            })
            csvfile.flush()

            safe_push_live_metrics(
                args.pushgateway_url,
                live_labels,
                {
                    "tfg_live_power_watts": power_w,
                    "tfg_live_delta_energy_joules": delta_j,
                    "tfg_live_total_energy_joules": cumulative_energy_j,
                    "tfg_live_interval_seconds": interval_s,
                    "tfg_live_job_running": 1.0,
                },
            )

            sample_idx += 1

    # Empuje final para dejar claro que el job terminó
    safe_push_live_metrics(
        args.pushgateway_url,
        live_labels,
        {
            "tfg_live_power_watts": 0.0,
            "tfg_live_delta_energy_joules": 0.0,
            "tfg_live_job_running": 0.0,
            "tfg_live_total_energy_joules": cumulative_energy_j,
        },
    )


if __name__ == "__main__":
    main()
