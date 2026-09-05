#!/usr/bin/env python3
"""Reproduce the Chapter 6 analysis using only internal Intel RAPL data."""

import argparse
import csv
import io
import json
import math
import statistics
import tarfile
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


PROFILES = ("p1", "p2", "p4", "p6", "p8", "p12")
PROFILE_CORES = {name: int(name[1:]) for name in PROFILES}
ACTIVE_BENCHMARKS = ("memory-stream-hpc", "compute-dgemm-hpc")
BENCHMARK_LABELS = {
    "idle": "Idle",
    "memory-stream-hpc": "STREAM",
    "compute-dgemm-hpc": "DGEMM",
}
T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate Chapter 6 RAPL-only datasets, tables and figures."
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=Path("analysis/capitulo6/datasets/hpc_tfg_main_campaign-20260723.tar.gz"),
        help="Campaign archive. External-measurement members are deliberately ignored.",
    )
    parser.add_argument("--analysis-dir", type=Path, default=Path("analysis/capitulo6"))
    parser.add_argument("--figures-dir", type=Path, default=Path("memoria/imagenes/capitulo6"))
    parser.add_argument("--min-duration", type=float, default=100.0)
    parser.add_argument("--max-duration", type=float, default=150.0)
    return parser.parse_args()


def csv_bytes(content):
    return list(csv.DictReader(io.StringIO(content.decode("utf-8"))))


def finite_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_json_lines(text):
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("benchmark"):
            records.append(value)
    return records


def load_archive(path):
    raw = {}
    rapl = {}
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if not member.isfile():
                continue
            if "raw" in parts and member.name.endswith(".json"):
                run_id = Path(member.name).stem
                raw[run_id] = json.loads(archive.extractfile(member).read())
            elif "samples" in parts and "rapl" in parts and member.name.endswith(".csv"):
                run_id = Path(member.name).stem
                rapl[run_id] = csv_bytes(archive.extractfile(member).read())
    return raw, rapl


def summarize_rapl(rows):
    required = {
        "epoch_s", "sample_idx", "interval_s", "delta_energy_j",
        "power_w", "total_energy_j", "node", "job_id",
    }
    columns = set(rows[0]) if rows else set()
    missing_columns = sorted(required - columns)
    parsed = []
    malformed = 0
    for row in rows:
        values = {key: finite_float(row.get(key)) for key in (
            "epoch_s", "interval_s", "delta_energy_j", "power_w", "total_energy_j"
        )}
        try:
            sample_idx = int(row.get("sample_idx", ""))
        except (TypeError, ValueError):
            sample_idx = None
        if sample_idx is None or any(value is None for value in values.values()):
            malformed += 1
            continue
        values["sample_idx"] = sample_idx
        parsed.append(values)

    epochs = [row["epoch_s"] for row in parsed]
    intervals = [row["interval_s"] for row in parsed]
    deltas = [row["delta_energy_j"] for row in parsed]
    powers = [row["power_w"] for row in parsed]
    totals = [row["total_energy_j"] for row in parsed]
    indices = [row["sample_idx"] for row in parsed]
    return {
        "columns_missing": ",".join(missing_columns),
        "malformed_rows": malformed,
        "samples": len(parsed),
        "duration_rapl_s": sum(intervals) if intervals else None,
        "mean_interval_s": statistics.mean(intervals) if intervals else None,
        "min_interval_s": min(intervals) if intervals else None,
        "max_interval_s": max(intervals) if intervals else None,
        "sample_span_s": epochs[-1] - epochs[0] if len(epochs) > 1 else 0.0,
        "energy_sum_j": sum(deltas) if deltas else None,
        "energy_last_j": totals[-1] if totals else None,
        "avg_power_sample_w": statistics.mean(powers) if powers else None,
        "avg_power_energy_w": sum(deltas) / sum(intervals) if intervals and sum(intervals) else None,
        "peak_power_w": max(powers) if powers else None,
        "min_power_w": min(powers) if powers else None,
        "negative_values": sum(value < 0 for value in deltas + powers),
        "epochs_monotonic": all(right > left for left, right in zip(epochs, epochs[1:])),
        "totals_monotonic": all(right >= left for left, right in zip(totals, totals[1:])),
        "duplicate_indices": len(indices) - len(set(indices)),
        "parsed": parsed,
    }


