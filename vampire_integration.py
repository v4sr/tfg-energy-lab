#!/usr/bin/env python3
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


FAIL_POLICIES = {"fail_run", "continue_internal"}
DEFAULT_PROVIDER = "vampire"


class ExternalMeasurementError(RuntimeError):
    def __init__(self, phase: str, message: str):
        super().__init__(f"{phase}: {message}")
        self.phase = phase
        self.message = message


@dataclass
class VampireConfig:
    enabled: bool = False
    provider: str = DEFAULT_PROVIDER
    client_path: Path = Path("./vampire.py")
    user: str = ""
    node_device_map: Dict[str, str] = field(default_factory=dict)
    pre_capture_s: float = 2.0
    # Vampire exports 5 s buckets. Keep enough time after the benchmark to
    # obtain at least one real sample beyond the end boundary.
    post_capture_s: float = 15.0
    sync_timeout_s: float = 120.0
    command_timeout_s: float = 60.0
    data_ready_timeout_s: float = 60.0
    data_ready_poll_s: float = 5.0
    failure_policy: str = "fail_run"
    base_dir: Path = Path(".")
    poll_interval_s: float = 0.5


def monotonic_time_ns() -> int:
    if hasattr(time, "time_ns"):
        return time.time_ns()
    return int(time.time() * 1_000_000_000)


def load_campaign_external_config(config_path: Optional[str]) -> VampireConfig:
    if not config_path:
        return VampireConfig(enabled=False)

    path = Path(config_path)
    with path.open("r") as handle:
        campaign = json.load(handle)
    return parse_external_config(campaign, base_dir=path.parent.parent if path.parent.name == "campaigns" else Path("."))


def parse_external_config(campaign: Dict, base_dir: Path = Path(".")) -> VampireConfig:
    section = campaign.get("external_measurement") or {}
    if not section or not bool(section.get("enabled", False)):
        return VampireConfig(enabled=False, base_dir=base_dir)

    provider = section.get("provider", DEFAULT_PROVIDER)
    if provider != DEFAULT_PROVIDER:
        raise ExternalMeasurementError(
            "config",
            f"external_measurement.provider no soportado: {provider!r}",
        )

    vampire = section.get("vampire") or {}
    failure_policy = vampire.get("failure_policy", "fail_run")
    if failure_policy not in FAIL_POLICIES:
        raise ExternalMeasurementError(
            "config",
            "failure_policy debe ser uno de {}".format(sorted(FAIL_POLICIES)),
        )

    user = vampire.get("user", "")
    if not user:
        raise ExternalMeasurementError("config", "external_measurement.vampire.user es obligatorio")

    node_device_map = vampire.get("node_device_map") or {}
    if not isinstance(node_device_map, dict) or not node_device_map:
        raise ExternalMeasurementError(
            "config",
            "external_measurement.vampire.node_device_map debe contener al menos un nodo",
        )

    client_path = Path(os.path.expandvars(vampire.get("client_path", "./vampire.py")))
    if not client_path.is_absolute():
        client_path = base_dir / client_path

    pre_capture_s = float(vampire.get("pre_capture_s", 2.0))
    post_capture_s = float(vampire.get("post_capture_s", 15.0))
    sync_timeout_s = float(vampire.get("sync_timeout_s", 120.0))
    benchmark_duration_s = float(campaign.get("duration", 0) or 0)
    # La espera de benchmark_finished comienza antes de la preparación de RAPL
    # y de srun. Un timeout igual a la duración nominal corta Vampire antes de
    # que el job pueda escribir el marcador final (caso real: 120 s vs 153 s).
    if benchmark_duration_s > 0:
        minimum_sync_timeout_s = benchmark_duration_s + pre_capture_s + post_capture_s + 60.0
        if sync_timeout_s < minimum_sync_timeout_s:
            raise ExternalMeasurementError(
                "config",
                "external_measurement.vampire.sync_timeout_s={} es insuficiente para "
                "duration={}; debe ser al menos {} s (benchmark, captura y margen de arranque)".format(
                    sync_timeout_s,
                    benchmark_duration_s,
                    minimum_sync_timeout_s,
                ),
            )

    return VampireConfig(
        enabled=True,
        provider=provider,
        client_path=client_path,
        user=user,
        node_device_map={str(k): str(v) for k, v in node_device_map.items()},
        pre_capture_s=pre_capture_s,
        post_capture_s=post_capture_s,
        sync_timeout_s=sync_timeout_s,
        command_timeout_s=float(vampire.get("command_timeout_s", 60.0)),
        data_ready_timeout_s=float(vampire.get("data_ready_timeout_s", 60.0)),
        data_ready_poll_s=float(vampire.get("data_ready_poll_s", 5.0)),
        failure_policy=failure_policy,
        base_dir=base_dir,
    )


