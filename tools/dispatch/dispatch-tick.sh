#!/bin/sh
# Starts one controller tick: a workflow_dispatch of replay.yml with command=tick.
#
# GitHub's own schedule for replay.yml barely fires, so an always-on machine calls this every
# 15 minutes (see README.md next to this file). Only the trigger moves; the tick itself still runs
# on GitHub. Overlapping triggers are harmless: replay.yml runs one tick at a time.
#
# Environment:
#   REPLAY_DISPATCH_TOKEN   fine-grained token for the controller repository, Actions: read and write
#   REPLAY_CONTROLLER_REPO  optional, default snazy/mammoth-replay-controller
#   REPLAY_GITHUB_API       optional, default https://api.github.com (for tests)
#
# Needs only sh and curl; no tty. Exits non-zero when GitHub doesn't accept the dispatch.

set -eu

: "${REPLAY_DISPATCH_TOKEN:?REPLAY_DISPATCH_TOKEN is not set}"
repo="${REPLAY_CONTROLLER_REPO:-snazy/mammoth-replay-controller}"
api="${REPLAY_GITHUB_API:-https://api.github.com}"

body=$(mktemp)
trap 'rm -f "$body"' EXIT

# The token goes to curl through its config on stdin, so it doesn't appear in the process list.
code=$(
  printf 'header = "Authorization: Bearer %s"\n' "$REPLAY_DISPATCH_TOKEN" |
    curl --config - --silent --show-error --max-time 30 \
      --retry 3 --retry-delay 10 --retry-all-errors \
      --output "$body" --write-out '%{http_code}' \
      --request POST \
      --header 'Accept: application/vnd.github+json' \
      --header 'X-GitHub-Api-Version: 2022-11-28' \
      --header 'User-Agent: mammoth-replay-dispatch' \
      --data '{"ref":"main","inputs":{"command":"tick"}}' \
      "${api}/repos/${repo}/actions/workflows/replay.yml/dispatches"
) || code="curl failed"

if [ "$code" != "204" ] && [ "$code" != "200" ]; then
  echo "dispatch of replay.yml in ${repo} failed: HTTP ${code}: $(head -c 500 "$body")" >&2
  exit 1
fi
echo "dispatched a tick of replay.yml in ${repo}"