def workload_metrics(benchmark, records):
    matching = [record for record in records if record.get("benchmark") == benchmark]
    durations = [finite_float(record.get("duration_real_s")) for record in matching]
    durations = [value for value in durations if value is not None]
    duration = max(durations) if durations else None
    if benchmark == "memory-stream-hpc":
        work = sum(finite_float(record.get("bytes_processed")) or 0.0 for record in matching)
        throughput = work / duration / 1e9 if duration and work else None
        return work, throughput, "GB/s", len(matching)
    if benchmark == "compute-dgemm-hpc":
        work = sum(finite_float(record.get("ops")) or 0.0 for record in matching)
        throughput = work / duration / 1e9 if duration and work else None
        return work, throughput, "GFLOP/s", len(matching)
    return None, None, "", len(matching)


def build_runs(raw_by_run, rapl_by_run, min_duration, max_duration):
    selected, excluded, quality = [], [], []
    for run_id in sorted(set(rapl_by_run) - set(raw_by_run)):
        excluded.append({"run_id": run_id, "reason": "rapl_csv_without_raw_json"})
    for run_id, raw in sorted(raw_by_run.items()):
        row = raw.get("row") or {}
        benchmark = row.get("benchmark", "")
        rapl_rows = rapl_by_run.get(run_id, [])
        if not rapl_rows:
            excluded.append({"run_id": run_id, "reason": "missing_rapl_csv"})
            continue
        rapl = summarize_rapl(rapl_rows)
        duration_rapl = rapl["duration_rapl_s"] or 0.0
        if not min_duration <= duration_rapl <= max_duration:
            excluded.append({"run_id": run_id, "reason": "outside_five_minute_rapl_window"})
            continue
        if benchmark not in ("idle",) + ACTIVE_BENCHMARKS:
            excluded.append({"run_id": run_id, "reason": "benchmark_out_of_scope"})
            continue

        records = parse_json_lines(raw.get("stdout", ""))
        work, throughput, throughput_unit, process_outputs = workload_metrics(benchmark, records)
        benchmark_duration = max(
            [finite_float(record.get("duration_real_s")) or 0.0 for record in records] or [0.0]
        )
        if benchmark == "idle":
            benchmark_duration = duration_rapl

        energy = finite_float(row.get("energy_j"))
        average = finite_float(row.get("avg_power_w"))
        peak = finite_float(row.get("peak_power_w"))
        cores = int(row.get("cores") or row.get("threads") or 0)
        profile = row.get("profile") or "p{}".format(cores)
        valid = all((
            str(row.get("job_state", "")).startswith("COMPLETED"),
            row.get("exit_code") == "0:0",
            rapl["malformed_rows"] == 0,
            not rapl["columns_missing"],
            rapl["negative_values"] == 0,
            rapl["epochs_monotonic"],
            rapl["totals_monotonic"],
            rapl["duplicate_indices"] == 0,
            energy is not None and average is not None and peak is not None,
            abs((rapl["energy_last_j"] or 0) - (energy or 0)) < 1e-5,
            abs((rapl["avg_power_sample_w"] or 0) - (average or 0)) < 1e-5,
            abs((rapl["peak_power_w"] or 0) - (peak or 0)) < 1e-5,
        ))
        if benchmark in ACTIVE_BENCHMARKS:
            valid = valid and process_outputs == cores and throughput is not None

        item = {
            "run_id": run_id,
            "campaign": row.get("campaign", ""),
            "benchmark": benchmark,
            "profile": profile,
            "cores": cores,
            "threads": int(row.get("threads") or 0),
            "rep": int(row.get("rep") or 0),
            "node": row.get("node_list", ""),
            "job_id": row.get("job_id", ""),
            "job_state": row.get("job_state", ""),
            "exit_code": row.get("exit_code", ""),
            "benchmark_duration_s": benchmark_duration,
            "rapl_duration_s": duration_rapl,
            "mean_interval_s": rapl["mean_interval_s"],
            "min_interval_s": rapl["min_interval_s"],
            "max_interval_s": rapl["max_interval_s"],
            "rapl_samples_count": rapl["samples"],
            "energy_j": energy,
            "avg_power_w": average,
            "avg_power_energy_w": rapl["avg_power_energy_w"],
            "peak_power_w": peak,
            "work": work,
            "throughput": throughput,
            "throughput_unit": throughput_unit,
            "process_outputs": process_outputs,
            "energy_efficiency": (work / energy) if work and energy else None,
            "edp_js": energy * benchmark_duration if energy and benchmark_duration else None,
            "valid": valid,
            "power_series": [record["power_w"] for record in rapl["parsed"]],
            "time_series": cumulative([record["interval_s"] for record in rapl["parsed"]]),
        }
        quality.append({
            "run_id": run_id,
            "benchmark": benchmark,
            "profile": profile,
            "job_completed": str(row.get("job_state", "")).startswith("COMPLETED"),
            "exit_ok": row.get("exit_code") == "0:0",
            "rapl_csv": True,
            "raw_json": True,
            "samples": rapl["samples"],
            "rapl_duration_s": duration_rapl,
            "malformed_rows": rapl["malformed_rows"],
            "negative_values": rapl["negative_values"],
            "timestamps_monotonic": rapl["epochs_monotonic"],
            "energy_matches_summary": abs((rapl["energy_last_j"] or 0) - (energy or 0)) < 1e-5,
            "average_matches_summary": abs((rapl["avg_power_sample_w"] or 0) - (average or 0)) < 1e-5,
            "peak_matches_summary": abs((rapl["peak_power_w"] or 0) - (peak or 0)) < 1e-5,
            "process_outputs": process_outputs,
            "valid": valid,
        })
        if valid:
            selected.append(item)
        else:
            excluded.append({"run_id": run_id, "reason": "failed_quality_checks"})
    return selected, quality, excluded