def normalize_vampire_experiment_id(run_id: str) -> str:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("._-")
    if not safe_run_id:
        safe_run_id = "run"
    experiment_id = f"vampire_{safe_run_id}"
    if len(experiment_id) <= 180:
        return experiment_id

    import hashlib

    digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()[:12]
    return f"{experiment_id[:160]}_{digest}"


def resolve_vampire_device(config: VampireConfig, hostname: str) -> str:
    if hostname in config.node_device_map:
        return config.node_device_map[hostname]
    # Slurm/socket puede devolver nombre corto o FQDN según el nodo. Solo se
    # acepta la equivalencia si identifica una única entrada del mapa.
    short_hostname = str(hostname).split(".", 1)[0].lower()
    matches = [
        device for node, device in config.node_device_map.items()
        if str(node).split(".", 1)[0].lower() == short_hostname
    ]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(sorted(config.node_device_map.keys()))
    raise ExternalMeasurementError(
        "device_resolution",
        "No hay dispositivo Vampire configurado para el hostname detectado "
        f"{hostname!r}. Nodos disponibles en la configuracion: {available}",
    )


def build_vampire_command(
    config: VampireConfig,
    action: str,
    experiment_id: Optional[str] = None,
    device: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> List[str]:
    cmd = [sys.executable, str(config.client_path), action, "-u", config.user]
    if experiment_id is not None:
        cmd.extend(["-e", experiment_id])
    if device is not None:
        cmd.extend(["-d", device])
    if output_path is not None:
        cmd.extend(["-o", str(output_path)])
    return cmd


def sanitize_text(value: str) -> str:
    value = re.sub(r"(?i)(token\s*[=:]\s*)[^\s,;]+", r"\1<redacted>", value)
    value = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1<redacted>", value)
    return value


def append_vampire_log(log_path: Path, lines: Iterable[str]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as handle:
        for line in lines:
            handle.write(sanitize_text(str(line)))
            if not str(line).endswith("\n"):
                handle.write("\n")


def run_vampire_command(
    config: VampireConfig,
    action: str,
    experiment_id: Optional[str] = None,
    device: Optional[str] = None,
    output_path: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> subprocess.CompletedProcess:
    if not config.client_path.exists():
        raise ExternalMeasurementError(
            action,
            f"Cliente Vampire no encontrado: {config.client_path}",
        )

    cmd = build_vampire_command(config, action, experiment_id, device, output_path)
    if log_path is not None:
        append_vampire_log(log_path, [f"$ {' '.join(cmd)}"])

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=config.command_timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExternalMeasurementError(
            action,
            f"Timeout ejecutando Vampire tras {config.command_timeout_s}s: {exc}",
        ) from exc

    if log_path is not None:
        append_vampire_log(
            log_path,
            [
                f"[returncode] {result.returncode}",
                "[stdout]",
                result.stdout or "",
                "[stderr]",
                result.stderr or "",
            ],
        )

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise ExternalMeasurementError(
            action,
            f"vampire.py {action} fallo con codigo {result.returncode}: {sanitize_text(message)}",
        )

    return result


def explain_vampire_preflight_error(exc: Exception) -> str:
    text = str(exc)
    lower = text.lower()
    lines = [text]
    if "connection refused" in lower or "failed to establish" in lower:
        lines.append(
            "No se puede contactar con InfluxDB mediante Vampire. "
            "Comprueba que el tunel SSH hacia 127.0.0.1:8086 esta abierto."
        )
    if "no module named" in lower or "modulenotfounderror" in lower:
        lines.append(
            "Faltan dependencias Python para ejecutar vampire.py en el entorno activo."
        )
    if "unauthorized" in lower or "authentication" in lower or "forbidden" in lower:
        lines.append("Vampire informa de un problema de autenticacion o permisos.")
    return "\n".join(lines)


def preflight_vampire(config: VampireConfig) -> None:
    if not config.enabled:
        return
    try:
        run_vampire_command(config, "list")
    except Exception as exc:
        raise ExternalMeasurementError("preflight", explain_vampire_preflight_error(exc)) from exc


def atomic_write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{monotonic_time_ns()}")
    with tmp.open("w") as handle:
        json.dump(payload, handle, indent=2)
    tmp.replace(path)


def atomic_touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{monotonic_time_ns()}")
    tmp.write_text("ready\n")
    tmp.replace(path)


def wait_for_json_file(
    path: Path,
    timeout_s: float,
    phase: str,
    run_id: str,
    poll_interval_s: float = 0.5,
    is_job_active: Optional[Callable[[], bool]] = None,
) -> Dict:
    deadline = time.monotonic() + timeout_s
    last_error = None
    while time.monotonic() < deadline:
        if path.exists():
            try:
                with path.open("r") as handle:
                    return json.load(handle)
            except json.JSONDecodeError as exc:
                last_error = exc
        if is_job_active is not None and not is_job_active():
            raise ExternalMeasurementError(
                phase,
                f"El trabajo Slurm termino antes de que apareciera {path} para run_id={run_id}",
            )
        time.sleep(poll_interval_s)

    detail = f"Timeout esperando {path} para run_id={run_id} tras {timeout_s}s"
    if last_error is not None:
        detail += f"; ultimo error leyendo JSON: {last_error}"
    raise ExternalMeasurementError(phase, detail)


def parse_vampire_json(stdout: str) -> Dict:
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except Exception:
            continue
    return {}


def timestamp_to_ns(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
            return int(dt.timestamp() * 1_000_000_000)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return int(dt.timestamp() * 1_000_000_000)
        return int(dt.timestamp() * 1_000_000_000)
    except Exception:
        return None


def ns_to_iso_utc(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc).isoformat()


def base_external_result(
    enabled: bool,
    provider: str = DEFAULT_PROVIDER,
    status: str = "disabled",
) -> Dict:
    return {
        "provider": provider,
        "enabled": enabled,
        "status": status,
        "user": "",
        "experiment_id": "",
        "device": "",
        "compute_node": "",
        "started_at": None,
        "stopped_at": None,
        "started_at_ns": None,
        "stopped_at_ns": None,
        "benchmark_start_ns": None,
        "benchmark_end_ns": None,
        "csv_path": "",
        "log_path": "",
        "samples_count": "",
        "duration_s": "",
        "capture_window_s": "",
        "energy_j": "",
        "avg_power_w": "",
        "peak_power_w": "",
        "error": "",
        "failure_policy": "",
        "fail_run": False,
    }


def external_summary_fields(result: Dict) -> Dict:
    return {
        "external_measurement_enabled": bool(result.get("enabled")),
        "external_measurement_status": result.get("status", ""),
        "vampire_experiment_id": result.get("experiment_id", ""),
        "vampire_device": result.get("device", ""),
        "external_samples_count": result.get("samples_count", ""),
        "external_duration_s": result.get("duration_s", ""),
        "external_capture_window_s": result.get("capture_window_s", ""),
        "external_energy_j": result.get("energy_j", ""),
        "external_avg_power_w": result.get("avg_power_w", ""),
        "external_peak_power_w": result.get("peak_power_w", ""),
        "external_error": result.get("error", ""),
    }


def _to_float(value: object) -> Optional[float]:
    if value in (None, "", "None"):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _parse_time_value(value: str, experiment_start_ns: int) -> Optional[float]:
    numeric = _to_float(value)
    if numeric is not None:
        return experiment_start_ns / 1_000_000_000 + numeric
    ns_value = timestamp_to_ns(value)
    if ns_value is None:
        return None
    return ns_value / 1_000_000_000


def _find_energy_column(columns: List[str]) -> Optional[Tuple[str, float]]:
    for column in columns:
        lower = column.lower()
        if "energy" not in lower and "kwh" not in lower and "joule" not in lower:
            continue
        if "kwh" in lower:
            return column, 3_600_000.0
        if re.search(r"(^|[_-])wh($|[_-])", lower):
            return column, 3_600.0
        if lower.endswith("_j") or "joule" in lower or lower.endswith("(j)"):
            return column, 1.0
    return None


def _interpolate(samples: List[Tuple[float, float]], target: float) -> Optional[float]:
    for timestamp_s, value in samples:
        if abs(timestamp_s - target) < 1e-9:
            return value
    before = None
    after = None
    for sample in samples:
        if sample[0] < target:
            before = sample
        elif sample[0] > target:
            after = sample
            break
    if before is None or after is None:
        return None
    t0, v0 = before
    t1, v1 = after
    if t1 == t0:
        return None
    ratio = (target - t0) / (t1 - t0)
    return v0 + (v1 - v0) * ratio


def _deduplicate_samples(samples: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    samples.sort(key=lambda item: item[0])
    deduped = []
    for timestamp_s, value in samples:
        if deduped and abs(deduped[-1][0] - timestamp_s) < 1e-9:
            if abs(deduped[-1][1] - value) > 1e-9:
                raise ExternalMeasurementError(
                    "external_csv",
                    f"Timestamp duplicado con valores distintos: {timestamp_s}",
                )
            continue
        deduped.append((timestamp_s, value))
    return deduped


def _require_window_bracketed(
    samples: List[Tuple[float, float]],
    window_start_s: float,
    window_end_s: float,
) -> None:
    """Require real samples on both sides of the complete benchmark window.

    Interpolation is valid only inside the observed interval.  Previously an
    incomplete tail was integrated up to the last available sample and divided
    by the full benchmark duration, producing a plausible but false low average.
    """
    if not samples:
        raise ExternalMeasurementError("external_csv", "CSV Vampire sin muestras validas")
    first_s = samples[0][0]
    last_s = samples[-1][0]
    missing_head_s = max(0.0, first_s - window_start_s)
    missing_tail_s = max(0.0, window_end_s - last_s)
    if missing_head_s > 1e-9 or missing_tail_s > 1e-9:
        duration_s = window_end_s - window_start_s
        covered_start_s = max(first_s, window_start_s)
        covered_end_s = min(last_s, window_end_s)
        covered_s = max(0.0, covered_end_s - covered_start_s)
        coverage = 100.0 * covered_s / duration_s if duration_s > 0 else 0.0
        raise ExternalMeasurementError(
            "external_csv",
            "CSV Vampire no cubre toda la ventana del benchmark: "
            "first={:.6f}, start={:.6f}, last={:.6f}, end={:.6f}, "
            "missing_head_s={:.6f}, missing_tail_s={:.6f}, coverage={:.2f}%".format(
                first_s,
                window_start_s,
                last_s,
                window_end_s,
                missing_head_s,
                missing_tail_s,
                coverage,
            ),
        )


def process_vampire_csv(
    csv_path: Path,
    device: str,
    benchmark_start_ns: int,
    benchmark_end_ns: int,
    experiment_start_ns: int,
) -> Dict:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        raise ExternalMeasurementError("external_csv", f"CSV Vampire vacio o inexistente: {csv_path}")
    if benchmark_end_ns <= benchmark_start_ns:
        raise ExternalMeasurementError("external_csv", "Duracion de benchmark no positiva")

    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        rows = list(reader)

    if not columns or not rows:
        raise ExternalMeasurementError("external_csv", f"CSV Vampire sin filas validas. Columnas: {columns}")

    time_column = "Time" if "Time" in columns else ("time" if "time" in columns else columns[0])
    if device in columns:
        power_column = device
    else:
        candidates = [c for c in columns if c.lower() in {"power", "power_w", "active_power_w"}]
        power_column = candidates[0] if candidates else None

    energy_column = _find_energy_column(columns)
    window_start_s = benchmark_start_ns / 1_000_000_000
    window_end_s = benchmark_end_ns / 1_000_000_000

    if energy_column is not None:
        column, factor = energy_column
        samples = []
        for row in rows:
            timestamp_s = _parse_time_value(row.get(time_column, ""), experiment_start_ns)
            value = _to_float(row.get(column))
            if timestamp_s is None or value is None:
                continue
            samples.append((timestamp_s, value))
        samples = _deduplicate_samples(samples)
        _require_window_bracketed(samples, window_start_s, window_end_s)
        start_energy = _interpolate(samples, window_start_s)
        end_energy = _interpolate(samples, window_end_s)
        if start_energy is None or end_energy is None:
            raise ExternalMeasurementError(
                "external_csv",
                f"No se pudo interpolar el contador de energia en los limites. Columnas: {columns}",
            )
        inside = [(t, v) for t, v in samples if window_start_s <= t <= window_end_s]
        energy_j = (end_energy - start_energy) * factor
        duration_s = (benchmark_end_ns - benchmark_start_ns) / 1_000_000_000
        return {
            "samples_count": len(inside),
            "duration_s": duration_s,
            "energy_j": energy_j,
            "avg_power_w": energy_j / duration_s if duration_s > 0 else "",
            "peak_power_w": "",
        }

    if power_column is None:
        raise ExternalMeasurementError(
            "external_csv",
            f"No se encontro columna de potencia para device={device!r}. Columnas reales: {columns}",
        )

    samples = []
    for row in rows:
        timestamp_s = _parse_time_value(row.get(time_column, ""), experiment_start_ns)
        value = _to_float(row.get(power_column))
        if timestamp_s is None or value is None:
            continue
        samples.append((timestamp_s, value))
    samples = _deduplicate_samples(samples)

    if len(samples) < 2:
        raise ExternalMeasurementError("external_csv", "CSV Vampire con menos de dos muestras validas")

    _require_window_bracketed(samples, window_start_s, window_end_s)

    inside = [(t, p) for t, p in samples if window_start_s <= t <= window_end_s]
    if not inside:
        raise ExternalMeasurementError(
            "external_csv",
            f"CSV sin muestras dentro del intervalo del benchmark. Columnas: {columns}",
        )

    points = []
    start_power = _interpolate(samples, window_start_s)
    end_power = _interpolate(samples, window_end_s)
    if start_power is not None:
        points.append((window_start_s, start_power))
    points.extend(inside)
    if end_power is not None:
        points.append((window_end_s, end_power))
    points = _deduplicate_samples(points)

    if len(points) < 2:
        raise ExternalMeasurementError("external_csv", "No hay puntos suficientes para integrar potencia")

    energy_j = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t1 <= t0:
            raise ExternalMeasurementError("external_csv", "Timestamps no ordenados en CSV Vampire")
        energy_j += ((p0 + p1) / 2.0) * (t1 - t0)

    duration_s = (benchmark_end_ns - benchmark_start_ns) / 1_000_000_000
    peak_power_w = max(value for _, value in points)
    return {
        "samples_count": len(inside),
        "duration_s": duration_s,
        "energy_j": energy_j,
        "avg_power_w": energy_j / duration_s if duration_s > 0 else "",
        "peak_power_w": peak_power_w,
    }


def download_and_process_vampire_csv(
    config: VampireConfig,
    experiment_id: str,
    device: str,
    csv_path: Path,
    log_path: Path,
    benchmark_start_ns: int,
    benchmark_end_ns: int,
    experiment_start_ns: int,
) -> Dict:
    """Retry get while InfluxDB is still exposing an incomplete capture tail."""
    deadline = time.monotonic() + max(0.0, config.data_ready_timeout_s)
    attempt = 0
    while True:
        attempt += 1
        run_vampire_command(
            config,
            "get",
            experiment_id=experiment_id,
            device=device,
            output_path=csv_path,
            log_path=log_path,
        )
        try:
            return process_vampire_csv(
                csv_path,
                device,
                benchmark_start_ns,
                benchmark_end_ns,
                experiment_start_ns,
            )
        except ExternalMeasurementError as exc:
            retryable = "no cubre toda la ventana del benchmark" in str(exc)
            remaining_s = deadline - time.monotonic()
            if not retryable or remaining_s <= 0:
                raise
            wait_s = min(max(0.1, config.data_ready_poll_s), remaining_s)
            append_vampire_log(
                log_path,
                [
                    "[get retry] attempt={} wait_s={:.3f} reason={}".format(
                        attempt, wait_s, exc
                    )
                ],
            )
            time.sleep(wait_s)


def run_vampire_measurement(
    config: VampireConfig,
    run_id: str,
    job_id: str,
    sync_dir: Path,
    vampire_csv_path: Path,
    vampire_log_path: Path,
    is_job_active: Optional[Callable[[], bool]] = None,
) -> Dict:
    result = base_external_result(True, status="pending")
    result.update({
        "user": config.user,
        "experiment_id": normalize_vampire_experiment_id(run_id),
        "csv_path": str(vampire_csv_path),
        "log_path": str(vampire_log_path),
        "failure_policy": config.failure_policy,
    })

    job_started_path = sync_dir / "job_started.json"
    benchmark_finished_path = sync_dir / "benchmark_finished.json"
    ready_path = sync_dir / "vampire_ready"
    abort_path = sync_dir / "vampire_abort.json"

    started = False
    ready_written = False
    stop_error = None
    primary_error = None

    try:
        job_started = wait_for_json_file(
            job_started_path,
            config.sync_timeout_s,
            "wait_job_started",
            run_id,
            config.poll_interval_s,
            is_job_active,
        )
        result["compute_node"] = job_started.get("hostname", "") or job_started.get("node", "")
        result["device"] = resolve_vampire_device(config, result["compute_node"])

        start_fallback_ns = monotonic_time_ns()
        start_result = run_vampire_command(
            config,
            "start",
            experiment_id=result["experiment_id"],
            log_path=vampire_log_path,
        )
        started = True
        start_payload = parse_vampire_json(start_result.stdout)
        result["started_at"] = start_payload.get("start_time") or ns_to_iso_utc(start_fallback_ns)
        result["started_at_ns"] = timestamp_to_ns(result["started_at"]) or start_fallback_ns

        atomic_touch(ready_path)
        ready_written = True

        benchmark_finished = wait_for_json_file(
            benchmark_finished_path,
            config.sync_timeout_s,
            "wait_benchmark_finished",
            run_id,
            config.poll_interval_s,
            is_job_active,
        )
        result["benchmark_start_ns"] = benchmark_finished.get("benchmark_start_ns")
        result["benchmark_end_ns"] = benchmark_finished.get("benchmark_end_ns")

        if config.post_capture_s > 0:
            time.sleep(config.post_capture_s)
    except Exception as exc:
        primary_error = exc
        if not ready_written:
            if config.failure_policy == "continue_internal":
                atomic_touch(ready_path)
                ready_written = True
            else:
                atomic_write_json(
                    abort_path,
                    {
                        "run_id": run_id,
                        "slurm_job_id": job_id,
                        "vampire_experiment_id": result["experiment_id"],
                        "phase": getattr(exc, "phase", "external_measurement"),
                        "error": str(exc),
                        "timestamp_ns": monotonic_time_ns(),
                    },
                )
        if config.failure_policy == "continue_internal" and benchmark_finished_path.exists():
            try:
                benchmark_finished = json.loads(benchmark_finished_path.read_text())
                result["benchmark_start_ns"] = benchmark_finished.get("benchmark_start_ns")
                result["benchmark_end_ns"] = benchmark_finished.get("benchmark_end_ns")
            except Exception:
                pass
    finally:
        if started:
            try:
                stop_fallback_ns = monotonic_time_ns()
                stop_result = run_vampire_command(
                    config,
                    "stop",
                    experiment_id=result["experiment_id"],
                    log_path=vampire_log_path,
                )
                stop_payload = parse_vampire_json(stop_result.stdout)
                result["stopped_at"] = stop_payload.get("stop_time") or ns_to_iso_utc(stop_fallback_ns)
                result["stopped_at_ns"] = timestamp_to_ns(result["stopped_at"]) or stop_fallback_ns
                if result.get("started_at_ns") and result.get("stopped_at_ns"):
                    result["capture_window_s"] = max(
                        0.0, (result["stopped_at_ns"] - result["started_at_ns"]) / 1_000_000_000
                    )
            except Exception as exc:
                stop_error = exc

    if primary_error is not None:
        result["status"] = "failed"
        result["error"] = str(primary_error)
        result["fail_run"] = config.failure_policy == "fail_run"
        return result

    if stop_error is not None:
        result["status"] = "failed"
        result["error"] = str(stop_error)
        result["fail_run"] = config.failure_policy == "fail_run"
        return result

    try:
        metrics = download_and_process_vampire_csv(
            config=config,
            experiment_id=result["experiment_id"],
            device=result["device"],
            csv_path=vampire_csv_path,
            log_path=vampire_log_path,
            benchmark_start_ns=int(result["benchmark_start_ns"]),
            benchmark_end_ns=int(result["benchmark_end_ns"]),
            experiment_start_ns=int(result["started_at_ns"]),
        )
        result.update(metrics)
        result["status"] = "completed"
        result["error"] = None
        shutil.rmtree(sync_dir, ignore_errors=True)
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        result["fail_run"] = config.failure_policy == "fail_run"

    return result
