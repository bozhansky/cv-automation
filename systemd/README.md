# systemd units for ApplyPilot

These user-mode systemd unit files replace the Hermes cron jobs that were
broken by an import error in Hermes's tool backend.

## Install on a fresh machine

```bash
# 1. Copy the unit files into your user systemd dir
mkdir -p ~/.config/systemd/user
cp systemd/applypilot-*.{service,timer} ~/.config/systemd/user/

# 2. Make sure the wrapper scripts exist where the units expect them
#    (these come from your existing install at ~/.hermes/scripts/)
ls -la ~/.hermes/scripts/applypilot_*.sh

# 3. Reload systemd, enable, and start the timers
systemctl --user daemon-reload
systemctl --user enable --now applypilot-discover.timer
systemctl --user enable --now applypilot-daily.timer
systemctl --user enable --now applypilot-weekly-purge.timer

# 4. Verify
systemctl --user list-timers 'applypilot*'
```

## Why user-mode and not system?

User-mode timers run as your user (no `sudo` needed) and read `~/.applypilot/`
and `~/.hermes/scripts/` directly. The user has `Linger=yes` so timers fire
even when you're not logged in.

If `Linger=no` on your system, enable it once with:
```bash
sudo loginctl enable-linger $USER
```

## Manual trigger (debugging)

```bash
# Run a service right now without waiting for the schedule
systemctl --user start applypilot-discover.service

# Watch the live journal
journalctl --user -u applypilot-discover.service -f

# Run the wrapper directly (skip systemd entirely)
bash ~/.hermes/scripts/applypilot_discover.sh
```

## Disable / re-enable

```bash
# Stop a timer and prevent it from auto-starting on next login
systemctl --user disable --now applypilot-daily.timer

# Re-enable
systemctl --user enable --now applypilot-daily.timer
```

## Why not Hermes cron?

The Hermes "cron jobs" are LLM agent sessions that read a prompt and use
tools. When the agent tried to invoke the `terminal` tool, an import error
(`cannot import name 'nous_tool_gateway_unavailable_message' from
'tools.tool_backend_helpers'`) crashed the agent before it could run the
wrapper script. systemd timers bypass the agent entirely — they just exec
the bash wrapper directly, so there's no LLM/tool surface to break.