def cumulative(values):
    result, total = [], 0.0
    for value in values:
        total += value
        result.append(total)
    return result


def describe(values):
    values = [value for value in values if value is not None]
    n = len(values)
    if not n:
        return {key: None for key in ("n", "mean", "median", "std", "min", "max", "cv_percent", "ci95")}
    mean = statistics.mean(values)
    std = statistics.stdev(values) if n > 1 else 0.0
    ci = T95.get(n - 1, 1.96) * std / math.sqrt(n) if n > 1 else None
    return {
        "n": n, "mean": mean, "median": statistics.median(values), "std": std,
        "min": min(values), "max": max(values),
        "cv_percent": 100.0 * std / mean if mean else None, "ci95": ci,
    }


def build_group_statistics(runs):
    groups = defaultdict(list)
    for run in runs:
        groups[(run["benchmark"], run["profile"])].append(run)
    output = []
    benchmark_order = {"idle": 0, "memory-stream-hpc": 1, "compute-dgemm-hpc": 2}
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (benchmark_order.get(item[0][0], 99), PROFILE_CORES.get(item[0][1], 99)),
    )
    for (benchmark, profile), group in ordered_groups:
        metrics = {
            "duration_s": "benchmark_duration_s",
            "energy_j": "energy_j",
            "avg_power_w": "avg_power_w",
            "peak_power_w": "peak_power_w",
            "throughput": "throughput",
            "energy_efficiency": "energy_efficiency",
        }
        row = {"benchmark": benchmark, "profile": profile, "cores": PROFILE_CORES.get(profile, 1)}
        for prefix, field in metrics.items():
            stats = describe([item.get(field) for item in group])
            for name, value in stats.items():
                row["{}_{}".format(prefix, name)] = value
        output.append(row)
    return output


