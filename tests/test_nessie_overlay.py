"""Structural checks of the generated Nessie overlays.

Needs a Nessie checkout (NESSIE_CHECKOUT, default: sibling ../../nessie) and PyYAML; skipped otherwise."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from replay import build

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
NESSIE = Path(os.environ.get("NESSIE_CHECKOUT", ROOT.parent.parent / "nessie"))
PROFILE = json.loads((ROOT / "profiles" / "nessie.json").read_text())
SEED = PROFILE["campaigns"][0]["events"][0]["upstream"]
MAMMOTH = "buildish-tooling/buildish-mammoth-cache/actions/github/gradle@5befea0047cb7f46d468765ffd8061475941c3dc"
INCUMBENT = ("./.github/actions/ci-incr-build-cache-prepare", "./.github/actions/ci-incr-build-cache-save")


def is_cache_step(step: dict) -> bool:
    uses = step.get("uses", "")
    return uses.startswith(MAMMOTH) or uses in INCUMBENT


@unittest.skipUnless(yaml is not None and (NESSIE / ".git").exists(), "needs PyYAML and a Nessie checkout")
class TestNessieOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.ci = {}
        cls.dirs = {}
        for variant, cfg in PROFILE["variants"].items():
            dest = Path(cls._tmp.name) / variant
            dest.mkdir()
            layers = [ROOT / "overlays" / n for n in [*PROFILE["overlay"]["common_layers"], *cfg["overlay_layers"]]]
            build.materialize_overlay(NESSIE, SEED, PROFILE["overlay"]["keep"], layers, dest)
            cls.dirs[variant] = dest
            cls.ci[variant] = yaml.safe_load((dest / ".github/workflows/ci.yml").read_text())

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def jobs(self, variant):
        return self.ci[variant]["jobs"]

    def test_job_sets(self):
        jobs = PROFILE["jobs"]
        expected = set(jobs["producers"]) | {jobs["aggregator"]} | set(jobs["other_measured"])
        for v in PROFILE["variants"]:
            self.assertEqual(set(self.jobs(v)), expected, v)

    def test_triggers_unchanged(self):
        self.assertEqual(self.ci["reference"][True], self.ci["mammoth"][True])  # YAML 'on' key
        self.assertEqual(self.ci["reference"]["concurrency"], self.ci["mammoth"]["concurrency"])

    def test_non_cache_steps_identical(self):
        for name, ref_job in self.jobs("reference").items():
            mam_job = self.jobs("mammoth")[name]
            ref_steps = [s for s in ref_job["steps"] if not is_cache_step(s)]
            mam_steps = [s for s in mam_job["steps"] if not is_cache_step(s)]
            self.assertEqual(ref_steps, mam_steps, name)
            for key in set(ref_job) | set(mam_job):
                # the aggregator's condition differs by design (see test_mammoth_roles)
                if key != "steps" and (name, key) != (PROFILE["jobs"]["aggregator"], "if"):
                    self.assertEqual(ref_job.get(key), mam_job.get(key), f"{name}.{key}")

    def test_telemetry_around_cache_steps(self):
        for v in PROFILE["variants"]:
            for name, job in self.jobs(v).items():
                steps = job["steps"]
                idx = [i for i, s in enumerate(steps) if s.get("id") == "gradle-cache"]
                if name == "determine-jobs":
                    self.assertEqual(idx, [])
                    continue
                self.assertEqual(len(idx), 1, f"{v}/{name}")
                self.assertTrue(is_cache_step(steps[idx[0]]), f"{v}/{name}")
                self.assertEqual(steps[idx[0] + 1]["uses"], "./.github/actions/replay-telemetry-begin", f"{v}/{name}")
                ends = [s for s in steps if s.get("uses") == "./.github/actions/replay-telemetry-end"]
                self.assertEqual(len(ends), 1, f"{v}/{name}")
                self.assertEqual(ends[0]["if"], "always()")
                self.assertEqual(ends[0]["with"]["job"], name)
                # end runs after every build step (only incumbent save may follow)
                tail = steps[steps.index(ends[0]) + 1 :]
                self.assertTrue(all(s.get("uses") == INCUMBENT[1] for s in tail), f"{v}/{name}: {tail}")

    def test_mammoth_roles(self):
        jobs = PROFILE["jobs"]
        for name, job in self.jobs("mammoth").items():
            cache = [s for s in job["steps"] if is_cache_step(s)]
            if name == "determine-jobs":
                self.assertEqual(cache, [])
                continue
            self.assertEqual(len(cache), 1, name)
            step = cache[0]
            self.assertTrue(step["uses"].startswith(MAMMOTH))
            w = step["with"]
            self.assertEqual(w["config-file"], ".github/mammoth-gradle.yml")
            # all other settings come from the config file only (Mammoth >= 0.6.0 applies them)
            self.assertNotIn("cache-gc-older-than-days", w, name)
            if name in jobs["producers"]:
                self.assertEqual(w["job-mode"], "distributed-worker")
            elif name == jobs["aggregator"]:
                self.assertEqual(w["job-mode"], "distributed-aggregator")
                self.assertEqual([j.strip() for j in w["dependent-jobs"].split(",")], jobs["producers"])
                self.assertEqual(set(job["needs"]), set(jobs["producers"]))
                # Mammoth also stores after failed builds; the reference keeps the incumbent's condition
                self.assertEqual(job["if"], "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'")
                self.assertEqual(self.jobs("reference")[name]["if"], "github.event_name == 'push' && github.ref == 'refs/heads/main'")
            else:
                self.assertEqual(w["job-mode"], "standalone")
                self.assertEqual(w["read-only"], "true")

    def test_mammoth_config(self):
        cfg = yaml.safe_load((self.dirs["mammoth"] / ".github/mammoth-gradle.yml").read_text())
        self.assertEqual(cfg["cache-partitions"], [{"id": "dependency-metadata", "writer": "code-checks"}])
        self.assertEqual(cfg["worker-artifact-retention-days"], 1)
        self.assertEqual(cfg["cache-gc-older-than-days"], 2)
        self.assertEqual(cfg["dependency-gc-interval-days"], 1)

    def test_reference_keeps_incumbent(self):
        jobs = PROFILE["jobs"]
        for name in jobs["producers"]:
            uses = [s.get("uses") for s in self.jobs("reference")[name]["steps"]]
            self.assertIn(INCUMBENT[0], uses, name)
            self.assertIn(INCUMBENT[1], uses, name)
        self.assertTrue((self.dirs["reference"] / ".github/actions/ci-incr-build-cache-prepare/action.yml").exists())
        self.assertFalse((self.dirs["mammoth"] / ".github/actions/ci-incr-build-cache-prepare").exists())

    def test_no_secrets_or_deploys_reachable(self):
        for v in PROFILE["variants"]:
            for name, job in self.jobs(v).items():
                for s in job["steps"]:
                    text = json.dumps(s)
                    if "secrets." in text:
                        self.assertIn("github.repository == 'projectnessie/nessie'", s.get("if", ""), f"{v}/{name}: {s.get('name')}")


if __name__ == "__main__":
    unittest.main()
