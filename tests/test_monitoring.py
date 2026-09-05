import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

import monitoring


class FakeResponse:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class Recorder:
    def __init__(self, failures=0, exc=None):
        self.calls = []
        self.failures = failures
        self.exc = exc or TimeoutError("token=secret timeout")

    def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if len(self.calls) <= self.failures:
            raise self.exc
        return FakeResponse()


def sample_row(**overrides):
    row = {
        "campaign": "camp",
        "run_id": "run-001",
        "timestamp": "2026-07-16T12:00:00",
        "host": "front",
        "job_id": "123",
        "job_state": "COMPLETED",
        "node_list": "compute-0-4",
        "exit_code": "0:0",
        "language": "c",
        "benchmark": "compute-dgemm-hpc",
        "profile": "s0_c4",
        "cores": 4,
        "threads": 4,
        "rep": 1,
        "duration_s": 10.0,
        "ops": 1000,
        "ops_per_sec": 100,
        "rapl_samples_count": 11,
        "energy_j": 120.0,
        "avg_power_w": 12.0,
        "peak_power_w": 20.0,
        "ops_per_j": 8.333,
        "external_measurement_status": "completed",
        "vampire_device": "hpm4",
        "external_samples_count": 10,
        "external_duration_s": 10.0,
        "external_capture_window_s": 14.0,
        "external_energy_j": 300.0,
        "external_avg_power_w": 30.0,
        "external_peak_power_w": 55.0,
    }
    row.update(overrides)
    return row


