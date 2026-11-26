"""Stateful in-memory stand-in for replay.github.GitHubApi."""

import io
import json
import zipfile

from replay.github import GitHubError


class LostResponse(GitHubError):
    """Raised after the remote effect was applied, like a timeout after the server acted."""


class Repo:
    def __init__(self, name):
        self.name = name
        self.refs = {}
        self.prs = []
        self.runs = []
        self.jobs = {}  # (run_id, attempt) -> [job]
        self.logs = {}  # job_id -> text
        self.artifacts = []  # {id, name, run_id, files, expired}
        self.caches = []


class FakeGitHub:
    def __init__(self, clock, job_names=("CI Build", "CI Store Cache")):
        self.clock = clock
        self.repos = {}
        self.job_names = list(job_names)
        self.calls = []
        self.lose_next = set()  # method names whose next call applies the effect then raises
        self.fail_next = set()  # method names whose next call fails without effect
        self._id = 1000

    def repo(self, name) -> Repo:
        return self.repos.setdefault(name, Repo(name))

    def _next_id(self):
        self._id += 1
        return self._id

    def _now(self):
        return self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _call(self, method, *args):
        self.calls.append((method, *args))
        if method in self.fail_next:
            self.fail_next.discard(method)
            raise GitHubError(f"{method}: simulated failure", 502)

    def _maybe_lose(self, method):
        if method in self.lose_next:
            self.lose_next.discard(method)
            raise LostResponse(f"{method}: response lost after effect")

    # --- test helpers ------------------------------------------------------------------------

    def start_run(self, repo, event, sha, branch):
        r = self.repo(repo)
        run = {"id": self._next_id(), "event": event, "head_sha": sha, "head_branch": branch, "status": "queued", "conclusion": None,
               "run_attempt": 1, "created_at": self._now(), "updated_at": self._now(), "run_started_at": self._now(),
               "html_url": f"https://github.com/{repo}/actions/runs/x"}
        r.runs.insert(0, run)
        self._make_jobs(r, run, conclusion=None)
        return run

    def _make_jobs(self, r, run, conclusion):
        jobs = []
        for n in self.job_names:
            jid = self._next_id()
            jobs.append({"id": jid, "name": n, "status": "completed" if conclusion else "queued", "conclusion": conclusion,
                         "created_at": self._now(), "started_at": self._now(), "completed_at": self._now(),
                         "steps": [{"number": 1, "name": "Build", "status": "completed", "conclusion": conclusion, "started_at": self._now(), "completed_at": self._now()}]})
            r.logs[jid] = "2026-01-01T00:00:00.0000000Z build ok\n"
        r.jobs[(run["id"], run["run_attempt"])] = jobs

    def finish_run(self, repo, run_id, conclusion="success", failed_job_log=None, telemetry=True):
        r = self.repo(repo)
        run = next(x for x in r.runs if x["id"] == run_id)
        run["status"] = "completed"
        run["conclusion"] = conclusion
        run["updated_at"] = self._now()
        jobs = r.jobs[(run_id, run["run_attempt"])]
        for i, j in enumerate(jobs):
            j["status"] = "completed"
            j["conclusion"] = "success" if conclusion == "success" or i > 0 else conclusion
            j["steps"][0]["conclusion"] = j["conclusion"]
            if i == 0 and conclusion != "success" and failed_job_log:
                r.logs[j["id"]] = failed_job_log
        if telemetry:
            files = {"job.json": json.dumps({"job": "build"}).encode(),
                     "tasks-1.jsonl": b'{"schema":1,"tasks":"build","startedAt":1}\n{"path":":a","outcome":"from-cache","start":1,"end":2}\n'}
            r.artifacts.append({"id": self._next_id(), "name": f"replay-telemetry-build-{run['run_attempt']}", "run_id": run_id, "files": files, "expired": False})
        r.caches.append({"id": self._next_id(), "key": f"cache-{run_id}"})
        return run

    # --- API surface -------------------------------------------------------------------------

    def get_repo(self, repo):
        if repo not in self.repos:
            return None
        return {"visibility": "public", "archived": False, "permissions": {"push": True}, "has_issues": False, "has_wiki": False, "has_projects": False}

    def commit_exists(self, repo, sha):
        return True

    def get_ref(self, repo, ref):
        self._call("get_ref", repo, ref)
        return self.repo(repo).refs.get(f"refs/{ref}")

    def matching_refs(self, repo, prefix):
        self._call("matching_refs", repo, prefix)
        return {k: v for k, v in self.repo(repo).refs.items() if k.startswith(f"refs/{prefix}")}

    def create_ref(self, repo, ref, sha):
        self._call("create_ref", repo, ref, sha)
        r = self.repo(repo)
        if f"refs/{ref}" in r.refs:
            raise GitHubError("Reference already exists", 422)
        r.refs[f"refs/{ref}"] = sha
        if ref == "heads/main":
            self.start_run(repo, "push", sha, "main")
        self._maybe_lose("create_ref")

    def update_ref(self, repo, ref, sha):
        self._call("update_ref", repo, ref, sha)
        r = self.repo(repo)
        r.refs[f"refs/{ref}"] = sha
        if ref == "heads/main":
            self.start_run(repo, "push", sha, "main")
        self._maybe_lose("update_ref")

    def list_prs(self, repo):
        self._call("list_prs", repo)
        return [dict(p) for p in self.repo(repo).prs]

    def create_pr(self, repo, head, base, title, body):
        self._call("create_pr", repo, head, base, title)
        r = self.repo(repo)
        if any(p["head_ref"] == head and p["state"] == "open" for p in r.prs):
            raise GitHubError("A pull request already exists", 422)
        number = len(r.prs) + 1
        sha = r.refs[f"refs/heads/{head}"]
        r.prs.append({"number": number, "state": "open", "head_ref": head, "head_sha": sha, "base_ref": base, "body": body,
                      "created_at": self._now(), "closed_at": None, "same_repo": True})
        self.start_run(repo, "pull_request", sha, head)
        self._maybe_lose("create_pr")
        return number

    def close_pr(self, repo, number):
        self._call("close_pr", repo, number)
        p = next(x for x in self.repo(repo).prs if x["number"] == number)
        p["state"] = "closed"
        p["closed_at"] = self._now()

    def list_workflow_runs(self, repo, workflow_file):
        self._call("list_workflow_runs", repo, workflow_file)
        return [dict(r) for r in self.repo(repo).runs]

    def get_run_attempt(self, repo, run_id, attempt):
        run = next(x for x in self.repo(repo).runs if x["id"] == run_id)
        return {**run, "run_attempt": attempt}

    def list_attempt_jobs(self, repo, run_id, attempt):
        return [dict(j) for j in self.repo(repo).jobs[(run_id, attempt)]]

    def _rerun(self, repo, run_id):
        r = self.repo(repo)
        run = next(x for x in r.runs if x["id"] == run_id)
        run["run_attempt"] += 1
        run["status"] = "queued"
        run["conclusion"] = None
        run["updated_at"] = self._now()
        self._make_jobs(r, run, conclusion=None)

    def rerun_failed_jobs(self, repo, run_id):
        self._call("rerun_failed_jobs", repo, run_id)
        self._rerun(repo, run_id)

    def rerun_job(self, repo, job_id):
        self._call("rerun_job", repo, job_id)
        r = self.repo(repo)
        run_id = next(rid for (rid, _), jobs in r.jobs.items() if any(j["id"] == job_id for j in jobs))
        self._rerun(repo, run_id)

    def job_log(self, repo, job_id):
        return self.repo(repo).logs.get(job_id, "")

    def list_run_artifacts(self, repo, run_id):
        return [{"id": a["id"], "name": a["name"], "expired": a["expired"]} for a in self.repo(repo).artifacts if a["run_id"] == run_id]

    def download_artifact_files(self, repo, artifact_id):
        a = next(x for x in self.repo(repo).artifacts if x["id"] == artifact_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, c in a["files"].items():
                z.writestr(n, c)
        with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
            return {n: z.read(n) for n in z.namelist()}

    def list_artifacts(self, repo):
        return [{"id": a["id"], "name": a["name"]} for a in self.repo(repo).artifacts]

    def delete_artifact(self, repo, artifact_id):
        self._call("delete_artifact", repo, artifact_id)
        r = self.repo(repo)
        r.artifacts = [a for a in r.artifacts if a["id"] != artifact_id]

    def cache_usage(self, repo):
        r = self.repo(repo)
        return {"active_caches_count": len(r.caches), "active_caches_size_in_bytes": 100 * len(r.caches)}

    def list_caches(self, repo):
        return [dict(c) for c in self.repo(repo).caches]

    def delete_cache(self, repo, cache_id):
        self._call("delete_cache", repo, cache_id)
        r = self.repo(repo)
        r.caches = [c for c in r.caches if c["id"] != cache_id]

