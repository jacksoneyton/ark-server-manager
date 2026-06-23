#!/usr/bin/env python3
"""
ark_autopause.py
-----------------
Standalone daemon (run via the ark-autopause systemd unit) that freezes the
ARK ShooterGameServer process with SIGSTOP when nobody is connected, and
resumes it with SIGCONT the instant someone tries to join. This stops wild
dinos from leveling/aging during multi-day idle stretches without losing
world state or requiring a slow cold restart.

Deliberately has zero dependency on the Flask app package: importing
anything under `app.*` runs `app/__init__.py`, which hard-requires
SECRET_KEY/.secret and boots the whole extensions stack (SQLAlchemy,
login_manager, migrate, swagger) -- unnecessary weight and fragile coupling
for a daemon that has nothing to do with Flask. Config/ini reading is
reimplemented here with plain configparser instead.

State machine:
  RUNNING -> poll player count via RCON every poll_interval_running seconds.
             Idle (0 players) for >= the effective idle threshold -> SIGSTOP.
  PAUSED  -> poll the kernel UDP receive queue for the game port every
             poll_interval_paused seconds. Any queued bytes (a join attempt
             arrived while frozen -- the kernel still buffers it) -> SIGCONT.

Players can extend how long the server stays up after everyone logs off by
typing "!keepalive <minutes>" in chat (read via RCON's GetChat), capped at
keepalive_max_minutes. This is a one-time override: it applies to the next
idle period only, then resets to default_idle_minutes.

RCON is required for player-count/chat features. If it's unavailable, the
daemon falls back to pause-only behavior using default_idle_minutes with no
chat features, rather than crashing.
"""

import configparser
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time

import psutil

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
WEBLGSM_DIR = os.path.dirname(BIN_DIR)
sys.path.insert(0, BIN_DIR)

from ark_rcon import ArkRcon, RconError  # noqa: E402

DATABASE_PATH = os.path.join(WEBLGSM_DIR, "app", "database.db")
MAIN_CONF_PATH = os.path.join(WEBLGSM_DIR, "main.conf")
LOG_PATH = os.path.join(WEBLGSM_DIR, "logs", "autopause.log")
STATE_PATH = os.path.join(WEBLGSM_DIR, "logs", "autopause_state.json")

# LinuxGSM always places game files under serverfiles/ within install_path.
ARK_CFG_RELATIVE = os.path.join(
    "serverfiles", "ShooterGame", "Saved", "Config", "LinuxServer"
)
ARK_PROC_NAME = "ShooterGameServer"

AUTOPAUSE_DEFAULTS = {
    "enabled": "yes",
    "default_idle_minutes": "30",
    "keepalive_max_minutes": "1440",
    "poll_interval_running": "20",
    "poll_interval_paused": "5",
}

JOIN_MESSAGE = (
    "Welcome! This server auto-pauses after {idle} min with nobody online "
    "(dinos don't age while paused). Leaving and want it to keep running "
    "longer -- for egg hatching, breeding, etc? Type '!keepalive <minutes>' "
    "in chat before you go (max {cap} min)."
)

KEEPALIVE_RE = re.compile(r"!keepalive\s+(\d+)", re.IGNORECASE)
WIPEDINOS_RE = re.compile(r"!wipedinos", re.IGNORECASE)
PLAYER_LINE_RE = re.compile(r"^\s*\d+\.\s*(.+?),\s*(\S+)\s*$")

RUNNING = "RUNNING"
PAUSED = "PAUSED"

logger = logging.getLogger("ark_autopause")


def ark_cfg_dir(install_path):
    """Return the LinuxServer config directory for a given install_path."""
    return os.path.join(install_path, ARK_CFG_RELATIVE)


def setup_logging():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def load_autopause_config():
    """Read the [autopause] section of main.conf with defaults as fallback."""
    parser = configparser.ConfigParser()
    parser.read(MAIN_CONF_PATH)
    section = dict(AUTOPAUSE_DEFAULTS)
    if parser.has_section("autopause"):
        section.update(parser["autopause"])
    return {
        "enabled": section["enabled"].strip().lower() in ("yes", "true", "1", "on"),
        "default_idle_minutes": int(section["default_idle_minutes"]),
        "keepalive_max_minutes": int(section["keepalive_max_minutes"]),
        "poll_interval_running": int(section["poll_interval_running"]),
        "poll_interval_paused": int(section["poll_interval_paused"]),
    }


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pending_keepalive_minutes": None, "set_by": None, "set_at": None}


def save_state(state):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as exc:
        logger.warning("Could not persist state: %s", exc)


def find_ark_install_path():
    """Return the install_path of the first ARK GameServer found in the DB."""
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        rows = conn.execute("SELECT install_path FROM game_server").fetchall()
    finally:
        conn.close()
    for (install_path,) in rows:
        if os.path.isdir(ark_cfg_dir(install_path)):
            return install_path
    return None


