"""Command line entry point: `python3 -m replay <command> ...`."""

import argparse
import json
import sys
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
    args = p.parse_args(argv)
    return args.func(args)
