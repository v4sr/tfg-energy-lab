#!/usr/bin/env python3
import argparse
import itertools
import json
import shlex
import subprocess
import sys

from monitoring import MonitoringError, load_campaign_monitoring_config
from vampire_integration import (
    ExternalMeasurementError,
    load_campaign_external_config,
    preflight_vampire,
)

DEFAULT_PUSHGATEWAY_URL = ""

COMMANDS = {
    ("c", "memory-stream-hpc"): "./benchmarks/c/memory-stream-hpc/memory-stream-hpc --threads {threads} --duration {duration}",
    ("c", "compute-dgemm-hpc"): "./benchmarks/c/compute-dgemm-hpc/compute-dgemm-hpc --threads {threads} --duration {duration}",
    ("c", "idle"): "sleep {duration}",
}

BENCHMARK_ALIASES = {
    "memory_stream_hpc": "memory-stream-hpc",
    "compute_dgemm_hpc": "compute-dgemm-hpc",
}

BENCHMARK_PARAM_ARGS = {
    "memory-stream-hpc": ("size",),
    "compute-dgemm-hpc": ("size",),
}


def load_campaign(path):
    with open(path, "r") as f:
        return json.load(f)


def build_parser():
    parser = argparse.ArgumentParser(description="Launch a campaign from JSON config")
    parser.add_argument("--config", required=True)
    parser.add_argument("--pushgateway-url", default=None)
    return parser


def require_profile_fields(profile):
    required_fields = ("name", "threads", "cpus", "command_prefix")
    missing = [field for field in required_fields if field not in profile]
    if missing:
        raise ValueError("Execution profile missing fields {}: {}".format(missing, profile))


def normalize_benchmark_name(benchmark):
    return BENCHMARK_ALIASES.get(benchmark, benchmark)


def get_benchmark_params(benchmark_params_all, benchmark, normalized_benchmark):
    for name in (
        benchmark,
        normalized_benchmark,
        benchmark.replace("-", "_"),
        normalized_benchmark.replace("-", "_"),
    ):
        if name in benchmark_params_all:
            return dict(benchmark_params_all[name])

    return {}


def build_benchmark_command(template, benchmark, threads, duration, benchmark_params):
    command = template.format(threads=threads, duration=duration)

    for param in BENCHMARK_PARAM_ARGS.get(benchmark, ()):
        if param in benchmark_params:
            command = "{} --{} {}".format(command, param.replace("_", "-"), benchmark_params[param])

    return command


def normalize_pushgateway_url(url):
    url = (url or DEFAULT_PUSHGATEWAY_URL).strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://{}".format(url)
    return url


def shell_join(args):
    return " ".join(shlex.quote(str(arg)) for arg in args)


def resolve_nodelist(config, external_config):
    slurm_config = config.get("slurm") or {}
    explicit = (
        config.get("nodelist")
        or config.get("node_list")
        or config.get("slurm_nodelist")
        or slurm_config.get("nodelist")
        or slurm_config.get("node_list")
    )
    if explicit:
        return str(explicit)

    if external_config.enabled and len(external_config.node_device_map) == 1:
        return next(iter(external_config.node_device_map.keys()))

    return ""


