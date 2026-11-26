"""Read-only launch checks. Prints OK/WARN/FAIL lines; changes nothing."""

MAMMOTH_REPO = "buildish-tooling/buildish-mammoth-cache"
MAMMOTH_COMMIT = "ef15e2461c37cbfbb0ed915f015f50bec2a7f122"


def preflight(plan: dict, api) -> list[tuple[str, str]]:
    out = []
    chain = {}
    for e in plan["events"]:
        if e["type"] in ("seed", "main"):
            for v, s in e["sha"].items():
                chain.setdefault(v, set()).add(s)
    for v, cfg in plan["variants"].items():
        repo = cfg["repo"]
        info = api.get_repo(repo)
        if info is None:
            out.append(("FAIL", f"{v}: {repo} not found or not accessible with this token"))
            continue
        out.append(("OK" if info.get("visibility") == "public" else "FAIL", f"{v}: {repo} visibility {info.get('visibility')}"))
        if info.get("archived"):
            out.append(("FAIL", f"{v}: {repo} is archived"))
        perms = info.get("permissions") or {}
        out.append(("OK" if perms.get("push") else "FAIL", f"{v}: token push permission {perms.get('push')}"))
        for feature in ("has_issues", "has_wiki", "has_projects"):
            if info.get(feature):
                out.append(("WARN", f"{v}: {feature} is enabled (not needed for the experiment)"))
        tags = api.matching_refs(repo, "tags/replay-staging")
        for ref, sha in plan["staging_refs"][v].items():
            out.append(("OK" if tags.get(ref) == sha else "FAIL", f"{v}: {ref} {'matches the plan' if tags.get(ref) == sha else f'is {tags.get(ref)}, plan expects {sha[:10]}'}"))
        main = api.get_ref(repo, "heads/main")
        if main is None:
            out.append(("OK", f"{v}: main does not exist yet (the controller creates it with the seed)"))
        else:
            out.append(("OK" if main in chain[v] else "FAIL", f"{v}: main at {main[:10]} {'is a plan commit' if main in chain[v] else 'is NOT a plan commit'}"))
        extra = [r for r in api.matching_refs(repo, "heads") if r != "refs/heads/main" and not r.startswith("refs/heads/replay/")]
        if extra:
            out.append(("WARN", f"{v}: unexpected branches: {', '.join(extra)}"))
    out.append(("OK" if api.commit_exists(MAMMOTH_REPO, MAMMOTH_COMMIT) else "FAIL", f"Mammoth action commit {MAMMOTH_COMMIT[:10]} in {MAMMOTH_REPO}"))
    return out
