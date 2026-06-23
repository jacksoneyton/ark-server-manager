# ARK Auto-Pause

The ARK server auto-pauses (`SIGSTOP`) when nobody is connected, so wild dinos
stop leveling/aging during idle stretches, and auto-resumes (`SIGCONT`) the
instant someone tries to join. This is handled by a standalone daemon
(`bin/ark_autopause.py`, run via the `ark-autopause` systemd service) — it is
**not** part of the Flask web app.

## Player-facing behavior

- **On join**: everyone online sees a broadcast explaining the feature and
  the default 30-minute idle threshold.
- **`!keepalive <minutes>`**: type this in normal in-game chat to make the
  server wait longer than the 30-minute default before pausing the *next*
  time it goes empty — useful for egg incubation, breeding/imprint timers,
  crop growth, etc. Capped at 1440 minutes (24h). This is a **one-time**
  override: once it's used (the server actually pauses, or someone logs back
  in first) it resets to the 30-minute default, so a forgotten long value
  can't permanently defeat auto-pause. If multiple players set conflicting
  values, the last one wins.

## Requirements

- RCON must be enabled (`RCONEnabled=True` in `GameUserSettings.ini`, toggle
  available on the **ARK Settings** page) — it's how the daemon reads player
  count, reads chat, and sends chat messages. The daemon reads
  `ServerAdminPassword`/`RCONPort` directly from `GameUserSettings.ini` on
  every check, so it always matches whatever the live server is actually
  configured with.
- If RCON is unreachable (disabled, wrong password, server still booting),
  the daemon logs a warning and falls back to pause-only behavior using the
  flat 30-minute default, with no chat features — it does not crash.

## Known trade-offs (by design, not bugs)

- **Manual Stop/Restart from the web UI while paused**: LGSM's configured
  stop mode for this server sends a graceful-stop signal that a frozen
  process can't react to until resumed, so expect it to sit through the
  existing graceful-stop timeout (tens of seconds) before falling through to
  a forced kill. Not a hang, just a delay.
- **Wake latency**: after a join attempt wakes the server, expect a few
  seconds before the resumed process actually answers — most game clients
  retry over that window, so it should resolve on its own.
- **`arkserver status` / `arkserver details`** still report the process as
  running (the PID exists) while paused — that's correct, not a fault.
- **Don't enable LGSM's own `monitor` cron** alongside this (it isn't enabled
  today). Its query-port health check would see a paused server as
  unresponsive and could fight the daemon by restarting it.
- A `SIGSTOP` can freeze the process mid-autosave. Accepted as low risk: a
  single `write()`/`fsync()` isn't corrupted by `SIGSTOP` (only userspace
  execution pauses, not in-flight syscalls).

## Outstanding security recommendation

This host currently has **no firewall enabled** (`ufw` is installed but
inactive), and `ServerAdminPassword` is identical to the public join
password (`ServerPassword`). That means RCON (port 27020/tcp), once enabled,
is reachable from the entire internet protected only by that shared
password. Recommend, whenever convenient:
- Firewalling port 27020 to `127.0.0.1` only (the daemon is the only thing
  that needs to reach it), and
- Setting `ServerAdminPassword` to something different from
  `ServerPassword` and not easily guessable.

Neither has been changed — left for you to action on your own schedule.

## Configuration

`main.conf`, `[autopause]` section:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `yes` | Master on/off switch. Flipping to `no` immediately resumes a paused server. |
| `default_idle_minutes` | `30` | Idle threshold used when no keep-alive override is pending. |
| `keepalive_max_minutes` | `1440` | Cap on `!keepalive <minutes>` (24h). |
| `poll_interval_running` | `20` | Seconds between player-count/chat checks while running. |
| `poll_interval_paused` | `5` | Seconds between wake-detection checks while paused. |

## Operating it

```
sudo systemctl status ark-autopause
sudo systemctl restart ark-autopause
journalctl -u ark-autopause   # or: tail -f logs/autopause.log
```
