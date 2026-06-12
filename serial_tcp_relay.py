#!/usr/bin/env python3
"""Expose a local serial port as a transparent TCP server.

Other devices can connect to this computer's IP address and TCP port, then send
and receive raw bytes through the configured local COM port.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import serial
from serial.tools import list_ports


SERIAL_READ_SIZE = 4096
TCP_READ_SIZE = 4096


@dataclass(frozen=True)
class SerialConfig:
    port: str
    baudrate: int
    bytesize: int
    parity: str
    stopbits: float
    timeout: float
    write_timeout: float
    dtr: bool
    rts: bool


class RelayServer:
    def __init__(
        self,
        serial_port: serial.Serial,
        listen_host: str,
        listen_port: int,
        allow_multi_client: bool,
        hex_log: bool,
    ) -> None:
        self.serial = serial_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.allow_multi_client = allow_multi_client
        self.hex_log = hex_log

        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._serial_write_lock = threading.Lock()
        self._stop_event = threading.Event()

    def serve_forever(self) -> None:
        serial_thread = threading.Thread(target=self._serial_to_tcp_loop, name="serial-reader", daemon=True)
        serial_thread.start()

        with socket.create_server((self.listen_host, self.listen_port), reuse_port=False) as server:
            server.listen(16)
            log(f"listening on {self.listen_host}:{self.listen_port}")
            log("other devices can connect to this computer IP and the TCP port above")

            while not self._stop_event.is_set():
                conn, addr = server.accept()
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(conn, addr),
                    name=f"tcp-client-{addr[0]}:{addr[1]}",
                    daemon=True,
                )
                thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._clients_lock:
            clients = list(self._clients)
        for conn in clients:
            safe_close(conn)
        safe_close(self.serial)

    def _handle_client(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        if not self._register_client(conn, peer):
            return

        try:
            while not self._stop_event.is_set():
                data = conn.recv(TCP_READ_SIZE)
                if not data:
                    break
                if self.hex_log:
                    log(f"TCP {peer} -> COM {format_bytes(data)}")
                with self._serial_write_lock:
                    self.serial.write(data)
                    self.serial.flush()
        except (OSError, serial.SerialException) as exc:
            log(f"client error {peer}: {exc}")
        finally:
            self._unregister_client(conn, peer)

    def _serial_to_tcp_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                data = self.serial.read(SERIAL_READ_SIZE)
            except serial.SerialException as exc:
                log(f"serial read error: {exc}")
                self._stop_event.set()
                break

            if not data:
                continue

            if self.hex_log:
                log(f"COM -> TCP {format_bytes(data)}")
            self._broadcast(data)

    def _register_client(self, conn: socket.socket, peer: str) -> bool:
        with self._clients_lock:
            if not self.allow_multi_client and self._clients:
                log(f"rejecting {peer}: another client is already connected")
                safe_close(conn)
                return False
            self._clients.add(conn)
        log(f"client connected: {peer}")
        return True

    def _unregister_client(self, conn: socket.socket, peer: str) -> None:
        with self._clients_lock:
            self._clients.discard(conn)
        safe_close(conn)
        log(f"client disconnected: {peer}")

    def _broadcast(self, data: bytes) -> None:
        with self._clients_lock:
            clients = list(self._clients)

        dead_clients: list[socket.socket] = []
        for conn in clients:
            try:
                conn.sendall(data)
            except OSError:
                dead_clients.append(conn)

        if dead_clients:
            with self._clients_lock:
                for conn in dead_clients:
                    self._clients.discard(conn)
                    safe_close(conn)


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def format_bytes(data: bytes) -> str:
    return data.hex(" ").upper()


def safe_close(resource: object) -> None:
    try:
        close = getattr(resource, "close")
        close()
    except Exception:
        pass


def list_serial_ports() -> None:
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return

    for port in ports:
        details = " - ".join(part for part in [port.description, port.hwid] if part)
        print(f"{port.device}\t{details}")


def parse_stop_bits(value: str) -> float:
    mapping = {
        "1": serial.STOPBITS_ONE,
        "1.5": serial.STOPBITS_ONE_POINT_FIVE,
        "2": serial.STOPBITS_TWO,
    }
    return mapping[value]


def make_serial_config(args: argparse.Namespace) -> SerialConfig:
    bytesize = {
        5: serial.FIVEBITS,
        6: serial.SIXBITS,
        7: serial.SEVENBITS,
        8: serial.EIGHTBITS,
    }[args.data_bits]
    parity = {
        "N": serial.PARITY_NONE,
        "E": serial.PARITY_EVEN,
        "O": serial.PARITY_ODD,
        "M": serial.PARITY_MARK,
        "S": serial.PARITY_SPACE,
    }[args.parity]
    return SerialConfig(
        port=args.com,
        baudrate=args.baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=parse_stop_bits(args.stop_bits),
        timeout=args.read_timeout,
        write_timeout=args.write_timeout,
        dtr=args.dtr,
        rts=args.rts,
    )


def open_serial(config: SerialConfig) -> serial.Serial:
    ser = serial.Serial(
        port=config.port,
        baudrate=config.baudrate,
        bytesize=config.bytesize,
        parity=config.parity,
        stopbits=config.stopbits,
        timeout=config.timeout,
        write_timeout=config.write_timeout,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    ser.dtr = config.dtr
    ser.rts = config.rts
    ser.reset_input_buffer()
    return ser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local serial port to TCP transparent relay for Windows COM ports.",
    )
    parser.add_argument("--list-ports", action="store_true", help="List available serial ports and exit.")
    parser.add_argument("--com", default="COM3", help="Windows COM port, for example COM3.")
    parser.add_argument("--listen", default="0.0.0.0", help="TCP listen address. Use 0.0.0.0 for LAN access.")
    parser.add_argument("--port", type=int, default=10123, help="TCP listen port.")
    parser.add_argument("--baudrate", type=int, default=9600, help="Serial baud rate.")
    parser.add_argument("--data-bits", type=int, choices=[5, 6, 7, 8], default=8, help="Serial data bits.")
    parser.add_argument("--parity", choices=["N", "E", "O", "M", "S"], default="N", help="Serial parity.")
    parser.add_argument("--stop-bits", choices=["1", "1.5", "2"], default="1", help="Serial stop bits.")
    parser.add_argument("--read-timeout", type=float, default=0.05, help="Serial read timeout in seconds.")
    parser.add_argument("--write-timeout", type=float, default=3.0, help="Serial write timeout in seconds.")
    parser.add_argument("--dtr", action=argparse.BooleanOptionalAction, default=True, help="Enable or disable DTR.")
    parser.add_argument("--rts", action=argparse.BooleanOptionalAction, default=True, help="Enable or disable RTS.")
    parser.add_argument("--multi-client", action="store_true", help="Allow multiple TCP clients at the same time.")
    parser.add_argument("--hex-log", action="store_true", help="Print forwarded bytes as hexadecimal.")
    return parser


def serial_mode_label(config: SerialConfig) -> str:
    reverse_bytesize = {
        serial.FIVEBITS: "5",
        serial.SIXBITS: "6",
        serial.SEVENBITS: "7",
        serial.EIGHTBITS: "8",
    }
    reverse_stopbits = {
        serial.STOPBITS_ONE: "1",
        serial.STOPBITS_ONE_POINT_FIVE: "1.5",
        serial.STOPBITS_TWO: "2",
    }
    return f"{config.baudrate}-{reverse_bytesize[config.bytesize]}{config.parity}{reverse_stopbits[config.stopbits]}"


def local_ip_hints() -> Iterable[str]:
    seen: set[str] = set()
    hostname = socket.gethostname()
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
        if family != socket.AF_INET:
            continue
        ip = sockaddr[0]
        if ip.startswith("127.") or ip in seen:
            continue
        seen.add(ip)
        yield ip


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_ports:
        list_serial_ports()
        return 0

    config = make_serial_config(args)

    try:
        ser = open_serial(config)
    except serial.SerialException as exc:
        print(f"Failed to open {config.port}: {exc}", file=sys.stderr)
        return 2

    log(
        f"opened {config.port} at {serial_mode_label(config)}, "
        f"DTR={config.dtr}, RTS={config.rts}, multi-client={args.multi_client}"
    )
    hints = ", ".join(local_ip_hints())
    if hints:
        log(f"local IP hint: {hints}")

    relay = RelayServer(
        serial_port=ser,
        listen_host=args.listen,
        listen_port=args.port,
        allow_multi_client=args.multi_client,
        hex_log=args.hex_log,
    )

    try:
        relay.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        relay.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
