# Launch plan

Every step below that changes GitHub state needs explicit approval, batch by batch. Nothing here
touches `projectnessie/nessie` or any other upstream repository.

## Scope

| Item | Value |
| --- | --- |
| Repositories (new, public, personal account) | `snazy/mammoth-replay-controller`, `snazy/nessie-mammoth-reference`, `snazy/nessie-mammoth-prototype` |
| Pushed by you/us once | Controller `main` and `data` branches; in each replay repo the two `replay-staging/*` tags (about 9 MB each). Tag pushes trigger no CI. |
| Pushed later by the controller only | Replay repos: `main` (moved through 40 plan commits), 7 branches `replay/<campaign>/pr-NN`, 7 PRs (opened and closed, never merged) |
| CI runs per replay repo | Smoke: 3 runs plus 1 job rerun. Scored: 44 runs (37 `main` pushes, 6 PRs, 1 seed). Automatic reruns of failed jobs: at most 2 per run. |
| Deletions | Once, at gate `scored/clear-caches` after you release it: all Actions caches and artifacts in both replay repos |
| Duration | Smoke about 1 day, then about 9 days for the scored campaign (4 h spacing plus CI time per event) |
| Cost | Public repositories on standard runners: no Actions minutes billed. Controller ticks: about 1–2 min every 15 min. |
| Limits | At most 6 mutations per tick; no force-push; runs are never cancelled; unrecognised failures pause the replay |
| Who can open issues and PRs | Collaborators only (repository settings), in all three repositories. No collaborators are invited, so effectively only the owner and the controller's token. |

## Batch A: create and seed (no CI runs)

```shell
B=/home/snazy/devel/projectnessie/nessie/mammoth-prototype
GH_CRED=(-c credential.helper= -c 'credential.helper=!gh auth git-credential')

# 1. Repositories (empty; nothing pushed yet)
gh repo create snazy/mammoth-replay-controller --public --disable-wiki \
  --description "Controller for the Mammoth vs incumbent Gradle caching replay experiment"
for r in nessie-mammoth-reference nessie-mammoth-prototype; do
  gh repo create snazy/$r --public --disable-wiki \
    --description "Mammoth caching replay experiment (synthetic replay of projectnessie/nessie history)"
  gh repo edit snazy/$r --enable-projects=false
done
```

Before anything is pushed, **you** restrict issue and PR creation in the GitHub UI, for each of the
three repositories, under Settings > General > Features:

- **Pull requests**: select **Collaborators only**
  ([docs](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/disabling-pull-requests)).
- **Issues**: keep the checkbox selected, and set **Creation allowed by: Collaborators only**
  ([changelog](https://github.blog/changelog/2026-06-29-restrict-issue-creation-to-collaborators-only/)).

GitHub documents both settings only for the UI (no REST API or `gh` field is documented), so
they're a manual step, and preflight can't verify them. The controller opens PRs with your
fine-grained token, i.e. as the repository owner; the smoke run confirms that the restriction
doesn't block it.

```shell
# 2. Controller: main and an empty data branch
cd $B/mammoth-replay-controller
git remote add origin https://github.com/snazy/mammoth-replay-controller.git
git "${GH_CRED[@]}" push origin main
tree=$(printf 'Replay evidence; written by the Replay tick workflow.\n' | git hash-object -w --stdin \
  | xargs -I{} printf '100644 blob {}\tREADME.md\n' | git mktree)
git update-ref refs/heads/data "$(git commit-tree "$tree" -m 'Initialize data branch')"
git "${GH_CRED[@]}" push origin data

# 3. Replay repos: staging tags only
for r in nessie-mammoth-reference nessie-mammoth-prototype; do
  git -C $B/$r remote add origin https://github.com/snazy/$r.git
  git -C $B/$r "${GH_CRED[@]}" push origin 'refs/tags/replay-staging/*:refs/tags/replay-staging/*'
done

# 4. Controller settings (paused)
gh variable set REPLAY_PAUSED --body true --repo snazy/mammoth-replay-controller
```

Then **you** create the fine-grained token in the GitHub UI:

- Resource owner `snazy`; repository access: only `nessie-mammoth-reference` and `nessie-mammoth-prototype`
- Permissions: Contents, Workflows, Pull requests, Actions: read and write (Metadata read is implied)
- Expiration: about 3 weeks

and store it yourself so it never passes through the agent:

```shell
gh secret set REPLAY_TOKEN --repo snazy/mammoth-replay-controller   # paste the token at the prompt
```

Read-only checks afterwards:

```shell
for r in nessie-mammoth-reference nessie-mammoth-prototype; do
  gh api repos/snazy/$r/actions/permissions/workflow   # expect default_workflow_permissions: read
done
gh workflow run replay.yml --repo snazy/mammoth-replay-controller -f command=preflight
```

The scheduled tick starts as soon as the controller's `main` is pushed. While paused it only reads
and commits status to the `data` branch.

## Batch B: smoke campaign

```shell
gh variable set REPLAY_PAUSED --body false --repo snazy/mammoth-replay-controller
```

The controller then, in both replay repos:

1. creates `main` at the smoke seed: first cold CI run;
2. moves `main` to `smoke/main-01`, then reruns `code-checks` once (its dependents rerun too);
3. opens and closes the PR `replay/smoke/pr-02`;
4. stops at gate `scored/clear-caches`.

Review of the smoke data before Batch C:

- the intended jobs ran in both repos and nothing published (site deploy skipped, no secrets)
- telemetry artifacts and task outcomes collected; scan URLs found
- post-step names match `cache_steps` in the profile (cache-time metric)
- `started_at` versus `created_at` plausibly excludes queue time
- the Mammoth aggregator published on `main` and the PR run stayed read-only
- the default branch of each replay repo is `main`
- the controller's PRs were created despite the collaborators-only PR setting, and both settings
  are still shown as "Collaborators only" in all three repositories

## Batch C: scored campaign

```shell
gh variable set REPLAY_RELEASED_GATES --body scored/clear-caches --repo snazy/mammoth-replay-controller
```

The controller deletes all caches and artifacts in both replay repos (once every smoke attempt is
collected), then replays the scored campaign starting from a cold seed. About 9 days.

## Stop and rollback

- Pause: `gh variable set REPLAY_PAUSED --body true --repo snazy/mammoth-replay-controller`
- Stop the schedule: `gh workflow disable replay.yml --repo snazy/mammoth-replay-controller`
- Running CI is never cancelled. Repos, PRs, evidence and artifacts are not deleted by any rollback
  step; deleting the repositories afterwards is a separate decision.
- Revoke the fine-grained token in the GitHub UI when the experiment ends.
