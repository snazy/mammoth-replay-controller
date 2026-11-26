"""Command line entry point: `python3 -m replay <command> ...`."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import build

ROOT = Path(__file__).resolve().parent.parent


def cmd_build_plan(args: argparse.Namespace) -> int:
    profile = json.loads(Path(args.profile).read_text())
    repos = {}
    for spec in args.repo:
        variant, _, path = spec.partition("=")
        repos[variant] = Path(path).resolve()
    missing = set(profile["variants"]) - set(repos)
    if missing:
        print(f"missing --repo for variants: {sorted(missing)}", file=sys.stderr)
        return 2
    plan = build.build_plan(profile, Path(args.upstream).resolve(), ROOT / "overlays", repos)
    build.dump_plan(plan, Path(args.out))
    if args.write_staging_refs:
        build.write_staging_refs(plan, repos)
    print_plan(plan)
    return 0


def print_plan(plan: dict) -> None:
    print(f"plan {plan['profile']} (schema {plan['schema']}), seed {plan['upstream']['seed'][:10]}")
    for v, cfg in plan["variants"].items():
        print(f"  {v}: {cfg['repo']} overlay {cfg['overlay_tree'][:10]}")
    for e in plan["events"]:
        shas = " ".join(f"{v}={s[:10]}" for v, s in e.get("sha", {}).items())
        extra = e.get("branch") or e.get("action") or ""
        if e.get("rerun_job"):
            extra = f"rerun job {e['rerun_job']}"
        print(f"  {e['campaign']:>6}/{e['id']:<13} {e['type']:<5} {e.get('upstream', '')[:10]:<10} {shas} {extra}")
    for d in plan["dropped"]:
        print(f"  dropped {d['campaign']}/{d['id']} {d['upstream'][:10]}: {d['reason']}")
    for v, refs in plan["staging_refs"].items():
        for ref, sha in refs.items():
            print(f"  staging {v} {ref} -> {sha[:10]}")


def cmd_tick(args: argparse.Namespace) -> int:
    from .controller import load_plan, tick
    from .decide import Env
    from .github import GitHubApi

    token = os.environ.get("REPLAY_GITHUB_TOKEN")
    if not token:
        print("REPLAY_GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    env = Env(
        now=datetime.now(timezone.utc),
        paused=os.environ.get("REPLAY_PAUSED", "").strip().lower() in ("1", "true", "yes"),
        released_gates=frozenset(g.strip() for g in os.environ.get("REPLAY_RELEASED_GATES", "").split(",") if g.strip()),
    )
    d, notes = tick(load_plan(Path(args.plan)), GitHubApi(token), Path(args.data), env)
    print((Path(args.data) / "status.md").read_text())
    return 1 if d.exceptions else 0


def cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.data) / "status.md"
    print(path.read_text() if path.exists() else "no status yet")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from .controller import load_plan
    from .report import write_report

    summary = write_report(load_plan(Path(args.plan)), Path(args.data), Path(args.out))
    print(json.dumps(summary, indent=2))
    return 0


def cmd_show_plan(args: argparse.Namespace) -> int:
    from .controller import load_plan

    plan = load_plan(Path(args.plan))
    print_plan(plan)
    print()
    print("Remote mutations per variant, in order (each waits for both variants' CI and the spacing):")
    for e in plan["events"]:
        key = f"{e['campaign']}/{e['id']}"
        if e["type"] == "gate":
            print(f"  {key}: wait for release, then delete all Actions caches and artifacts")
        elif e["type"] in ("seed", "main"):
            extra = f", then rerun job {e['rerun_job']}" if e.get("rerun_job") else ""
            print(f"  {key}: move main to the replay commit{extra}")
        else:
            print(f"  {key}: create branch {e['branch']}, open PR against main, close it after CI")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="replay")
    sub = p.add_subparsers(dest="cmd", required=True)
    bp = sub.add_parser("build-plan", help="build replay commits locally and write the frozen plan")
    bp.add_argument("--profile", required=True)
    bp.add_argument("--upstream", required=True, help="local clone of the upstream repository")
    bp.add_argument("--repo", action="append", default=[], metavar="VARIANT=PATH", help="local repo receiving the variant's commits")
    bp.add_argument("--out", required=True)
    bp.add_argument("--write-staging-refs", action="store_true", help="point local staging tags at the plan's commits")
    bp.set_defaults(func=cmd_build_plan)
    tp = sub.add_parser("tick", help="one controller tick against GitHub (needs REPLAY_GITHUB_TOKEN)")
    tp.add_argument("--plan", required=True)
    tp.add_argument("--data", required=True)
    tp.set_defaults(func=cmd_tick)
    sp = sub.add_parser("status", help="print the last status (offline)")
    sp.add_argument("--data", required=True)
    sp.set_defaults(func=cmd_status)
    rp = sub.add_parser("report", help="write the report from collected evidence (offline)")
    rp.add_argument("--plan", required=True)
    rp.add_argument("--data", required=True)
    rp.add_argument("--out", required=True)
    rp.set_defaults(func=cmd_report)
    pp = sub.add_parser("show-plan", help="print the plan and its mutation sequence (offline)")
    pp.add_argument("--plan", required=True)
    pp.set_defaults(func=cmd_show_plan)
    args = p.parse_args(argv)
    return args.func(args)
