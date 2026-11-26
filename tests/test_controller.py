import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from replay import classify, collect, controller
from replay.decide import Env

from tests.fake_github import FakeGitHub

VARIANTS = {"reference": "me/ref", "mammoth": "me/mam"}
T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
SPACING = 300


def sha(variant, name):
    return f"{variant[0]}{name}".ljust(40, "0")


def make_plan():
    def ev(campaign, eid, typ, name=None, **kw):
        e = {"campaign": campaign, "id": eid, "type": typ, **kw}
        if name:
            e["upstream"] = f"u{name}".ljust(40, "0")
            e["sha"] = {v: sha(v, name) for v in VARIANTS}
        if typ == "pr":
            e["branch"] = f"replay/{campaign}/{eid}"
            e["title"] = f"Title {eid}"
        return e

    events = [
        ev("smoke", "seed", "seed", "s0"),
        ev("smoke", "main-01", "main", "s1", rerun_job="build"),
        ev("smoke", "pr-02", "pr", "s2", push_after=False),
        {"campaign": "scored", "id": "clear-caches", "type": "gate", "action": "delete-caches-and-artifacts"},
        ev("scored", "seed", "seed", "c0"),
        ev("scored", "pr-01", "pr", "c1", push_after=True),
        ev("scored", "main-01", "main", "c1"),
        ev("scored", "main-02", "main", "c2"),
    ]
    return {
        "schema": 1,
        "profile": "test",
        "variants": {v: {"repo": r, "overlay_tree": "t"} for v, r in VARIANTS.items()},
        "settings": {"max_reruns": 2, "min_event_spacing_minutes": SPACING, "run_visibility_timeout_minutes": 30,
                     "run_max_duration_minutes": 360, "on_unknown_failure": "pause",
                     "failure_patterns": {"flaky_tests": [r"FlakyTest"]}},
        "workflow": {"file": "ci.yml", "name": "CI", "jobs": {"build": {"name": "CI Build", "needs": []}, "store-cache": {"name": "CI Store Cache", "needs": ["build"]}}},
        "jobs": {"producers": ["build"], "aggregator": "store-cache"},
        "events": events,
        "dropped": [],
        "staging_refs": {v: {"refs/tags/replay-staging/main": sha(v, "c2"), "refs/tags/replay-staging/smoke-pr-02": sha(v, "s2")} for v in VARIANTS},
    }


class Harness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data = Path(self._tmp.name) / "data"
        self.now = T0
        self.plan = make_plan()
        self.gh = FakeGitHub(lambda: self.now)
        for v, repo in VARIANTS.items():
            for ref, s in self.plan["staging_refs"][v].items():
                self.gh.repo(repo).refs[ref] = s
        self.released = set()
        self.paused = False

    def tearDown(self):
        self._tmp.cleanup()

    def tick(self):
        env = Env(now=self.now, paused=self.paused, released_gates=frozenset(self.released))
        return controller.tick(self.plan, self.gh, self.data, env)

    def active_runs(self):
        return [(repo, r) for repo in VARIANTS.values() for r in self.gh.repo(repo).runs if r["status"] != "completed"]

    def finish_all(self, conclusion="success", **kw):
        for repo, r in self.active_runs():
            self.gh.finish_run(repo, r["id"], conclusion, **kw)

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)

    def mutations(self, method):
        return [c for c in self.gh.calls if c[0] == method]

    def main(self, variant):
        return self.gh.repo(VARIANTS[variant]).refs.get("refs/heads/main")


