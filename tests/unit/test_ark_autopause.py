"""Unit tests for bin/ark_autopause.py and bin/ark_rcon.py."""

import configparser
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, mock_open, patch

# Allow direct import from bin/ without touching the Flask app.
BIN_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "bin")
sys.path.insert(0, os.path.abspath(BIN_DIR))

import ark_autopause as ap
from ark_autopause import (
    AUTOPAUSE_DEFAULTS,
    KEEPALIVE_RE,
    PAUSED,
    RUNNING,
    AutopauseDaemon,
    ark_cfg_dir,
    load_autopause_config,
    load_state,
    parse_chat_sender,
    parse_player_lines,
    parse_ports,
    proc_stat_char,
    save_state,
    udp_recv_queue,
)
from ark_rcon import ArkRcon, RconError


# ---------------------------------------------------------------------------
# ark_rcon
# ---------------------------------------------------------------------------

class TestRconPacket(unittest.TestCase):
    """Wire-level packet encoding/decoding."""

    def _make_response(self, req_id, ptype, body=b""):
        """Build a valid RCON response packet."""
        payload = struct.pack("<ii", req_id, ptype) + body + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    def test_send_builds_correct_packet(self):
        sock = MagicMock()
        rcon = ArkRcon("127.0.0.1", 27020, "pass")
        rcon._sock = sock

        rcon._send(2, "ListPlayers")

        data = sock.sendall.call_args[0][0]
        size = struct.unpack("<i", data[:4])[0]
        self.assertEqual(size, len(data) - 4)
        req_id, ptype = struct.unpack("<ii", data[4:12])
        self.assertEqual(ptype, 2)
        body = data[12:-2]
        self.assertEqual(body, b"ListPlayers")

    def test_command_returns_decoded_body(self):
        response_body = b"1. PlayerOne, ABC123"
        response_packet = self._make_response(1, 0, response_body)

        sock = MagicMock()
        # recv_exact reads 4 bytes for size, then size bytes for body
        def side_effect(n):
            # Return the right slice on each call
            if not hasattr(side_effect, "offset"):
                side_effect.offset = 0
            chunk = response_packet[side_effect.offset:side_effect.offset + n]
            side_effect.offset += n
            return chunk

        sock.recv.side_effect = lambda n: side_effect(n)

        rcon = ArkRcon("127.0.0.1", 27020, "pass")
        rcon._sock = sock
        rcon._next_id = 1

        with patch.object(rcon, "_send", return_value=1):
            result = rcon.command("ListPlayers")

        self.assertEqual(result, "1. PlayerOne, ABC123")

    def test_connect_raises_rcon_error_on_failure(self):
        rcon = ArkRcon("127.0.0.1", 27020, "pass", timeout=1)
        with self.assertRaises(RconError):
            rcon.connect()  # nothing listening on that port

    def test_context_manager_closes_on_exception(self):
        rcon = ArkRcon("127.0.0.1", 27020, "pass")
        mock_sock = MagicMock()
        with patch.object(rcon, "connect", lambda: setattr(rcon, "_sock", mock_sock)):
            try:
                with rcon:
                    raise ValueError("boom")
            except ValueError:
                pass
        mock_sock.close.assert_called_once()

    def test_recv_exact_raises_on_closed_connection(self):
        sock = MagicMock()
        sock.recv.return_value = b""  # connection closed
        rcon = ArkRcon("127.0.0.1", 27020, "pass")
        rcon._sock = sock
        with self.assertRaises(RconError):
            rcon._recv_exact(4)

    def test_send_raises_when_not_connected(self):
        rcon = ArkRcon("127.0.0.1", 27020, "pass")
        with self.assertRaises(RconError):
            rcon._send(2, "cmd")


# ---------------------------------------------------------------------------
# parse_player_lines
# ---------------------------------------------------------------------------

