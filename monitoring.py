#!/usr/bin/env python3
import base64
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


DEFAULT_JOB = "tfg_energy_experiment"
FAIL_POLICIES = {"continue", "fail_run"}
AUTH_TYPES = {"none", "basic", "bearer"}
GROUPING_MODES = {"run_id", "latest_by_combination"}
PROHIBITED_LABELS = {"error", "external_error", "stdout", "stderr", "path", "timestamp"}


class MonitoringError(RuntimeError):
    pass


@dataclass
class PushgatewayConfig:
    url: str = ""
    job: str = DEFAULT_JOB
    timeout_s: float = 10.0
    retries: int = 3
    retry_backoff_s: float = 2.0
    tls_verify: bool = True
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    authentication: Dict[str, str] = field(default_factory=lambda: {"type": "none"})


@dataclass
class PublishConfig:
    internal_metrics: bool = True
    external_metrics: bool = True


@dataclass
class GroupingConfig:
    mode: str = "run_id"


@dataclass
class MonitoringConfig:
    enabled: bool = False
    pushgateway: PushgatewayConfig = field(default_factory=PushgatewayConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    grouping: GroupingConfig = field(default_factory=GroupingConfig)
    failure_policy: str = "continue"


def _expand_env(value: object, environ: Dict[str, str]) -> str:
    if value is None:
        return ""
    text = str(value)

    def repl(match):
        return environ.get(match.group(1), "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, text)


def _env_or_config(config_value: object, env_name: str, environ: Dict[str, str]) -> str:
    expanded = _expand_env(config_value, environ)
    return expanded or environ.get(env_name, "")


def sanitize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def sanitize_text(text: object) -> str:
    value = str(text)
    value = re.sub(r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s]+", r"\1<redacted>", value)
    value = re.sub(r"(?i)(token\s*[=:]\s*)[^\s,;]+", r"\1<redacted>", value)
    value = re.sub(r"(https?://)([^/@\s:]+):([^/@\s]+)@", r"\1<redacted>:<redacted>@", value)
    return value


def normalize_pushgateway_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if "://" not in url:
        url = "https://{}".format(url)
    return url.rstrip("/")


def parse_monitoring_config(
    campaign: Dict,
    cli_pushgateway_url: Optional[str] = None,
    environ: Optional[Dict[str, str]] = None,
) -> MonitoringConfig:
    environ = environ or os.environ
    section = campaign.get("monitoring")
    legacy_url = _expand_env(campaign.get("pushgateway_url", ""), environ)
    env_url = environ.get("PUSHGATEWAY_URL", "")
    cli_url = cli_pushgateway_url or ""

    if not section:
        url = normalize_pushgateway_url(cli_url or legacy_url or env_url)
        return MonitoringConfig(
            enabled=bool(url),
            pushgateway=PushgatewayConfig(url=url),
        )

    enabled = bool(section.get("enabled", False))
    pushgateway_section = section.get("pushgateway") or {}
    publish_section = section.get("publish") or {}
    grouping_section = section.get("grouping") or {}

    url = normalize_pushgateway_url(
        cli_url
        or _env_or_config(pushgateway_section.get("url", ""), "PUSHGATEWAY_URL", environ)
        or legacy_url
        or env_url
    )

    failure_policy = section.get("failure_policy", "continue")
    if failure_policy not in FAIL_POLICIES:
        raise MonitoringError("monitoring.failure_policy debe ser uno de {}".format(sorted(FAIL_POLICIES)))

    auth = dict(pushgateway_section.get("authentication") or {"type": "none"})
    auth_type = auth.get("type", "none")
    if auth_type not in AUTH_TYPES:
        raise MonitoringError("monitoring.pushgateway.authentication.type no soportado: {}".format(auth_type))

    grouping_mode = grouping_section.get("mode", "run_id")
    if grouping_mode not in GROUPING_MODES:
        raise MonitoringError("monitoring.grouping.mode debe ser uno de {}".format(sorted(GROUPING_MODES)))

    if enabled and not url:
        raise MonitoringError(
            "monitoring.enabled=true requiere pushgateway.url, PUSHGATEWAY_URL o --pushgateway-url"
        )

    return MonitoringConfig(
        enabled=enabled,
        pushgateway=PushgatewayConfig(
            url=url,
            job=str(pushgateway_section.get("job", DEFAULT_JOB)),
            timeout_s=float(pushgateway_section.get("timeout_s", 10.0)),
            retries=int(pushgateway_section.get("retries", 3)),
            retry_backoff_s=float(pushgateway_section.get("retry_backoff_s", 2.0)),
            tls_verify=bool(pushgateway_section.get("tls_verify", True)),
            ca_file=_expand_env(pushgateway_section.get("ca_file", ""), environ),
            cert_file=_expand_env(pushgateway_section.get("cert_file", ""), environ),
            key_file=_expand_env(pushgateway_section.get("key_file", ""), environ),
            authentication=auth,
        ),
        publish=PublishConfig(
            internal_metrics=bool(publish_section.get("internal_metrics", True)),
            external_metrics=bool(publish_section.get("external_metrics", True)),
        ),
        grouping=GroupingConfig(mode=grouping_mode),
        failure_policy=failure_policy,
    )


def load_campaign_monitoring_config(
    config_path: Optional[str],
    cli_pushgateway_url: Optional[str] = None,
    environ: Optional[Dict[str, str]] = None,
) -> MonitoringConfig:
    if not config_path:
        return parse_monitoring_config({}, cli_pushgateway_url, environ)
    with Path(config_path).open("r") as handle:
        campaign = json.load(handle)
    return parse_monitoring_config(campaign, cli_pushgateway_url, environ)


def _clean_label_value(value: object, default: str = "unknown") -> str:
    if value in (None, ""):
        return default
    return str(value)


def run_status(row: Dict) -> str:
    state = str(row.get("job_state", ""))
    if state.startswith("COMPLETED"):
        return "completed"
    if not state:
        return "unknown"
    return "failed"


def base_labels(row: Dict) -> Dict[str, str]:
    labels = {
        "campaign": _clean_label_value(row.get("campaign")),
        "benchmark": _clean_label_value(row.get("benchmark")),
        "language": _clean_label_value(row.get("language")),
        "profile": _clean_label_value(row.get("profile")),
        "cores": _clean_label_value(row.get("cores")),
        "threads": _clean_label_value(row.get("threads")),
        "rep": _clean_label_value(row.get("rep")),
        "node": _clean_label_value(row.get("node_list") or row.get("host")),
        "vampire_device": _clean_label_value(row.get("vampire_device"), "none"),
        "run_id": _clean_label_value(row.get("run_id")),
        "status": run_status(row),
    }
    return labels


def grouping_key(config: MonitoringConfig, row: Dict) -> Dict[str, str]:
    labels = base_labels(row)
    if config.grouping.mode == "latest_by_combination":
        return {
            "campaign": labels["campaign"],
            "benchmark": labels["benchmark"],
            "profile": labels["profile"],
            "cores": labels["cores"],
            "rep": labels["rep"],
        }
    return {"run_id": labels["run_id"]}


def effective_labels(row: Dict) -> Dict[str, str]:
    labels = base_labels(row)
    forbidden_present = sorted(PROHIBITED_LABELS.intersection(labels.keys()))
    if forbidden_present:
        raise MonitoringError("Etiquetas prohibidas presentes: {}".format(forbidden_present))
    return labels


def payload_labels(row: Dict, grouping: Dict[str, str]) -> Dict[str, str]:
    labels = effective_labels(row)
    for key in grouping:
        labels.pop(key, None)
    return labels


def _float_or_none(value: object) -> Optional[float]:
    if value in (None, "", "None"):
        return None
    try:
        parsed = float(value)
    except Exception:
        return None
    if parsed != parsed:
        return None
    return parsed


def _add_metric(metrics: Dict[str, float], name: str, value: object) -> None:
    parsed = _float_or_none(value)
    if parsed is not None:
        metrics[name] = parsed


def build_metrics(row: Dict, publish: Optional[PublishConfig] = None) -> Dict[str, float]:
    publish = publish or PublishConfig()
    metrics: Dict[str, float] = {}

    if publish.internal_metrics:
        _add_metric(metrics, "tfg_internal_energy_joules", row.get("energy_j"))
        _add_metric(metrics, "tfg_internal_average_power_watts", row.get("avg_power_w"))
        _add_metric(metrics, "tfg_internal_peak_power_watts", row.get("peak_power_w"))
        _add_metric(metrics, "tfg_internal_rapl_samples", row.get("rapl_samples_count"))
        _add_metric(metrics, "tfg_experiment_duration_seconds", row.get("duration_s"))
        _add_metric(metrics, "tfg_run_energy_joules", row.get("energy_j"))
        _add_metric(metrics, "tfg_run_avg_power_watts", row.get("avg_power_w"))
        _add_metric(metrics, "tfg_run_peak_power_watts", row.get("peak_power_w"))
        _add_metric(metrics, "tfg_run_duration_seconds", row.get("duration_s"))
        _add_metric(metrics, "tfg_run_ops_total", row.get("ops"))
        _add_metric(metrics, "tfg_run_ops_per_second", row.get("ops_per_sec"))
        _add_metric(metrics, "tfg_run_ops_per_joule", row.get("ops_per_j"))
        _add_metric(metrics, "tfg_run_rapl_samples_total", row.get("rapl_samples_count"))

    if publish.external_metrics:
        success = 1.0 if row.get("external_measurement_status") == "completed" else 0.0
        metrics["tfg_external_capture_success"] = success
        _add_metric(metrics, "tfg_external_duration_seconds", row.get("external_duration_s"))
        _add_metric(metrics, "tfg_external_capture_window_seconds", row.get("external_capture_window_s"))
        _add_metric(metrics, "tfg_external_samples", row.get("external_samples_count"))
        if success == 1.0:
            _add_metric(metrics, "tfg_external_energy_joules", row.get("external_energy_j"))
            _add_metric(metrics, "tfg_external_average_power_watts", row.get("external_avg_power_w"))
            _add_metric(metrics, "tfg_external_peak_power_watts", row.get("external_peak_power_w"))

    metrics["tfg_run_status"] = 1.0 if run_status(row) == "completed" else 0.0
    completed_unixtime = time.time()
    timestamp_iso = row.get("timestamp", "")
    if timestamp_iso:
        try:
            from datetime import datetime

            completed_unixtime = datetime.fromisoformat(str(timestamp_iso)).timestamp()
        except Exception:
            pass
    metrics["tfg_run_completed_unixtime"] = completed_unixtime
    return metrics


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_prometheus_payload(metrics: Dict[str, float], labels: Dict[str, str]) -> str:
    lines = []
    label_str = ""
    if labels:
        label_str = "{" + ",".join(
            '{}="{}"'.format(key, _escape_label(value))
            for key, value in sorted(labels.items())
        ) + "}"
    for name, value in sorted(metrics.items()):
        if _float_or_none(value) is None:
            continue
        lines.append("{}{} {}".format(name, label_str, float(value)))
    return "\n".join(lines) + "\n"


def _quote_path_segment(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def pushgateway_group_url(config: MonitoringConfig, grouping: Dict[str, str]) -> str:
    base = config.pushgateway.url.rstrip("/")
    parts = [base, "metrics", "job", _quote_path_segment(config.pushgateway.job)]
    for key, value in grouping.items():
        parts.append(_quote_path_segment(key))
        parts.append(_quote_path_segment(value))
    return "/".join(parts)


def _auth_headers(config: MonitoringConfig, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    environ = environ or os.environ
    auth = config.pushgateway.authentication or {"type": "none"}
    auth_type = auth.get("type", "none")
    if auth_type == "none":
        return {}
    if auth_type == "basic":
        username = _env_or_config(auth.get("username", ""), auth.get("username_env", ""), environ)
        password = _env_or_config(auth.get("password", ""), auth.get("password_env", ""), environ)
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        return {"Authorization": "Basic {}".format(token)}
    if auth_type == "bearer":
        token = _env_or_config(auth.get("token", ""), auth.get("token_env", ""), environ)
        header = auth.get("header", "Authorization")
        prefix = auth.get("prefix", "Bearer")
        return {header: "{} {}".format(prefix, token).strip()}
    raise MonitoringError("Tipo de autenticacion no soportado: {}".format(auth_type))


def _ssl_context(config: MonitoringConfig) -> Optional[ssl.SSLContext]:
    if not config.pushgateway.url.lower().startswith("https://"):
        return None
    if config.pushgateway.tls_verify:
        context = ssl.create_default_context(cafile=config.pushgateway.ca_file or None)
    else:
        print("[WARN] monitoring.pushgateway.tls_verify=false; TLS certificate verification disabled")
        context = ssl._create_unverified_context()
    if config.pushgateway.cert_file:
        context.load_cert_chain(config.pushgateway.cert_file, config.pushgateway.key_file or None)
    return context


def _request(
    method: str,
    url: str,
    payload: Optional[str],
    config: MonitoringConfig,
    urlopen: Callable = urllib.request.urlopen,
) -> None:
    headers = {"User-Agent": "tfg-energy-lab/monitoring"}
    headers.update(_auth_headers(config))
    data = None
    if payload is not None:
        data = payload.encode("utf-8")
        headers["Content-Type"] = "text/plain; version=0.0.4; charset=utf-8"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = _ssl_context(config)
    kwargs = {"timeout": config.pushgateway.timeout_s}
    if context is not None:
        kwargs["context"] = context
    with urlopen(request, **kwargs) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise MonitoringError("HTTP {} publicando en {}".format(status, sanitize_url(url)))


def publish_payload(
    config: MonitoringConfig,
    payload: str,
    grouping: Dict[str, str],
    urlopen: Callable = urllib.request.urlopen,
) -> Dict:
    if not config.enabled:
        return {"enabled": False, "status": "disabled", "attempts": 0, "error": ""}

    url = pushgateway_group_url(config, grouping)
    attempts = max(0, int(config.pushgateway.retries)) + 1
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            _request("PUT", url, payload, config, urlopen=urlopen)
            return {
                "enabled": True,
                "status": "completed",
                "attempts": attempt,
                "url": sanitize_url(url),
                "grouping": dict(grouping),
                "error": "",
                "fail_run": False,
            }
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, MonitoringError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(config.pushgateway.retry_backoff_s)

    error = sanitize_text("{}: {}".format(type(last_error).__name__, last_error))
    return {
        "enabled": True,
        "status": "failed",
        "attempts": attempts,
        "url": sanitize_url(url),
        "grouping": dict(grouping),
        "error": error,
        "fail_run": config.failure_policy == "fail_run",
    }


def publish_run_metrics(
    config: MonitoringConfig,
    row: Dict,
    urlopen: Callable = urllib.request.urlopen,
    debug_payload_path: Optional[Path] = None,
) -> Dict:
    if not config.enabled:
        return {"enabled": False, "status": "disabled", "attempts": 0, "error": "", "fail_run": False}
    grouping = grouping_key(config, row)
    labels = payload_labels(row, grouping)
    metrics = build_metrics(row, config.publish)
    payload = format_prometheus_payload(metrics, labels)
    if debug_payload_path is not None:
        debug_payload_path.parent.mkdir(parents=True, exist_ok=True)
        debug_payload_path.write_text(payload)
    result = publish_payload(config, payload, grouping, urlopen=urlopen)
    result["metrics_count"] = len(metrics)
    if debug_payload_path is not None:
        result["debug_payload_path"] = str(debug_payload_path)
    return result


def delete_group(
    config: MonitoringConfig,
    grouping: Dict[str, str],
    urlopen: Callable = urllib.request.urlopen,
) -> Dict:
    if not config.enabled:
        return {"enabled": False, "status": "disabled", "attempts": 0, "error": ""}
    url = pushgateway_group_url(config, grouping)
    try:
        _request("DELETE", url, None, config, urlopen=urlopen)
        return {"enabled": True, "status": "completed", "url": sanitize_url(url), "grouping": dict(grouping)}
    except Exception as exc:
        return {"enabled": True, "status": "failed", "url": sanitize_url(url), "error": sanitize_text(exc)}
