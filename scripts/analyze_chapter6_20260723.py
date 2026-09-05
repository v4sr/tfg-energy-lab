#!/usr/bin/env python3
"""Reproduce Chapter 6 from the 23 July RAPL and Vampire campaign."""

import argparse
import csv
import io
import json
import math
import shutil
import statistics
import sys
import tarfile
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from vampire_integration import process_vampire_csv
from scripts.analyze_chapter6_rapl import (
    ACTIVE_BENCHMARKS,
    BENCHMARK_LABELS,
    PROFILES,
    PROFILE_CORES,
    T95,
    describe,
    fmt,
    font,
    heatmap,
    parse_json_lines,
    pearson,
    summarize_rapl,
    workload_metrics,
    write_csv,
    write_latex_table,
)


RUN_PREFIX = "20260723_"
EXPECTED_NODE = "compute-0-4"
EXPECTED_DEVICE = "hpm4"
COLORS = {
    ("memory-stream-hpc", "rapl"): "#2e7d32",
    ("memory-stream-hpc", "vampire"): "#2e7d32",
    ("compute-dgemm-hpc", "rapl"): "#1565c0",
    ("compute-dgemm-hpc", "vampire"): "#1565c0",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path(
            "analysis/capitulo6/datasets/"
            "hpc_tfg_main_campaign-20260723.tar.gz"
        ),
    )
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        default=Path("analysis/capitulo6"),
    )
    parser.add_argument(
        "--figures-dir",
        type=Path,
        default=Path("memoria/imagenes/capitulo6"),
    )
    parser.add_argument(
        "--latex-analysis-dir",
        type=Path,
        default=Path("memoria/analysis/capitulo6"),
    )
    return parser.parse_args()


def decode_csv(content):
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def load_archive(path):
    raw, rapl, vampire, payloads = {}, {}, {}, {}
    system_info = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            content = archive.extractfile(member).read()
            if "raw" in parts and member.name.endswith(".json"):
                raw[Path(member.name).stem] = json.loads(content)
            elif "samples" in parts and "rapl" in parts:
                rapl[Path(member.name).stem] = decode_csv(content)
            elif "samples" in parts and "vampire" in parts:
                vampire[Path(member.name).stem] = content
            elif "logs" in parts and "monitoring" in parts:
                name = Path(member.name).name
                suffix = "-prometheus.txt"
                run_id = name[:-len(suffix)] if name.endswith(suffix) else name
                payloads[run_id] = content.decode("utf-8")
            elif "system-info" in parts:
                system_info[Path(member.name).name] = content.decode(
                    "utf-8", errors="replace"
                )
    return raw, rapl, vampire, payloads, system_info


def vampire_series(content):
    rows = decode_csv(content)
    points = []
    for row in rows:
        try:
            points.append((float(row["Time"]), float(row["hpm4"])))
        except (KeyError, TypeError, ValueError):
            continue
    return points


def recalculate_vampire(content, ext):
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "vampire.csv"
        path.write_bytes(content)
        return process_vampire_csv(
            path,
            ext["device"],
            int(ext["benchmark_start_ns"]),
            int(ext["benchmark_end_ns"]),
            int(ext["started_at_ns"]),
        )


def approx(left, right, tolerance=1e-5):
    return abs(float(left) - float(right)) <= tolerance


