# Observations

What the experiment showed so far, newest first. **Observed** entries are backed by runs (links
may stop working when runs are deleted or expire); **expected** entries are design properties still
to be confirmed by evidence. Smoke-campaign observations don't count towards the scored comparison.

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
Polaris); `01-container-startup-retry.patch` adds the container-state diagnostics.

## No push run when `main` is force-pushed to an unrelated root commit (observed, 2026-11-27)

Force-pushing `main` from one replay seed (a root commit) to another root commit created no
workflow run, not even a pending one, in either repository; a commit on top did trigger. Moving
`main` from a shared root commit to its child seed triggers normally (runs 45981940560,
45981942611). Every replay history therefore starts at a shared, workflow-free root commit.

## Scheduled controller ticks rarely fire (observed, 2026-11-27)

The tick cron (every 15 minutes) fired once in the first ~14 hours after the controller was pushed
(03:40 UTC; it failed, most likely on the expected state-mismatch exception while the replay repos
were being reset; its log was not retrievable). All progress so far came from manual ticks. Open:
whether GitHub's schedule settles for the new repository, or the controller needs another trigger.

## Mammoth 0.5.1: config-file values overridden by action defaults (observed, 2026-11-26)

`cache-gc-older-than-days: 2` from the config file did not apply: the action's input default (14)
won, because GitHub passes metadata defaults like explicit inputs. Fixed in Mammoth 0.6.0, which
the replay uses since 2026-11-27; 0.6.0 also stops counting failed Gradle workers towards GC sweep
coverage and lets the aggregator merge when a worker uploaded nothing.

## Gradle settings-plugin resolution failures, only on the Mammoth side (observed, 2026-11-26)

Three, then four Mammoth jobs of the first prototype seed run failed within ~20 s with "could not
resolve plugin artifact" for `com.gradle.develocity` / `com.gradleup.nmcp.settings`; no reference
job did. `pluginManagement` resolves from Maven Central first, so this is probably a non-404
response from Maven Central; Gradle shows the HTTP status only with `--info`. Mammoth changes no
Gradle networking before the build, so the asymmetry is unexplained. Classified as infrastructure
(rerun) since then. To watch: whether it recurs, and whether only on one side.

## Other

- GitHub's dependency submission creates `dynamic` runs (`update-pip-graph`) in both replay
  repositories; the controller ignores them.