class TestParsePlayerLines(unittest.TestCase):

    def test_single_player(self):
        resp = "1. PlayerOne, STEAM_0:0:12345"
        result = parse_player_lines(resp)
        self.assertEqual(result, {"PlayerOne"})

    def test_multiple_players(self):
        resp = "1. Alice, ID_1\n2. Bob Smith, ID_2\n3. Charlie, ID_3"
        result = parse_player_lines(resp)
        self.assertEqual(result, {"Alice", "Bob Smith", "Charlie"})

    def test_no_players(self):
        self.assertEqual(parse_player_lines("No Players Connected"), set())
        self.assertEqual(parse_player_lines(""), set())

    def test_mixed_empty_lines(self):
        resp = "\n1. Solo, ID\n\n"
        self.assertEqual(parse_player_lines(resp), {"Solo"})

    def test_player_name_with_spaces(self):
        resp = "1. First Last Name, SomeID123"
        self.assertEqual(parse_player_lines(resp), {"First Last Name"})


# ---------------------------------------------------------------------------
# parse_chat_sender
# ---------------------------------------------------------------------------

class TestParseChatSender(unittest.TestCase):

    def test_colon_separated(self):
        self.assertEqual(parse_chat_sender("Alice: hello"), "Alice")

    def test_no_colon_returns_unknown(self):
        self.assertEqual(parse_chat_sender("!keepalive 60"), "unknown")

    def test_strips_whitespace(self):
        self.assertEqual(parse_chat_sender("  Bob  : hi there"), "Bob")


# ---------------------------------------------------------------------------
# parse_ports
# ---------------------------------------------------------------------------

class TestParsePorts(unittest.TestCase):

    def test_explicit_ports(self):
        cmdline = ["./ShooterGameServer", "-Port=7778", "-QueryPort=27016"]
        game, query = parse_ports(cmdline)
        self.assertEqual(game, 7778)
        self.assertEqual(query, 27016)

    def test_default_ports_when_absent(self):
        game, query = parse_ports(["./ShooterGameServer"])
        self.assertEqual(game, 7777)
        self.assertEqual(query, 27015)

    def test_case_insensitive(self):
        cmdline = ["./ShooterGameServer", "-port=9000", "-queryport=28000"]
        game, query = parse_ports(cmdline)
        self.assertEqual(game, 9000)
        self.assertEqual(query, 28000)


# ---------------------------------------------------------------------------
# KEEPALIVE_RE
# ---------------------------------------------------------------------------

class TestKeepaliveRegex(unittest.TestCase):

    def test_matches_basic(self):
        m = KEEPALIVE_RE.search("Alice: !keepalive 120")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "120")

    def test_case_insensitive(self):
        self.assertIsNotNone(KEEPALIVE_RE.search("!KEEPALIVE 60"))

    def test_no_match_without_number(self):
        self.assertIsNone(KEEPALIVE_RE.search("!keepalive"))

    def test_no_match_in_unrelated_text(self):
        self.assertIsNone(KEEPALIVE_RE.search("hello world"))


# ---------------------------------------------------------------------------
# load_autopause_config
# ---------------------------------------------------------------------------

class TestLoadAutopauseConfig(unittest.TestCase):

    def test_defaults_when_no_section(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write("[server]\nhost=127.0.0.1\n")
            name = f.name
        try:
            with patch.object(ap, "MAIN_CONF_PATH", name):
                cfg = load_autopause_config()
        finally:
            os.unlink(name)
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["default_idle_minutes"], 30)
        self.assertEqual(cfg["keepalive_max_minutes"], 1440)
        self.assertEqual(cfg["poll_interval_running"], 20)
        self.assertEqual(cfg["poll_interval_paused"], 5)

    def test_custom_values_overridden(self):
        conf = "[autopause]\nenabled = no\ndefault_idle_minutes = 10\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
            f.write(conf)
            name = f.name
        try:
            with patch.object(ap, "MAIN_CONF_PATH", name):
                cfg = load_autopause_config()
        finally:
            os.unlink(name)
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["default_idle_minutes"], 10)

    def test_enabled_parses_true_variants(self):
        for val in ("yes", "true", "1", "on", "YES", "True"):
            conf = f"[autopause]\nenabled = {val}\n"
            with tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False) as f:
                f.write(conf)
                name = f.name
            try:
                with patch.object(ap, "MAIN_CONF_PATH", name):
                    cfg = load_autopause_config()
            finally:
                os.unlink(name)
            self.assertTrue(cfg["enabled"], f"enabled should be True for '{val}'")


