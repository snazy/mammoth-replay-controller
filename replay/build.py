"""Builds deterministic replay commits and the frozen replay plan.

Each replay commit has the upstream commit's tree with `.github/` replaced by the variant's
frozen overlay. Identity and dates are fixed by the plan, so the same inputs always produce the
same commit SHAs. Messages carry no upstream body, issue references or mentions, so pushing them
cannot create cross-references, close issues or credit upstream authors.
"""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import gitops
from .gitops import git

PLAN_SCHEMA = 1
OVERLAY_ROOT = ".github"


class BuildError(RuntimeError):
    pass


# --- GitHub-style path filters ---------------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
                out[-1] = "(?:.*/)?"
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def is_ignored(path: str, paths_ignore: list[str]) -> bool:
    """Evaluates `paths-ignore` patterns in order; later `!` patterns re-include paths."""
    ignored = False
    for pat in paths_ignore:
        negate = pat.startswith("!")
        if glob_to_regex(pat[1:] if negate else pat).match(path):
            ignored = not negate
    return ignored


def triggers_push(changed: list[str], paths_ignore: list[str]) -> bool:
    return any(not is_ignored(p, paths_ignore) for p in changed)


# --- workflow job graph ---------------------------------------------------------------------


def parse_workflow_jobs(text: str) -> dict[str, dict]:
    """Job IDs with display name and `needs`, from a workflow file's top-level job layout."""
    jobs: dict[str, dict] = {}
    lines = text.splitlines()
    try:
        start = lines.index("jobs:") + 1
    except ValueError as e:
        raise BuildError("workflow has no top-level 'jobs:'") from e
    current = None
    in_needs = False
    for line in lines[start:]:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = {"name": current, "needs": []}
            in_needs = False
            continue
        if current is None:
            continue
        if re.match(r"^    \S", line):
            in_needs = False
            nm = re.match(r"^    name:\s*(.+?)\s*$", line)
            if nm:
                jobs[current]["name"] = nm.group(1).strip("'\"")
            nd = re.match(r"^    needs:\s*(.*)$", line)
            if nd:
                rest = nd.group(1).strip()
                if rest.startswith("["):
                    jobs[current]["needs"] = [x.strip() for x in rest.strip("[]").split(",") if x.strip()]
                elif rest:
                    jobs[current]["needs"] = [rest]
                else:
                    in_needs = True
            continue
        if in_needs:
            item = re.match(r"^      - ([A-Za-z0-9_-]+)\s*$", line)
            if item:
                jobs[current]["needs"].append(item.group(1))
            elif line.strip() and not line.strip().startswith("#"):
                in_needs = False
    return jobs


# --- overlays --------------------------------------------------------------------------------


