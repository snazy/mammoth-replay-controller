import json
import tempfile
import unittest
from pathlib import Path

from replay import build, gitops
from replay.gitops import git

IDENTITY = {"GIT_AUTHOR_NAME": "Up Stream", "GIT_AUTHOR_EMAIL": "up@example.com", "GIT_COMMITTER_NAME": "Up Stream", "GIT_COMMITTER_EMAIL": "up@example.com"}


class Upstream:
    """A tiny upstream repository with a linear main branch."""

    def __init__(self, root: Path):
        self.path = root / "upstream"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "main")
        self.n = 0

    def commit(self, subject: str, files: dict[str, str | None]) -> str:
        for name, content in files.items():
            p = self.path / name
            if content is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
        git(self.path, "add", "-A")
        self.n += 1
        date = f"2026-10-01T{self.n:02d}:00:00+00:00"
        git(self.path, "commit", "-q", "--no-gpg-sign", "-m", subject, "-m", "Body with fixes #1 and @someone", env=IDENTITY | {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        return gitops.rev_parse(self.path, "HEAD")


def write_layer(base: Path, name: str, files: dict[str, str] | None = None, patches: dict[str, str] | None = None) -> None:
    layer = base / name
    for rel, content in (files or {}).items():
        p = layer / "files" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for fname, content in (patches or {}).items():
        p = layer / "patches" / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


CI_YML = "name: CI build\njobs:\n  build:\n    steps:\n      - run: build\n"
MAMMOTH_PATCH = """--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -2,4 +2,4 @@
 jobs:
   build:
     steps:
-      - run: build
+      - run: build-with-mammoth
"""


class BuildFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.up = Upstream(self.root)
        self.c = {}
        self.c["seed"] = self.up.commit("Seed (#100)", {"src/a.txt": "a1", ".github/workflows/ci.yml": CI_YML, ".github/workflows/other.yml": "x", ".github/CODEOWNERS": "* @someone"})
        self.c["dep"] = self.up.commit("Update dependency foo to v2 (#101)", {"gradle/libs.versions.toml": "foo=2"})
        self.c["gh"] = self.up.commit("Update actions/checkout digest (#102)", {".github/workflows/ci.yml": CI_YML + "# bump\n"})
        self.c["feat"] = self.up.commit("feat: thing (#103), see owner/repo#5", {"src/a.txt": "a2", "src/b.txt": "b"})
        self.c["doc"] = self.up.commit("Docs only (#104)", {"README.md": "docs"})
        self.c["last"] = self.up.commit("Update dependency bar (#105)", {"gradle/libs.versions.toml": "foo=2\nbar=1"})
        self.c["smokepr"] = self.c["dep"]

        self.overlays = self.root / "overlays"
        write_layer(self.overlays, "t/common", files={".github/actions/replay-telemetry/action.yml": "telemetry"})
        write_layer(self.overlays, "t/reference")
        write_layer(self.overlays, "t/mammoth", patches={"01-ci.patch": MAMMOTH_PATCH})

        self.profile = {
            "name": "t",
            "upstream": {"repo": "example/upstream", "branch": "main"},
            "identity": {"name": "Mammoth Replay", "email": "mammoth-replay@invalid"},
            "variants": {
                "reference": {"repo": "me/ref", "overlay_layers": ["t/reference"]},
                "mammoth": {"repo": "me/mam", "overlay_layers": ["t/mammoth"]},
            },
            "overlay": {"keep": [".github/workflows/ci.yml"], "common_layers": ["t/common"]},
            "workflow": {"file": "ci.yml", "name": "CI build", "push_paths_ignore": ["**.md", "!site/**"]},
            "jobs": {"producers": ["build"]},
            "controller": {"max_reruns": 2},
            "campaigns": [
                {"id": "smoke", "events": [
                    {"id": "seed", "type": "seed", "upstream": self.c["seed"]},
                    {"id": "main-01", "type": "main", "upstream": self.c["dep"]},
                    {"id": "pr-02", "type": "pr", "upstream": self.c["feat"], "push_after": False},
                ]},
                {"id": "scored", "events": [
                    {"id": "clear-caches", "type": "gate", "action": "delete-caches-and-artifacts"},
                    {"id": "seed", "type": "seed", "upstream": self.c["seed"]},
                    {"type": "range", "after": self.c["seed"], "to": self.c["last"], "pr": [self.c["feat"]]},
                ]},
            ],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def new_repos(self, tag: str) -> dict[str, Path]:
        repos = {}
        for v in ("reference", "mammoth"):
            p = self.root / f"{tag}-{v}"
            git(p.parent, "init", "-q", "-b", "main", str(p))
            repos[v] = p
        return repos

    def plan(self, tag: str = "a") -> tuple[dict, dict[str, Path]]:
        repos = self.new_repos(tag)
        return build.build_plan(self.profile, self.up.path, self.overlays, repos), repos


class TestBuildPlan(BuildFixture):
    def test_events_and_drops(self):
        plan, _ = self.plan()
        keys = [f"{e['campaign']}/{e['id']}" for e in plan["events"]]
        self.assertEqual(
            keys,
            ["smoke/seed", "smoke/main-01", "smoke/pr-02", "scored/clear-caches", "scored/seed",
             "scored/main-01", "scored/pr-03", "scored/main-03", "scored/main-05"],
        )
        dropped = {d["id"]: d["reason"] for d in plan["dropped"]}
        self.assertEqual(dropped, {"main-02": "no change outside .github/", "main-04": "all changes match push paths-ignore"})

    def test_deterministic_shas(self):
        plan_a, _ = self.plan("a")
        plan_b, _ = self.plan("b")
        self.assertEqual([e["sha"] for e in plan_a["events"]], [e["sha"] for e in plan_b["events"]])
        self.assertEqual(json.dumps(plan_a, sort_keys=True), json.dumps(plan_b, sort_keys=True))

    def test_trees_match_upstream_outside_github(self):
        plan, repos = self.plan()
        for e in plan["events"]:
            if e["type"] == "gate":
                continue
            up_tree = gitops.tree_of(self.up.path, e["upstream"])
            for v, repo in repos.items():
                rp_tree = gitops.tree_of(repo, e["sha"][v])
                up = {x[3]: x[2] for x in gitops.ls_tree(repo, up_tree) if x[3] != ".github"}
                rp = {x[3]: x[2] for x in gitops.ls_tree(repo, rp_tree) if x[3] != ".github"}
                self.assertEqual(up, rp, f"{e['id']} {v}")

    def test_overlay_contents(self):
        plan, repos = self.plan()
        seed = plan["events"][0]
        for v, repo in repos.items():
            files = git(repo, "ls-tree", "-r", "--name-only", seed["sha"][v], ".github").split()
            self.assertEqual(sorted(files), [".github/actions/replay-telemetry/action.yml", ".github/workflows/ci.yml"])
        ref_ci = git(repos["reference"], "show", f"{seed['sha']['reference']}:.github/workflows/ci.yml")
        mam_ci = git(repos["mammoth"], "show", f"{seed['sha']['mammoth']}:.github/workflows/ci.yml")
        self.assertIn("run: build\n", ref_ci)
        self.assertIn("run: build-with-mammoth", mam_ci)
        # the frozen ci.yml ignores the upstream workflow bump
        last = plan["events"][-1]
        self.assertNotIn("# bump", git(repos["reference"], "show", f"{last['sha']['reference']}:.github/workflows/ci.yml"))

    def test_chain_and_pr(self):
        plan, repos = self.plan()
        ev = {f"{e['campaign']}/{e['id']}": e for e in plan["events"]}
        for v, repo in repos.items():
            # scored seed continues the smoke main chain; the smoke PR is off-chain
            parent = git(repo, "rev-parse", f"{ev['scored/seed']['sha'][v]}^").strip()
            self.assertEqual(parent, ev["smoke/main-01"]["sha"][v])
            self.assertEqual(ev["scored/pr-03"]["sha"][v], ev["scored/main-03"]["sha"][v])
            refs = plan["staging_refs"][v]
            self.assertEqual(refs["refs/tags/replay-staging/main"], ev["scored/main-05"]["sha"][v])
            self.assertEqual(refs["refs/tags/replay-staging/smoke-pr-02"], ev["smoke/pr-02"]["sha"][v])
            self.assertEqual(len(refs), 2)
            # the root commit has no parent
            self.assertEqual(git(repo, "rev-list", "--max-parents=0", refs["refs/tags/replay-staging/main"]).strip(), ev["smoke/seed"]["sha"][v])
        self.assertEqual(ev["scored/pr-03"]["branch"], "replay/scored/pr-03")

    def test_message_and_identity(self):
        plan, repos = self.plan()
        e = next(x for x in plan["events"] if x["id"] == "pr-03")
        repo = repos["mammoth"]
        msg = git(repo, "show", "-s", "--format=%B", e["sha"]["mammoth"])
        self.assertEqual(msg.splitlines()[0], "feat: thing (PR 103), see PR 5")
        self.assertNotIn("#", msg)
        self.assertNotIn("@", msg)
        trailers = git(repo, "show", "-s", "--format=%(trailers:only,unfold)", e["sha"]["mammoth"])
        self.assertIn("Replay-Event: pr-03", trailers)
        self.assertIn(f"Replay-Upstream: {e['upstream']}", trailers)
        who = git(repo, "show", "-s", "--format=%an <%ae>|%cn <%ce>|%cI", e["sha"]["mammoth"]).strip()
        self.assertEqual(who.split("|")[:2], ["Mammoth Replay <mammoth-replay@invalid>"] * 2)
        self.assertEqual(who.split("|")[2], gitops.commit_meta(self.up.path, e["upstream"])["committer_date"])

    def test_staging_refs_written_locally(self):
        plan, repos = self.plan()
        build.write_staging_refs(plan, repos)
        for v, repo in repos.items():
            self.assertEqual(gitops.rev_parse(repo, "refs/tags/replay-staging/main"), plan["staging_refs"][v]["refs/tags/replay-staging/main"])

    def test_pr_must_match_main_push(self):
        # a PR whose main push gets dropped (docs-only) is rejected
        self.profile["campaigns"][1]["events"][2]["pr"] = [self.c["doc"]]
        with self.assertRaisesRegex(build.BuildError, "differs from its main push"):
            self.plan()

    def test_pr_not_in_range(self):
        self.profile["campaigns"][1]["events"][2]["pr"] = [self.c["seed"]]
        with self.assertRaisesRegex(build.BuildError, "not in range"):
            self.plan()

    def test_overlay_file_outside_github_rejected(self):
        write_layer(self.overlays, "t/mammoth", files={"src/evil.txt": "x"})
        with self.assertRaisesRegex(build.BuildError, "outside .github"):
            self.plan()

    def test_broken_patch_rejected(self):
        write_layer(self.overlays, "t/mammoth", patches={"02-bad.patch": MAMMOTH_PATCH})
        with self.assertRaisesRegex(build.BuildError, "02-bad.patch failed"):
            self.plan()


class TestFilters(unittest.TestCase):
    IGNORE = ["LICENSE", "**.md", "!site/**", ".idea/**", ".github/workflows/release*.yml"]

    def test_paths_ignore(self):
        cases = {
            "README.md": True,
            "docs/x/y.md": True,
            "site/docs/x.md": False,
            "LICENSE": True,
            "sub/LICENSE": False,
            ".idea/a/b.xml": True,
            ".github/workflows/release-publish.yml": True,
            ".github/workflows/ci.yml": False,
            "src/Main.java": False,
        }
        for path, expected in cases.items():
            self.assertEqual(build.is_ignored(path, self.IGNORE), expected, path)

    def test_triggers(self):
        self.assertFalse(build.triggers_push(["a.md", "b/c.md"], self.IGNORE))
        self.assertTrue(build.triggers_push(["a.md", "build.gradle.kts"], self.IGNORE))

    def test_sanitize(self):
        self.assertEqual(build.sanitize_subject("Update x to v2 (#13109)"), "Update x to v2 (PR 13109)")
        self.assertEqual(build.sanitize_subject("Fix quarkusio/quarkus#42 for @bob"), "Fix PR 42 for (at)bob")
        self.assertEqual(build.sanitize_subject("C# support"), "C# support")


if __name__ == "__main__":
    unittest.main()