class TestHappyPath(Harness):
    def test_full_replay(self):
        trace = []
        for _ in range(400):
            d, notes = self.tick()
            trace.append((self.now, d.current, list(notes)))
            if d.finished:
                break
            if d.current == "scored/clear-caches" and not self.released:
                self.released.add("scored/clear-caches")
            self.advance(15)
            if self.active_runs():
                self.advance(45)
                self.finish_all()
        self.assertTrue(d.finished, trace[-3:])
        for v in VARIANTS:
            self.assertEqual(self.main(v), sha(v, "c2"))
            prs = self.gh.repo(VARIANTS[v]).prs
            self.assertEqual([(p["head_ref"], p["state"]) for p in prs], [("replay/smoke/pr-02", "closed"), ("replay/scored/pr-01", "closed")])
            self.assertIn("<!-- replay-event: scored/pr-01 -->", prs[1]["body"])
        # one rerun of the smoke job, no failed-job reruns
        self.assertEqual(len(self.mutations("rerun_job")), 2)
        self.assertEqual(self.mutations("rerun_failed_jobs"), [])
        # every completed attempt was collected
        files = sorted((self.data / "runs").glob("*/*.json"))
        attempts = sum(r["run_attempt"] for repo in VARIANTS.values() for r in self.gh.repo(repo).runs)
        self.assertEqual(len(files), attempts)
        rec = json.loads(files[0].read_text())
        self.assertEqual(rec["telemetry"]["build"]["invocations"][0]["outcomes"], {"from-cache": 1})
        # the gate deleted smoke caches/artifacts only after collection, before the scored seed
        deletes = [i for i, c in enumerate(self.gh.calls) if c[0] in ("delete_cache", "delete_artifact")]
        scored_seed = [i for i, c in enumerate(self.gh.calls) if c[0] == "update_ref" and c[3] in (sha("reference", "c0"), sha("mammoth", "c0"))]
        self.assertTrue(deletes and scored_seed and max(deletes) < min(scored_seed))

    def test_spacing_and_barrier(self):
        self.tick()  # creates main for the seed in both variants
        self.assertEqual({v: self.main(v) for v in VARIANTS}, {v: sha(v, "s0") for v in VARIANTS})
        # one variant finishes, the other is still running: no next event
        ref_run = self.gh.repo("me/ref").runs[0]
        self.advance(30)
        self.gh.finish_run("me/ref", ref_run["id"])
        self.tick()
        self.assertEqual(self.main("reference"), sha("reference", "s0"))
        self.advance(30)
        self.finish_all()
        d, _ = self.tick()
        self.assertEqual(d.current, "smoke/main-01")
        self.assertEqual(d.not_before, self.now + timedelta(minutes=SPACING))
        self.assertEqual(self.main("mammoth"), sha("mammoth", "s0"))
        self.advance(SPACING - 1)
        self.tick()
        self.assertEqual(self.main("mammoth"), sha("mammoth", "s0"))
        self.advance(2)
        self.tick()
        self.assertEqual({v: self.main(v) for v in VARIANTS}, {v: sha(v, "s1") for v in VARIANTS})
        status = json.loads((self.data / "status.json").read_text())
        self.assertEqual(status["current"], "smoke/main-01")