# ---------------------------------------------------------------------------
# load_state / save_state
# ---------------------------------------------------------------------------

class TestStatePersistence(unittest.TestCase):

    def test_save_and_load_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            name = f.name
        try:
            state = {"pending_keepalive_minutes": 60, "set_by": "Alice", "set_at": 1234.0}
            with patch.object(ap, "STATE_PATH", name):
                save_state(state)
                loaded = load_state()
        finally:
            os.unlink(name)
        self.assertEqual(loaded["pending_keepalive_minutes"], 60)
        self.assertEqual(loaded["set_by"], "Alice")

    def test_load_state_returns_defaults_on_missing_file(self):
        with patch.object(ap, "STATE_PATH", "/nonexistent/path/state.json"):
            state = load_state()
        self.assertIsNone(state["pending_keepalive_minutes"])
        self.assertIsNone(state["set_by"])

    def test_save_state_logs_warning_on_error(self):
        with patch.object(ap, "STATE_PATH", "/nonexistent/dir/state.json"):
            with patch.object(ap.logger, "warning") as mock_warn:
                save_state({"pending_keepalive_minutes": None})
                mock_warn.assert_called_once()


# ---------------------------------------------------------------------------
# udp_recv_queue
# ---------------------------------------------------------------------------

class TestUdpRecvQueue(unittest.TestCase):

    def _make_udp_line(self, port, recv_q):
        hex_port = f"{port:04X}"
        hex_q = f"{0:08X}:{recv_q:08X}"
        return f"  0: 00000000:{hex_port} 00000000:0000 07 {hex_q} 00000000  0 0 123 1 0000000000000000 0\n"

    def test_returns_recv_q_for_matching_port(self):
        port = 7777
        recv_q = 48
        content = "  sl  local_address rem_address   st tx_queue:rx_queue ...\n"
        content += self._make_udp_line(port, recv_q)
        with patch("builtins.open", mock_open(read_data=content)):
            result = udp_recv_queue(port)
        self.assertEqual(result, recv_q)

    def test_returns_zero_when_port_not_found(self):
        content = "  sl  local_address rem_address   st tx_queue:rx_queue ...\n"
        content += self._make_udp_line(7778, 10)
        with patch("builtins.open", mock_open(read_data=content)):
            result = udp_recv_queue(7777)
        self.assertEqual(result, 0)

    def test_returns_zero_on_file_error(self):
        with patch("builtins.open", side_effect=OSError):
            self.assertEqual(udp_recv_queue(7777), 0)


# ---------------------------------------------------------------------------
# proc_stat_char
# ---------------------------------------------------------------------------

class TestProcStatChar(unittest.TestCase):

    def test_returns_state_char(self):
        # Format: pid (comm) state ...
        content = "1234 (ShooterGameServer) T 1 2 3 4\n"
        with patch("builtins.open", mock_open(read_data=content)):
            char = proc_stat_char(1234)
        self.assertEqual(char, "T")

    def test_returns_s_for_sleeping(self):
        content = "5678 (myproc) S 1 2 3\n"
        with patch("builtins.open", mock_open(read_data=content)):
            self.assertEqual(proc_stat_char(5678), "S")

    def test_returns_none_on_oserror(self):
        with patch("builtins.open", side_effect=OSError):
            self.assertIsNone(proc_stat_char(9999))

    def test_handles_comm_with_spaces_and_parens(self):
        # comm field may contain spaces and parens itself
        content = "42 (my (weird) proc) R 1 2 3\n"
        with patch("builtins.open", mock_open(read_data=content)):
            self.assertEqual(proc_stat_char(42), "R")


# ---------------------------------------------------------------------------
# AutopauseDaemon._handle_chat (keepalive command processing)
# ---------------------------------------------------------------------------