def build_runs(raw, rapl, vampire, payloads):
    runs, quality = [], []
    for run_id, data in sorted(raw.items()):
        row = data.get("row") or {}
        ext = data.get("external_measurement") or {}
        rapl_summary = summarize_rapl(rapl.get(run_id, []))
        vampire_summary = recalculate_vampire(vampire[run_id], ext)
        payload = payloads.get(run_id, "")
        records = parse_json_lines(data.get("stdout", ""))
        work, throughput, unit, process_outputs = workload_metrics(
            row["benchmark"], records
        )
        duration_values = [
            float(record["duration_real_s"])
            for record in records
            if record.get("duration_real_s") is not None
        ]
        benchmark_duration = (
            max(duration_values)
            if duration_values
            else rapl_summary["duration_rapl_s"]
        )
        checks = {
            "run_prefix": run_id.startswith(RUN_PREFIX),
            "completed": str(row.get("job_state", "")).startswith("COMPLETED"),
            "exit_ok": row.get("exit_code") == "0:0",
            "node_ok": row.get("node_list") == EXPECTED_NODE,
            "device_ok": ext.get("device") == EXPECTED_DEVICE,
            "external_completed": ext.get("status") == "completed",
            "rapl_csv": run_id in rapl,
            "vampire_csv": run_id in vampire,
            "payload": run_id in payloads,
            "rapl_energy_matches": approx(
                row["energy_j"], rapl_summary["energy_last_j"]
            ),
            "rapl_average_matches": approx(
                row["avg_power_w"], rapl_summary["avg_power_sample_w"]
            ),
            "rapl_peak_matches": approx(
                row["peak_power_w"], rapl_summary["peak_power_w"]
            ),
            "external_energy_matches": approx(
                row["external_energy_j"], vampire_summary["energy_j"]
            ),
            "external_average_matches": approx(
                row["external_avg_power_w"], vampire_summary["avg_power_w"]
            ),
            "external_peak_matches": approx(
                row["external_peak_power_w"], vampire_summary["peak_power_w"]
            ),
            "payload_internal": all(
                name in payload
                for name in (
                    "tfg_internal_energy_joules",
                    "tfg_internal_average_power_watts",
                    "tfg_internal_peak_power_watts",
                )
            ),
            "payload_external": all(
                name in payload
                for name in (
                    "tfg_external_energy_joules",
                    "tfg_external_average_power_watts",
                    "tfg_external_peak_power_watts",
                    "tfg_external_capture_success",
                )
            ),
        }
        if row["benchmark"] in ACTIVE_BENCHMARKS:
            checks["process_outputs"] = process_outputs == int(row["cores"])
            checks["throughput"] = throughput is not None
        valid = all(checks.values())
        quality.append({
            "run_id": run_id,
            **checks,
            "valid": valid,
        })

        rapl_energy = float(row["energy_j"])
        external_energy = float(row["external_energy_j"])
        runs.append({
            "run_id": run_id,
            "campaign": row["campaign"],
            "benchmark": row["benchmark"],
            "profile": row["profile"],
            "cores": int(row["cores"]),
            "threads": int(row["threads"]),
            "rep": int(row["rep"]),
            "node": row["node_list"],
            "job_id": row["job_id"],
            "benchmark_duration_s": benchmark_duration,
            "rapl_duration_s": rapl_summary["duration_rapl_s"],
            "rapl_samples": rapl_summary["samples"],
            "rapl_energy_j": rapl_energy,
            "rapl_avg_power_w": float(row["avg_power_w"]),
            "rapl_peak_power_w": float(row["peak_power_w"]),
            "vampire_device": ext["device"],
            "vampire_duration_s": float(row["external_duration_s"]),
            "vampire_capture_window_s": float(
                row["external_capture_window_s"]
            ),
            "vampire_samples": int(row["external_samples_count"]),
            "vampire_energy_j": external_energy,
            "vampire_avg_power_w": float(row["external_avg_power_w"]),
            "vampire_peak_power_w": float(row["external_peak_power_w"]),
            "vampire_minus_rapl_j": external_energy - rapl_energy,
            "rapl_coverage_percent": 100.0 * rapl_energy / external_energy,
            "external_internal_ratio": external_energy / rapl_energy,
            "work": work,
            "throughput": throughput,
            "throughput_unit": unit,
            "process_outputs": process_outputs,
            "rapl_energy_efficiency": (
                work / rapl_energy if work and rapl_energy else None
            ),
            "external_energy_efficiency": (
                work / external_energy if work and external_energy else None
            ),
            "_rapl_power": [
                item["power_w"] for item in rapl_summary["parsed"]
            ],
            "_rapl_time": cumulative(
                item["interval_s"] for item in rapl_summary["parsed"]
            ),
            "_vampire_series": vampire_series(vampire[run_id]),
        })
    return runs, quality


def cumulative(values):
    output, total = [], 0.0
    for value in values:
        total += value
        output.append(total)
    return output