class TestFailures(Harness):
    def start_seed(self):
        self.tick()
        self.advance(20)

    def test_maven_429_is_rerun_up_to_limit(self):
        self.start_seed()
        log = "2026-01-01T00:00:00Z Could not GET 'https://repo.maven.apache.org/x.pom'. Received status code 429 from server: Too Many Requests\n> Task :x:compileJava FAILED\n"
        for attempt in (1, 2, 3):
            self.gh.finish_run("me/ref", self.gh.repo("me/ref").runs[0]["id"], "failure", failed_job_log=log)
            if attempt == 1:
                self.gh.finish_run("me/mam", self.gh.repo("me/mam").runs[0]["id"])
            d, _ = self.tick()
            self.advance(20)
        self.assertEqual(len(self.mutations("rerun_failed_jobs")), 2)
        self.assertTrue(any("rerun limit reached" in x for x in d.exceptions), d.exceptions)
        rec = json.loads(sorted((self.data / "runs" / "reference").glob("*-a1.json"))[0].read_text())
        self.assertEqual(rec["failures"][0]["class"], "maven-central-429")
        self.assertEqual(rec["jobs"][0]["log"]["http_429_maven_central"], 1)

    def test_unknown_failure_pauses_until_manual_rerun(self):
        self.start_seed()
        self.gh.finish_run("me/ref", self.gh.repo("me/ref").runs[0]["id"], "failure", failed_job_log="Z AssertionError\n> Task :x:test FAILED\n")
        self.gh.finish_run("me/mam", self.gh.repo("me/mam").runs[0]["id"])
        d, notes = self.tick()
        self.assertEqual(self.mutations("rerun_failed_jobs"), [])
        self.assertTrue(any("unclassified failures" in x for x in d.exceptions))
        self.assertIn("exceptions present; no mutations", notes)
        # the user reruns manually; the controller continues
        self.gh.rerun_failed_jobs("me/ref", self.gh.repo("me/ref").runs[0]["id"])
        d, _ = self.tick()
        self.assertEqual(d.exceptions, [])
        self.gh.finish_run("me/ref", self.gh.repo("me/ref").runs[0]["id"])
        d, _ = self.tick()
        self.assertEqual(d.current, "smoke/main-01")

    def test_flaky_test_is_rerun(self):
        self.start_seed()
        self.gh.finish_run("me/ref", self.gh.repo("me/ref").runs[0]["id"], "failure", failed_job_log="Z FlakyTest > testIt() FAILED\n")
        self.tick()
        self.assertEqual(len(self.mutations("rerun_failed_jobs")), 1)

    def test_startup_failure_is_an_exception(self):
        self.start_seed()
        self.gh.finish_run("me/ref", self.gh.repo("me/ref").runs[0]["id"], "startup_failure")
        d, _ = self.tick()
        self.assertTrue(any("concluded startup_failure" in x for x in d.exceptions))

    def test_no_run_appears(self):
        self.start_seed()
        self.gh.repo("me/mam").runs.clear()
        self.advance(5)
        d, _ = self.tick()
        self.assertEqual(d.exceptions, [])
        self.advance(30)
        d, _ = self.tick()
        self.assertTrue(any("no CI run for smoke/seed" in x for x in d.exceptions))

    def test_duplicate_runs(self):
        self.start_seed()
        self.gh.start_run("me/ref", "push", sha("reference", "s0"), "main")
        d, _ = self.tick()
        self.assertTrue(any("2 runs for smoke/seed" in x for x in d.exceptions))

    def test_unexpected_main(self):
        self.start_seed()
        self.gh.repo("me/mam").refs["refs/heads/main"] = "f" * 40
        d, _ = self.tick()
        self.assertTrue(any("not a plan commit" in x for x in d.exceptions))

    def test_missing_staging(self):
        del self.gh.repo("me/ref").refs["refs/tags/replay-staging/main"]
        d, _ = self.tick()
        self.assertTrue(any("staging refs" in x for x in d.exceptions))
        self.assertEqual(self.mutations("create_ref"), [])

    def test_paused_collects_but_does_not_mutate(self):
        self.start_seed()
        self.finish_all()
        self.paused = True
        self.advance(SPACING + 1)
        _, notes = self.tick()
        self.assertEqual(len(self.mutations("update_ref")), 0)
        self.assertEqual(len(list((self.data / "runs").glob("*/*.json"))), 2)
        self.assertTrue(any(n.startswith("paused") for n in notes))


