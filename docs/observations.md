# Observations

What the experiment showed so far, newest first. **Observed** entries are backed by runs (links
may stop working when runs are deleted or expire); **expected** entries are design properties still
to be confirmed by evidence. Smoke-campaign observations don't count towards the scored comparison.

## Mammoth's partial saves let reruns get past Maven Central throttling (observed, 2026-11-27)

Scored seed, caches cleared just before. In both variants only `intTest` failed in attempt 1;
the other 11 jobs resolved the same Gradle plugins and succeeded.

| | `intTest` attempt 1 | `intTest` attempt 2 |
| --- | --- | --- |
| reference 46018806957 | failed after 14 s: could not resolve plugin artifact `com.gradle.develocity…:4.6.0` | failed after 11 s, same plugin |
| prototype 46018809569 | failed after 17 s: could not resolve plugin artifact `com.gradleup.nmcp.settings…:1.6.2` | past plugin resolution (Gradle still running after 9 min) |

Mechanism:

- Mammoth's store-cache runs with `always()`, so attempt 1 already merged what the 11 successful
  jobs had downloaded, plugins included. A rerun of the failed job restores that cache and doesn't
  need Maven Central for them.
- The incumbent saves only when all producer jobs succeed; its Store Cache was skipped in both
  attempts. On a cold cache, every rerun of the failed job downloads everything again and gets
  through only when Maven Central happens not to throttle.

One case per side; the reference's next rerun may succeed by chance. The effect doesn't show in
job timings (taken from first attempts) but in reruns per event and time until the run succeeds.
To watch in the scored campaign: reruns per variant after `maven-central-429` and infrastructure
failures.

## Incumbent: no cache save on a later attempt of the same commit (observed, 2026-11-27)

