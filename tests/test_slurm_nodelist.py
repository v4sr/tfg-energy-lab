import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import run_experiment
import run_matrix
from vampire_integration import VampireConfig


class SlurmNodelistTests(unittest.TestCase):
    def test_resolve_nodelist_from_single_vampire_node(self):
        config = {}
        external_config = VampireConfig(
            enabled=True,
            user="u",
            node_device_map={"compute-0-4": "hpm4"},
        )

        self.assertEqual(run_matrix.resolve_nodelist(config, external_config), "compute-0-4")

    def test_explicit_nodelist_wins(self):
        config = {"nodelist": "compute-0-7"}
        external_config = VampireConfig(
            enabled=True,
            user="u",
            node_device_map={"compute-0-4": "hpm4"},
        )

        self.assertEqual(run_matrix.resolve_nodelist(config, external_config), "compute-0-7")

    def test_rendered_slurm_script_contains_nodelist_directive(self):
        original_generated = run_experiment.SLURM_GENERATED_DIR
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_experiment.SLURM_GENERATED_DIR = root / "slurm"
            dirs = {
                "logs_dir": root / "logs",
                "samples_rapl_dir": root / "samples" / "rapl",
                "samples_external_dir": root / "samples" / "external",
                "samples_vampire_dir": root / "samples" / "vampire",
                "logs_vampire_dir": root / "logs" / "vampire",
                "sync_root_dir": root / "sync",
            }
            for path in dirs.values():
                path.mkdir(parents=True, exist_ok=True)
            run_experiment.SLURM_GENERATED_DIR.mkdir(parents=True, exist_ok=True)

            args = SimpleNamespace(
                label="test",
                partition="guest",
                threads=1,
                nodelist="compute-0-4",
                time_limit="00:01:00",
                workdir=".",
                command="sleep 1",
                language="c",
                benchmark="idle",
                rep=1,
                campaign="test",
                pushgateway_url="",
                rapl_interval=1.0,
            )

            try:
                script_path, *_ = run_experiment.render_slurm_script(args, "run1", dirs)
            finally:
                run_experiment.SLURM_GENERATED_DIR = original_generated

            script = script_path.read_text()

        self.assertIn("#SBATCH --nodelist=compute-0-4", script)
        self.assertIn("REQUESTED_NODELIST=compute-0-4", script)


if __name__ == "__main__":
    unittest.main()