def main():
    args = build_parser().parse_args()
    config = load_campaign(args.config)
    try:
        external_config = load_campaign_external_config(args.config)
        if external_config.enabled:
            print("[INFO] Medicion externa Vampire activada; ejecutando comprobacion previa")
            preflight_vampire(external_config)
        monitoring_config = load_campaign_monitoring_config(args.config, args.pushgateway_url)
        if monitoring_config.enabled:
            print("[INFO] Publicacion de metricas agregadas activada")
    except ExternalMeasurementError as exc:
        print("[ERROR] {}".format(exc), file=sys.stderr)
        sys.exit(1)
    except MonitoringError as exc:
        print("[ERROR] {}".format(exc), file=sys.stderr)
        sys.exit(1)

    campaign_name = config["campaign_name"]
    launcher = config.get("launcher", "slurm")
    partition = config.get("partition", "guest")
    time_limit = config.get("time_limit", "00:10:00")
    workdir = config.get("workdir", ".")
    duration = config.get("duration", 10)
    nodelist = resolve_nodelist(config, external_config)
    if nodelist:
        print("[INFO] Slurm nodelist fijado a {}".format(nodelist))

    pushgateway_url = monitoring_config.pushgateway.url if monitoring_config.enabled else ""

    languages = config["languages"]
    benchmarks = config["benchmarks"]
    execution_profiles = config["execution_profiles"]
    reps_list = config["reps"]

    benchmark_params_all = config.get("benchmark_params", {})

    combinations = itertools.product(languages, benchmarks, execution_profiles, reps_list)

    for language, benchmark, profile, rep in combinations:
        normalized_benchmark = normalize_benchmark_name(benchmark)
        key = (language, normalized_benchmark)
        template = COMMANDS.get(key)

        if not template:
            print("[SKIP] No command defined for {}".format(key))
            continue

        require_profile_fields(profile)

        # Run `idle` only once per campaign (no need to repeat per-profile or per-rep)
        if normalized_benchmark == "idle":
            first_profile = execution_profiles[0]
            first_rep = reps_list[0]
            if profile.get("name") != first_profile.get("name") or rep != first_rep:
                print(
                    "[SKIP] idle benchmark: only running once per campaign (skipping {} r{})".format(
                        profile.get("name"), rep
                    )
                )
                continue

        profile_name = profile["name"]
        threads = int(profile["threads"])
        cpus = int(profile["cpus"])
        command_prefix = profile.get("command_prefix", "").strip()
        processes = profile.get("processes")

        metadata_threads = threads
        benchmark_threads = threads
        if processes is not None:
            metadata_threads = int(processes)
            benchmark_threads = 1
            if command_prefix:
                command_prefix = "srun -n {} -c 1 --cpu-bind=cores {}".format(processes, command_prefix)
            else:
                command_prefix = "srun -n {} -c 1 --cpu-bind=cores".format(processes)

        # threads representa los hilos de ejecución del benchmark. En esta fase
        # HPC los perfiles deben mapear threads/cpus a cores físicos, no a
        # hilos lógicos/SMT. La afinidad real del command_prefix debe
        # comprobarse en el nodo asignado con: lscpu -e=CPU,CORE,SOCKET,NODE
        if cpus != metadata_threads:
            print(
                "[WARN] Profile {} has cpus={} and effective threads={}; run_experiment.py "
                "will request --threads={} for metadata.".format(
                    profile_name, cpus, metadata_threads, metadata_threads
                ),
                file=sys.stderr,
            )

        benchmark_params = get_benchmark_params(
            benchmark_params_all,
            benchmark,
            normalized_benchmark,
        )

        command_core = build_benchmark_command(
            template,
            normalized_benchmark,
            benchmark_threads,
            duration,
            benchmark_params,
        )

        command = "{} {}".format(command_prefix, command_core).strip()
        label = "{}_{}_{}_r{}".format(benchmark, language, profile_name, rep)

        print("=" * 72)
        print("[RUN] campaign={} label={}".format(campaign_name, label))
        print("=" * 72)

        cmd = [
            sys.executable,
            "run_experiment.py",
            "--label", label,
            "--launcher", launcher,
            "--language", language,
            "--benchmark", benchmark,
            "--profile", profile_name,
            "--cores", str(cpus),
            "--rep", str(rep),
            "--threads", str(metadata_threads),
            "--duration", str(duration),
            "--command", command,
            "--partition", partition,
            "--time-limit", time_limit,
            "--workdir", workdir,
            "--campaign", campaign_name,
            "--campaign-config", args.config,
        ]

        if nodelist:
            cmd.extend(["--nodelist", nodelist])

        if pushgateway_url:
            cmd.extend(["--pushgateway-url", pushgateway_url])

        print("[CMD] {}".format(shell_join(cmd)))

        result = subprocess.run(cmd)
        if result.returncode != 0:
            print("[ERROR] Failed {}".format(label), file=sys.stderr)


if __name__ == "__main__":
    main()
