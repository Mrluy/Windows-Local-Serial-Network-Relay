from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import serial_tcp_relay_gui as relay_gui


def make_settings(
    *,
    restart_application: bool = True,
    restart_application_after: float = 10,
    restart_system: bool = True,
    restart_system_after: float = 20,
) -> relay_gui.RelaySettings:
    serial_settings = relay_gui.SerialSettings(
        port="COM_TEST",
        baudrate=9600,
        data_bits=8,
        parity="N",
        stop_bits="1",
        dtr=True,
        rts=True,
        reset_input=True,
        auto_reconnect=True,
        reconnect_interval=2,
        restart_device_on_timeout=False,
        restart_device_after=60,
        restart_application_on_timeout=restart_application,
        restart_application_after=restart_application_after,
        restart_system_on_timeout=restart_system,
        restart_system_after=restart_system_after,
    )
    return relay_gui.RelaySettings(
        serial=serial_settings,
        network_mode="tcp_server",
        bind_host="127.0.0.1",
        local_port=10123,
        remote_host="",
        remote_port=10123,
        client_policy="single",
        access_mode="allow_all",
        access_rules=(),
        hex_log=False,
        network_auto_reconnect=True,
        network_reconnect_interval=3,
    )


class SerialRecoveryActionTests(unittest.TestCase):
    def test_application_restart_is_requested_once_before_system_escalation(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        outage_started_at = time.time() - 30
        relay = relay_gui.SerialNetworkRelay(
            make_settings(),
            lambda kind, payload: events.append((kind, payload)),
            inherited_outage_started_at=outage_started_at,
        )
        relay._serial_reconnect_started_at = time.monotonic() - 30

        relay._maybe_request_serial_timeout_actions()
        relay._maybe_request_serial_timeout_actions()

        action_events = [event for event in events if event[0].startswith("restart_")]
        self.assertEqual([event[0] for event in action_events], ["restart_application"])
        self.assertAlmostEqual(action_events[0][1]["outage_started_at"], outage_started_at, places=3)

    def test_inherited_application_restart_state_allows_system_escalation(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        relay = relay_gui.SerialNetworkRelay(
            make_settings(),
            lambda kind, payload: events.append((kind, payload)),
            inherited_outage_started_at=time.time() - 30,
            application_restart_already_done=True,
        )
        relay._serial_reconnect_started_at = time.monotonic()

        relay._maybe_request_serial_timeout_actions()

        self.assertIn("restart_system", [event[0] for event in events])
        self.assertNotIn("restart_application", [event[0] for event in events])

    def test_failed_action_can_be_retried(self) -> None:
        events: list[tuple[str, dict[str, object]]] = []
        relay = relay_gui.SerialNetworkRelay(
            make_settings(restart_system=False),
            lambda kind, payload: events.append((kind, payload)),
            inherited_outage_started_at=time.time() - 30,
        )
        relay._serial_reconnect_started_at = time.monotonic() - 30

        relay._maybe_request_serial_timeout_actions()
        relay.reset_timeout_action("application")
        relay._maybe_request_serial_timeout_actions()

        self.assertEqual([event[0] for event in events].count("restart_application"), 2)


class RestartCommandTests(unittest.TestCase):
    def test_restart_command_preserves_recovery_state(self) -> None:
        with mock.patch.object(relay_gui.sys, "frozen", True, create=True), mock.patch.object(
            relay_gui.sys, "executable", r"C:\Relay\本地串口网络中继.exe"
        ):
            command = relay_gui.application_restart_command(1234, 456.25, True)

        options = relay_gui.parse_runtime_options(command[1:])
        self.assertEqual(options.restart_wait_pid, 1234)
        self.assertEqual(options.inherited_outage_started_at, 456.25)
        self.assertTrue(options.force_start_service)
        self.assertTrue(options.application_restart_already_done)
        self.assertTrue(options.start_minimized)

    @mock.patch.object(relay_gui.subprocess, "run")
    def test_windows_restart_uses_restart_and_forced_close(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        with mock.patch.object(relay_gui.os, "name", "nt"):
            ok, message = relay_gui.schedule_windows_restart(5)

        self.assertTrue(ok)
        self.assertIn("5 秒后重启", message)
        command = run.call_args.args[0]
        self.assertIn("/r", command)
        self.assertIn("/f", command)
        self.assertEqual(command[command.index("/t") + 1], "5")


if __name__ == "__main__":
    unittest.main()