def materialize_overlay(upstream: Path, seed: str, keep: list[str], layers: list[Path], dest: Path) -> None:
    """Writes the variant's `.github/` tree into dest: kept seed paths, then each layer's
    `files/` copied and `patches/*.patch` applied, in layer order."""
    for p in keep:
        if not (p == OVERLAY_ROOT or p.startswith(OVERLAY_ROOT + "/")):
            raise BuildError(f"keep path outside {OVERLAY_ROOT}/: {p}")
    archive = subprocess.run(
        ["git", "-C", str(upstream), "archive", "--format=tar", seed, "--", *keep],
        capture_output=True,
        env=gitops.isolated_env(),
        check=False,
        timeout=gitops.GIT_TIMEOUT_SECONDS,
    )
    if archive.returncode != 0:
        raise BuildError(f"git archive failed: {archive.stderr.decode(errors='replace')[-1000:]}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    for layer in layers:
        files = layer / "files"
        if files.is_dir():
            for src in sorted(files.rglob("*")):
                rel = src.relative_to(files)
                if not str(rel).startswith(OVERLAY_ROOT + "/") and str(rel) != OVERLAY_ROOT:
                    raise BuildError(f"overlay file outside {OVERLAY_ROOT}/: {layer.name}/files/{rel}")
                target = dest / rel
                if src.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
        patches = layer / "patches"
        if patches.is_dir():
            for patch in sorted(patches.glob("*.patch")):
                proc = subprocess.run(
                    ["git", "apply", "--whitespace=nowarn", str(patch.resolve())],
                    cwd=dest,
                    capture_output=True,
                    env=gitops.isolated_env(),
                    check=False,
                )
                if proc.returncode != 0:
                    raise BuildError(f"patch {layer.name}/{patch.name} failed: {proc.stderr.decode()[-1500:]}")
    for path in dest.rglob("*"):
        if path.is_file() and not str(path.relative_to(dest)).startswith(OVERLAY_ROOT + "/"):
            raise BuildError(f"overlay produced a file outside {OVERLAY_ROOT}/: {path.relative_to(dest)}")


def overlay_tree(repo: Path, overlay_dir: Path) -> str:
    """Hashes overlay_dir's `.github/` into repo's object store and returns that subtree's SHA."""
    git_dir = git(repo, "rev-parse", "--absolute-git-dir").strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(overlay_dir, f"--git-dir={git_dir}", "--work-tree=.", "add", "-A", "-f", ".", env=env)
        root = git(overlay_dir, f"--git-dir={git_dir}", "write-tree", env=env).strip()
    return git(repo, "rev-parse", f"{root}:{OVERLAY_ROOT}").strip()


def source_patch_paths(patch: Path) -> list[str]:
    out = subprocess.run(["git", "apply", "--numstat", "-z", str(patch)], capture_output=True, check=True, env=gitops.isolated_env()).stdout.decode()
    return [f.split("\t")[-1] for f in out.split("\0") if "\t" in f]


def check_source_patches(patches: list[Path]) -> None:
    for patch in patches:
        inside = [f for f in source_patch_paths(patch) if f == OVERLAY_ROOT or f.startswith(f"{OVERLAY_ROOT}/")]
        if inside:
            raise BuildError(f"source patch {patch.name} changes {OVERLAY_ROOT}/, which belongs to the overlays: {inside}")


def patched_tree(repo: Path, tree: str, patches: list[Path]) -> str:
    """Upstream tree with the source patches applied (outside .github/), for every variant alike."""
    if not patches:
        return tree
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git(repo, "read-tree", tree, env=env)
        for patch in patches:
            try:
                git(repo, "apply", "--cached", "--whitespace=nowarn", str(patch), env=env)
            except gitops.GitError as e:
                raise BuildError(f"source patch {patch.name} does not apply to tree {tree[:10]}: {e}") from None
        return git(repo, "write-tree", env=env).strip()


def replay_tree(repo: Path, upstream_tree: str, ovl_tree: str) -> str:
    entries = [e for e in gitops.ls_tree(repo, upstream_tree) if e[3] != OVERLAY_ROOT]
    entries.append(("040000", "tree", ovl_tree, OVERLAY_ROOT))
    return gitops.mktree(repo, entries)


def changed_paths(repo: Path, tree_a: str, tree_b: str) -> list[str]:
    out = git(repo, "diff-tree", "-r", "--no-renames", "--name-only", "-z", tree_a, tree_b)
    return [p for p in out.split("\0") if p]


# --- commits ---------------------------------------------------------------------------------


def sanitize_subject(subject: str) -> str:
    s = re.sub(r"(?<![\w/])#(\d+)", r"PR \1", subject)
    s = re.sub(r"\b[\w.-]+/[\w.-]+#(\d+)", r"PR \1", s)
    s = s.replace("@", "(at)")
    return s.strip()[:200]


def replay_message(subject: str, upstream_sha: str, campaign: str, event_id: str) -> str:
    return (
        f"{sanitize_subject(subject)}\n\n"
        f"Replay-Campaign: {campaign}\n"
        f"Replay-Event: {event_id}\n"
        f"Replay-Upstream: {upstream_sha}\n"
    )


def commit(repo: Path, tree: str, parent: str | None, message: str, date: str, identity: dict[str, str]) -> str:
    env = {
        "GIT_AUTHOR_NAME": identity["name"],
        "GIT_AUTHOR_EMAIL": identity["email"],
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": identity["name"],
        "GIT_COMMITTER_EMAIL": identity["email"],
        "GIT_COMMITTER_DATE": date,
    }
    args = ["commit-tree", "--no-gpg-sign", tree]
    if parent:
        args += ["-p", parent]
    return git(repo, *args, env=env, stdin=message.encode()).strip()


# --- plan ------------------------------------------------------------------------------------


def expand_events(profile: dict, upstream: Path) -> list[dict]:
    """Expands campaign event specs into a flat ordered list with full upstream SHAs."""
    events = []
    for campaign in profile["campaigns"]:
        cid = campaign["id"]
        for spec in campaign["events"]:
            if spec["type"] == "range":
                base = gitops.rev_parse(upstream, spec["after"])
                shas = gitops.first_parent_range(upstream, base, gitops.rev_parse(upstream, spec["to"]))
                pr_set = {gitops.rev_parse(upstream, p) for p in spec.get("pr", [])}
                missing = pr_set - set(shas)
                if missing:
                    raise BuildError(f"{cid}: PR commits not in range: {sorted(missing)}")
                width = max(2, len(str(len(shas))))
                for n, sha in enumerate(shas, 1):
                    num = str(n).zfill(width)
                    if sha in pr_set:
                        events.append({"campaign": cid, "id": f"pr-{num}", "type": "pr", "upstream": sha, "push_after": True})
                    events.append({"campaign": cid, "id": f"main-{num}", "type": "main", "upstream": sha})
            else:
                ev = {"campaign": cid, **spec}
                if "upstream" in ev:
                    ev["upstream"] = gitops.rev_parse(upstream, ev["upstream"])
                events.append(ev)
    keys = [f"{e['campaign']}/{e['id']}" for e in events]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise BuildError(f"duplicate event keys: {sorted(dupes)}")
    for e in events:
        if e["type"] not in ("seed", "main", "pr", "gate"):
            raise BuildError(f"unknown event type {e['type']!r} in {e['campaign']}/{e['id']}")
        if e["type"] == "pr" and e.get("push_after", False) is False and "upstream" not in e:
            raise BuildError(f"{e['campaign']}/{e['id']}: PR without upstream commit")
    return events


ROOT_README = """# Replay repository

Synthetic replay of {upstream} history for a CI caching experiment. The commits are generated and
pushed by a replay controller. This is not a fork: please don't open issues or pull requests.
"""


def root_commit(repo: Path, profile: dict, date: str) -> str:
    """The shared, workflow-free root of every variant's replay history.

    The seed is its child, so moving main from the root to the seed is an ordinary fast-forward
    push; GitHub starts no push run when main is force-pushed to an unrelated root commit.
    """
    blob = git(repo, "hash-object", "-w", "--stdin", stdin=ROOT_README.format(upstream=profile["upstream"]["repo"]).encode()).strip()
    tree = gitops.mktree(repo, [("100644", "blob", blob, "README.md")])
    return commit(repo, tree, None, "Initialize replay repository", date, profile["identity"])


def build_plan(profile: dict, upstream: Path, overlay_base: Path, repos: dict[str, Path]) -> dict:
    """Builds replay commits in each variant's local repo and returns the frozen plan.

    repos maps variant name to a local Git repository whose object store receives the commits.
    Upstream objects are fetched into it; nothing is pushed."""
    events = expand_events(profile, upstream)
    seeds = [e for e in events if e["type"] == "seed"]
    if not seeds:
        raise BuildError("plan has no seed event")
    seed_sha = seeds[0]["upstream"]
    paths_ignore = profile["workflow"].get("push_paths_ignore", [])
    identity = profile["identity"]
    ov = profile["overlay"]
    source_patches = [overlay_base / name for name in ov.get("source_patches", [])]
    check_source_patches(source_patches)

    plan_events = [{k: v for k, v in e.items()} | {"sha": {}} for e in events]
    dropped: dict[str, dict] = {}
    overlay_trees = {}
    tips = {}
    roots = {}
    root_date = gitops.commit_meta(upstream, seed_sha)["committer_date"]
    workflow_jobs: dict | None = None

    for variant, vcfg in profile["variants"].items():
        repo = repos[variant]
        git(repo, "fetch", "--no-tags", "--quiet", str(upstream), f"+{profile['upstream']['branch']}:refs/upstream/{profile['upstream']['branch']}")
        with tempfile.TemporaryDirectory() as tmp:
            layers = [overlay_base / name for name in [*ov.get("common_layers", []), *vcfg["overlay_layers"]]]
            materialize_overlay(upstream, seed_sha, ov["keep"], layers, Path(tmp))
            ovl = overlay_tree(repo, Path(tmp))
            wf = Path(tmp) / OVERLAY_ROOT / "workflows" / profile["workflow"]["file"]
            jobs = parse_workflow_jobs(wf.read_text()) if wf.exists() else {}
        overlay_trees[variant] = ovl
        if workflow_jobs is None:
            workflow_jobs = jobs
        elif jobs != workflow_jobs:
            raise BuildError(f"{variant}: workflow job names or needs differ from the other variant")

        roots[variant] = root_commit(repo, profile, root_date)
        tip: str | None = roots[variant]
        tip_tree: str | None = None  # the seed always triggers: compare against nothing, not the root's README
        pending_pr: dict | None = None
        for pe in plan_events:
            key = f"{pe['campaign']}/{pe['id']}"
            if pe["type"] == "gate":
                continue
            meta = gitops.commit_meta(upstream, pe["upstream"])
            tree = replay_tree(repo, patched_tree(repo, meta["tree"], source_patches), ovl)
            changed = changed_paths(repo, tip_tree, tree) if tip_tree else ["<root>"]
            if pe["type"] in ("seed", "main"):
                if not changed or (tip_tree and not triggers_push(changed, paths_ignore)):
                    reason = "no change outside .github/" if not changed else "all changes match push paths-ignore"
                    if pe["type"] == "seed":
                        raise BuildError(f"{key}: seed would not trigger CI ({reason})")
                    dropped[key] = {"campaign": pe["campaign"], "id": pe["id"], "upstream": pe["upstream"], "reason": reason}
                    continue
                if pending_pr and pending_pr["upstream"] == pe["upstream"]:
                    sha = pending_pr["sha"][variant]
                else:
                    sha = commit(repo, tree, tip, replay_message(meta["subject"], pe["upstream"], pe["campaign"], pe["id"]), meta["committer_date"], identity)
                pe["sha"][variant] = sha
                pending_pr = None
                tip, tip_tree = sha, tree
            else:  # pr
                if not changed:
                    dropped[key] = {"campaign": pe["campaign"], "id": pe["id"], "upstream": pe["upstream"], "reason": "no change outside .github/"}
                    continue
                sha = commit(repo, tree, tip, replay_message(meta["subject"], pe["upstream"], pe["campaign"], pe["id"]), meta["committer_date"], identity)
                pe["sha"][variant] = sha
                pe["branch"] = f"replay/{pe['campaign']}/{pe['id']}"
                pe["title"] = sanitize_subject(meta["subject"])
                pending_pr = pe if pe.get("push_after", False) else None
        tips[variant] = tip

    final_events = [pe for pe in plan_events if f"{pe['campaign']}/{pe['id']}" not in dropped]
    for pe in final_events:
        if pe["type"] == "pr" and pe.get("push_after"):
            nxt = next((x for x in final_events if x["campaign"] == pe["campaign"] and x["type"] == "main" and x["upstream"] == pe["upstream"]), None)
            if nxt is None or nxt["sha"] != pe["sha"]:
                raise BuildError(f"{pe['campaign']}/{pe['id']}: PR commit differs from its main push")

    if len(set(roots.values())) != 1:
        raise BuildError(f"root commits differ between variants: {roots}")

    staging = {}
    for variant in profile["variants"]:
        refs = {"refs/tags/replay-staging/main": tips[variant]}
        main_chain = set(git(repos[variant], "rev-list", tips[variant]).split())
        for pe in final_events:
            sha = pe["sha"].get(variant)
            if sha and sha not in main_chain:
                refs[f"refs/tags/replay-staging/{pe['campaign']}-{pe['id']}"] = sha
        staging[variant] = refs

    return {
        "schema": PLAN_SCHEMA,
        "profile": profile["name"],
        "upstream": {"repo": profile["upstream"]["repo"], "seed": seed_sha},
        "root": next(iter(roots.values())),
        "variants": {v: {"repo": c["repo"], "overlay_tree": overlay_trees[v]} for v, c in profile["variants"].items()},
        "source_patches": [{"path": name, "sha256": hashlib.sha256((overlay_base / name).read_bytes()).hexdigest()} for name in ov.get("source_patches", [])],
        "settings": profile["controller"],
        "campaign_settings": {c["id"]: c["settings"] for c in profile["campaigns"] if c.get("settings")},
        "workflow": {**profile["workflow"], "jobs": workflow_jobs},
        "jobs": profile["jobs"],
        "events": final_events,
        "dropped": list(dropped.values()),
        "staging_refs": staging,
    }


def write_staging_refs(plan: dict, repos: dict[str, Path]) -> None:
    """Points the local staging tags at the plan's commits (local only)."""
    for variant, refs in plan["staging_refs"].items():
        for ref, sha in refs.items():
            git(repos[variant], "update-ref", ref, sha)


def dump_plan(plan: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2, sort_keys=False) + "\n")