def build_scalability(group_stats):
    rows = []
    for benchmark in ACTIVE_BENCHMARKS:
        groups = [row for row in group_stats if row["benchmark"] == benchmark]
        base = next(row for row in groups if row["profile"] == "p1")
        base_throughput = base["throughput_mean"]
        base_efficiency = base["energy_efficiency_mean"]
        for row in sorted(groups, key=lambda value: value["cores"]):
            speedup = row["throughput_mean"] / base_throughput
            rows.append({
                "benchmark": benchmark,
                "profile": row["profile"],
                "cores": row["cores"],
                "throughput_mean": row["throughput_mean"],
                "throughput_unit": "GB/s" if benchmark == "memory-stream-hpc" else "GFLOP/s",
                "speedup": speedup,
                "parallel_efficiency_percent": 100.0 * speedup / row["cores"],
                "avg_power_w": row["avg_power_w_mean"],
                "energy_j": row["energy_j_mean"],
                "energy_efficiency": row["energy_efficiency_mean"],
                "energy_efficiency_relative": row["energy_efficiency_mean"] / base_efficiency,
            })
    return rows


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 2:
        return None
    xvals, yvals = zip(*pairs)
    mx, my = statistics.mean(xvals), statistics.mean(yvals)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = math.sqrt(
        sum((x - mx) ** 2 for x in xvals) * sum((y - my) ** 2 for y in yvals)
    )
    return numerator / denominator if denominator else None


def build_correlation(runs, group_stats):
    active = [run for run in runs if run["benchmark"] in ACTIVE_BENCHMARKS]
    throughput_base = {}
    efficiency_base = {}
    for row in group_stats:
        if row["profile"] == "p1" and row["benchmark"] in ACTIVE_BENCHMARKS:
            throughput_base[row["benchmark"]] = row["throughput_mean"]
            efficiency_base[row["benchmark"]] = row["energy_efficiency_mean"]
    variables = {
        "cores": [run["cores"] for run in active],
        "duration": [run["benchmark_duration_s"] for run in active],
        "energy": [run["energy_j"] for run in active],
        "avg_power": [run["avg_power_w"] for run in active],
        "peak_power": [run["peak_power_w"] for run in active],
        "samples": [run["rapl_samples_count"] for run in active],
        "relative_throughput": [run["throughput"] / throughput_base[run["benchmark"]] for run in active],
        "relative_energy_eff": [run["energy_efficiency"] / efficiency_base[run["benchmark"]] for run in active],
    }
    names = list(variables)
    matrix = []
    for left in names:
        matrix.append({"metric": left, **{
            right: pearson(variables[left], variables[right]) for right in names
        }})
    return names, matrix


