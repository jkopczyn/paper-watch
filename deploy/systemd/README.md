# Scheduling paper-watch with systemd (catch-up across reboots)

These user units run `paper-watch run` at **08:00** and **18:00** local time. Unlike
plain cron, the timer uses `Persistent=true`, so if the machine was **off or asleep**
when a run was due, it fires once on the next boot to catch up the missed digest. The
catch-up run **covers the whole gap**: `paper-watch run` widens its fetch window back to
the last completed run, so nothing is missed even if it was off across several runs.

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
systemctl --user start paper-watch.service        # run once, right now
journalctl --user -u paper-watch -n 50            # logs from the last runs
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
