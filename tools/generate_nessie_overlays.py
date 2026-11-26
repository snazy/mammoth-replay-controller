"""Generates the Nessie overlay patches from the seed's .github/workflows/ci.yml.

Usage: python3 -m tools.generate_nessie_overlays <nessie-checkout>

Writes overlays/nessie/common/patches/01-ci.patch (telemetry, nesqueit removed) and
overlays/nessie/mammoth/patches/01-ci.patch (Mammoth replaces the incumbent cache actions).
The patches are the reviewed artifacts; this script documents and reproduces them.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

from replay import gitops
from replay.gitops import git

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "profiles" / "nessie.json").read_text())
SEED = PROFILE["campaigns"][0]["events"][0]["upstream"]
MAMMOTH_ACTION = "buildish-tooling/buildish-mammoth-cache/actions/github/gradle@ef15e2461c37cbfbb0ed915f015f50bec2a7f122 # v0.5.1"
MAMMOTH_CONFIG = ".github/mammoth-gradle.yml"
PREPARE_USES = "uses: ./.github/actions/ci-incr-build-cache-prepare"
SAVE_USES = "uses: ./.github/actions/ci-incr-build-cache-save"
PRODUCERS = PROFILE["jobs"]["producers"]
AGGREGATOR = PROFILE["jobs"]["aggregator"]
READERS = ["docker-testing", "site"]
EXCLUDED = PROFILE["jobs"]["excluded"]

JOB_RE = re.compile(r"^  ([a-z0-9-]+):\s*$")
STEP_RE = re.compile(r"^      - ")


class Job:
    def __init__(self, name: str, lines: list[str]):
        self.name = name
        self.lines = lines

    def steps(self) -> list[tuple[int, int]]:
        """(start, end) line ranges of steps, end exclusive; trailing blank lines stay outside."""
        starts = [i for i, line in enumerate(self.lines) if STEP_RE.match(line)]
        ranges = []
        for n, s in enumerate(starts):
            e = starts[n + 1] if n + 1 < len(starts) else len(self.lines)
            while e > s + 1 and self.lines[e - 1].strip() == "":
                e -= 1
            ranges.append((s, e))
        return ranges

    def find_step(self, needle: str) -> tuple[int, int] | None:
        for s, e in self.steps():
            if any(needle in line for line in self.lines[s:e]):
                return s, e
        return None


def split_jobs(text: str) -> tuple[list[str], list[Job]]:
    lines = text.splitlines(keepends=True)
    jobs_at = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    head = lines[: jobs_at + 1]
    jobs: list[Job] = []
    pending: list[str] = []
    for line in lines[jobs_at + 1 :]:
        m = JOB_RE.match(line)
        if m:
            jobs.append(Job(m.group(1), pending + [line]))
            pending = []
        elif jobs:
            jobs[-1].lines.append(line)
        else:
            pending.append(line)
    return head, jobs


def join(head: list[str], jobs: list[Job]) -> str:
    return "".join(head) + "".join("".join(j.lines) for j in jobs)


def step_lines(*lines: str) -> list[str]:
    return [f"      {lines[0]}\n"] + [f"        {line}\n" for line in lines[1:]]


def common(text: str) -> str:
    head, jobs = split_jobs(text)
    jobs = [j for j in jobs if j.name not in EXCLUDED]
    for job in jobs:
        prep = job.find_step(PREPARE_USES)
        if prep is None:
            continue
        s, e = prep
        name_line = job.lines[s]
        assert name_line.lstrip().startswith("- name:"), f"{job.name}: unexpected prepare step {name_line!r}"
        begin = step_lines("- name: Replay telemetry / begin", "uses: ./.github/actions/replay-telemetry-begin")
        save = job.find_step(SAVE_USES)
        end = step_lines(
            "- name: Replay telemetry / end",
            "if: always()",
            "uses: ./.github/actions/replay-telemetry-end",
            "with:",
            f"  job: {job.name}",
            "  cache-outputs: ${{ toJSON(steps.gradle-cache.outputs) }}",
        )
        if save:
            job.lines[save[0] : save[0]] = end + ["\n"]
        else:
            last_end = job.steps()[-1][1]
            job.lines[last_end:last_end] = ["\n"] + end
        job.lines[e:e] = ["\n"] + begin
        job.lines[s + 1 : s + 1] = ["        id: gradle-cache\n"]
    return join(head, jobs)


def mammoth(text: str) -> str:
    head, jobs = split_jobs(text)
    for job in jobs:
        save = job.find_step(SAVE_USES)
        if save:
            s, e = save
            while e < len(job.lines) and job.lines[e].strip() == "":
                e += 1
            del job.lines[s:e]
        prep = job.find_step(PREPARE_USES)
        if prep is None:
            continue
        s, e = prep
        if job.name in PRODUCERS:
            name, inputs = "Mammoth Gradle cache (distributed worker)", ["job-mode: distributed-worker"]
        elif job.name == AGGREGATOR:
            name, inputs = "Mammoth Gradle cache (aggregator)", ["job-mode: distributed-aggregator", f"dependent-jobs: {', '.join(PRODUCERS)}"]
        elif job.name in READERS:
            name, inputs = "Mammoth Gradle cache (read-only)", ["job-mode: standalone", "read-only: 'true'"]
        else:
            raise SystemExit(f"job {job.name} uses the incumbent cache but has no Mammoth role")
        job.lines[s:e] = step_lines(
            f"- name: {name}",
            "id: gradle-cache",
            f"uses: {MAMMOTH_ACTION}",
            "with:",
            f"  config-file: {MAMMOTH_CONFIG}",
            *[f"  {i}" for i in inputs],
        )
    out = join(head, jobs)
    for needle in (PREPARE_USES, SAVE_USES):
        if needle in out:
            raise SystemExit(f"incumbent reference left in Mammoth ci.yml: {needle}")
    return out


def write_patch(work: Path, before: dict[str, str | None], after: dict[str, str | None], dest: Path) -> None:
    """Writes a git patch transforming the `before` file set into `after` (paths repo-relative)."""
    for label, files in (("before", before), ("after", after)):
        for p in work.rglob("*"):
            if p.is_file() and ".git" not in p.parts:
                p.unlink()
        for rel, content in files.items():
            if content is None:
                continue
            f = work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
        git(work, "add", "-A")
        git(work, "commit", "-q", "--no-gpg-sign", "--allow-empty", "-m", label, env={"GIT_AUTHOR_NAME": "g", "GIT_AUTHOR_EMAIL": "g@invalid", "GIT_COMMITTER_NAME": "g", "GIT_COMMITTER_EMAIL": "g@invalid"})
    patch = git(work, "diff", "--no-color", "--no-renames", "HEAD~1", "HEAD")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(patch)


def main(argv: list[str]) -> int:
    upstream = Path(argv[1]).resolve()
    seed = gitops.rev_parse(upstream, SEED)
    ci_path = ".github/workflows/ci.yml"
    seed_ci = git(upstream, "show", f"{seed}:{ci_path}")
    common_ci = common(seed_ci)
    mammoth_ci = mammoth(common_ci)
    incumbent_actions = {}
    for action in ("ci-incr-build-cache-prepare", "ci-incr-build-cache-save"):
        rel = f".github/actions/{action}/action.yml"
        incumbent_actions[rel] = git(upstream, "show", f"{seed}:{rel}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        git(work, "init", "-q", "-b", "main")
        write_patch(work, {ci_path: seed_ci}, {ci_path: common_ci}, ROOT / "overlays/nessie/common/patches/01-ci.patch")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        git(work, "init", "-q", "-b", "main")
        write_patch(
            work,
            {ci_path: common_ci, **incumbent_actions},
            {ci_path: mammoth_ci, **{k: None for k in incumbent_actions}},
            ROOT / "overlays/nessie/mammoth/patches/01-ci.patch",
        )
    print("wrote overlays/nessie/{common,mammoth}/patches/01-ci.patch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
