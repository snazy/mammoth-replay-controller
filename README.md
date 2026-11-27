# Mammoth replay controller

Replays a stretch of upstream Nessie history into two public repositories and compares their CI:

- `snazy/nessie-mammoth-reference` keeps Nessie's incumbent distributed Gradle caching.
- `snazy/nessie-mammoth-prototype` replaces it with the Mammoth cache action (v0.6.0).

Both get identical telemetry. A scheduled workflow in this repository is the only driver; no local
machine is needed once the experiment runs. Launch steps and their approval scope are in
[docs/launch-plan.md](docs/launch-plan.md).
What the experiment showed so far is collected in [docs/observations.md](docs/observations.md).

## How it works

- `python3 -m replay build-plan` builds every replay commit locally and writes the frozen plan
  `plans/nessie.json`: upstream tree plus the variant's frozen `.github/`, fixed identity and dates,
  so SHAs are deterministic. Commit messages carry no upstream body, issue references or mentions.
  All commits are pushed once under `replay-staging/*` tags, which trigger no CI.
  Source patches (`overlay.source_patches` in the profile, outside `.github/`) are applied to every
  replayed upstream tree in both variants alike, e.g. a backported test-resilience fix.
  Each replay history starts at a shared root commit without workflows; `main` starts there, and
  the controller fast-forwards it to the seed (GitHub starts no push run when `main` is force-pushed
  to an unrelated root commit).
- [`.github/workflows/replay.yml`](.github/workflows/replay.yml) runs `python3 -m replay tick` every
  15 minutes. A tick reads both repos, collects evidence of finished CI attempts, decides, makes at
  most a few changes (move `main`, create a branch, open/close a PR, rerun failed jobs) and commits
  evidence, `status.md` and the report to the `data` branch.
- The controller holds no state of its own: progress is derived from `main`, the replay PRs and the
  CI runs. Each change is logged as an intent first; lost responses are reconciled on the next tick.
- Each event goes to both repos together. The next event starts only when both variants finished
  the current one, and at least `min_event_spacing_minutes` later: 4 h in the scored campaign,
  15 min in the smoke campaign (a campaign's `settings` override the default).

## Operating it

Repository settings of this controller repo:

| Setting | Meaning |
| --- | --- |
| secret `REPLAY_TOKEN` | Fine-grained token for the two replay repos: Contents, Workflows, Pull requests, Actions read/write. |
| variable `REPLAY_PAUSED` | `true` stops all mutations; evidence collection continues. |
| variable `REPLAY_RELEASED_GATES` | Comma-separated gates to release, e.g. `scored/clear-caches`. |

Current state: `status.md` on the `data` branch, or the job summary of the latest "Replay tick" run.
A failed tick run means the controller stopped mutations because of an exception; its summary
names the run and what to do.

- **Unrecognised CI failure**: the controller pauses and links the run. Inspect it; rerun the failed
  jobs in the GitHub UI (or `gh run rerun <id> --failed --repo <repo>`) to continue. Known
  patterns (Maven Central HTTP 429, infrastructure errors, configured flaky tests in
  `failure_patterns`) are rerun automatically, at most twice per run.
  A `store-cache` failure next to a failed producer job is a follow-on failure (Mammoth's aggregator
  runs after failed builds, too, and fails when a worker's delta is missing); the producers' failures
  decide.
- **Smoke run → scored run**: after the smoke campaign the controller waits at gate
  `scored/clear-caches`. Release it to delete all Actions caches and artifacts in both replay
  repos (after every smoke attempt is collected); the scored campaign then starts cold.
- **Stop**: set `REPLAY_PAUSED=true`, or disable the "Replay tick" workflow. Running CI is never
  cancelled.
- **Tick trigger**: GitHub's schedule barely fires for this repository, so an always-on machine
  starts the ticks with `workflow_dispatch`; see [tools/dispatch](tools/dispatch/README.md). The cron
  in `replay.yml` stays as a backup.
- **Manual tick or read-only checks**: run the "Replay tick" workflow manually, with `command`
  `tick` or `preflight`.

Offline, against a checkout of the `data` branch:

```shell
python3 -m replay status --data ../data/nessie
python3 -m replay report --plan plans/nessie.json --data ../data/nessie --out /tmp/report
python3 -m replay show-plan --plan plans/nessie.json
```

## Changing things during the experiment

Controller fixes are fine; collected evidence is immutable. Don't change `plans/nessie.json`,
the overlays or the profile while a campaign runs: the replay commits are already pushed and the
two variants must stay comparable. Regenerating the overlay patches:
`python3 -m tools.generate_nessie_overlays <nessie-checkout>`.

## Tests

```shell
python3 -m unittest discover -s tests -t .
```

The structural overlay tests need PyYAML and a Nessie checkout (`NESSIE_CHECKOUT`, default
`../../nessie`); they are skipped otherwise.
