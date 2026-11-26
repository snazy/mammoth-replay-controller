"""Runs the telemetry composite actions' shell steps locally (expressions substituted)."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ACTIONS = Path(__file__).resolve().parent.parent / "overlays/nessie/common/files/.github/actions"


def run_steps(action: str, home: Path, temp: Path, expressions: dict[str, str], env: dict[str, str]) -> None:
    spec = yaml.safe_load((ACTIONS / action / "action.yml").read_text())
    for step in spec["runs"]["steps"]:
        if "run" not in step:
            continue
        script = step["run"]
        for expr, value in {"github.action_path": str(ACTIONS / action), **expressions}.items():
            script = script.replace("${{ " + expr + " }}", value)
        step_env = {k: v for k, v in (step.get("env") or {}).items()}
        for k, v in step_env.items():
            for expr, value in expressions.items():
                v = v.replace("${{ " + expr + " }}", value)
            step_env[k] = v
        subprocess.run(["bash", "-e", "-c", script], check=True, env={"PATH": os.environ["PATH"], "HOME": str(home), "RUNNER_TEMP": str(temp), **env, **step_env})


@unittest.skipUnless(yaml is not None, "needs PyYAML")
class TestTelemetryActions(unittest.TestCase):
    def test_begin_and_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, temp = Path(tmp) / "home", Path(tmp) / "temp"
            files = home / ".gradle/caches/modules-2/files-2.1/g/a"
            files.mkdir(parents=True)
            (files / "a.jar").write_bytes(b"x" * 10)
            temp.mkdir()
            github_env = Path(tmp) / "github_env"
            env = {"GITHUB_ENV": str(github_env), "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_EVENT_NAME": "push", "RUNNER_OS": "Linux"}
            run_steps("replay-telemetry-begin", home, temp, {}, env)
            self.assertTrue((home / ".gradle/init.d/zz-replay-telemetry.init.gradle").exists())
            self.assertIn(f"REPLAY_TELEMETRY_DIR={temp}/replay-telemetry", github_env.read_text())
            (files / "b.jar").write_bytes(b"y" * 20)
            run_steps("replay-telemetry-end", home, temp, {"inputs.job": "code-checks", "inputs.cache-outputs": '{"dependency-gc-due": "true"}'}, env)
            out = temp / "replay-telemetry"
            begin = json.loads((out / "deps-begin.json").read_text())
            end = json.loads((out / "deps-end.json").read_text())
            self.assertEqual((begin["files"], end["files"]), (1, 2))
            self.assertEqual(end["bytes"] - begin["bytes"], 20)
            job = json.loads((out / "job.json").read_text())
            self.assertEqual((job["job"], job["runAttempt"], job["cacheOutputs"]), ("code-checks", "2", {"dependency-gc-due": "true"}))

    def test_end_tolerates_reference_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, temp = Path(tmp) / "home", Path(tmp) / "temp"
            home.mkdir()
            temp.mkdir()
            env = {"GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "1"}
            # the incumbent's composite prepare step has no outputs: toJSON yields '{}'; a missing step yields 'null'
            for outputs in ("{}", "null"):
                run_steps("replay-telemetry-end", home, temp, {"inputs.job": "test", "inputs.cache-outputs": outputs}, env)
                job = json.loads((temp / "replay-telemetry/job.json").read_text())
                self.assertEqual(job["cacheOutputs"], {})
                self.assertEqual(json.loads((temp / "replay-telemetry/deps-end.json").read_text())["files"], 0)


if __name__ == "__main__":
    unittest.main()