def group_statistics(runs):
    groups = defaultdict(list)
    for run in runs:
        groups[(run["benchmark"], run["profile"])].append(run)
    fields = (
        "benchmark_duration_s",
        "rapl_duration_s",
        "rapl_samples",
        "rapl_energy_j",
        "rapl_avg_power_w",
        "rapl_peak_power_w",
        "vampire_energy_j",
        "vampire_duration_s",
        "vampire_samples",
        "vampire_avg_power_w",
        "vampire_peak_power_w",
        "vampire_minus_rapl_j",
        "rapl_coverage_percent",
        "external_internal_ratio",
        "throughput",
        "rapl_energy_efficiency",
        "external_energy_efficiency",
    )
    order = {"idle": 0, "memory-stream-hpc": 1, "compute-dgemm-hpc": 2}
    output = []
    for (benchmark, profile), group in sorted(
        groups.items(),
        key=lambda item: (
            order[item[0][0]],
            PROFILE_CORES[item[0][1]],
        ),
    ):
        row = {
            "benchmark": benchmark,
            "profile": profile,
            "cores": PROFILE_CORES[profile],
            "n": len(group),
        }
        for field in fields:
            stats = describe([item[field] for item in group])
            for name, value in stats.items():
                row["{}_{}".format(field, name)] = value
        output.append(row)
    return output


def build_scalability(groups):
    output = []
    lookup = {
        (row["benchmark"], row["profile"]): row for row in groups
    }
    for benchmark in ACTIVE_BENCHMARKS:
        base = lookup[(benchmark, "p1")]
        for profile in PROFILES:
            row = lookup[(benchmark, profile)]
            speedup = row["throughput_mean"] / base["throughput_mean"]
            output.append({
                "benchmark": benchmark,
                "profile": profile,
                "cores": PROFILE_CORES[profile],
                "throughput": row["throughput_mean"],
                "speedup": speedup,
                "parallel_efficiency_percent": (
                    100.0 * speedup / PROFILE_CORES[profile]
                ),
                "rapl_efficiency_relative": (
                    row["rapl_energy_efficiency_mean"]
                    / base["rapl_energy_efficiency_mean"]
                ),
                "external_efficiency_relative": (
                    row["external_energy_efficiency_mean"]
                    / base["external_energy_efficiency_mean"]
                ),
            })
    return output


def build_correlation(runs, groups):
    active = [run for run in runs if run["benchmark"] in ACTIVE_BENCHMARKS]
    group_lookup = {
        (row["benchmark"], row["profile"]): row for row in groups
    }
    variables = {
        "cpu": [run["cores"] for run in active],
        "duracion": [run["benchmark_duration_s"] for run in active],
        "energia_rapl": [run["rapl_energy_j"] for run in active],
        "potencia_rapl": [run["rapl_avg_power_w"] for run in active],
        "energia_vampire": [run["vampire_energy_j"] for run in active],
        "potencia_vampire": [run["vampire_avg_power_w"] for run in active],
        "rendimiento_rel": [
            run["throughput"]
            / group_lookup[(run["benchmark"], "p1")]["throughput_mean"]
            for run in active
        ],
    }
    names = list(variables)
    matrix = [
        {
            "metric": left,
            **{
                right: pearson(variables[left], variables[right])
                for right in names
            },
        }
        for left in names
    ]
    return names, matrix