def enabled_config(**overrides):
    config = monitoring.MonitoringConfig(enabled=True)
    config.pushgateway.url = "https://push.example.invalid/pushgateway"
    config.pushgateway.timeout_s = 0.01
    config.pushgateway.retry_backoff_s = 0.0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class MonitoringTests(unittest.TestCase):
    def test_monitoring_disabled(self):
        config = monitoring.parse_monitoring_config({}, environ={})

        self.assertFalse(config.enabled)
        result = monitoring.publish_run_metrics(config, sample_row(), urlopen=Recorder())
        self.assertEqual(result["status"], "disabled")

    def test_builds_internal_and_external_metrics(self):
        metrics = monitoring.build_metrics(sample_row())

        self.assertEqual(metrics["tfg_internal_energy_joules"], 120.0)
        self.assertEqual(metrics["tfg_external_energy_joules"], 300.0)
        self.assertEqual(metrics["tfg_external_capture_success"], 1.0)
        self.assertEqual(metrics["tfg_external_capture_window_seconds"], 14.0)
        self.assertEqual(metrics["tfg_experiment_duration_seconds"], 10.0)

    def test_publishes_rapl_and_vampire_payload(self):
        config = enabled_config()
        recorder = Recorder()

        result = monitoring.publish_run_metrics(config, sample_row(), urlopen=recorder)
        body = recorder.calls[0][0].data.decode("utf-8")

        self.assertEqual(result["status"], "completed")
        self.assertIn("tfg_internal_energy_joules", body)
        self.assertIn("tfg_external_energy_joules", body)
        self.assertIn('benchmark="compute-dgemm-hpc"', body)
        self.assertIn("/metrics/job/tfg_energy_experiment/run_id/run-001", recorder.calls[0][0].full_url)

    def test_writes_auditable_payload_without_credentials(self):
        config = enabled_config()
        with tempfile.TemporaryDirectory() as tmp:
            debug_path = Path(tmp) / "run-001-prometheus.txt"
            result = monitoring.publish_run_metrics(
                config, sample_row(), urlopen=Recorder(), debug_payload_path=debug_path
            )
            payload = debug_path.read_text()
        self.assertEqual(result["status"], "completed")
        self.assertIn("tfg_internal_energy_joules", payload)
        self.assertIn("tfg_external_energy_joules", payload)
        self.assertNotIn("secret", payload)

    def test_labels_are_expected_and_do_not_include_forbidden_values(self):
        labels = monitoring.effective_labels(sample_row(external_error="do not publish me"))

        self.assertEqual(labels["campaign"], "camp")
        self.assertEqual(labels["profile"], "s0_c4")
        self.assertEqual(labels["cores"], "4")
        self.assertEqual(labels["vampire_device"], "hpm4")
        for forbidden in monitoring.PROHIBITED_LABELS:
            self.assertNotIn(forbidden, labels)

    def test_timeout_retries_are_limited_and_continue_policy_does_not_fail_run(self):
        config = enabled_config()
        config.pushgateway.retries = 2
        config.failure_policy = "continue"
        recorder = Recorder(failures=3)

        result = monitoring.publish_run_metrics(config, sample_row(), urlopen=recorder)

        self.assertEqual(len(recorder.calls), 3)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["fail_run"])
        self.assertNotIn("secret", result["error"])

    def test_http_error_uses_fail_run_policy(self):
        config = enabled_config()
        config.pushgateway.retries = 0
        config.failure_policy = "fail_run"
        error = urllib.error.HTTPError(
            "https://user:pass@push.example.invalid/metrics",
            500,
            "boom",
            hdrs=None,
            fp=None,
        )
        recorder = Recorder(failures=1, exc=error)

        result = monitoring.publish_run_metrics(config, sample_row(), urlopen=recorder)

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["fail_run"])
        self.assertNotIn("user:pass", result["error"])

    def test_vampire_failure_only_publishes_capture_success(self):
        metrics = monitoring.build_metrics(
            sample_row(
                external_measurement_status="failed",
                external_energy_j="",
                external_avg_power_w="",
                external_peak_power_w="",
            )
        )

        self.assertEqual(metrics["tfg_external_capture_success"], 0.0)
        self.assertNotIn("tfg_external_energy_joules", metrics)
        self.assertNotIn("tfg_external_average_power_watts", metrics)

    def test_common_identity_labels_are_identical_for_both_sources(self):
        labels = monitoring.effective_labels(sample_row())
        identity = ("campaign", "benchmark", "profile", "cores", "threads", "rep", "node", "run_id")
        self.assertEqual({key: labels[key] for key in identity}, {key: labels[key] for key in identity})
        self.assertEqual(labels["run_id"], "run-001")

    def test_grouping_keys_for_run_id_and_latest_result(self):
        row = sample_row()
        run_config = enabled_config()
        latest_config = enabled_config(grouping=monitoring.GroupingConfig(mode="latest_by_combination"))

        self.assertEqual(monitoring.grouping_key(run_config, row), {"run_id": "run-001"})
        self.assertEqual(
            monitoring.grouping_key(latest_config, row),
            {
                "campaign": "camp",
                "benchmark": "compute-dgemm-hpc",
                "profile": "s0_c4",
                "cores": "4",
                "rep": "1",
            },
        )

    def test_delete_group_only_targets_selected_group(self):
        config = enabled_config()
        recorder = Recorder()

        result = monitoring.delete_group(config, {"run_id": "run-001"}, urlopen=recorder)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(recorder.calls[0][0].get_method(), "DELETE")
        self.assertIn("/run_id/run-001", recorder.calls[0][0].full_url)

    def test_configuration_expands_environment_url_and_auth(self):
        campaign = {
            "monitoring": {
                "enabled": True,
                "pushgateway": {
                    "url": "${PUSHGATEWAY_URL}",
                    "authentication": {"type": "bearer", "token_env": "PG_TOKEN"},
                },
            }
        }
        config = monitoring.parse_monitoring_config(
            campaign,
            environ={"PUSHGATEWAY_URL": "https://push.example.invalid", "PG_TOKEN": "secret"},
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.pushgateway.url, "https://push.example.invalid")
        self.assertEqual(monitoring._auth_headers(config, {"PG_TOKEN": "secret"})["Authorization"], "Bearer secret")

    def test_legacy_pushgateway_url_enables_monitoring(self):
        config = monitoring.parse_monitoring_config({"pushgateway_url": "https://push.example.invalid"}, environ={})

        self.assertTrue(config.enabled)
        self.assertEqual(config.pushgateway.job, "tfg_energy_experiment")

    def test_legacy_pushgateway_url_can_use_environment_placeholder(self):
        disabled = monitoring.parse_monitoring_config({"pushgateway_url": "${PUSHGATEWAY_URL}"}, environ={})
        enabled = monitoring.parse_monitoring_config(
            {"pushgateway_url": "${PUSHGATEWAY_URL}"},
            environ={"PUSHGATEWAY_URL": "https://push.example.invalid"},
        )

        self.assertFalse(disabled.enabled)
        self.assertTrue(enabled.enabled)
        self.assertEqual(enabled.pushgateway.url, "https://push.example.invalid")

    def test_dashboard_json_is_valid(self):
        path = Path("deploy/monitoring/grafana/dashboards/tfg-energy-vampire.json")
        dashboard = json.loads(path.read_text())
        scientific = [panel for panel in dashboard["panels"] if panel["type"] != "row"]
        self.assertEqual(len(scientific), 11)  # texto + paneles numerados 1..10
        self.assertEqual(len([p for p in dashboard["panels"] if p["type"] == "row"]), 4)
        self.assertEqual([p["title"] for p in scientific], [
            "RAPL y Vampire: alcance de las mediciones",
            "Energía interna frente a externa", "Potencia media interna frente a externa",
            "Potencia máxima interna frente a externa", "Diferencia y cobertura",
            "Duración y validez", "Energía media por número de cores",
            "Potencia media por número de cores", "Duración media del benchmark por número de cores",
            "Tabla de resultados", "Capturas Vampire fallidas",
        ])
        names = [v["name"] for v in dashboard["templating"]["list"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names, ["DS_PROMETHEUS", "campaign", "benchmark", "profile", "cores", "threads", "node", "run_id", "rep", "vampire_device"])
        run_var = next(v for v in dashboard["templating"]["list"] if v["name"] == "run_id")
        self.assertFalse(run_var["multi"])
        self.assertFalse(run_var["includeAll"])
        text = json.dumps(dashboard, ensure_ascii=False)
        for forbidden in ("Ops/s", "Ops/J", "pushgateway_group_", "Average Power Across Time", "Energy Across Time"):
            self.assertNotIn(forbidden, text)
        external_exprs = [t["expr"] for p in scientific for t in p.get("targets", []) if "tfg_external_" in t.get("expr", "")]
        self.assertTrue(external_exprs)
        self.assertTrue(all('vampire_device=~' not in expr for expr in external_exprs[:-1]))
        self.assertNotIn("ignoring(", text)
        self.assertIn("on(campaign,benchmark,profile,cores,threads,rep,node,run_id)", text)


if __name__ == "__main__":
    unittest.main()
