"""Minimal GitHub REST client (standard library only).

Reads retry on transient errors and rate limits within a bounded wait. Writes are never retried:
a failed or timed-out write is reported, and the next tick reconciles from remote state.
Downloads follow redirects without forwarding the token to the redirect target.
"""

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = "https://api.github.com"
TIMEOUT_SECONDS = 60
MAX_READ_ATTEMPTS = 4
MAX_RATE_LIMIT_WAIT_SECONDS = 120


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubApi:
    def __init__(self, token: str, api: str = API, sleep=time.sleep):
        self._token = token
        self._api = api.rstrip("/")
        self._sleep = sleep
        self._no_redirect = urllib.request.build_opener(_NoRedirect)
        self._plain = urllib.request.build_opener()

    # --- transport ---------------------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None, raw: bool = False):
        url = path if path.startswith("http") else f"{self._api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "mammoth-replay-controller",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = MAX_READ_ATTEMPTS if method == "GET" else 1
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._no_redirect.open(req, timeout=TIMEOUT_SECONDS) as resp:
                    payload = resp.read()
                    return payload if raw else (json.loads(payload) if payload else None)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and method == "GET":
                    location = e.headers.get("Location")
                    with self._plain.open(urllib.request.Request(location, headers={"User-Agent": "mammoth-replay-controller"}), timeout=TIMEOUT_SECONDS) as resp:
                        return resp.read()
                detail = e.read()[:500].decode(errors="replace")
                wait = self._retry_wait(e, attempt)
                if method == "GET" and wait is not None and attempt < attempts:
                    self._sleep(wait)
                    continue
                raise GitHubError(f"{method} {path}: HTTP {e.code}: {detail}", e.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if method == "GET" and attempt < attempts:
                    self._sleep(min(2**attempt, 30))
                    continue
                raise GitHubError(f"{method} {path}: {e}") from None
        raise GitHubError(f"{method} {path}: retries exhausted")

    def _retry_wait(self, e: urllib.error.HTTPError, attempt: int) -> float | None:
        if e.code >= 500:
            return min(2**attempt, 30)
        if e.code in (403, 429):
            retry_after = e.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), MAX_RATE_LIMIT_WAIT_SECONDS)
            if e.headers.get("X-RateLimit-Remaining") == "0":
                reset = float(e.headers.get("X-RateLimit-Reset") or 0)
                wait = reset - time.time() + 1
                return wait if 0 < wait <= MAX_RATE_LIMIT_WAIT_SECONDS else None
            if e.code == 429:
                return min(2**attempt * 5, 60)
        return None

    def _paginate(self, path: str, key: str | None, params: dict | None = None, limit: int = 1000) -> list:
        out = []
        page = 1
        while len(out) < limit:
            resp = self._request("GET", path, {**(params or {}), "per_page": 100, "page": page})
            items = resp[key] if key else resp
            out.extend(items)
            if len(items) < 100:
                break
            page += 1
        return out[:limit]

    # --- repositories ------------------------------------------------------------------------

    def get_repo(self, repo: str) -> dict | None:
        try:
            return self._request("GET", f"/repos/{repo}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise

    def commit_exists(self, repo: str, sha: str) -> bool:
        try:
            self._request("GET", f"/repos/{repo}/commits/{sha}")
            return True
        except GitHubError as e:
            if e.status in (404, 422):
                return False
            raise

    # --- refs --------------------------------------------------------------------------------

    def get_ref(self, repo: str, ref: str) -> str | None:
        try:
            return self._request("GET", f"/repos/{repo}/git/ref/{ref}")["object"]["sha"]
        except GitHubError as e:
            if e.status in (404, 409):
                return None
            raise

    def matching_refs(self, repo: str, prefix: str) -> dict[str, str]:
        try:
            refs = self._request("GET", f"/repos/{repo}/git/matching-refs/{prefix}")
        except GitHubError as e:
            if e.status == 409:  # empty repository
                return {}
            raise
        return {r["ref"]: r["object"]["sha"] for r in refs}

    def create_ref(self, repo: str, ref: str, sha: str) -> None:
        self._request("POST", f"/repos/{repo}/git/refs", body={"ref": f"refs/{ref}", "sha": sha})

    def update_ref(self, repo: str, ref: str, sha: str) -> None:
        self._request("PATCH", f"/repos/{repo}/git/refs/{ref}", body={"sha": sha, "force": False})

    # --- pull requests -----------------------------------------------------------------------

    def list_prs(self, repo: str) -> list[dict]:
        prs = self._paginate(f"/repos/{repo}/pulls", None, {"state": "all"})
        return [
            {
                "number": p["number"],
                "state": p["state"],
                "head_ref": p["head"]["ref"],
                "head_sha": p["head"]["sha"],
                "base_ref": p["base"]["ref"],
                "body": p.get("body") or "",
                "created_at": p["created_at"],
                "closed_at": p.get("closed_at"),
                "same_repo": (p["head"].get("repo") or {}).get("full_name") == repo,
            }
            for p in prs
        ]

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> int:
        return self._request("POST", f"/repos/{repo}/pulls", body={"head": head, "base": base, "title": title, "body": body})["number"]

    def close_pr(self, repo: str, number: int) -> None:
        self._request("PATCH", f"/repos/{repo}/pulls/{number}", body={"state": "closed"})

    # --- workflow runs -----------------------------------------------------------------------

    def list_workflow_runs(self, repo: str, workflow_file: str) -> list[dict]:
        try:
            runs = self._paginate(f"/repos/{repo}/actions/workflows/{workflow_file}/runs", "workflow_runs", limit=500)
        except GitHubError as e:
            if e.status == 404:  # workflow not yet known (no default branch yet)
                return []
            raise
        keep = ("id", "event", "head_sha", "head_branch", "status", "conclusion", "run_attempt", "created_at", "updated_at", "run_started_at", "html_url")
        return [{k: r.get(k) for k in keep} for r in runs]

    def get_run_attempt(self, repo: str, run_id: int, attempt: int) -> dict:
        return self._request("GET", f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}")

    def list_attempt_jobs(self, repo: str, run_id: int, attempt: int) -> list[dict]:
        return self._paginate(f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")

    def rerun_failed_jobs(self, repo: str, run_id: int) -> None:
        self._request("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs", body={})

    def rerun_job(self, repo: str, job_id: int) -> None:
        self._request("POST", f"/repos/{repo}/actions/jobs/{job_id}/rerun", body={})

    def job_log(self, repo: str, job_id: int) -> str:
        return self._request("GET", f"/repos/{repo}/actions/jobs/{job_id}/logs", raw=True).decode(errors="replace")

    # --- artifacts and caches ----------------------------------------------------------------

    def list_run_artifacts(self, repo: str, run_id: int) -> list[dict]:
        return self._paginate(f"/repos/{repo}/actions/runs/{run_id}/artifacts", "artifacts")

    def download_artifact_files(self, repo: str, artifact_id: int) -> dict[str, bytes]:
        data = self._request("GET", f"/repos/{repo}/actions/artifacts/{artifact_id}/zip", raw=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}

    def list_artifacts(self, repo: str) -> list[dict]:
        return self._paginate(f"/repos/{repo}/actions/artifacts", "artifacts", limit=5000)

    def delete_artifact(self, repo: str, artifact_id: int) -> None:
        self._request("DELETE", f"/repos/{repo}/actions/artifacts/{artifact_id}")

    def cache_usage(self, repo: str) -> dict:
        return self._request("GET", f"/repos/{repo}/actions/cache/usage")

    def list_caches(self, repo: str) -> list[dict]:
        return self._paginate(f"/repos/{repo}/actions/caches", "actions_caches", limit=5000)

    def delete_cache(self, repo: str, cache_id: int) -> None:
        self._request("DELETE", f"/repos/{repo}/actions/caches/{cache_id}")