def line_chart(path, series, title, ylabel, ideal=None):
    width, height = 1500, 880
    left, top, right, bottom = 125, 100, 55, 130
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    plot_right, plot_bottom = width - right, height - bottom
    values = [
        value
        for item in series
        for value in item["values"]
        if value is not None
    ]
    if ideal:
        values.extend(ideal["values"])
    ymin, ymax = 0.0, max(values) * 1.12

    def xy(core, value):
        x = left + (core - 1) / 11 * (plot_right - left)
        y = plot_bottom - value / ymax * (plot_bottom - top)
        return x, y

    def draw_polyline(points, color, dashed=False, width=5):
        if not dashed:
            draw.line(points, fill=color, width=width)
            return
        dash_length, gap_length = 18.0, 10.0
        for start, end in zip(points, points[1:]):
            x1, y1 = start
            x2, y2 = end
            distance = math.hypot(x2 - x1, y2 - y1)
            if distance == 0:
                continue
            position = 0.0
            while position < distance:
                dash_end = min(position + dash_length, distance)
                sx = x1 + (x2 - x1) * position / distance
                sy = y1 + (y2 - y1) * position / distance
                ex = x1 + (x2 - x1) * dash_end / distance
                ey = y1 + (y2 - y1) * dash_end / distance
                draw.line((sx, sy, ex, ey), fill=color, width=width)
                position += dash_length + gap_length

    def draw_marker(x, y, color, marker):
        if marker == "square":
            draw.rectangle(
                (x - 7, y - 7, x + 7, y + 7),
                fill="white",
                outline=color,
                width=4,
            )
        else:
            draw.ellipse(
                (x - 7, y - 7, x + 7, y + 7),
                fill="white",
                outline=color,
                width=4,
            )

    for index in range(6):
        value = ymax * index / 5
        y = xy(1, value)[1]
        draw.line((left, y, plot_right, y), fill="#dddddd")
        draw.text((15, y - 10), fmt(value, 1), fill="black", font=font(18))
    draw.line((left, top, left, plot_bottom), fill="black", width=2)
    draw.line((left, plot_bottom, plot_right, plot_bottom), fill="black", width=2)
    for core in (1, 2, 4, 6, 8, 12):
        x = xy(core, 0)[0]
        draw.text((x - 10, plot_bottom + 14), str(core), fill="black", font=font(18))
    if ideal:
        draw.line(
            [xy(core, value) for core, value in zip((1, 2, 4, 6, 8, 12), ideal["values"])],
            fill="#777777",
            width=3,
        )
    for item in series:
        points = [
            xy(core, value)
            for core, value in zip((1, 2, 4, 6, 8, 12), item["values"])
        ]
        draw_polyline(
            points,
            item["color"],
            dashed=item.get("line") == "dashed",
        )
        for x, y in points:
            draw_marker(x, y, item["color"], item.get("marker", "circle"))
    draw.text((left, 28), title, fill="black", font=font(30, True))
    draw.text((width // 2 - 90, height - 52), "CPU asignadas", fill="black", font=font(22))
    draw.text((15, 66), ylabel, fill="black", font=font(22))
    legend_x, legend_y = plot_right - 380, top + 8
    legend = list(series)
    if ideal:
        legend.append({**ideal, "color": "#777777"})
    draw.rectangle(
        (
            legend_x - 14,
            legend_y - 10,
            plot_right - 4,
            legend_y + len(legend) * 30 + 4,
        ),
        fill="white",
        outline="#cccccc",
        width=1,
    )
    for item in legend:
        legend_points = (
            (legend_x, legend_y + 10),
            (legend_x + 38, legend_y + 10),
        )
        draw_polyline(
            legend_points,
            item["color"],
            dashed=item.get("line") == "dashed",
        )
        draw_marker(
            legend_x + 19,
            legend_y + 10,
            item["color"],
            item.get("marker", "circle"),
        )
        draw.text(
            (legend_x + 48, legend_y - 3),
            item["label"],
            fill="black",
            font=font(17),
        )
        legend_y += 30
    image.save(path)


def representative_chart(path, runs):
    selected = [
        next(
            run for run in runs
            if run["benchmark"] == benchmark
            and run["profile"] == profile
            and run["rep"] == 1
        )
        for benchmark in ACTIVE_BENCHMARKS
        for profile in ("p1", "p12")
    ]
    series = []
    palette = ("#66bb6a", "#1b5e20", "#ffb74d", "#e65100")
    for run, color in zip(selected, palette):
        series.append({
            "label": "{} {}".format(
                BENCHMARK_LABELS[run["benchmark"]], run["profile"]
            ),
            "color": color,
            "times": run["_rapl_time"],
            "values": run["_rapl_power"],
        })
    width, height = 1500, 880
    left, top, right, bottom = 110, 100, 60, 120
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    plot_right, plot_bottom = width - right, height - bottom
    xmax = max(max(item["times"]) for item in series)
    ymax = max(max(item["values"]) for item in series) * 1.08
    draw.line((left, top, left, plot_bottom), fill="black", width=2)
    draw.line((left, plot_bottom, plot_right, plot_bottom), fill="black", width=2)
    for item in series:
        points = [
            (
                left + t / xmax * (plot_right - left),
                plot_bottom - value / ymax * (plot_bottom - top),
            )
            for t, value in zip(item["times"], item["values"])
        ]
        draw.line(points, fill=item["color"], width=3)
    draw.text((left, 28), "Series RAPL representativas (23/07/2026)", fill="black", font=font(30, True))
    draw.text((width // 2 - 70, height - 50), "Tiempo (s)", fill="black", font=font(22))
    draw.text((15, 65), "Potencia (W)", fill="black", font=font(22))
    for index, item in enumerate(series):
        x, y = plot_right - 320, top + index * 30
        draw.line((x, y + 8, x + 35, y + 8), fill=item["color"], width=4)
        draw.text((x + 45, y), item["label"], fill="black", font=font(17))
    image.save(path)


def export_outputs(analysis_dir, figures_dir, runs, quality, groups, scalability):
    analysis_dir.mkdir(parents=True, exist_ok=True)
    figures_dir.mkdir(parents=True, exist_ok=True)
    clean_runs = [
        {k: v for k, v in run.items() if not k.startswith("_")}
        for run in runs
    ]
    write_csv(analysis_dir / "ejecuciones_20260723.csv", clean_runs)
    write_csv(analysis_dir / "control_calidad_20260723.csv", quality)
    write_csv(analysis_dir / "estadistica_20260723.csv", groups)
    write_csv(analysis_dir / "escalabilidad_20260723.csv", scalability)
    correlation_names, correlation = build_correlation(runs, groups)
    write_csv(
        analysis_dir / "matriz_correlacion_20260723.csv",
        correlation,
    )

    tables = analysis_dir / "tablas"
    tables.mkdir(parents=True, exist_ok=True)
    write_latex_table(
        tables / "configuracion.tex",
        ["Parámetro", "Valor"],
        [
            ("Campaña", r"\texttt{hpc\_tfg\_main\_campaign}"),
            ("Fecha analizada", "23 de julio de 2026"),
            ("Nodo", r"\texttt{compute-0-4}"),
            ("CPU", r"$2\times$ Intel Xeon Silver 4214, 12 núcleos por socket"),
            ("Duración objetivo", "120 s"),
            ("Perfiles", "1, 2, 4, 6, 8 y 12 procesos monohilo"),
            ("Repeticiones", "5 por perfil activo; 1 para Idle"),
            ("Fuentes", r"RAPL \texttt{package} y Vampire \texttt{hpm4}"),
        ],
        "lp{8.0cm}",
    )

    lookup = {(row["benchmark"], row["profile"]): row for row in groups}
    summary_rows = []
    for benchmark in ACTIVE_BENCHMARKS:
        for profile in PROFILES:
            row = lookup[(benchmark, profile)]
            summary_rows.append((
                BENCHMARK_LABELS[benchmark],
                row["cores"],
                fmt(row["throughput_mean"], 3),
                "GB/s" if benchmark == "memory-stream-hpc" else "GFLOP/s",
                fmt(row["rapl_avg_power_w_mean"], 2),
                fmt(row["vampire_avg_power_w_mean"], 2),
                fmt(row["rapl_energy_j_mean"] / 1000, 2),
                fmt(row["vampire_energy_j_mean"] / 1000, 2),
                fmt(row["rapl_coverage_percent_mean"], 1),
            ))
    write_latex_table(
        tables / "resumen_activos.tex",
        [
            "Benchmark", "CPU", "Rend.", "Unidad",
            "RAPL (W)", "Vampire (W)",
            "RAPL (kJ)", "Vampire (kJ)", r"Cobertura (\%)",
        ],
        summary_rows,
        "lrrrrrrrr",
    )

    idle = lookup[("idle", "p1")]
    write_latex_table(
        tables / "idle.tex",
        ["Métrica", "RAPL", "Vampire"],
        [
            ("Duración integrada (s)", fmt(idle["rapl_duration_s_mean"], 3), fmt(idle["vampire_duration_s_mean"], 3) if "vampire_duration_s_mean" in idle else fmt(runs[0]["vampire_duration_s"], 3)),
            ("Muestras", runs[0]["rapl_samples"], runs[0]["vampire_samples"]),
            ("Energía (kJ)", fmt(idle["rapl_energy_j_mean"] / 1000, 3), fmt(idle["vampire_energy_j_mean"] / 1000, 3)),
            ("Potencia media (W)", fmt(idle["rapl_avg_power_w_mean"], 3), fmt(idle["vampire_avg_power_w_mean"], 3)),
            ("Potencia máxima (W)", fmt(idle["rapl_peak_power_w_mean"], 3), fmt(idle["vampire_peak_power_w_mean"], 3)),
        ],
        "lrr",
    )

    write_latex_table(
        tables / "validacion.tex",
        ["Comprobación", "Resultado"],
        [
            ("Ejecuciones del 23/07", "61/61"),
            ("Nodo", r"61/61 en \texttt{compute-0-4}"),
            ("Dispositivo", r"61/61 con \texttt{hpm4}"),
            ("Estado RAPL y SLURM", "61/61 válidas"),
            ("Capturas Vampire", "61/61 completadas"),
            ("Recálculo desde CSV", "Coincide en 61/61"),
            ("Payload con ambas fuentes", "61/61"),
        ],
        "lr",
    )

    write_latex_table(
        tables / "escalabilidad.tex",
        ["Benchmark", "CPU", "Speedup", r"Eficiencia paralela (\%)", "Efic. RAPL", "Efic. Vampire"],
        [
            (
                BENCHMARK_LABELS[row["benchmark"]],
                row["cores"],
                fmt(row["speedup"], 3),
                fmt(row["parallel_efficiency_percent"], 1),
                fmt(row["rapl_efficiency_relative"], 3),
                fmt(row["external_efficiency_relative"], 3),
            )
            for row in scalability
        ],
        "lrrrrr",
    )

    active_groups = [
        row for row in groups if row["benchmark"] in ACTIVE_BENCHMARKS
    ]
    write_latex_table(
        tables / "vampire.tex",
        ["Benchmark", "CPU", "Vampire--RAPL (kJ)", r"RAPL/Vampire (\%)", "Vampire/RAPL"],
        [
            (
                BENCHMARK_LABELS[row["benchmark"]],
                row["cores"],
                fmt(row["vampire_minus_rapl_j_mean"] / 1000, 2),
                fmt(row["rapl_coverage_percent_mean"], 1),
                fmt(row["external_internal_ratio_mean"], 3),
            )
            for row in active_groups
        ],
        "lrrrr",
    )

    chart_series = lambda field: [
        {
            "label": "{} · {}".format(
                BENCHMARK_LABELS[benchmark],
                "RAPL" if source == "rapl" else "Vampire",
            ),
            "color": COLORS[(benchmark, source)],
            "line": "solid" if source == "rapl" else "dashed",
            "marker": "circle" if source == "rapl" else "square",
            "values": [
                lookup[(benchmark, profile)]["{}_{}_mean".format(source, field)]
                for profile in PROFILES
            ],
        }
        for benchmark in ACTIVE_BENCHMARKS
        for source in ("rapl", "vampire")
    ]
    line_chart(
        figures_dir / "potencia_media_por_nucleos.png",
        chart_series("avg_power_w"),
        "Potencia media interna y externa (23/07/2026)",
        "Potencia (W)",
    )
    energy_series = chart_series("energy_j")
    for item in energy_series:
        item["values"] = [value / 1000 for value in item["values"]]
    line_chart(
        figures_dir / "energia_por_nucleos.png",
        energy_series,
        "Energía interna y externa durante 120 s (23/07/2026)",
        "Energía (kJ)",
    )
    scale_lookup = {
        (row["benchmark"], row["profile"]): row for row in scalability
    }
    line_chart(
        figures_dir / "speedup_por_nucleos.png",
        [
            {
                "label": BENCHMARK_LABELS[benchmark],
                "color": COLORS[(benchmark, "rapl")],
                "values": [
                    scale_lookup[(benchmark, profile)]["speedup"]
                    for profile in PROFILES
                ],
            }
            for benchmark in ACTIVE_BENCHMARKS
        ],
        "Speedup respecto a un proceso (23/07/2026)",
        "Speedup",
        ideal={
            "label": "Escalado ideal",
            "values": [1, 2, 4, 6, 8, 12],
        },
    )
    line_chart(
        figures_dir / "eficiencia_energetica_relativa.png",
        [
            {
                "label": "{} · {}".format(
                    BENCHMARK_LABELS[benchmark],
                    "RAPL" if source == "rapl" else "Vampire",
                ),
                "color": COLORS[(benchmark, source)],
                "line": "solid" if source == "rapl" else "dashed",
                "marker": "circle" if source == "rapl" else "square",
                "values": [
                    scale_lookup[(benchmark, profile)][
                        "{}_efficiency_relative".format(
                            "rapl" if source == "rapl" else "external"
                        )
                    ]
                    for profile in PROFILES
                ],
            }
            for benchmark in ACTIVE_BENCHMARKS
            for source in ("rapl", "vampire")
        ],
        "Eficiencia energética relativa (23/07/2026)",
        "Índice relativo",
    )
    representative_chart(
        figures_dir / "series_rapl_representativas.png", runs
    )
    line_chart(
        figures_dir / "variabilidad_potencia.png",
        [
            {
                "label": "{} · {}".format(
                    BENCHMARK_LABELS[benchmark],
                    "RAPL" if source == "rapl" else "Vampire",
                ),
                "color": COLORS[(benchmark, source)],
                "line": "solid" if source == "rapl" else "dashed",
                "marker": "circle" if source == "rapl" else "square",
                "values": [
                    lookup[(benchmark, profile)][
                        "{}_avg_power_w_cv_percent".format(source)
                    ]
                    for profile in PROFILES
                ],
            }
            for benchmark in ACTIVE_BENCHMARKS
            for source in ("rapl", "vampire")
        ],
        "Variabilidad de la potencia media (23/07/2026)",
        "CV (%)",
    )
    heatmap(
        figures_dir / "matriz_correlacion.png",
        correlation_names,
        correlation,
        title="Correlación de Pearson: RAPL y Vampire (23/07/2026)",
    )


def main():
    args = parse_args()
    raw, rapl, vampire, payloads, system_info = load_archive(args.archive)
    runs, quality = build_runs(raw, rapl, vampire, payloads)
    groups = group_statistics(runs)
    scalability = build_scalability(groups)
    export_outputs(
        args.analysis_dir,
        args.figures_dir,
        runs,
        quality,
        groups,
        scalability,
    )
    latex_tables = args.latex_analysis_dir / "tablas"
    latex_tables.mkdir(parents=True, exist_ok=True)
    for source in (args.analysis_dir / "tablas").glob("*.tex"):
        shutil.copy2(source, latex_tables / source.name)
    metadata = {
        "archive": str(args.archive),
        "run_prefix": RUN_PREFIX,
        "runs": len(runs),
        "valid_runs": sum(row["valid"] for row in quality),
        "benchmarks": Counter(run["benchmark"] for run in runs),
        "nodes": Counter(run["node"] for run in runs),
        "devices": Counter(run["vampire_device"] for run in runs),
        "rapl_files": len(rapl),
        "vampire_files": len(vampire),
        "payloads": len(payloads),
        "external_measurements_used": True,
        "system_info_files": sorted(system_info),
    }
    serializable = {
        key: dict(value) if isinstance(value, Counter) else value
        for key, value in metadata.items()
    }
    (args.analysis_dir / "metadatos.json").write_text(
        json.dumps(serializable, indent=2), encoding="utf-8"
    )
    print(json.dumps(serializable, indent=2))


if __name__ == "__main__":
    main()