def write_csv(path, rows, fieldnames=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def fmt(value, decimals=2):
    if value is None:
        return "--"
    return ("{:,.%df}" % decimals).format(value).replace(",", " ")


def latex_escape(value):
    return str(value).replace("_", r"\_").replace("%", r"\%")


def write_latex_table(path, columns, rows, align=None):
    align = align or ("l" + "r" * (len(columns) - 1))
    lines = [r"\begin{tabular}{%s}" % align, r"\hline", " & ".join(columns) + r" \\", r"\hline"]
    lines.extend(" & ".join(map(str, row)) + r" \\" for row in rows)
    lines.extend([r"\hline", r"\end{tabular}", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def font(size=22, bold=False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def line_chart(path, series, title, ylabel, ideal=None):
    width, height = 1400, 850
    left, top, right, bottom = 125, 90, 55, 125
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, tick_font = font(30, True), font(23), font(19)
    plot_right, plot_bottom = width - right, height - bottom
    all_values = [value for item in series for value in item["values"] if value is not None]
    if ideal:
        all_values += ideal["values"]
    ymin = min(0.0, min(all_values) * 0.92)
    ymax = max(all_values) * 1.10
    if ymax == ymin:
        ymax += 1

    def xy(x, y):
        return (
            left + (x - 1) / 11 * (plot_right - left),
            plot_bottom - (y - ymin) / (ymax - ymin) * (plot_bottom - top),
        )

    for index in range(6):
        value = ymin + index * (ymax - ymin) / 5
        y = xy(1, value)[1]
        draw.line((left, y, plot_right, y), fill="#dddddd", width=1)
        draw.text((15, y - 10), fmt(value, 1), fill="black", font=tick_font)
    draw.line((left, top, left, plot_bottom), fill="black", width=2)
    draw.line((left, plot_bottom, plot_right, plot_bottom), fill="black", width=2)
    for core in (1, 2, 4, 6, 8, 12):
        x = xy(core, ymin)[0]
        draw.line((x, plot_bottom, x, plot_bottom + 7), fill="black", width=2)
        draw.text((x - 10, plot_bottom + 14), str(core), fill="black", font=tick_font)
    if ideal:
        points = [xy(core, value) for core, value in zip((1, 2, 4, 6, 8, 12), ideal["values"])]
        draw.line(points, fill="#777777", width=3)
    colors = ("#1976d2", "#d55e00")
    for item, color in zip(series, colors):
        points = [xy(core, value) for core, value in zip((1, 2, 4, 6, 8, 12), item["values"])]
        draw.line(points, fill=color, width=5)
        for x, y in points:
            draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=color)
    draw.text((left, 25), title, fill="black", font=title_font)
    draw.text((width // 2 - 80, height - 55), "CPU asignadas", fill="black", font=label_font)
    draw.text((15, 55), ylabel, fill="black", font=label_font)
    legend_x = plot_right - 315
    legend_y = top + 12
    for item, color in zip(series, colors):
        draw.line((legend_x, legend_y + 10, legend_x + 38, legend_y + 10), fill=color, width=5)
        draw.text((legend_x + 48, legend_y - 3), item["label"], fill="black", font=tick_font)
        legend_y += 34
    if ideal:
        draw.line((legend_x, legend_y + 10, legend_x + 38, legend_y + 10), fill="#777777", width=3)
        draw.text((legend_x + 48, legend_y - 3), ideal["label"], fill="black", font=tick_font)
    image.save(path)


def heatmap(path, names, matrix, title="Matriz de correlación de Pearson (RAPL)"):
    width, height = 1300, 1120
    margin_left, margin_top, margin_right, margin_bottom = 250, 180, 70, 90
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, cell_font = font(29, True), font(18), font(17)
    cell = min((width - margin_left - margin_right) / len(names), (height - margin_top - margin_bottom) / len(names))
    draw.text((margin_left, 30), title, fill="black", font=title_font)
    for i, name in enumerate(names):
        draw.text((margin_left + i * cell + 5, margin_top - 28), name.replace("_", "\n"), fill="black", font=font(13))
        draw.text((8, margin_top + i * cell + cell / 2 - 9), name, fill="black", font=label_font)
    for row_index, row in enumerate(matrix):
        for col_index, name in enumerate(names):
            value = row[name]
            intensity = abs(value or 0.0)
            if value is None:
                color = (225, 225, 225)
            elif value >= 0:
                color = (int(255 - 95 * intensity), int(255 - 45 * intensity), 255)
            else:
                color = (255, int(255 - 105 * intensity), int(255 - 105 * intensity))
            x0 = margin_left + col_index * cell
            y0 = margin_top + row_index * cell
            draw.rectangle((x0, y0, x0 + cell, y0 + cell), fill=color, outline="white")
            text = "--" if value is None else "{:.2f}".format(value)
            draw.text((x0 + cell / 2 - 18, y0 + cell / 2 - 9), text, fill="black", font=cell_font)
    image.save(path)


def variability_chart(path, group_stats):
    width, height = 1400, 820
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    title_font, label_font, tick_font = font(30, True), font(22), font(18)
    left, top, right, bottom = 110, 100, 50, 125
    plot_right, plot_bottom = width - right, height - bottom
    active = [row for row in group_stats if row["benchmark"] in ACTIVE_BENCHMARKS]
    ymax = max(row["avg_power_w_cv_percent"] for row in active) * 1.25
    draw.line((left, top, left, plot_bottom), fill="black", width=2)
    draw.line((left, plot_bottom, plot_right, plot_bottom), fill="black", width=2)
    group_width = (plot_right - left) / 6
    colors = ("#1976d2", "#d55e00")
    for index, profile in enumerate(PROFILES):
        x = left + index * group_width
        for offset, benchmark in enumerate(ACTIVE_BENCHMARKS):
            row = next(value for value in active if value["benchmark"] == benchmark and value["profile"] == profile)
            value = row["avg_power_w_cv_percent"]
            bar_width = group_width * 0.34
            x0 = x + group_width * (0.12 + offset * 0.38)
            y0 = plot_bottom - value / ymax * (plot_bottom - top)
            draw.rectangle((x0, y0, x0 + bar_width, plot_bottom), fill=colors[offset])
        draw.text((x + group_width / 2 - 12, plot_bottom + 15), profile[1:], fill="black", font=tick_font)
    for index in range(6):
        value = index * ymax / 5
        y = plot_bottom - value / ymax * (plot_bottom - top)
        draw.line((left, y, plot_right, y), fill="#dddddd", width=1)
        draw.text((25, y - 10), "{:.2f}".format(value), fill="black", font=tick_font)
    draw.text((left, 28), "Variabilidad de la potencia media entre repeticiones", fill="black", font=title_font)
    draw.text((width // 2 - 80, height - 52), "CPU asignadas", fill="black", font=label_font)
    draw.text((16, 65), "CV (%)", fill="black", font=label_font)
    for index, label in enumerate(("STREAM", "DGEMM")):
        x = plot_right - 260
        y = top + 10 + index * 35
        draw.rectangle((x, y, x + 25, y + 20), fill=colors[index])
        draw.text((x + 36, y - 2), label, fill="black", font=tick_font)
    image.save(path)


def representative_series(path, runs):
    selected = []
    for benchmark in ACTIVE_BENCHMARKS:
        for profile in ("p1", "p12"):
            selected.append(next(run for run in runs if run["benchmark"] == benchmark and run["profile"] == profile and run["rep"] == 1))
    width, height = 1500, 900
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = 110, 100, 60, 120
    plot_right, plot_bottom = width - right, height - bottom
    ymax = max(max(run["power_series"]) for run in selected) * 1.08
    draw.line((left, top, left, plot_bottom), fill="black", width=2)
    draw.line((left, plot_bottom, plot_right, plot_bottom), fill="black", width=2)
    colors = ("#64b5f6", "#0d47a1", "#ffb74d", "#bf360c")
    for run, color in zip(selected, colors):
        points = []
        for second, power in zip(run["time_series"], run["power_series"]):
            x = left + second / 305 * (plot_right - left)
            y = plot_bottom - power / ymax * (plot_bottom - top)
            points.append((x, y))
        draw.line(points, fill=color, width=3)
    for second in range(0, 301, 60):
        x = left + second / 305 * (plot_right - left)
        draw.text((x - 10, plot_bottom + 15), str(second), fill="black", font=font(18))
    for index in range(6):
        value = index * ymax / 5
        y = plot_bottom - value / ymax * (plot_bottom - top)
        draw.line((left, y, plot_right, y), fill="#e3e3e3", width=1)
        draw.text((25, y - 10), "{:.0f}".format(value), fill="black", font=font(18))
    draw.text((left, 25), "Series temporales RAPL representativas", fill="black", font=font(30, True))
    draw.text((width // 2 - 65, height - 50), "Tiempo (s)", fill="black", font=font(22))
    draw.text((20, 65), "Potencia (W)", fill="black", font=font(22))
    for index, (run, color) in enumerate(zip(selected, colors)):
        x, y = plot_right - 330, top + 10 + index * 32
        draw.line((x, y + 9, x + 35, y + 9), fill=color, width=4)
        draw.text((x + 45, y), "{} {}".format(BENCHMARK_LABELS[run["benchmark"]], run["profile"]), fill="black", font=font(17))
    image.save(path)


def export_tables(analysis_dir, group_stats, scalability, runs, quality):
    tables = analysis_dir / "tablas"
    tables.mkdir(parents=True, exist_ok=True)
    config_rows = [
        ("Campaña", r"\texttt{hpc\_tfg\_main\_campaign}"),
        ("Nodo", r"\texttt{compute-0-4}"),
        ("Lenguaje", "C"),
        ("Duración objetivo", "120 s"),
        ("Perfiles activos", "1, 2, 4, 6, 8 y 12 procesos monohilo"),
        ("Repeticiones", "5 por benchmark y perfil activos; 1 para Idle"),
        ("Muestreo RAPL", "Intervalo nominal de 1 s; dominios package"),
    ]
    write_latex_table(tables / "configuracion.tex", ["Parámetro", "Valor"], config_rows, "lp{8.2cm}")

    summary_rows = []
    for row in group_stats:
        if row["benchmark"] not in ACTIVE_BENCHMARKS:
            continue
        unit = "GB/s" if row["benchmark"] == "memory-stream-hpc" else "GFLOP/s"
        summary_rows.append((
            BENCHMARK_LABELS[row["benchmark"]], row["cores"],
            fmt(row["throughput_mean"]), fmt(row["throughput_ci95"]), unit,
            fmt(row["avg_power_w_mean"]), fmt(row["avg_power_w_ci95"]),
            fmt(row["energy_j_mean"] / 1000), fmt(row["energy_j_cv_percent"]),
        ))
    write_latex_table(
        tables / "resumen_activos.tex",
        ["Benchmark", "Núcleos", "Rend.", "IC$_{95}$", "Unidad", "Pot. (W)", "IC$_{95}$", "Energía (kJ)", r"CV$_E$ (\%)"],
        summary_rows,
        "lrrrrrrrr",
    )

    scale_rows = []
    for row in scalability:
        scale_rows.append((
            BENCHMARK_LABELS[row["benchmark"]], row["cores"], fmt(row["speedup"], 3),
            fmt(row["parallel_efficiency_percent"], 1), fmt(row["energy_efficiency_relative"], 3),
        ))
    write_latex_table(
        tables / "escalabilidad.tex",
        ["Benchmark", "Núcleos", "Speedup", r"Eficiencia paralela (\%)", "Eficiencia energética relativa"],
        scale_rows,
        "lrrrr",
    )

    idle = next(run for run in runs if run["benchmark"] == "idle")
    idle_rows = [
        ("Duración efectiva RAPL (s)", fmt(idle["rapl_duration_s"], 3)),
        ("Muestras RAPL", idle["rapl_samples_count"]),
        ("Energía (kJ)", fmt(idle["energy_j"] / 1000, 3)),
        ("Potencia media (W)", fmt(idle["avg_power_w"], 3)),
        ("Potencia máxima (W)", fmt(idle["peak_power_w"], 3)),
    ]
    write_latex_table(tables / "idle.tex", ["Métrica", "Valor"], idle_rows, "lr")

    validation_rows = [
        ("Ejecuciones RAPL seleccionadas", len(runs), "61/61 válidas"),
        ("Trabajos SLURM completados", sum(row["job_completed"] for row in quality), "Sin fallos en la selección"),
        ("CSV y JSON presentes", len(quality), r"Correspondencia por run\_id"),
        ("Valores negativos", sum(row["negative_values"] for row in quality), "No detectados"),
        ("Timestamps monótonos", sum(row["timestamps_monotonic"] for row in quality), "61/61 series"),
        ("Coincidencia resumen--CSV", sum(row["energy_matches_summary"] and row["average_matches_summary"] and row["peak_matches_summary"] for row in quality), "61/61 ejecuciones"),
    ]
    write_latex_table(tables / "validacion.tex", ["Comprobación", "Resultado", "Interpretación"], validation_rows, "lrl")


def main():
    args = parse_args()
    args.analysis_dir.mkdir(parents=True, exist_ok=True)
    args.figures_dir.mkdir(parents=True, exist_ok=True)
    raw_by_run, rapl_by_run = load_archive(args.archive)
    runs, quality, excluded = build_runs(
        raw_by_run, rapl_by_run, args.min_duration, args.max_duration
    )
    group_stats = build_group_statistics(runs)
    scalability = build_scalability(group_stats)
    corr_names, corr_matrix = build_correlation(runs, group_stats)

    serializable_runs = [{key: value for key, value in run.items() if key not in ("power_series", "time_series")} for run in runs]
    write_csv(args.analysis_dir / "ejecuciones_rapl.csv", serializable_runs)
    write_csv(args.analysis_dir / "control_calidad.csv", quality)
    write_csv(args.analysis_dir / "ejecuciones_descartadas.csv", excluded)
    write_csv(args.analysis_dir / "estadistica_descriptiva.csv", group_stats)
    write_csv(args.analysis_dir / "escalabilidad.csv", scalability)
    write_csv(args.analysis_dir / "matriz_correlacion_pearson.csv", corr_matrix)

    lookup = {(row["benchmark"], row["profile"]): row for row in group_stats}
    line_chart(
        args.figures_dir / "potencia_media_por_nucleos.png",
        [
            {"label": "STREAM", "values": [lookup[("memory-stream-hpc", profile)]["avg_power_w_mean"] for profile in PROFILES]},
            {"label": "DGEMM", "values": [lookup[("compute-dgemm-hpc", profile)]["avg_power_w_mean"] for profile in PROFILES]},
        ],
        "Potencia media RAPL", "Potencia (W)",
    )
    line_chart(
        args.figures_dir / "energia_por_nucleos.png",
        [
            {"label": "STREAM", "values": [lookup[("memory-stream-hpc", profile)]["energy_j_mean"] / 1000 for profile in PROFILES]},
            {"label": "DGEMM", "values": [lookup[("compute-dgemm-hpc", profile)]["energy_j_mean"] / 1000 for profile in PROFILES]},
        ],
        "Energía registrada mediante RAPL", "Energía (kJ)",
    )
    scale_lookup = {(row["benchmark"], row["profile"]): row for row in scalability}
    line_chart(
        args.figures_dir / "speedup_por_nucleos.png",
        [
            {"label": "STREAM", "values": [scale_lookup[("memory-stream-hpc", profile)]["speedup"] for profile in PROFILES]},
            {"label": "DGEMM", "values": [scale_lookup[("compute-dgemm-hpc", profile)]["speedup"] for profile in PROFILES]},
        ],
        "Speedup respecto a un proceso", "Speedup",
        ideal={"label": "Escalado ideal", "values": [1, 2, 4, 6, 8, 12]},
    )
    line_chart(
        args.figures_dir / "eficiencia_energetica_relativa.png",
        [
            {"label": "STREAM", "values": [scale_lookup[("memory-stream-hpc", profile)]["energy_efficiency_relative"] for profile in PROFILES]},
            {"label": "DGEMM", "values": [scale_lookup[("compute-dgemm-hpc", profile)]["energy_efficiency_relative"] for profile in PROFILES]},
        ],
        "Eficiencia energética relativa a un proceso", "Índice relativo",
    )
    variability_chart(args.figures_dir / "variabilidad_potencia.png", group_stats)
    heatmap(args.figures_dir / "matriz_correlacion.png", corr_names, corr_matrix)
    representative_series(args.figures_dir / "series_rapl_representativas.png", runs)
    export_tables(args.analysis_dir, group_stats, scalability, runs, quality)

    metadata = {
        "archive": str(args.archive),
        "raw_json_files": len(raw_by_run),
        "rapl_csv_files": len(rapl_by_run),
        "selected_runs": len(runs),
        "selected_by_benchmark": {
            benchmark: sum(run["benchmark"] == benchmark for run in runs)
            for benchmark in ("idle",) + ACTIVE_BENCHMARKS
        },
        "quality_failures_in_selected_window": sum(not row["valid"] for row in quality),
        "excluded_runs": len(excluded),
        "duration_filter_rapl_s": [args.min_duration, args.max_duration],
        "external_measurements_used": False,
    }
    (args.analysis_dir / "metadatos.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