class TestAutopauseDaemonChat(unittest.TestCase):

    def _make_daemon(self):
        with patch.object(ap, "load_state", return_value={
            "pending_keepalive_minutes": None, "set_by": None, "set_at": None
        }):
            return AutopauseDaemon()

    def test_keepalive_sets_state(self):
        daemon = self._make_daemon()
        cfg = {**{k: int(v) if v.isdigit() else v for k, v in AUTOPAUSE_DEFAULTS.items()},
               "default_idle_minutes": 30, "keepalive_max_minutes": 1440,
               "poll_interval_running": 20, "poll_interval_paused": 5, "enabled": True}
        rcon = MagicMock()
        rcon.command.return_value = "Alice: !keepalive 120"
        with patch.object(ap, "save_state") as mock_save:
            daemon._handle_chat(rcon, cfg)
        self.assertEqual(daemon.state["pending_keepalive_minutes"], 120)
        self.assertEqual(daemon.state["set_by"], "Alice")
        mock_save.assert_called_once()

    def test_keepalive_clamped_to_max(self):
        daemon = self._make_daemon()
        cfg = {"keepalive_max_minutes": 60, "default_idle_minutes": 30,
               "poll_interval_running": 20, "poll_interval_paused": 5, "enabled": True}
        rcon = MagicMock()
        rcon.command.return_value = "Bob: !keepalive 9999"
        with patch.object(ap, "save_state"):
            daemon._handle_chat(rcon, cfg)
        self.assertEqual(daemon.state["pending_keepalive_minutes"], 60)

    def test_no_keepalive_command_leaves_state_unchanged(self):
        daemon = self._make_daemon()
        cfg = {"keepalive_max_minutes": 1440, "default_idle_minutes": 30,
               "poll_interval_running": 20, "poll_interval_paused": 5, "enabled": True}
        rcon = MagicMock()
        rcon.command.return_value = "Alice: just chatting"
        with patch.object(ap, "save_state") as mock_save:
            daemon._handle_chat(rcon, cfg)
        self.assertIsNone(daemon.state["pending_keepalive_minutes"])
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# AutopauseDaemon._pause / _tick_paused
# ---------------------------------------------------------------------------

class TestAutopauseDaemonPauseResume(unittest.TestCase):

    def _make_daemon(self):
        with patch.object(ap, "load_state", return_value={
            "pending_keepalive_minutes": None, "set_by": None, "set_at": None
        }):
            return AutopauseDaemon()

    def test_pause_sends_sigstop_and_sets_mode(self):
        import signal
        daemon = self._make_daemon()
        proc = MagicMock()
        daemon._pause(proc, reason="test")
        proc.send_signal.assert_called_once_with(signal.SIGSTOP)
        self.assertEqual(daemon.mode, PAUSED)

    def test_pause_handles_missing_process(self):
        import psutil
        daemon = self._make_daemon()
        proc = MagicMock()
        proc.send_signal.side_effect = psutil.NoSuchProcess(pid=0)
        daemon._pause(proc, reason="test")  # must not raise
        self.assertEqual(daemon.mode, RUNNING)  # mode not changed

    def test_tick_paused_resumes_on_recvq(self):
        import signal
        daemon = self._make_daemon()
        daemon.mode = PAUSED
        proc = MagicMock()
        proc.cmdline.return_value = ["./ShooterGameServer", "-Port=7777"]
        with patch.object(ap, "udp_recv_queue", return_value=64):
            daemon._tick_paused(proc)
        proc.send_signal.assert_called_once_with(signal.SIGCONT)
        self.assertEqual(daemon.mode, RUNNING)

    def test_tick_paused_stays_paused_when_no_recvq(self):
        daemon = self._make_daemon()
        daemon.mode = PAUSED
        proc = MagicMock()
        proc.cmdline.return_value = ["./ShooterGameServer", "-Port=7777"]
        with patch.object(ap, "udp_recv_queue", return_value=0):
            daemon._tick_paused(proc)
        proc.send_signal.assert_not_called()
        self.assertEqual(daemon.mode, PAUSED)


if __name__ == "__main__":
    unittest.main()
