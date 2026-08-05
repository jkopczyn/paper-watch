# Scheduling paper-watch with systemd (catch-up across reboots)

These user units tick `paper-watch run` **every 4 hours** (00, 04, 08, 12, 16, 20 local).
A tick is not a digest: most ticks only ingest, keeping the lossy sources (page-watch
link diffs, Slack history) covered. **When** a digest is mailed is decided by
`schedule:` in `config.yaml` — by default local **noon on Tuesday and Friday**, so
Friday's digest covers Wed–Fri and Tuesday's covers Sat–Tue.

The 4-hourly cadence is what makes delivery robust. A digest is "owed" until a send
actually succeeds, so:

- a failed noon send is retried at **16:00, 20:00, …** until one gets through;
- retries do not stop at midnight — an overdue digest keeps being owed;
- several missed deliveries **collapse into one email**, since each digest covers
  everything since the last *successful* send rather than a fixed window.

`Persistent=true` covers the machine being **off or asleep**: the timer fires once on
the next boot, and that one tick both re-widens the fetch window back to the last
completed run and delivers anything still owed.

To send off-schedule (e.g. right after fixing a broken SMTP password):

```bash
uv run paper-watch run --force-send
uv run paper-watch run --dry-run     # preview into out/ without sending or consuming
```

## Install

```bash
# 1. Link the units into the user systemd directory
mkdir -p ~/.config/systemd/user
ln -sf ~/Code/paper-watch/deploy/systemd/paper-watch.service ~/.config/systemd/user/
ln -sf ~/Code/paper-watch/deploy/systemd/paper-watch.timer   ~/.config/systemd/user/

# 2. Let user services run without an active login session, and survive reboots
sudo loginctl enable-linger "$USER"

# 3. Load and start the timer
systemctl --user daemon-reload
systemctl --user enable --now paper-watch.timer
```

## Verify / operate

```bash
systemctl --user list-timers paper-watch.timer   # next + last trigger
systemctl --user status paper-watch.timer
systemctl --user start paper-watch.service        # tick once, right now
journalctl --user -u paper-watch -n 50            # logs from the last ticks

# An active timer does NOT mean digests are going out. Check the service's exit
# status and the delivery watermark, not just the timer:
systemctl --user status paper-watch.service
sqlite3 ~/Code/paper-watch/paper_watch.db "select * from meta"
```

## Notes

- The service runs from `~/Code/paper-watch` (the main checkout, via the `%h` specifier)
  and calls uv by absolute path (`~/.local/bin/uv`) because systemd's PATH does not
  include it. Units use `%h` rather than a hardcoded home, so they work for any user.
- This **replaces** the old crontab line, which both lacked catch-up and pointed at a
  non-existent `/usr/bin/uv`.
- `Persistent=true` catches up *one* missed elapse on boot. That single fire is enough:
  `paper-watch run` fetches back to the last completed run, so the one catch-up covers
  every run missed while powered off — not just the most recent.
- Two watermarks live in the DB's `meta` table and mean different things.
  `last_run_at` moves on every real tick and bounds *ingestion*; `last_sent_at` moves
  only when an email is actually delivered, and is what decides whether a digest is
  still owed and how far back "new" reaches. A send that raises leaves `last_sent_at`
  alone on purpose — that is the retry.
- Changing `deliver_days` / `deliver_at` needs no systemd change; only the 4-hourly
  cadence lives in the timer. Keep the tick interval a divisor of the fallback spacing
  you want.