def find_ark_process():
    """
    Return the psutil.Process actually executing ShooterGameServer, or None.

    LGSM launches it as `tmux ... new-session ... ./ShooterGameServer ...`,
    so the tmux wrapper's cmdline also *contains* "ShooterGameServer" as a
    substring of the args it was told to exec. Matching on cmdline[0]'s
    basename (the real argv[0] of each process) instead of scanning every
    arg avoids ever pausing the tmux wrapper instead of the game binary.
    """
    for proc in psutil.process_iter(["cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if cmdline and os.path.basename(cmdline[0]) == ARK_PROC_NAME:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def parse_ports(cmdline):
    """Extract -Port= and -QueryPort= from the process cmdline, with fallbacks."""
    game_port, query_port = 7777, 27015
    for arg in cmdline:
        m = re.match(r"-Port=(\d+)", arg, re.IGNORECASE)
        if m:
            game_port = int(m.group(1))
        m = re.match(r"-QueryPort=(\d+)", arg, re.IGNORECASE)
        if m:
            query_port = int(m.group(1))
    return game_port, query_port


def proc_stat_char(pid):
    """Return the single-character process state from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            raw = f.read()
    except OSError:
        return None
    # Field 3 is the state char, but field 2 (comm) is parenthesized and may
    # itself contain spaces/parens, so split after the last ')'.
    after_comm = raw.rsplit(")", 1)[-1].split()
    return after_comm[0] if after_comm else None


def udp_recv_queue(port):
    """Return the Recv-Q byte count for the IPv4 UDP socket bound to *port*."""
    target_hex = f"{port:04X}"
    try:
        with open("/proc/net/udp") as f:
            lines = f.readlines()[1:]
    except OSError:
        return 0
    for line in lines:
        fields = line.split()
        if len(fields) < 5:
            continue
        local_addr = fields[1]
        if ":" not in local_addr:
            continue
        if local_addr.split(":")[1].upper() != target_hex:
            continue
        queues = fields[4]
        if ":" not in queues:
            continue
        try:
            return int(queues.split(":")[1], 16)
        except ValueError:
            return 0
    return 0


def rcon_credentials(install_path):
    """Read RCON enablement/port/password from GameUserSettings.ini."""
    gus_path = os.path.join(ark_cfg_dir(install_path), "GameUserSettings.ini")
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(gus_path)
    except configparser.Error as exc:
        logger.warning("Could not parse %s: %s", gus_path, exc)
        return False, 27020, ""
    if not parser.has_section("ServerSettings"):
        return False, 27020, ""
    section = parser["ServerSettings"]
    enabled = section.get("RCONEnabled", "False").strip().lower() in (
        "true", "1", "yes",
    )
    port = int(section.get("RCONPort", "27020"))
    password = section.get("ServerAdminPassword", "")
    return enabled, port, password


def parse_player_lines(list_players_response):
    """Parse RCON ListPlayers output into a set of player names."""
    names = set()
    for line in list_players_response.splitlines():
        if not line.strip() or "no players connected" in line.lower():
            continue
        m = PLAYER_LINE_RE.match(line)
        if m:
            names.add(m.group(1).strip())
    return names


def parse_chat_sender(line):
    """Best-effort sender name extraction from a GetChat line of unknown format."""
    if ":" in line:
        return line.split(":", 1)[0].strip()
    return "unknown"


class AutopauseDaemon:
    def __init__(self):
        self.install_path = None
        self.state = load_state()
        self.last_active_ts = time.time()
        self.seen_players = set()
        self.mode = RUNNING
        self.running = True

    def handle_signal(self, signum, _frame):
        logger.info("Received signal %s, shutting down.", signum)
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

        logger.info("ark_autopause starting up.")
        while self.running:
            cfg = load_autopause_config()

            if not cfg["enabled"]:
                self._ensure_resumed_if_disabled()
                time.sleep(cfg["poll_interval_running"])
                continue

            proc = find_ark_process()
            if proc is None:
                time.sleep(cfg["poll_interval_running"])
                continue

            if self.install_path is None:
                self.install_path = find_ark_install_path()

            stat_char = proc_stat_char(proc.pid)
            if stat_char == "T" and self.mode != PAUSED:
                logger.info("Detected pre-existing PAUSED state for pid %s.", proc.pid)
                self.mode = PAUSED
            elif stat_char != "T" and self.mode == PAUSED:
                logger.info("Process is no longer stopped externally; resyncing to RUNNING.")
                self.mode = RUNNING
                self.last_active_ts = time.time()

            if self.mode == RUNNING:
                self._tick_running(proc, cfg)
                time.sleep(cfg["poll_interval_running"])
            else:
                self._tick_paused(proc)
                time.sleep(cfg["poll_interval_paused"])

        logger.info("ark_autopause shut down.")

    def _ensure_resumed_if_disabled(self):
        proc = find_ark_process()
        if proc and proc_stat_char(proc.pid) == "T":
            logger.info("autopause disabled via config while paused; resuming.")
            proc.send_signal(signal.SIGCONT)
            self.mode = RUNNING
            self.last_active_ts = time.time()

    # -------------------------------------------------------------- RUNNING

    def _tick_running(self, proc, cfg):
        if not self.install_path:
            return

        enabled, port, password = rcon_credentials(self.install_path)
        if not enabled or not password:
            self._idle_check_no_rcon(proc, cfg)
            return

        try:
            with ArkRcon("127.0.0.1", port, password) as rcon:
                self._handle_players(rcon, proc, cfg)
                self._handle_chat(rcon, cfg)
        except RconError as exc:
            logger.warning("RCON unavailable this tick (%s); skipping idle check.", exc)

    def _idle_check_no_rcon(self, proc, cfg):
        idle_minutes = cfg["default_idle_minutes"]
        if time.time() - self.last_active_ts >= idle_minutes * 60:
            self._pause(proc, reason="idle (no RCON, default threshold)")

    def _handle_players(self, rcon, proc, cfg):
        response = rcon.command("ListPlayers")
        current = parse_player_lines(response)

        if current:
            self.last_active_ts = time.time()
            new_joins = current - self.seen_players
            for name in new_joins:
                self._broadcast_join(rcon, name, cfg)
            self.seen_players = current
        else:
            self.seen_players = set()
            pending = self.state.get("pending_keepalive_minutes")
            idle_minutes = pending or cfg["default_idle_minutes"]
            if time.time() - self.last_active_ts >= idle_minutes * 60:
                self._pause(proc, reason=f"idle ({idle_minutes} min threshold)")
                if pending:
                    self.state["pending_keepalive_minutes"] = None
                    save_state(self.state)

    def _broadcast_join(self, rcon, name, cfg):
        msg = JOIN_MESSAGE.format(
            idle=cfg["default_idle_minutes"], cap=cfg["keepalive_max_minutes"]
        )
        try:
            rcon.command(f"ServerChat {msg}")
        except RconError as exc:
            logger.warning("Failed to broadcast join message: %s", exc)
        logger.info("Player joined: %s", name)

    def _handle_chat(self, rcon, cfg):
        try:
            chat = rcon.command("GetChat")
        except RconError:
            return
        cap = cfg["keepalive_max_minutes"]
        for line in chat.splitlines():
            if WIPEDINOS_RE.search(line):
                self._wipe_dinos(rcon, parse_chat_sender(line))
                continue
            m = KEEPALIVE_RE.search(line)
            if not m:
                continue
            requested = int(m.group(1))
            clamped = max(1, min(requested, cap))
            sender = parse_chat_sender(line)
            self.state["pending_keepalive_minutes"] = clamped
            self.state["set_by"] = sender
            self.state["set_at"] = time.time()
            save_state(self.state)
            logger.info("%s set keepalive override to %s minutes.", sender, clamped)
            note = ""
            if clamped != requested:
                note = f" (clamped from {requested} to the {cap}-minute cap)"
            try:
                rcon.command(
                    f"ServerChat Keep-alive set: server will wait {clamped} "
                    f"minutes after everyone logs off before pausing{note}."
                )
            except RconError as exc:
                logger.warning("Failed to send keepalive confirmation: %s", exc)

    def _wipe_dinos(self, rcon, sender):
        logger.info("%s triggered !wipedinos via chat.", sender)
        try:
            rcon.command(
                f"ServerChat {sender} is wiping all wild dinos in 10 seconds..."
            )
            time.sleep(10)
            rcon.command("DestroyWildDinos")
            rcon.command("ServerChat Wild dino wipe complete.")
            logger.info("Wild dino wipe complete (triggered by %s).", sender)
        except RconError as exc:
            logger.warning("Failed to complete wild dino wipe: %s", exc)

    def _pause(self, proc, reason):
        try:
            proc.send_signal(signal.SIGSTOP)
        except psutil.NoSuchProcess:
            return
        self.mode = PAUSED
        logger.info("PAUSED pid %s (%s).", proc.pid, reason)

    # --------------------------------------------------------------- PAUSED

    def _tick_paused(self, proc):
        game_port, _query_port = parse_ports(proc.cmdline())
        recvq = udp_recv_queue(game_port)
        if recvq > 0:
            try:
                proc.send_signal(signal.SIGCONT)
            except psutil.NoSuchProcess:
                return
            self.mode = RUNNING
            self.last_active_ts = time.time()
            self.seen_players = set()
            logger.info("RESUMED pid %s (recvq=%s bytes on port %s).", proc.pid, recvq, game_port)


def main():
    setup_logging()
    AutopauseDaemon().run()


if __name__ == "__main__":
    main()