class TestReconciliation(Harness):
    def advance_to_pr(self):
        # seed -> main-01 (with job rerun) -> pr-02
        for _ in range(40):
            d, _ = self.tick()
            if self.mutations("create_pr"):
                return d
            self.advance(SPACING + 1)
            self.finish_all()
        self.fail("did not reach the PR event")

    def test_lost_create_pr_response(self):
        self.gh.lose_next.add("create_pr")
        self.advance_to_pr()
        prs = {v: self.gh.repo(r).prs for v, r in VARIANTS.items()}
        lost_first = [v for v in VARIANTS if prs[v]]
        self.assertEqual(len(lost_first), 1, prs)  # the first variant's PR exists, response lost; tick stopped
        d, _ = self.tick()
        for r in VARIANTS.values():
            self.assertEqual(len(self.gh.repo(r).prs), 1)
        self.assertEqual(len(self.mutations("create_pr")), 2)

    def test_lost_branch_creation_then_pr(self):
        self.gh.lose_next.add("create_ref")
        self.tick()  # seed main creation response lost in the first variant
        self.assertEqual(sum(1 for v in VARIANTS if self.main(v)), 1)
        self.tick()
        self.assertEqual({v: self.main(v) for v in VARIANTS}, {v: sha(v, "s0") for v in VARIANTS})
        self.assertEqual(len(self.mutations("create_ref")), 2)

    def test_failed_write_is_not_retried_in_same_tick(self):
        self.gh.fail_next.add("create_ref")
        _, notes = self.tick()
        self.assertEqual(len(self.mutations("create_ref")), 1)
        self.assertTrue(any("failed" in n for n in notes))
        self.tick()
        self.assertEqual({v: self.main(v) for v in VARIANTS}, {v: sha(v, "s0") for v in VARIANTS})

    def test_update_ref_precondition(self):
        self.tick()
        self.advance(20)
        self.finish_all()
        self.advance(SPACING + 1)
        real_get_ref = self.gh.get_ref

        def moved(repo, ref):
            return "e" * 40 if repo == "me/ref" else real_get_ref(repo, ref)

        self.gh.get_ref = moved
        _, notes = self.tick()
        self.assertTrue(any("moved" in n for n in notes), notes)
        self.gh.get_ref = real_get_ref

    def test_gate_waits_for_release(self):
        for _ in range(60):
            d, _ = self.tick()
            if d.current == "scored/clear-caches":
                break
            self.advance(SPACING + 1)
            self.finish_all()
        self.assertTrue(any("waiting for release" in w for w in d.waiting))
        self.assertEqual(self.mutations("delete_cache"), [])
        self.released.add("scored/clear-caches")
        self.tick()
        self.assertTrue(self.mutations("delete_cache"))
        for r in VARIANTS.values():
            self.assertEqual(self.gh.repo(r).caches, [])
            self.assertEqual(self.gh.repo(r).artifacts, [])


class TestClassify(unittest.TestCase):
    def test_extract(self):
        log = (
            "2026-11-26T10:00:00.1234567Z Publishing Build Scan...\n"
            "2026-11-26T10:00:01.0000000Z https://scans.gradle.com/s/abc123def\n"
            "2026-11-26T10:00:02.0000000Z Received status code 429 from server: Too Many Requests (repo1.maven.org)\n"
            "2026-11-26T10:00:03.0000000Z > Task :nessie-x:test FAILED\n"
        )
        x = classify.extract(log)
        self.assertEqual(x["scan_urls"], ["https://scans.gradle.com/s/abc123def"])
        self.assertEqual((x["http_429"], x["http_429_maven_central"]), (1, 1))
        self.assertEqual(x["failure_excerpt"], ["> Task :nessie-x:test FAILED"])

    def test_classes(self):
        self.assertEqual(classify.classify("cancelled", {"failure_excerpt": []}, {}), "cancelled")
        self.assertEqual(classify.classify("failure", {"failure_excerpt": ["No space left on device"]}, {}), "infrastructure")
        flaky = {"flaky_tests": [r"TestFoo"]}
        self.assertEqual(classify.classify("failure", {"failure_excerpt": ["TestFoo > bar() FAILED"]}, flaky), "flaky-test")
        self.assertEqual(classify.classify("failure", {"failure_excerpt": ["TestFoo > bar() FAILED", "TestBaz > x() FAILED"]}, flaky), "unknown")
        self.assertEqual(classify.classify("failure", {"failure_excerpt": ["> Task :a FAILED"]}, {}), "unknown")

    def test_parse_telemetry(self):
        files = {
            "job.json": b'{"job":"test"}',
            "deps-begin.json": b'{"phase":"begin","files":10,"bytes":100}',
            "deps-end.json": b'{"phase":"end","files":12,"bytes":150}',
            "tasks-2.jsonl": b'{"schema":1,"startedAt":2}\n{"path":":b","outcome":"executed","start":1,"end":2}\n',
            "tasks-1.jsonl": b'{"schema":1,"startedAt":1}\n{"path":":a","outcome":"from-cache","start":1,"end":2}\n{"path":":c","outcome":"from-cache","start":1,"end":2}\n',
            "broken.jsonl": b"x",
            "tasks-3.jsonl": b"{broken",
        }
        t = collect.parse_telemetry(files)
        self.assertEqual([i["outcomes"] for i in t["invocations"]], [{"from-cache": 2}, {"executed": 1}])
        self.assertEqual(t["deps"]["end"]["files"] - t["deps"]["begin"]["files"], 2)
        self.assertEqual(len(t["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
