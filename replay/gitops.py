"""Thin, deterministic wrappers around the git CLI."""

import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 600


class GitError(RuntimeError):
    pass


def isolated_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment that ignores user/system git config, so results don't depend on the host."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def git(repo: Path, *args: str, env: dict[str, str] | None = None, stdin: bytes | None = None) -> str:
    cmd = ["git", "-C", str(repo), *args]
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            env=isolated_env(env),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise GitError(f"timeout: {' '.join(cmd)}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()[-2000:]
        raise GitError(f"{' '.join(cmd)} failed ({proc.returncode}): {err}")
    return proc.stdout.decode()


def rev_parse(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def tree_of(repo: Path, rev: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{rev}^{{tree}}").strip()


def ls_tree(repo: Path, tree: str) -> list[tuple[str, str, str, str]]:
    """Top-level entries as (mode, type, sha, name)."""
    out = git(repo, "ls-tree", "-z", tree)
    entries = []
    for rec in out.split("\0"):
        if not rec:
            continue
        meta, name = rec.split("\t", 1)
        mode, typ, sha = meta.split(" ")
        entries.append((mode, typ, sha, name))
    return entries


def mktree(repo: Path, entries: list[tuple[str, str, str, str]]) -> str:
    data = "".join(f"{m} {t} {s}\t{n}\0" for m, t, s, n in entries).encode()
    return git(repo, "mktree", "-z", stdin=data).strip()


def commit_meta(repo: Path, rev: str) -> dict[str, str]:
    fmt = "%H%x00%T%x00%aI%x00%cI%x00%s"
    sha, tree, adate, cdate, subject = git(repo, "show", "-s", f"--format={fmt}", rev).rstrip("\n").split("\0")
    return {"sha": sha, "tree": tree, "author_date": adate, "committer_date": cdate, "subject": subject}


def first_parent_range(repo: Path, exclusive_base: str, inclusive_end: str) -> list[str]:
    out = git(repo, "rev-list", "--first-parent", "--reverse", f"{exclusive_base}..{inclusive_end}")
    return [line for line in out.splitlines() if line]
