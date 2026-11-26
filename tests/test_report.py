import json
import tempfile
import unittest
from pathlib import Path

from replay import collect, report

from tests.test_controller import make_plan


def ts(sec: int) -> str:
    sec += 3600
    return f"2026-12-01T{10 + sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}Z"


def job(job_id, name, start, end, steps, conclusion="success", created=None):
    return {
        "job": job_id, "name": name, "conclusion": conclusion, "status": "completed",
        "created_at": ts(created if created is not None else start - 5), "started_at": ts(start), "completed_at": ts(end),
        "steps": [{"name": n, "started_at": ts(a), "completed_at": ts(b), "conclusion": "success"} for n, a, b in steps],
        "log": {"http_429": 0, "scan_urls": [f"https://scans.gradle.com/s/{job_id}{start}"]},
    }


def attempt_record(variant, event, run_id, attempt, jobs, conclusion="success"):
    return {
        "variant": variant, "event": event, "run_id": run_id, "attempt": attempt, "jobs": jobs, "failures": [],
        "run": {"conclusion": conclusion},
        "telemetry": {
            "build": {"job": {"cacheOutputs": {"dependency-gc-due": "true"} if variant == "mammoth" else {}},
                      "deps": {"begin": {"files": 10, "bytes": 1000}, "end": {"files": 13, "bytes": 1600}},
                      "invocations": [{"outcomes": {"from-cache": 40, "executed": 2}}, {"outcomes": {"up-to-date": 5}}]},
        },
    }


class TestReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name)
        self.plan = make_plan()
        self.plan["settings"]["warmup_events"] = 1
        self.plan["settings"]["cache_steps"] = {"reference": ["Prepare cache", "Save cache"], "mammoth": ["Mammoth cache"]}
        self.plan["settings"]["representative_events"] = ["scored/main-01"]

        def write(variant, event, run_id, attempt, jobs, conclusion="success"):
            collect.write_atomic(collect.run_path(self.data, variant, run_id, attempt), attempt_record(variant, event, run_id, attempt, jobs, conclusion))

        # scored/seed is warm-up; scored/main-01 is the scored pair; scored/main-02 needed a rerun in mammoth
        for event, base in (("scored/seed", 0), ("scored/main-01", 1000), ("scored/main-02", 2000)):
            write("reference", event, base + 1, 1, [
                job("build", "CI Build", 0, 600, [("Prepare cache", 0, 60), ("Gradle", 60, 550), ("Replay telemetry / end", 550, 580), ("Save cache", 580, 590), ("Post Prepare cache", 590, 600)]),
                job("store-cache", "CI Store Cache", 610, 760, [("Prepare cache", 610, 700), ("Replay telemetry / end", 700, 710)]),
            ])
        for event, base in (("scored/seed", 0), ("scored/main-01", 1000)):
            write("mammoth", event, base + 2, 1, [
                job("build", "CI Build", 0, 500, [("Mammoth cache", 0, 20), ("Gradle", 20, 470), ("Replay telemetry / end", 470, 480), ("Post Mammoth cache", 480, 500)]),
                job("store-cache", "CI Store Cache", 510, 590, [("Mammoth cache", 510, 570), ("Replay telemetry / end", 570, 580)]),
            ])
        write("mammoth", "scored/main-02", 2002, 1, [job("build", "CI Build", 0, 300, [], conclusion="failure")], conclusion="failure")
        write("mammoth", "scored/main-02", 2002, 2, [job("build", "CI Build", 400, 900, [])])

    def tearDown(self):
        self._tmp.cleanup()

    def test_rows(self):
        jobs, runs = report.build_rows(self.plan, self.data)
        r = {(x["event"], x["variant"]): x for x in runs}
        ref = r[("scored/main-01", "reference")]
        mam = r[("scored/main-01", "mammoth")]
        # build: 600 - 30 telemetry = 570; store-cache: 150 - 10 = 140
        self.assertEqual(ref["total_exec_s"], 710)
        self.assertEqual(ref["critical_path_s"], 710)
        # cache: prepare 60 + save 10 + post 10 in build, plus the aggregator's net 140
        self.assertEqual(ref["cache_s"], 220)
        self.assertEqual(ref["measurement_s"], 40)
        self.assertEqual((mam["total_exec_s"], mam["cache_s"]), (490 + 70, 20 + 20 + 70))
        self.assertEqual(mam["gc_due_jobs"], 1)
        self.assertEqual(mam["tasks"], {"from-cache": 40, "executed": 2, "up-to-date": 5})
        self.assertEqual(mam["dep_files_added"], 3)
        self.assertEqual(r[("scored/seed", "reference")]["flags"], ["warm-up"])
        self.assertEqual(ref["flags"], [])
        rerun = r[("scored/main-02", "mammoth")]
        self.assertEqual((rerun["attempts"], rerun["flags"], rerun["total_exec_s"], rerun["final_conclusion"]), (2, ["rerun"], None, "success"))
        self.assertEqual(r[("scored/main-02", "reference")]["total_exec_s"], 710)
        missing = r[("smoke/seed", "reference")]
        self.assertEqual(missing["missing"], "not collected")
        build = next(j for j in jobs if j["event"] == "scored/main-01" and j["variant"] == "reference" and j["job"] == "build")
        self.assertEqual((build["queue_s"], build["net_s"]), (5, 570))

    def test_summary_and_files(self):
        out = self.data / "report"
        summary = report.write_report(self.plan, self.data, out)
        main = summary["groups"]["main"]["total_exec_s"]
        # only scored/main-01 is unflagged in both; reference also has scored/main-02
        self.assertEqual(main["mammoth"], {"n": 1, "median": 560})
        self.assertEqual(main["reference"], {"n": 2, "median": 710})
        page = (out / "index.html").read_text()
        self.assertIn("scored/main-01", page)
        self.assertIn("https://scans.gradle.com/s/build0", page)  # representative scan links
        self.assertTrue((out / "runs.csv").read_text().startswith("event,"))
        self.assertTrue(json.loads((out / "summary.json").read_text())["groups"])


if __name__ == "__main__":
    unittest.main()
