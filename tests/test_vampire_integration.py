import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import vampire_integration as vampire


class VampireIntegrationTests(unittest.TestCase):
    def test_rejects_sync_timeout_that_cannot_cover_benchmark(self):
        campaign = {
            "duration": 120,
            "external_measurement": {
                "enabled": True,
                "provider": "vampire",
                "vampire": {
                    "user": "u",
                    "node_device_map": {"node-a": "hpm4"},
                    "pre_capture_s": 2,
                    "post_capture_s": 2,
                    "sync_timeout_s": 120,
                },
            },
        }

        with self.assertRaises(vampire.ExternalMeasurementError) as ctx:
            vampire.parse_external_config(campaign)

        self.assertIn("sync_timeout_s=120.0 es insuficiente", str(ctx.exception))
        self.assertIn("al menos 184.0", str(ctx.exception))

    def test_accepts_sync_timeout_with_operational_margin(self):
        campaign = {
            "duration": 120,
            "external_measurement": {
                "enabled": True,
                "provider": "vampire",
                "vampire": {
                    "user": "u",
                    "node_device_map": {"node-a": "hpm4"},
                    "pre_capture_s": 2,
                    "post_capture_s": 2,
                    "sync_timeout_s": 300,
                },
            },
        }

        config = vampire.parse_external_config(campaign)

        self.assertEqual(config.sync_timeout_s, 300.0)

    def test_disabled_config_keeps_external_summary_disabled(self):
        config = vampire.parse_external_config({}, base_dir=Path("."))

        self.assertFalse(config.enabled)
        summary = vampire.external_summary_fields(vampire.base_external_result(False))
        self.assertFalse(summary["external_measurement_enabled"])
        self.assertEqual(summary["external_measurement_status"], "disabled")

    def test_builds_vampire_commands_without_shell(self):
        config = vampire.VampireConfig(
            enabled=True,
            client_path=Path("/tmp/vampire.py"),
            user="alemadbu",
        )

        cmd = vampire.build_vampire_command(
            config,
            "get",
            experiment_id="vampire_run",
            device="hpm4",
            output_path=Path("out.csv"),
        )

        self.assertEqual(
            cmd,
            [
                sys.executable,
                "/tmp/vampire.py",
                "get",
                "-u",
                "alemadbu",
                "-e",
                "vampire_run",
                "-d",
                "hpm4",
                "-o",
                "out.csv",
            ],
        )

    def test_normalizes_experiment_id(self):
        self.assertEqual(
            vampire.normalize_vampire_experiment_id("2026 07/idle r1"),
            "vampire_2026_07_idle_r1",
        )
        self.assertTrue(vampire.normalize_vampire_experiment_id("x" * 500).startswith("vampire_"))

    def test_resolves_hostname_to_device(self):
        config = vampire.VampireConfig(
            enabled=True,
            user="u",
            node_device_map={"compute-0-4": "hpm4"},
        )

        self.assertEqual(vampire.resolve_vampire_device(config, "compute-0-4"), "hpm4")
        self.assertEqual(vampire.resolve_vampire_device(config, "compute-0-4.cluster.local"), "hpm4")
        with self.assertRaises(vampire.ExternalMeasurementError) as ctx:
            vampire.resolve_vampire_device(config, "unknown-node")
        self.assertIn("unknown-node", str(ctx.exception))
        self.assertIn("compute-0-4", str(ctx.exception))

    def test_wait_for_json_file_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"

            start = time.monotonic()
            with self.assertRaises(vampire.ExternalMeasurementError):
                vampire.wait_for_json_file(
                    path,
                    timeout_s=0.02,
                    phase="wait_missing",
                    run_id="run",
                    poll_interval_s=0.001,
                )
            self.assertLess(time.monotonic() - start, 1)

    def test_atomic_helpers_work_without_time_ns(self):
        original_time_ns = getattr(vampire.time, "time_ns", None)
        if original_time_ns is None:
            self.skipTest("runtime already lacks time.time_ns")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ready"
            delattr(vampire.time, "time_ns")
            try:
                vampire.atomic_touch(path)
            finally:
                setattr(vampire.time, "time_ns", original_time_ns)

            self.assertTrue(path.exists())

    def test_start_success_benchmark_failure_still_stops_vampire(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_dir = root / "sync" / "run"
            vampire.atomic_write_json(
                sync_dir / "job_started.json",
                {
                    "run_id": "run",
                    "slurm_job_id": "123",
                    "hostname": "node-a",
                    "timestamp_ns": 0,
                },
            )
            calls = []

            def fake_command(config, action, experiment_id=None, device=None, output_path=None, log_path=None):
                calls.append(action)
                if action == "start":
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        stdout='{"start_time":"1970-01-01T00:00:00+00:00"}\n',
                        stderr="",
                    )
                if action == "stop":
                    return subprocess.CompletedProcess(
                        [],
                        0,
                        stdout='{"stop_time":"1970-01-01T00:00:05+00:00"}\n',
                        stderr="",
                    )
                raise AssertionError(action)

            old_command = vampire.run_vampire_command
            vampire.run_vampire_command = fake_command
            try:
                config = vampire.VampireConfig(
                    enabled=True,
                    user="u",
                    node_device_map={"node-a": "hpm4"},
                    sync_timeout_s=0.05,
                    failure_policy="fail_run",
                )
                result = vampire.run_vampire_measurement(
                    config,
                    "run",
                    "123",
                    sync_dir,
                    root / "samples" / "run.csv",
                    root / "logs" / "run.log",
                    is_job_active=lambda: False,
                )
            finally:
                vampire.run_vampire_command = old_command

        self.assertEqual(calls, ["start", "stop"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["fail_run"])

    def test_processes_power_csv_with_trapezoidal_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vampire.csv"
            csv_path.write_text("Time,hpm4\n0,10\n1,10\n2,20\n3,20\n4,30\n")

            metrics = vampire.process_vampire_csv(
                csv_path,
                "hpm4",
                benchmark_start_ns=1_000_000_000,
                benchmark_end_ns=3_000_000_000,
                experiment_start_ns=0,
            )

        self.assertEqual(metrics["samples_count"], 3)
        self.assertEqual(metrics["duration_s"], 2.0)
        self.assertAlmostEqual(metrics["energy_j"], 35.0)
        self.assertAlmostEqual(metrics["avg_power_w"], 17.5)
        self.assertAlmostEqual(metrics["peak_power_w"], 20.0)

    def test_converts_kwh_energy_to_joules(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vampire.csv"
            csv_path.write_text("Time,energy_kWh\n0,0.000\n2,0.001\n")

            metrics = vampire.process_vampire_csv(
                csv_path,
                "hpm4",
                benchmark_start_ns=0,
                benchmark_end_ns=2_000_000_000,
                experiment_start_ns=0,
            )

        self.assertEqual(metrics["samples_count"], 2)
        self.assertAlmostEqual(metrics["energy_j"], 3600.0)
        self.assertAlmostEqual(metrics["avg_power_w"], 1800.0)

    def test_rejects_empty_or_invalid_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.csv"
            empty.write_text("")
            invalid = Path(tmp) / "invalid.csv"
            invalid.write_text("Time,other\n0,1\n")

            with self.assertRaises(vampire.ExternalMeasurementError):
                vampire.process_vampire_csv(empty, "hpm4", 0, 1_000_000_000, 0)
            with self.assertRaises(vampire.ExternalMeasurementError):
                vampire.process_vampire_csv(invalid, "hpm4", 0, 1_000_000_000, 0)

    def test_rejects_csv_without_samples_inside_benchmark_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vampire.csv"
            csv_path.write_text("Time,hpm4\n0,10\n1,10\n")

            with self.assertRaises(vampire.ExternalMeasurementError) as ctx:
                vampire.process_vampire_csv(
                    csv_path,
                    "hpm4",
                    benchmark_start_ns=3_000_000_000,
                    benchmark_end_ns=4_000_000_000,
                    experiment_start_ns=0,
                )

        self.assertIn("coverage=0.00%", str(ctx.exception))

    def test_rejects_partial_tail_instead_of_dividing_by_full_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vampire.csv"
            csv_path.write_text("Time,hpm4\n0,80\n30,80\n60,80\n90,80\n")

            with self.assertRaises(vampire.ExternalMeasurementError) as ctx:
                vampire.process_vampire_csv(
                    csv_path,
                    "hpm4",
                    benchmark_start_ns=30_000_000_000,
                    benchmark_end_ns=150_000_000_000,
                    experiment_start_ns=0,
                )

        message = str(ctx.exception)
        self.assertIn("no cubre toda la ventana", message)
        self.assertIn("missing_tail_s=60.000000", message)
        self.assertIn("coverage=50.00%", message)

    def test_energy_counter_is_interpolated_at_benchmark_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "vampire.csv"
            csv_path.write_text("Time,energy_kWh\n0,0.000\n10,0.010\n20,0.020\n")

            metrics = vampire.process_vampire_csv(
                csv_path,
                "hpm4",
                benchmark_start_ns=5_000_000_000,
                benchmark_end_ns=15_000_000_000,
                experiment_start_ns=0,
            )

        self.assertAlmostEqual(metrics["energy_j"], 36_000.0)

    def test_retries_download_until_influx_exposes_complete_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "vampire.csv"
            log_path = root / "vampire.log"
            calls = []

            def fake_command(config, action, experiment_id=None, device=None, output_path=None, log_path=None):
                calls.append(action)
                if len(calls) == 1:
                    output_path.write_text("Time,hpm4\n0,80\n30,80\n90,80\n140,80\n")
                else:
                    output_path.write_text("Time,hpm4\n0,80\n30,80\n90,80\n150,80\n160,80\n")
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")

            old_command = vampire.run_vampire_command
            old_sleep = vampire.time.sleep
            vampire.run_vampire_command = fake_command
            vampire.time.sleep = lambda _seconds: None
            try:
                metrics = vampire.download_and_process_vampire_csv(
                    config=vampire.VampireConfig(
                        enabled=True,
                        data_ready_timeout_s=1,
                        data_ready_poll_s=0.1,
                    ),
                    experiment_id="experiment",
                    device="hpm4",
                    csv_path=csv_path,
                    log_path=log_path,
                    benchmark_start_ns=30_000_000_000,
                    benchmark_end_ns=150_000_000_000,
                    experiment_start_ns=0,
                )
            finally:
                vampire.run_vampire_command = old_command
                vampire.time.sleep = old_sleep

            log_text = log_path.read_text()

        self.assertEqual(calls, ["get", "get"])
        self.assertAlmostEqual(metrics["energy_j"], 9600.0)
        self.assertIn("[get retry]", log_text)

    def test_continue_internal_policy_releases_slurm_job_on_start_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_dir = root / "sync" / "run"
            vampire.atomic_write_json(
                sync_dir / "job_started.json",
                {
                    "run_id": "run",
                    "slurm_job_id": "123",
                    "hostname": "node-a",
                    "timestamp_ns": 0,
                },
            )

            def fake_command(config, action, experiment_id=None, device=None, output_path=None, log_path=None):
                raise vampire.ExternalMeasurementError(action, "boom")

            old_command = vampire.run_vampire_command
            vampire.run_vampire_command = fake_command
            try:
                config = vampire.VampireConfig(
                    enabled=True,
                    user="u",
                    node_device_map={"node-a": "hpm4"},
                    failure_policy="continue_internal",
                )
                result = vampire.run_vampire_measurement(
                    config,
                    "run",
                    "123",
                    sync_dir,
                    root / "samples" / "run.csv",
                    root / "logs" / "run.log",
                    is_job_active=lambda: True,
                )
            finally:
                vampire.run_vampire_command = old_command

            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["fail_run"])
            self.assertTrue((sync_dir / "vampire_ready").exists())
            self.assertFalse((sync_dir / "vampire_abort.json").exists())

    def test_fail_run_policy_writes_abort_on_start_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_dir = root / "sync" / "run"
            vampire.atomic_write_json(
                sync_dir / "job_started.json",
                {
                    "run_id": "run",
                    "slurm_job_id": "123",
                    "hostname": "node-a",
                    "timestamp_ns": 0,
                },
            )

            def fake_command(config, action, experiment_id=None, device=None, output_path=None, log_path=None):
                raise vampire.ExternalMeasurementError(action, "boom")

            old_command = vampire.run_vampire_command
            vampire.run_vampire_command = fake_command
            try:
                config = vampire.VampireConfig(
                    enabled=True,
                    user="u",
                    node_device_map={"node-a": "hpm4"},
                    failure_policy="fail_run",
                )
                result = vampire.run_vampire_measurement(
                    config,
                    "run",
                    "123",
                    sync_dir,
                    root / "samples" / "run.csv",
                    root / "logs" / "run.log",
                    is_job_active=lambda: True,
                )
            finally:
                vampire.run_vampire_command = old_command

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["fail_run"])
            self.assertTrue((sync_dir / "vampire_abort.json").exists())
            self.assertFalse((sync_dir / "vampire_ready").exists())


if __name__ == "__main__":
    unittest.main()