The incumbent's `gradle-home` cache key ends in the commit SHA, and `gradle/actions` doesn't save
on an exact key hit (GitHub cache entries can't be overwritten either). In reference run
45998965334 (`smoke/main-01`, head `0f545a8def`):

- attempt 1: Store Cache skipped, because `intTest Quarkus Server` failed (`ContainerFetchException`);
- attempt 2: Store Cache merged the 4 deltas and saved `gradle-home-v1|java-21|nessie-ci[ci]-0f545a8d…`
  (542 MiB);
- attempt 3 (the planned `code-checks` rerun): Store Cache restored that exact key and logged
  "Cache hit occurred on the cache key …, not saving cache". The new `code-checks` delta never
  reached the cache.

So once a commit has a cache entry, later attempts on it can't add to it. Not a defect of the replay
setup. It doesn't distort the scored comparison: there, the incumbent's Store Cache runs only in
the first fully successful attempt, and that attempt saves. (Of the artifacts that job downloads,
only `ci-gradle-caches-*-21` are cache deltas; reports and replay telemetry are ignored.)

## Mammoth reuses what failed and cancelled runs downloaded (observed, 2026-11-27)

The Mammoth store-cache job runs with `always()` on pushes to `main` (see DESIGN.md, "store-cache
difference"). Job [CI Test](https://github.com/snazy/nessie-mammoth-prototype/actions/runs/45989127716/job/140472190598)
of the prototype's smoke seed run restored every Mammoth partition (`current-lineage-hit`,
preparation 38 s), all written by two runs that did not succeed:

| Restored partition | Created (UTC) | Size | Written by |
| --- | --- | --- | --- |
| dependency-artifacts | 09:58:48 | 1.86 GB | a manual test run with 4 failed intTest jobs (MongoDB, see below) |
| wrapper-dists | 09:59:09 | 203 MB | same |
| transforms-metadata | 09:58:57 | 121 MB | same |
| kotlin-dsl, groovy-dsl, module-support | 09:58–09:59 | 45 MB, small | same |
| build-cache | 11:14:59 | 445 MB | cancelled seed run 45981942611 |
| dependency-metadata, generation record | 11:14 | 3.7 MB, 1.2 KB | same |

- After a failed run, the next run doesn't download the same dependencies again. The incumbent
  saves only when all producer jobs succeed, so it would have stored nothing after that run.
- `always()` also runs after a cancellation; the aggregator merged the deltas that were uploaded.
- Consequence: the smoke seed of the prototype started warm. The scored campaign starts after the
  `scored/clear-caches` gate, which deletes all caches.

## MongoDB test containers fail on the runners' kernel (observed, 2026-11-27)

"MongoDB cannot start: Linux kernel versions 6.19 and newer has a known incompatibility with this
version of MongoDB" ([SERVER-121912](https://jira.mongodb.org/browse/SERVER-121912)). It failed the
Mongo-based ITs in both variants (reference seed run 45897936513, the manual test run above), and
looked like test flakiness until the container log was visible. Fixed for the replay by the source
patch `02-mongodb-kernel-6.19.patch` (`GLIBC_TUNABLES=glibc.pthread.rseq=1`, as in Apache
Polaris); `01-container-startup-retry.patch` adds the container-state diagnostics. Confirmed: all
Mongo-based ITs passed in the first attempt of both smoke seed runs (reference 45989126099,
prototype 45991379878).

## No push run when `main` is force-pushed to an unrelated root commit (observed, 2026-11-27)

Force-pushing `main` from one replay seed (a root commit) to another root commit created no
workflow run, not even a pending one, in either repository; a commit on top did trigger. Moving
`main` from a shared root commit to its child seed triggers normally (runs 45981940560,
45981942611). Every replay history therefore starts at a shared, workflow-free root commit.

## Scheduled controller ticks rarely fire (observed, 2026-11-27)

The tick cron (every 15 minutes) fired once in the first ~14 hours after the controller was pushed
(03:40 UTC; it failed, most likely on the expected state-mismatch exception while the replay repos
were being reset; its log was not retrievable). A failed run doesn't disable a schedule, and the
workflow stayed `active`; the slots before that run didn't fire either. `REPLAY_PAUSED` doesn't
affect whether ticks start. Worked around since 12:00 UTC: an always-on machine dispatches the
ticks every 15 minutes (`tools/dispatch`); the cron stays as a backup.

## Mammoth 0.5.1: config-file values overridden by action defaults (observed, 2026-11-26)

`cache-gc-older-than-days: 2` from the config file did not apply: the action's input default (14)
won, because GitHub passes metadata defaults like explicit inputs. Fixed in Mammoth 0.6.0, which
the replay uses since 2026-11-27; 0.6.0 also stops counting failed Gradle workers towards GC sweep
coverage and lets the aggregator merge when a worker uploaded nothing.

## Early Gradle failures from Maven Central errors, on both sides (observed, 2026-11-26/27)

Three, then four Mammoth jobs of the first prototype seed run (2026-11-26) failed within ~20 s with
"could not resolve plugin artifact" for `com.gradle.develocity` / `com.gradleup.nmcp.settings`,
while no reference job did. `pluginManagement` resolves from Maven Central first, so this is
probably a non-404 response from Maven Central; Gradle shows the HTTP status only with `--info`.

The smoke seed runs (2026-11-27) showed it isn't Mammoth-specific:

- reference 45989126099, attempt 1: `code-checks` failed after 14 s with the same "could not resolve
  plugin artifact … com.gradle.develocity"; classified as infrastructure;
- prototype 45991379878, attempt 1: `int-test` failed after 16 s with three HTTP 429 responses from
  Maven Central; classified as `maven-central-429`.

Both passed on a manual rerun (attempt 2). So the failures are Maven Central throttling or errors
early in the build, hitting whichever jobs resolve first, on both sides. Both classes are rerun
automatically; in the scored campaign up to 5 times per run, with growing waits in between.

## Other

- GitHub's dependency submission creates `dynamic` runs (`update-pip-graph`) in both replay
  repositories; the controller ignores them.
