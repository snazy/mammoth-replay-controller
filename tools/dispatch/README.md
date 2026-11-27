# External tick trigger

GitHub's schedule for `replay.yml` barely fires (see `docs/observations.md`), so an always-on
machine starts the ticks instead: every 15 minutes it sends a `workflow_dispatch` of `replay.yml`
with `command=tick`. Only the trigger moves; ticks still run on GitHub, and `REPLAY_PAUSED` still
stops their mutations. The cron in `replay.yml` stays as a backup; overlapping triggers are
harmless because `replay.yml` runs one tick at a time.

`dispatch-tick.sh` needs only `sh` and `curl`, no tty and no `gh`.

## Token

A separate fine-grained token, not the controller's `REPLAY_TOKEN`:

- Resource owner `snazy`; repository access: only `mammoth-replay-controller`
- Permissions: Actions: read and write (Metadata read is implied)
- Expiration: about 3 weeks

## Install (Ubuntu, systemd)

```shell
sudo install -m 755 dispatch-tick.sh /usr/local/bin/mammoth-replay-dispatch-tick
sudo install -d -m 700 /etc/mammoth-replay
sudo sh -c 'umask 077; printf "REPLAY_DISPATCH_TOKEN=%s\n" "<token>" > /etc/mammoth-replay/dispatch.env'
sudo install -m 644 mammoth-replay-dispatch.service mammoth-replay-dispatch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start mammoth-replay-dispatch.service      # one tick now, to test
sudo systemctl enable --now mammoth-replay-dispatch.timer
```

To avoid the token in the shell history, write `dispatch.env` with an editor (`sudo -e`) instead.

Check:

```shell
systemctl list-timers mammoth-replay-dispatch.timer
journalctl -u mammoth-replay-dispatch.service --since today
```

Stop: `sudo systemctl disable --now mammoth-replay-dispatch.timer`. Remove: also delete the two
unit files, `/usr/local/bin/mammoth-replay-dispatch-tick` and `/etc/mammoth-replay/`, and revoke
the token.
