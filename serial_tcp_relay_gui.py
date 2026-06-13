#!/usr/bin/env python3
"""Windows GUI for a local serial-to-TCP transparent relay."""

from __future__ import annotations

import datetime as dt
import ctypes
import fnmatch
import ipaddress
import json
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import pystray
import serial
from PIL import Image
from serial.tools import list_ports


APP_NAME = "本地串口网络中继"
APP_VERSION = "1.0.0"
APP_DIR_NAME = "SerialTcpRelay"
APP_ICON_PATH = Path("img") / "app.png"
BIND_ALL_VALUE = "0.0.0.0"
BIND_ALL_LABEL = "允许所有"
SETTINGS_FILE_NAME = "settings.json"
LOG_DIR_NAME = "log"
SYSTEM_LOG_DB_NAME = "system_logs.sqlite"
DATA_LOG_DB_NAME = "data_logs.sqlite"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SERIAL_READ_SIZE = 4096
TCP_READ_SIZE = 4096


NETWORK_MODES = {
    "TCP Server": "tcp_server",
    "TCP Client": "tcp_client",
    "UDP Server": "udp_server",
    "UDP Client": "udp_client",
}
NETWORK_MODE_LABELS = {value: key for key, value in NETWORK_MODES.items()}

CLIENT_POLICIES = {
    "仅允许一个客户端": "single",
    "允许多个客户端": "multi",
}
CLIENT_POLICY_LABELS = {value: key for key, value in CLIENT_POLICIES.items()}

ACCESS_MODES = {
    "允许全部": "allow_all",
    "仅允许白名单": "whitelist",
    "拒绝黑名单": "blacklist",
}
ACCESS_MODE_LABELS = {value: key for key, value in ACCESS_MODES.items()}


@dataclass(frozen=True)
class SerialSettings:
    port: str
    baudrate: int
    data_bits: int
    parity: str
    stop_bits: str
    dtr: bool
    rts: bool
    reset_input: bool
    auto_reconnect: bool
    reconnect_interval: float


@dataclass(frozen=True)
class RelaySettings:
    serial: SerialSettings
    network_mode: str
    bind_host: str
    local_port: int
    remote_host: str
    remote_port: int
    client_policy: str
    access_mode: str
    access_rules: tuple[str, ...]
    hex_log: bool
    network_auto_reconnect: bool
    network_reconnect_interval: float


@dataclass(eq=False)
class ClientSession:
    peer_ip: str
    peer_port: int
    protocol: str
    connected_at: float
    conn: socket.socket | None = None
    udp_addr: tuple[str, int] | None = None
    network_to_serial_bytes: int = 0
    serial_to_network_bytes: int = 0

    @property
    def peer(self) -> str:
        return f"{self.protocol} {self.peer_ip}:{self.peer_port}"


class AccessControl:
    def __init__(self, mode: str, rules: tuple[str, ...]) -> None:
        self.mode = mode
        self.networks: list[ipaddress._BaseNetwork] = []
        self.patterns: list[str] = []
        self.invalid_rules: list[str] = []

        for raw_rule in rules:
            rule = raw_rule.strip()
            if not rule or rule.startswith("#"):
                continue

            if "*" in rule or "?" in rule:
                self.patterns.append(rule)
                continue

            try:
                network = ipaddress.ip_network(rule, strict=False)
            except ValueError:
                self.invalid_rules.append(rule)
            else:
                self.networks.append(network)

    def allows(self, ip: str) -> bool:
        if self.mode == "allow_all":
            return True

        matched = self._matches(ip)
        if self.mode == "whitelist":
            return matched
        if self.mode == "blacklist":
            return not matched
        return True

    def _matches(self, ip: str) -> bool:
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            address = None

        if address is not None:
            for network in self.networks:
                if address in network:
                    return True

        return any(fnmatch.fnmatch(ip, pattern) for pattern in self.patterns)


class LogStore:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.system_db_path = self.log_dir / SYSTEM_LOG_DB_NAME
        self.data_db_path = self.log_dir / DATA_LOG_DB_NAME
        self._system_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._system_conn = sqlite3.connect(self.system_db_path, check_same_thread=False)
        self._data_conn = sqlite3.connect(self.data_db_path, check_same_thread=False)
        self._init_databases()

    def close(self) -> None:
        with self._system_lock:
            self._system_conn.close()
        with self._data_lock:
            self._data_conn.close()

    def log_system(self, level: str, message: str, category: str = "运行日志", peer: str = "", detail: str = "") -> None:
        created_at = time.time()
        time_text = dt.datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")
        with self._system_lock:
            self._system_conn.execute(
                """
                INSERT INTO system_logs(created_at, time_text, level, category, peer, message, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, time_text, level, category, peer, message, detail),
            )
            self._system_conn.commit()
            self._prune_system_locked(created_at)

    def log_data(self, peer: str, direction: str, byte_count: int, hex_data: str) -> None:
        created_at = time.time()
        time_text = dt.datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M:%S")
        with self._data_lock:
            self._data_conn.execute(
                """
                INSERT INTO data_logs(created_at, time_text, peer, direction, byte_count, hex_data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (created_at, time_text, peer, direction, byte_count, hex_data),
            )
            self._data_conn.commit()
            self._prune_data_locked(created_at)

    def query(
        self,
        log_type: str,
        level_filter: str,
        keyword: str,
        start_ts: float | None,
        end_ts: float | None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if log_type in {"全部", "运行日志", "连接记录"}:
            rows.extend(self._query_system(log_type, level_filter, keyword, start_ts, end_ts, limit))
        if log_type in {"全部", "数据收发"}:
            rows.extend(self._query_data(level_filter, keyword, start_ts, end_ts, limit))
        rows.sort(key=lambda item: item["created_at"], reverse=True)
        return rows[:limit]

    def _init_databases(self) -> None:
        with self._system_lock:
            self._system_conn.execute("PRAGMA journal_mode=WAL")
            self._system_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    time_text TEXT NOT NULL,
                    level TEXT NOT NULL,
                    category TEXT NOT NULL,
                    peer TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._system_conn.execute("CREATE INDEX IF NOT EXISTS idx_system_created_at ON system_logs(created_at)")
            self._system_conn.execute("CREATE INDEX IF NOT EXISTS idx_system_category ON system_logs(category)")
            self._system_conn.commit()

        with self._data_lock:
            self._data_conn.execute("PRAGMA journal_mode=WAL")
            self._data_conn.execute(
                """
                CREATE TABLE IF NOT EXISTS data_logs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    time_text TEXT NOT NULL,
                    peer TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    hex_data TEXT NOT NULL
                )
                """
            )
            self._data_conn.execute("CREATE INDEX IF NOT EXISTS idx_data_created_at ON data_logs(created_at)")
            self._data_conn.execute("CREATE INDEX IF NOT EXISTS idx_data_direction ON data_logs(direction)")
            self._data_conn.commit()

    def _query_system(
        self,
        log_type: str,
        level_filter: str,
        keyword: str,
        start_ts: float | None,
        end_ts: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = "SELECT created_at, time_text, category, level, peer, message, detail FROM system_logs WHERE 1=1"
        params: list[Any] = []
        if log_type in {"运行日志", "连接记录"}:
            sql += " AND category = ?"
            params.append(log_type)
        if level_filter:
            sql += " AND level LIKE ?"
            params.append(f"%{level_filter}%")
        if start_ts is not None:
            sql += " AND created_at >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND created_at <= ?"
            params.append(end_ts)
        if keyword:
            like = f"%{keyword}%"
            sql += " AND (level LIKE ? OR peer LIKE ? OR message LIKE ? OR detail LIKE ?)"
            params.extend([like, like, like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._system_lock:
            records = self._system_conn.execute(sql, params).fetchall()
        return [
            {
                "created_at": row[0],
                "time": row[1],
                "type": row[2],
                "level": row[3],
                "peer": row[4],
                "bytes": "",
                "message": row[5] if not row[6] else f"{row[5]} {row[6]}",
            }
            for row in records
        ]

    def _query_data(
        self,
        level_filter: str,
        keyword: str,
        start_ts: float | None,
        end_ts: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        sql = "SELECT created_at, time_text, peer, direction, byte_count, hex_data FROM data_logs WHERE 1=1"
        params: list[Any] = []
        if level_filter:
            sql += " AND direction LIKE ?"
            params.append(f"%{level_filter}%")
        if start_ts is not None:
            sql += " AND created_at >= ?"
            params.append(start_ts)
        if end_ts is not None:
            sql += " AND created_at <= ?"
            params.append(end_ts)
        if keyword:
            like = f"%{keyword}%"
            sql += " AND (peer LIKE ? OR direction LIKE ? OR hex_data LIKE ?)"
            params.extend([like, like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._data_lock:
            records = self._data_conn.execute(sql, params).fetchall()
        return [
            {
                "created_at": row[0],
                "time": row[1],
                "type": "数据收发",
                "level": row[3],
                "peer": row[2],
                "bytes": str(row[4]),
                "message": row[5],
            }
            for row in records
        ]

    def _prune_data_locked(self, now_ts: float) -> None:
        oldest = self._data_conn.execute("SELECT MIN(created_at) FROM data_logs").fetchone()[0]
        if oldest is not None and oldest < now_ts - 25 * 3600:
            self._data_conn.execute("DELETE FROM data_logs WHERE created_at < ?", (now_ts - 24 * 3600,))
            self._data_conn.commit()

    def _prune_system_locked(self, now_ts: float) -> None:
        oldest = self._system_conn.execute("SELECT MIN(created_at) FROM system_logs").fetchone()[0]
        if oldest is not None and oldest < now_ts - 190 * 86400:
            self._system_conn.execute("DELETE FROM system_logs WHERE created_at < ?", (now_ts - 180 * 86400,))
            self._system_conn.commit()

        count = self._system_conn.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
        if count >= 105000:
            self._system_conn.execute(
                "DELETE FROM system_logs WHERE id IN (SELECT id FROM system_logs ORDER BY id LIMIT 5000)"
            )
            self._system_conn.commit()


class LogViewer(tk.Toplevel):
    def __init__(self, master: tk.Tk, log_store: LogStore) -> None:
        super().__init__(master)
        self.log_store = log_store
        self.rows: list[dict[str, Any]] = []

        self.title("查看日志")
        self.minsize(980, 560)
        self.transient(master)

        self.log_type_var = tk.StringVar(value="全部")
        self.level_filter_var = tk.StringVar(value="")
        self.keyword_var = tk.StringVar(value="")
        self.start_time_var = tk.StringVar(value="")
        self.end_time_var = tk.StringVar(value="")

        self._build_ui()
        self._refresh()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        filters = ttk.Frame(self, padding=(10, 10, 10, 6))
        filters.grid(row=0, column=0, sticky="ew")
        filters.columnconfigure(5, weight=1)

        ttk.Label(filters, text="类型").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            filters,
            textvariable=self.log_type_var,
            values=("全部", "运行日志", "连接记录", "数据收发"),
            state="readonly",
            width=12,
        ).grid(row=0, column=1, sticky="w", padx=(6, 14))

        ttk.Label(filters, text="等级/方向").grid(row=0, column=2, sticky="w")
        level_combo = ttk.Combobox(
            filters,
            textvariable=self.level_filter_var,
            values=("INFO", "WARN", "ERROR", "已连接", "已断开", "已拒绝", "网络->串口", "串口->网络"),
            width=12,
        )
        level_combo.grid(row=0, column=3, sticky="w", padx=(6, 14))
        level_combo.bind("<Return>", lambda _event: self._refresh())

        ttk.Label(filters, text="关键字").grid(row=0, column=4, sticky="w")
        keyword_entry = ttk.Entry(filters, textvariable=self.keyword_var)
        keyword_entry.grid(row=0, column=5, sticky="ew", padx=(6, 14))
        keyword_entry.bind("<Return>", lambda _event: self._refresh())

        ttk.Label(filters, text="开始").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.start_time_var, width=19).grid(
            row=1, column=1, sticky="w", padx=(6, 14), pady=(8, 0)
        )
        ttk.Label(filters, text="结束").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.end_time_var, width=19).grid(
            row=1, column=3, sticky="w", padx=(6, 14), pady=(8, 0)
        )

        actions = ttk.Frame(filters)
        actions.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="查询", command=self._refresh).grid(row=0, column=0)
        ttk.Button(actions, text="最近24小时", command=self._set_recent_day).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(actions, text="清空条件", command=self._clear_filters).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(actions, text="导出结果", command=self._export_results).grid(row=0, column=3, padx=(8, 0))
        self.count_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.count_var).grid(row=0, column=4, sticky="w", padx=(14, 0))

        body = ttk.Frame(self, padding=(10, 0, 10, 10))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        body.rowconfigure(2, weight=0)

        self.tree = ttk.Treeview(
            body,
            columns=("time", "type", "level", "peer", "bytes", "message"),
            show="headings",
            height=16,
        )
        for column, title, width, anchor in (
            ("time", "时间", 150, "w"),
            ("type", "类型", 86, "w"),
            ("level", "级别/方向", 100, "w"),
            ("peer", "对端", 180, "w"),
            ("bytes", "字节", 70, "e"),
            ("message", "内容", 480, "w"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor, stretch=(column == "message"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", self._show_selected_detail)

        tree_scroll_y = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        tree_scroll_x.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        self.detail_text = tk.Text(body, height=5, wrap="word", state="disabled")
        self.detail_text.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _refresh(self) -> None:
        try:
            start_ts = parse_log_time(self.start_time_var.get(), is_end=False)
            end_ts = parse_log_time(self.end_time_var.get(), is_end=True)
        except ValueError as exc:
            messagebox.showerror("查看日志", str(exc), parent=self)
            return

        if start_ts is not None and end_ts is not None and start_ts > end_ts:
            messagebox.showerror("查看日志", "开始时间不能晚于结束时间。", parent=self)
            return

        self.rows = self.log_store.query(
            self.log_type_var.get(),
            self.level_filter_var.get().strip(),
            self.keyword_var.get().strip(),
            start_ts,
            end_ts,
            limit=1000,
        )

        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, row in enumerate(self.rows):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row["time"],
                    row["type"],
                    row["level"],
                    row["peer"],
                    row["bytes"],
                    truncate_text(str(row["message"]), 500),
                ),
            )
        self.count_var.set(f"显示 {len(self.rows)} 条")
        self._set_detail("")

    def _set_recent_day(self) -> None:
        now = dt.datetime.now()
        self.start_time_var.set((now - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"))
        self.end_time_var.set(now.strftime("%Y-%m-%d %H:%M:%S"))
        self._refresh()

    def _clear_filters(self) -> None:
        self.log_type_var.set("全部")
        self.level_filter_var.set("")
        self.keyword_var.set("")
        self.start_time_var.set("")
        self.end_time_var.set("")
        self._refresh()

    def _show_selected_detail(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            self._set_detail("")
            return
        row = self.rows[int(selection[0])]
        detail = (
            f"时间: {row['time']}\n"
            f"类型: {row['type']}\n"
            f"级别/方向: {row['level']}\n"
            f"对端: {row['peer']}\n"
            f"字节: {row['bytes']}\n"
            f"内容: {row['message']}"
        )
        self._set_detail(detail)

    def _set_detail(self, text: str) -> None:
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")

    def _export_results(self) -> None:
        if not self.rows:
            self._refresh()
            if not self.rows:
                return
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出日志查询结果",
            defaultextension=".tsv",
            filetypes=[("TSV files", "*.tsv"), ("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        lines = ["时间\t类型\t级别/方向\t对端\t字节\t内容"]
        for row in self.rows:
            lines.append(
                "\t".join(
                    clean_tsv(value)
                    for value in (
                        row["time"],
                        row["type"],
                        row["level"],
                        row["peer"],
                        row["bytes"],
                        row["message"],
                    )
                )
            )
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class SerialTcpRelay:
    def __init__(self, settings: RelaySettings, emit: Callable[[str, dict[str, Any]], None]) -> None:
        self.settings = settings
        self.emit = emit

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: socket.socket | None = None
        self._serial: serial.Serial | None = None
        self._serial_write_lock = threading.Lock()
        self._clients_lock = threading.Lock()
        self._clients: dict[socket.socket, ClientSession] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="relay-main", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_server()
        self._close_all_clients()
        self._close_serial()

    def _run(self) -> None:
        access = AccessControl(self.settings.access_mode, self.settings.access_rules)
        if access.invalid_rules:
            self._log("WARN", f"已忽略无效黑白名单规则: {', '.join(access.invalid_rules)}")

        try:
            self._serial = self._open_serial(self.settings.serial)
            self._server = socket.create_server((self.settings.bind_host, self.settings.tcp_port), reuse_port=False)
            self._server.listen(16)
            self._server.settimeout(0.5)
        except Exception as exc:
            self._log("ERROR", f"启动失败: {exc}")
            self._emit_status(False, f"启动失败: {exc}")
            self.stop()
            return

        serial_label = (
            f"{self.settings.serial.port} "
            f"{self.settings.serial.baudrate}-{self.settings.serial.data_bits}"
            f"{self.settings.serial.parity}{self.settings.serial.stop_bits}"
        )
        self._log("INFO", f"已打开串口 {serial_label}")
        self._log("INFO", f"正在监听 {self.settings.bind_host}:{self.settings.tcp_port}")
        self._emit_status(True, "运行中")

        if self._serial_to_tcp_enabled:
            threading.Thread(target=self._serial_to_tcp_loop, name="serial-reader", daemon=True).start()

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = self._server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break

                peer_ip = addr[0]
                peer_port = addr[1]
                peer = f"{peer_ip}:{peer_port}"

                if not access.allows(peer_ip):
                    self._record(peer, "已拒绝", "命中访问控制")
                    self._log("WARN", f"已拒绝客户端 {peer}")
                    safe_close(conn)
                    continue

                if not self._register_client(conn, peer_ip, peer_port):
                    self._record(peer, "已拒绝", "当前只允许一个客户端")
                    self._log("WARN", f"已拒绝客户端 {peer}: 当前只允许一个客户端")
                    safe_close(conn)
                    continue

                threading.Thread(
                    target=self._client_to_serial_loop,
                    args=(conn,),
                    name=f"client-{peer}",
                    daemon=True,
                ).start()
        finally:
            self.stop()
            self._emit_serial_status("串口: 已停止")
            self._emit_status(False, "已停止")
            self._log("INFO", "服务已停止")

    @property
    def _tcp_to_serial_enabled(self) -> bool:
        return True

    @property
    def _serial_to_tcp_enabled(self) -> bool:
        return True

    def _open_serial(self, settings: SerialSettings) -> serial.Serial:
        bytesize = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }[settings.data_bits]
        parity = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }[settings.parity]
        stopbits = {
            "1": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
        }[settings.stop_bits]

        port = serial.Serial(
            port=settings.port,
            baudrate=settings.baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=0.05,
            write_timeout=3,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        port.dtr = settings.dtr
        port.rts = settings.rts
        if settings.reset_input:
            port.reset_input_buffer()
        return port

    def _register_client(self, conn: socket.socket, peer_ip: str, peer_port: int) -> bool:
        with self._clients_lock:
            if self.settings.client_policy == "single" and self._clients:
                return False
            session = ClientSession(conn=conn, peer_ip=peer_ip, peer_port=peer_port, connected_at=time.time())
            self._clients[conn] = session

        conn.settimeout(0.5)
        self._record(session.peer, "已连接", "")
        self._log("INFO", f"客户端已连接 {session.peer}")
        self._emit_clients()
        return True

    def _unregister_client(self, conn: socket.socket, reason: str) -> None:
        with self._clients_lock:
            session = self._clients.pop(conn, None)

        if session is None:
            safe_close(conn)
            return

        safe_close(conn)
        detail = (
            f"{reason}; TCP->串口 {session.tcp_to_serial_bytes} B, "
            f"串口->TCP {session.serial_to_tcp_bytes} B"
        )
        self._record(session.peer, "已断开", detail)
        self._log("INFO", f"客户端已断开 {session.peer}: {detail}")
        self._emit_clients()

    def _client_to_serial_loop(self, conn: socket.socket) -> None:
        reason = "客户端关闭"
        while not self._stop_event.is_set():
            try:
                data = conn.recv(TCP_READ_SIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                reason = str(exc)
                break

            if not data:
                break

            session = self._get_session(conn)
            peer = session.peer if session else "unknown"

            if not self._tcp_to_serial_enabled:
                self._log("WARN", f"已忽略 {peer} 发来的 {len(data)} B: 当前模式不写串口")
                continue

            try:
                with self._serial_write_lock:
                    if self._serial is None:
                        raise serial.SerialException("serial port is closed")
                    self._serial.write(data)
                    self._serial.flush()
            except (OSError, serial.SerialException) as exc:
                reason = f"写串口失败: {exc}"
                self._log("ERROR", reason)
                self._stop_event.set()
                break

            if session:
                session.tcp_to_serial_bytes += len(data)
            self._traffic(peer, "TCP->串口", len(data), data)

        self._unregister_client(conn, reason)

    def _serial_to_tcp_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._serial is None:
                    break
                data = self._serial.read(SERIAL_READ_SIZE)
            except serial.SerialException as exc:
                self._log("ERROR", f"读串口失败: {exc}")
                self._stop_event.set()
                break

            if not data:
                continue

            self._broadcast(data)

    def _broadcast(self, data: bytes) -> None:
        with self._clients_lock:
            sessions = list(self._clients.values())

        dead: list[tuple[socket.socket, str]] = []
        for session in sessions:
            try:
                session.conn.sendall(data)
            except OSError as exc:
                dead.append((session.conn, str(exc)))
                continue

            session.serial_to_tcp_bytes += len(data)
            self._traffic(session.peer, "串口->TCP", len(data), data)

        for conn, reason in dead:
            self._unregister_client(conn, reason)

    def _get_session(self, conn: socket.socket) -> ClientSession | None:
        with self._clients_lock:
            return self._clients.get(conn)

    def _emit_clients(self) -> None:
        with self._clients_lock:
            clients = [
                {
                    "peer": session.peer,
                    "connected_at": session.connected_at,
                    "tcp_to_serial_bytes": session.tcp_to_serial_bytes,
                    "serial_to_tcp_bytes": session.serial_to_tcp_bytes,
                }
                for session in self._clients.values()
            ]
        self.emit("clients", {"clients": clients})

    def _record(self, peer: str, event: str, detail: str) -> None:
        self.emit(
            "record",
            {
                "time": dt.datetime.now().strftime("%H:%M:%S"),
                "peer": peer,
                "event": event,
                "detail": detail,
            },
        )

    def _traffic(self, peer: str, direction: str, byte_count: int, data: bytes) -> None:
        payload: dict[str, Any] = {
            "peer": peer,
            "direction": direction,
            "byte_count": byte_count,
            "hex": data.hex(" ").upper(),
        }
        self.emit("traffic", payload)
        self._emit_clients()

    def _log(self, level: str, message: str) -> None:
        self.emit(
            "log",
            {
                "time": dt.datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            },
        )

    def _emit_status(self, running: bool, text: str) -> None:
        self.emit("status", {"running": running, "text": text})

    def _close_server(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            safe_close(server)

    def _close_serial(self) -> None:
        with self._serial_write_lock:
            port = self._serial
            self._serial = None
        if port is not None:
            safe_close(port)

    def _close_all_clients(self) -> None:
        with self._clients_lock:
            clients = list(self._clients.keys())
        for conn in clients:
            self._unregister_client(conn, "服务停止")


class SerialNetworkRelay:
    def __init__(self, settings: RelaySettings, emit: Callable[[str, dict[str, Any]], None]) -> None:
        self.settings = settings
        self.emit = emit

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._network_socket: socket.socket | None = None
        self._udp_socket: socket.socket | None = None
        self._serial: serial.Serial | None = None
        self._serial_reader_started = False
        self._serial_write_lock = threading.RLock()
        self._serial_reconnect_lock = threading.Lock()
        self._serial_reconnect_thread: threading.Thread | None = None
        self._last_serial_drop_log = 0.0
        self._clients_lock = threading.Lock()
        self._clients: dict[Any, ClientSession] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="relay-main", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_network_socket()
        self._close_all_clients()
        self._close_serial()

    def _run(self) -> None:
        access = AccessControl(self.settings.access_mode, self.settings.access_rules)
        if access.invalid_rules:
            self._log("WARN", f"已忽略无效黑白名单规则: {', '.join(access.invalid_rules)}")

        try:
            self._preflight_network_bind()
            try:
                self._serial = self._open_serial(self.settings.serial)
                self._log("INFO", f"已打开串口 {self._serial_label()}")
                self._emit_serial_status("串口: 在线")
            except (OSError, serial.SerialException) as exc:
                if not self.settings.serial.auto_reconnect:
                    raise
                self._serial = None
                self._emit_serial_status("串口: 重连中")
                self._log(
                    "WARN",
                    f"打开串口失败: {exc}。服务会继续运行，并每 {self.settings.serial.reconnect_interval:g} 秒尝试重连。",
                )
                self._start_serial_reconnect()

            if self.settings.network_mode == "tcp_server":
                self._run_tcp_server(access)
            elif self.settings.network_mode == "tcp_client":
                self._run_tcp_client(access)
            elif self.settings.network_mode == "udp_server":
                self._run_udp_server(access)
            elif self.settings.network_mode == "udp_client":
                self._run_udp_client(access)
            else:
                raise ValueError(f"未知网络模式: {self.settings.network_mode}")
        except Exception as exc:
            if not self._stop_event.is_set():
                message = format_runtime_error(exc, self.settings)
                self._log("ERROR", message)
                self._emit_status(False, message)
        finally:
            self.stop()
            self._emit_status(False, "已停止")
            self._log("INFO", "服务已停止")

    def _preflight_network_bind(self) -> None:
        if self.settings.network_mode == "tcp_server":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((self.settings.bind_host, self.settings.local_port))
        elif self.settings.network_mode == "udp_server":
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind((self.settings.bind_host, self.settings.local_port))

    def _run_tcp_server(self, access: AccessControl) -> None:
        server = socket.create_server((self.settings.bind_host, self.settings.local_port), reuse_port=False)
        self._network_socket = server
        server.listen(16)
        server.settimeout(0.5)

        self._log("INFO", f"TCP Server 正在监听 {self.settings.bind_host}:{self.settings.local_port}")
        self._emit_status(True, "运行中")
        self._start_serial_reader()

        while not self._stop_event.is_set():
            try:
                conn, addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    raise
                break

            peer_ip, peer_port = addr[0], addr[1]
            peer = f"TCP {peer_ip}:{peer_port}"
            if not access.allows(peer_ip):
                self._record(peer, "已拒绝", "命中访问控制")
                self._log("WARN", f"已拒绝对端 {peer}")
                safe_close(conn)
                continue

            key = self._register_tcp_client(conn, peer_ip, peer_port)
            if key is None:
                self._record(peer, "已拒绝", "当前只允许一个对端")
                self._log("WARN", f"已拒绝对端 {peer}: 当前只允许一个对端")
                safe_close(conn)
                continue

            threading.Thread(
                target=self._tcp_to_serial_loop,
                args=(key, conn),
                name=f"tcp-client-{peer_ip}:{peer_port}",
                daemon=True,
            ).start()

    def _run_tcp_client(self, access: AccessControl) -> None:
        remote = resolve_ipv4_endpoint(self.settings.remote_host, self.settings.remote_port, socket.SOCK_STREAM)
        if not access.allows(remote[0]):
            raise PermissionError(f"目标地址 {remote[0]} 被访问控制拒绝")

        self._start_serial_reader()

        while not self._stop_event.is_set():
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if self.settings.bind_host != BIND_ALL_VALUE or self.settings.local_port > 0:
                    conn.bind((self.settings.bind_host, self.settings.local_port))
                conn.settimeout(8)
                conn.connect(remote)
                conn.settimeout(0.5)
                self._network_socket = conn

                key = self._register_tcp_client(conn, remote[0], remote[1])
                if key is None:
                    raise RuntimeError("无法注册 TCP 连接")

                self._log("INFO", f"TCP Client 已连接 {remote[0]}:{remote[1]}")
                self._emit_status(True, "运行中")
                self._tcp_to_serial_loop(key, conn)
            except OSError as exc:
                safe_close(conn)
                if self._stop_event.is_set():
                    break
                self._log("WARN", f"TCP Client 连接失败或已断开: {exc}")
                self._notify("TCP Client 已断开", str(exc))

            if not self.settings.network_auto_reconnect or self._stop_event.is_set():
                break
            self._emit_status(True, "运行中（网络重连中）")
            self._log("INFO", f"{self.settings.network_reconnect_interval:g} 秒后重连 TCP 目标 {remote[0]}:{remote[1]}")
            self._stop_event.wait(self.settings.network_reconnect_interval)

    def _run_udp_server(self, access: AccessControl) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((self.settings.bind_host, self.settings.local_port))
        sock.settimeout(0.5)
        self._network_socket = sock
        self._udp_socket = sock

        self._log("INFO", f"UDP Server 正在监听 {self.settings.bind_host}:{self.settings.local_port}")
        self._emit_status(True, "运行中")
        self._start_serial_reader()

        while not self._stop_event.is_set():
            try:
                data, addr = sock.recvfrom(TCP_READ_SIZE)
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_event.is_set():
                    raise
                break

            peer_ip, peer_port = addr[0], addr[1]
            peer = f"UDP {peer_ip}:{peer_port}"
            if not access.allows(peer_ip):
                self._record(peer, "已拒绝", "命中访问控制")
                self._log("WARN", f"已拒绝对端 {peer}")
                continue

            session = self._register_udp_peer(addr)
            if session is None:
                self._record(peer, "已拒绝", "当前只允许一个对端")
                self._log("WARN", f"已拒绝对端 {peer}: 当前只允许一个对端")
                continue
            self._write_serial_from_network(session, data)

    def _run_udp_client(self, access: AccessControl) -> None:
        remote = resolve_ipv4_endpoint(self.settings.remote_host, self.settings.remote_port, socket.SOCK_DGRAM)
        if not access.allows(remote[0]):
            raise PermissionError(f"目标地址 {remote[0]} 被访问控制拒绝")

        self._start_serial_reader()

        while not self._stop_event.is_set():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if self.settings.bind_host != BIND_ALL_VALUE or self.settings.local_port > 0:
                    sock.bind((self.settings.bind_host, self.settings.local_port))
                sock.connect(remote)
                sock.settimeout(0.5)
                self._network_socket = sock
                self._udp_socket = sock

                session = self._register_udp_peer(remote)
                if session is None:
                    raise RuntimeError("无法注册 UDP 对端")

                self._log("INFO", f"UDP Client 已连接 {remote[0]}:{remote[1]}")
                self._emit_status(True, "运行中")

                while not self._stop_event.is_set():
                    try:
                        data = sock.recv(TCP_READ_SIZE)
                    except socket.timeout:
                        continue
                    if data:
                        self._write_serial_from_network(session, data)
            except OSError as exc:
                safe_close(sock)
                if not self._stop_event.is_set():
                    self._log("WARN", f"UDP Client 网络错误: {exc}")
                    self._notify("UDP Client 网络错误", str(exc))
            finally:
                self._udp_socket = None
                self._network_socket = None
                self._unregister_session(("UDP", remote[0], remote[1]), "网络重连")

            if not self.settings.network_auto_reconnect or self._stop_event.is_set():
                break
            self._emit_status(True, "运行中（网络重连中）")
            self._log("INFO", f"{self.settings.network_reconnect_interval:g} 秒后重连 UDP 目标 {remote[0]}:{remote[1]}")
            self._stop_event.wait(self.settings.network_reconnect_interval)

    def _start_serial_reader(self) -> None:
        if self._serial_reader_started:
            return
        self._serial_reader_started = True
        threading.Thread(target=self._serial_to_network_loop, name="serial-reader", daemon=True).start()

    def _open_serial(self, settings: SerialSettings) -> serial.Serial:
        bytesize = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }[settings.data_bits]
        parity = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }[settings.parity]
        stopbits = {
            "1": serial.STOPBITS_ONE,
            "1.5": serial.STOPBITS_ONE_POINT_FIVE,
            "2": serial.STOPBITS_TWO,
        }[settings.stop_bits]

        port = serial.Serial(
            port=settings.port,
            baudrate=settings.baudrate,
            bytesize=bytesize,
            parity=parity,
            stopbits=stopbits,
            timeout=0.05,
            write_timeout=3,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        port.dtr = settings.dtr
        port.rts = settings.rts
        if settings.reset_input:
            port.reset_input_buffer()
        return port

    def _serial_label(self) -> str:
        serial_settings = self.settings.serial
        return (
            f"{serial_settings.port} {serial_settings.baudrate}-"
            f"{serial_settings.data_bits}{serial_settings.parity}{serial_settings.stop_bits}"
        )

    def _register_tcp_client(self, conn: socket.socket, peer_ip: str, peer_port: int) -> socket.socket | None:
        with self._clients_lock:
            if self.settings.client_policy == "single" and self._clients:
                return None
            session = ClientSession(
                peer_ip=peer_ip,
                peer_port=peer_port,
                protocol="TCP",
                connected_at=time.time(),
                conn=conn,
            )
            self._clients[conn] = session

        self._record(session.peer, "已连接", "")
        self._log("INFO", f"对端已连接 {session.peer}")
        self._emit_clients()
        return conn

    def _register_udp_peer(self, addr: tuple[str, int]) -> ClientSession | None:
        key = ("UDP", addr[0], addr[1])
        with self._clients_lock:
            existing = self._clients.get(key)
            if existing is not None:
                return existing
            if self.settings.client_policy == "single" and self._clients:
                return None
            session = ClientSession(
                peer_ip=addr[0],
                peer_port=addr[1],
                protocol="UDP",
                connected_at=time.time(),
                udp_addr=addr,
            )
            self._clients[key] = session

        self._record(session.peer, "已连接", "")
        self._log("INFO", f"对端已连接 {session.peer}")
        self._emit_clients()
        return session

    def _unregister_session(self, key: Any, reason: str) -> None:
        with self._clients_lock:
            session = self._clients.pop(key, None)

        if session is None:
            if isinstance(key, socket.socket):
                safe_close(key)
            return

        if session.conn is not None:
            safe_close(session.conn)
        detail = (
            f"{reason}; 网络->串口 {session.network_to_serial_bytes} B, "
            f"串口->网络 {session.serial_to_network_bytes} B"
        )
        self._record(session.peer, "已断开", detail)
        self._log("INFO", f"对端已断开 {session.peer}: {detail}")
        self._notify("对端已断开", f"{session.peer} {reason}")
        self._emit_clients()

    def _tcp_to_serial_loop(self, key: socket.socket, conn: socket.socket) -> None:
        reason = "对端关闭"
        while not self._stop_event.is_set():
            try:
                data = conn.recv(TCP_READ_SIZE)
            except socket.timeout:
                continue
            except OSError as exc:
                reason = str(exc)
                break

            if not data:
                break

            session = self._get_session(key)
            if session is None:
                break
            if not self._write_serial_from_network(session, data):
                reason = "写串口失败"
                break

        self._unregister_session(key, reason)

    def _write_serial_from_network(self, session: ClientSession, data: bytes) -> bool:
        if self._serial is None:
            self._drop_serial_data(session, data, "串口离线")
            self._start_serial_reconnect()
            return True

        try:
            with self._serial_write_lock:
                if self._serial is None:
                    self._drop_serial_data(session, data, "串口离线")
                    self._start_serial_reconnect()
                    return True
                self._serial.write(data)
                self._serial.flush()
        except (OSError, serial.SerialException) as exc:
            self._drop_serial_data(session, data, f"写串口失败: {exc}")
            return self._handle_serial_error("写串口失败", exc)

        session.network_to_serial_bytes += len(data)
        self._traffic(session.peer, "网络->串口", len(data), data)
        return True

    def _serial_to_network_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._serial is None:
                self._start_serial_reconnect()
                self._stop_event.wait(0.2)
                continue

            try:
                if self._serial is None:
                    continue
                data = self._serial.read(SERIAL_READ_SIZE)
            except (OSError, serial.SerialException) as exc:
                if not self._handle_serial_error("读串口失败", exc):
                    break
                continue

            if data:
                self._broadcast(data)

    def _drop_serial_data(self, session: ClientSession, data: bytes, reason: str) -> None:
        now = time.monotonic()
        if now - self._last_serial_drop_log >= 2:
            self._last_serial_drop_log = now
            self._log("WARN", f"{reason}，已丢弃来自 {session.peer} 的 {len(data)} B 网络数据。")

    def _handle_serial_error(self, action: str, exc: Exception) -> bool:
        if self._stop_event.is_set():
            return False

        if not self.settings.serial.auto_reconnect:
            self._log("ERROR", f"{action}: {exc}")
            self._emit_serial_status("串口: 错误")
            self._stop_event.set()
            return False

        self._mark_serial_offline()
        self._emit_serial_status("串口: 重连中")
        self._log(
            "WARN",
            f"{action}: {exc}。串口已离线，服务保持运行，并每 {self.settings.serial.reconnect_interval:g} 秒尝试重连。",
        )
        self._emit_status(True, "运行中（串口重连中）")
        self._start_serial_reconnect()
        return True

    def _mark_serial_offline(self) -> None:
        with self._serial_write_lock:
            port = self._serial
            self._serial = None
        if port is not None:
            safe_close(port)

    def _start_serial_reconnect(self) -> None:
        if not self.settings.serial.auto_reconnect or self._stop_event.is_set():
            return

        with self._serial_reconnect_lock:
            if self._serial is not None:
                return
            if self._serial_reconnect_thread is not None and self._serial_reconnect_thread.is_alive():
                return
            self._serial_reconnect_thread = threading.Thread(
                target=self._serial_reconnect_loop,
                name="serial-reconnect",
                daemon=True,
            )
            self._serial_reconnect_thread.start()

    def _serial_reconnect_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._serial_write_lock:
                if self._serial is not None:
                    return

            try:
                port = self._open_serial(self.settings.serial)
            except (OSError, serial.SerialException) as exc:
                self._log(
                    "WARN",
                    f"串口重连失败: {exc}。{self.settings.serial.reconnect_interval:g} 秒后重试。",
                )
                self._stop_event.wait(self.settings.serial.reconnect_interval)
                continue

            with self._serial_write_lock:
                if self._serial is None:
                    self._serial = port
                    port = None
            if port is not None:
                safe_close(port)
                return

            self._log("INFO", f"串口已重新连接 {self._serial_label()}")
            self._emit_serial_status("串口: 在线")
            self._emit_status(True, "运行中")
            self._notify("串口已重新连接", self._serial_label())
            return

    def _broadcast(self, data: bytes) -> None:
        with self._clients_lock:
            items = list(self._clients.items())

        dead: list[tuple[Any, str]] = []
        for key, session in items:
            try:
                if session.conn is not None:
                    session.conn.sendall(data)
                elif session.udp_addr is not None and self._udp_socket is not None:
                    if self.settings.network_mode == "udp_client":
                        self._udp_socket.send(data)
                    else:
                        self._udp_socket.sendto(data, session.udp_addr)
                else:
                    continue
            except OSError as exc:
                dead.append((key, str(exc)))
                continue

            session.serial_to_network_bytes += len(data)
            self._traffic(session.peer, "串口->网络", len(data), data)

        for key, reason in dead:
            self._unregister_session(key, reason)

    def _get_session(self, key: Any) -> ClientSession | None:
        with self._clients_lock:
            return self._clients.get(key)

    def _emit_clients(self) -> None:
        with self._clients_lock:
            clients = [
                {
                    "peer": session.peer,
                    "connected_at": session.connected_at,
                    "network_to_serial_bytes": session.network_to_serial_bytes,
                    "serial_to_network_bytes": session.serial_to_network_bytes,
                }
                for session in self._clients.values()
            ]
        self.emit("clients", {"clients": clients})

    def _record(self, peer: str, event: str, detail: str) -> None:
        self.emit(
            "record",
            {
                "time": dt.datetime.now().strftime("%H:%M:%S"),
                "peer": peer,
                "event": event,
                "detail": detail,
            },
        )

    def _traffic(self, peer: str, direction: str, byte_count: int, data: bytes) -> None:
        payload: dict[str, Any] = {
            "peer": peer,
            "direction": direction,
            "byte_count": byte_count,
            "hex": data.hex(" ").upper(),
        }
        self.emit("traffic", payload)
        self._emit_clients()

    def _log(self, level: str, message: str) -> None:
        self.emit(
            "log",
            {
                "time": dt.datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": message,
            },
        )

    def _emit_status(self, running: bool, text: str) -> None:
        self.emit("status", {"running": running, "text": text})

    def _emit_serial_status(self, text: str) -> None:
        self.emit("serial_status", {"text": text})

    def _notify(self, title: str, message: str) -> None:
        self.emit("notify", {"title": title, "message": message})

    def _close_network_socket(self) -> None:
        network_socket = self._network_socket
        self._network_socket = None
        self._udp_socket = None
        if network_socket is not None:
            safe_close(network_socket)

    def _close_serial(self) -> None:
        port = self._serial
        self._serial = None
        if port is not None:
            safe_close(port)

    def _close_all_clients(self) -> None:
        with self._clients_lock:
            keys = list(self._clients.keys())
        for key in keys:
            self._unregister_session(key, "服务停止")


class SerialRelayApp(tk.Tk):
    def __init__(self, start_minimized: bool = False) -> None:
        super().__init__()
        self.title(APP_NAME)
        self._set_window_icon()
        self.minsize(980, 680)

        self.event_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self.log_store = LogStore(log_dir_path())
        self.relay: SerialNetworkRelay | None = None
        self.running = False
        self._allow_exit = False
        self._start_minimized = start_minimized
        self.tray_icon: pystray.Icon | None = None

        self._build_variables()
        self._build_ui()
        self._load_settings()
        self._refresh_ports()
        self._refresh_bind_hosts()
        self._update_network_mode_state()
        self._create_tray_icon()
        self._append_log("INFO", f"{APP_NAME} v{APP_VERSION} 已启动，程序目录: {app_root()}")
        if self.auto_start_service_var.get():
            self.after(700, self._start)
        if self._start_minimized:
            self.after(300, self._hide_to_tray)
        self._poll_events()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self) -> None:
        icon_path = resource_path(APP_ICON_PATH)
        if not icon_path.exists():
            return
        try:
            self._app_icon = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(True, self._app_icon)
        except tk.TclError:
            pass

    def _build_variables(self) -> None:
        self.serial_port_var = tk.StringVar()
        self.baudrate_var = tk.StringVar(value="9600")
        self.data_bits_var = tk.StringVar(value="8")
        self.parity_var = tk.StringVar(value="N")
        self.stop_bits_var = tk.StringVar(value="1")
        self.dtr_var = tk.BooleanVar(value=True)
        self.rts_var = tk.BooleanVar(value=True)
        self.reset_input_var = tk.BooleanVar(value=True)
        self.serial_auto_reconnect_var = tk.BooleanVar(value=True)
        self.serial_reconnect_interval_var = tk.StringVar(value="2")

        self.network_mode_var = tk.StringVar(value=NETWORK_MODE_LABELS["tcp_server"])
        self.bind_host_var = tk.StringVar(value=BIND_ALL_LABEL)
        self.local_port_var = tk.StringVar(value="10123")
        self.remote_host_var = tk.StringVar(value="")
        self.remote_port_var = tk.StringVar(value="10123")
        self.client_policy_var = tk.StringVar(value=CLIENT_POLICY_LABELS["single"])
        self.access_mode_var = tk.StringVar(value=ACCESS_MODE_LABELS["allow_all"])
        self.hex_log_var = tk.BooleanVar(value=False)
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.start_with_windows_var = tk.BooleanVar(value=False)
        self.auto_start_service_var = tk.BooleanVar(value=False)
        self.close_to_tray_var = tk.BooleanVar(value=True)
        self.network_auto_reconnect_var = tk.BooleanVar(value=True)
        self.network_reconnect_interval_var = tk.StringVar(value="3")

        self.status_var = tk.StringVar(value="已停止")
        self.serial_status_var = tk.StringVar(value="串口: 未启动")
        self.address_hint_var = tk.StringVar(value="")
        self.network_mode_var.trace_add("write", lambda *_: self._on_network_mode_changed())
        self.bind_host_var.trace_add("write", lambda *_: self._update_address_hint())
        self.local_port_var.trace_add("write", lambda *_: self._update_address_hint())
        self.remote_host_var.trace_add("write", lambda *_: self._update_address_hint())
        self.remote_port_var.trace_add("write", lambda *_: self._update_address_hint())

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="状态:").grid(row=0, column=0, sticky="w")
        self.status_label = ttk.Label(top, textvariable=self.status_var, foreground="#9a3412")
        self.status_label.grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Label(top, textvariable=self.serial_status_var).grid(row=0, column=2, sticky="w", padx=(6, 18))
        ttk.Label(top, textvariable=self.address_hint_var).grid(row=0, column=3, sticky="e", padx=(6, 14))
        self.start_button = ttk.Button(top, text="启动", command=self._start)
        self.start_button.grid(row=0, column=4, padx=(0, 8))
        self.stop_button = ttk.Button(top, text="停止", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=5)

        settings = ttk.Frame(self, padding=(10, 4, 10, 8))
        settings.grid(row=1, column=0, sticky="ew")
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(2, weight=1)

        self._build_serial_frame(settings).grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_network_frame(settings).grid(row=0, column=1, sticky="nsew", padx=4)
        self._build_policy_frame(settings).grid(row=0, column=2, sticky="nsew", padx=(8, 0))

        lower = ttk.PanedWindow(self, orient=tk.VERTICAL)
        lower.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self._build_client_frame(lower)
        self._build_log_frame(lower)

    def _build_serial_frame(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="串口")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="串口").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.port_combo = ttk.Combobox(frame, textvariable=self.serial_port_var, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=4, pady=(8, 4))
        ttk.Button(frame, text="刷新", command=self._refresh_ports).grid(row=0, column=2, padx=(4, 8), pady=(8, 4))

        ttk.Label(frame, text="波特率").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(
            frame,
            textvariable=self.baudrate_var,
            values=("1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400"),
        ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4)

        ttk.Label(frame, text="数据位").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(frame, textvariable=self.data_bits_var, values=("5", "6", "7", "8"), state="readonly").grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4
        )

        ttk.Label(frame, text="校验").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(frame, textvariable=self.parity_var, values=("N", "E", "O", "M", "S"), state="readonly").grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4
        )

        ttk.Label(frame, text="停止位").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(frame, textvariable=self.stop_bits_var, values=("1", "1.5", "2"), state="readonly").grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4
        )

        checks = ttk.Frame(frame)
        checks.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        ttk.Checkbutton(checks, text="DTR", variable=self.dtr_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(checks, text="RTS", variable=self.rts_var).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(checks, text="启动时清空串口缓冲", variable=self.reset_input_var).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(checks, text="串口断开自动重连", variable=self.serial_auto_reconnect_var).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(checks, text="重连间隔(秒)").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Combobox(
            checks,
            textvariable=self.serial_reconnect_interval_var,
            values=("0.5", "1", "2", "3", "5", "10"),
            width=8,
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(4, 0))
        return frame

    def _build_network_frame(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="网络")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="网络模式").grid(row=0, column=0, sticky="w", padx=8, pady=(8, 4))
        self.network_mode_combo = ttk.Combobox(
            frame,
            textvariable=self.network_mode_var,
            values=tuple(NETWORK_MODES.keys()),
            state="readonly",
        )
        self.network_mode_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=(8, 4))

        ttk.Label(frame, text="绑定地址").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.bind_combo = ttk.Combobox(frame, textvariable=self.bind_host_var, state="readonly")
        self.bind_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(frame, text="刷新", command=self._refresh_bind_hosts).grid(row=1, column=2, padx=(4, 8), pady=4)

        ttk.Label(frame, text="本地端口").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        self.local_port_entry = ttk.Entry(frame, textvariable=self.local_port_var)
        self.local_port_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4)

        ttk.Label(frame, text="目标地址").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        self.remote_host_entry = ttk.Entry(frame, textvariable=self.remote_host_var)
        self.remote_host_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4)

        ttk.Label(frame, text="目标端口").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        self.remote_port_entry = ttk.Entry(frame, textvariable=self.remote_port_var)
        self.remote_port_entry.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4)

        ttk.Label(frame, text="对端策略").grid(row=5, column=0, sticky="w", padx=8, pady=4)
        self.client_policy_combo = ttk.Combobox(
            frame,
            textvariable=self.client_policy_var,
            values=tuple(CLIENT_POLICIES.keys()),
            state="readonly",
        )
        self.client_policy_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(4, 8), pady=4)

        options = ttk.Frame(frame)
        options.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 8))
        ttk.Checkbutton(options, text="十六进制日志", variable=self.hex_log_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="日志自动滚动", variable=self.autoscroll_var).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(options, text="Client 自动重连", variable=self.network_auto_reconnect_var).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(options, text="网络重连间隔(秒)").grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(6, 0))
        ttk.Combobox(
            options,
            textvariable=self.network_reconnect_interval_var,
            values=("1", "2", "3", "5", "10", "30"),
            width=8,
        ).grid(row=1, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Button(options, text="复制地址", command=self._copy_address).grid(row=2, column=0, sticky="w", pady=(8, 0))
        return frame

    def _build_policy_frame(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="黑白名单")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        ttk.Combobox(
            frame,
            textvariable=self.access_mode_var,
            values=tuple(ACCESS_MODES.keys()),
            state="readonly",
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ttk.Label(frame, text="每行一个 IP / CIDR / 通配符").grid(row=1, column=0, sticky="w", padx=8, pady=(2, 0))
        self.access_text = tk.Text(frame, height=8, wrap="none", undo=True)
        self.access_text.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.access_text.yview)
        scroll.grid(row=2, column=1, sticky="ns", pady=4)
        self.access_text.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        ttk.Button(buttons, text="清空", command=lambda: self.access_text.delete("1.0", "end")).grid(row=0, column=0)

        run_options = ttk.Frame(frame)
        run_options.grid(row=4, column=0, sticky="ew", padx=8, pady=(0, 8))
        ttk.Checkbutton(run_options, text="开机启动", variable=self.start_with_windows_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(run_options, text="启动后自动启动服务", variable=self.auto_start_service_var).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Checkbutton(run_options, text="关闭按钮最小化到托盘", variable=self.close_to_tray_var).grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        return frame

    def _build_client_frame(self, parent: ttk.PanedWindow) -> None:
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        active_tab = ttk.Frame(notebook)
        active_tab.columnconfigure(0, weight=1)
        active_tab.rowconfigure(0, weight=1)
        self.clients_tree = ttk.Treeview(
            active_tab,
            columns=("peer", "connected", "network_to_serial", "serial_to_network"),
            show="headings",
            height=5,
        )
        self.clients_tree.heading("peer", text="对端")
        self.clients_tree.heading("connected", text="连接时间")
        self.clients_tree.heading("network_to_serial", text="网络->串口")
        self.clients_tree.heading("serial_to_network", text="串口->网络")
        self.clients_tree.column("peer", width=180, anchor="w")
        self.clients_tree.column("connected", width=120, anchor="w")
        self.clients_tree.column("network_to_serial", width=110, anchor="e")
        self.clients_tree.column("serial_to_network", width=110, anchor="e")
        self.clients_tree.grid(row=0, column=0, sticky="nsew")
        active_scroll = ttk.Scrollbar(active_tab, orient="vertical", command=self.clients_tree.yview)
        active_scroll.grid(row=0, column=1, sticky="ns")
        self.clients_tree.configure(yscrollcommand=active_scroll.set)

        records_tab = ttk.Frame(notebook)
        records_tab.columnconfigure(0, weight=1)
        records_tab.rowconfigure(0, weight=1)
        self.records_tree = ttk.Treeview(
            records_tab,
            columns=("time", "peer", "event", "detail"),
            show="headings",
            height=7,
        )
        for column, title, width in (
            ("time", "时间", 80),
            ("peer", "对端", 180),
            ("event", "事件", 90),
            ("detail", "详情", 420),
        ):
            self.records_tree.heading(column, text=title)
            self.records_tree.column(column, width=width, anchor="w")
        self.records_tree.grid(row=0, column=0, sticky="nsew")
        records_scroll = ttk.Scrollbar(records_tab, orient="vertical", command=self.records_tree.yview)
        records_scroll.grid(row=0, column=1, sticky="ns")
        self.records_tree.configure(yscrollcommand=records_scroll.set)

        notebook.add(active_tab, text="当前连接")
        notebook.add(records_tab, text="连接记录")
        parent.add(frame, weight=1)

    def _build_log_frame(self, parent: ttk.PanedWindow) -> None:
        frame = ttk.LabelFrame(parent, text="运行日志")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(frame, height=12, wrap="none", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        y_scroll.grid(row=0, column=1, sticky="ns", pady=8)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        x_scroll.grid(row=1, column=0, sticky="ew", padx=(8, 0), pady=(0, 8))
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        ttk.Button(buttons, text="清空日志", command=self._clear_logs).grid(row=0, column=0)
        ttk.Button(buttons, text="导出日志", command=self._export_logs).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(buttons, text="查看日志", command=self._open_log_viewer).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(buttons, text="导出配置", command=self._export_settings).grid(row=0, column=3, padx=(8, 0))
        ttk.Button(buttons, text="导入配置", command=self._import_settings).grid(row=0, column=4, padx=(8, 0))
        parent.add(frame, weight=2)

    def _refresh_ports(self) -> None:
        ports = list(list_ports.comports())
        values = [port.device for port in ports]
        self.port_combo.configure(values=values)
        if values and self.serial_port_var.get() not in values:
            self.serial_port_var.set(values[0])
        if not values:
            self.serial_port_var.set("")

    def _refresh_bind_hosts(self) -> None:
        current = bind_host_value(self.bind_host_var.get())
        values = [BIND_ALL_LABEL, "127.0.0.1"]
        for ip in detect_local_ipv4_addresses():
            if ip not in values:
                values.append(ip)
        self.bind_combo.configure(values=values)
        current_display = bind_host_display(current)
        self.bind_host_var.set(current_display if current_display in values else values[0])
        self._update_address_hint()

    def _on_network_mode_changed(self) -> None:
        mode = self._network_mode_value()
        if mode in {"tcp_client", "udp_client"} and self.local_port_var.get() == "10123":
            self.local_port_var.set("0")
        elif mode in {"tcp_server", "udp_server"} and self.local_port_var.get() == "0":
            self.local_port_var.set(self.remote_port_var.get().strip() or "10123")
        self._update_network_mode_state()

    def _update_network_mode_state(self) -> None:
        if not hasattr(self, "remote_host_entry"):
            return

        mode = self._network_mode_value()
        remote_state = "normal" if mode in {"tcp_client", "udp_client"} else "disabled"
        for widget in (self.remote_host_entry, self.remote_port_entry):
            widget.configure(state=remote_state)

        policy_state = "readonly" if mode in {"tcp_server", "udp_server"} else "disabled"
        self.client_policy_combo.configure(state=policy_state)
        self._update_address_hint()

    def _network_mode_value(self) -> str:
        return NETWORK_MODES.get(self.network_mode_var.get(), "tcp_server")

    def _copy_address(self) -> None:
        address = self._current_connect_address()
        self.clipboard_clear()
        self.clipboard_append(address)
        self._append_log("INFO", f"已复制连接地址: {address}")

    def _create_tray_icon(self) -> None:
        if self.tray_icon is not None:
            return
        try:
            image = Image.open(resource_path(APP_ICON_PATH))
        except OSError:
            image = Image.new("RGBA", (64, 64), "#2563eb")
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", lambda: self.after(0, self._show_window)),
            pystray.MenuItem("启动服务", lambda: self.after(0, self._start)),
            pystray.MenuItem("停止服务", lambda: self.after(0, self._stop)),
            pystray.MenuItem("退出", lambda: self.after(0, self._exit_from_tray)),
        )
        self.tray_icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
        threading.Thread(target=self.tray_icon.run, name="tray-icon", daemon=True).start()

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _hide_to_tray(self) -> None:
        self.withdraw()
        self._notify(APP_NAME, "程序已最小化到托盘")

    def _exit_from_tray(self) -> None:
        self._allow_exit = True
        self._on_close()

    def _notify(self, title: str, message: str) -> None:
        try:
            if self.tray_icon is not None:
                self.tray_icon.notify(message, title)
        except Exception:
            pass

    def _current_connect_address(self) -> str:
        mode = self._network_mode_value()
        if mode in {"tcp_client", "udp_client"}:
            host = self.remote_host_var.get().strip() or "目标IP"
            port = self.remote_port_var.get().strip()
            return f"{host}:{port}"

        bind_host = bind_host_value(self.bind_host_var.get())
        port = self.local_port_var.get().strip()
        if bind_host == BIND_ALL_VALUE:
            host = BIND_ALL_LABEL
        else:
            host = bind_host
        return f"{host}:{port}"

    def _update_address_hint(self) -> None:
        mode = self._network_mode_value()
        prefix = "目标地址" if mode in {"tcp_client", "udp_client"} else "监听地址"
        self.address_hint_var.set(f"{prefix}: {self._current_connect_address()}")

    def _start(self) -> None:
        if self.running:
            return
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        self._save_settings()
        self._set_controls_running(True)
        self._append_log("INFO", "正在启动服务")
        self._append_log("INFO", f"版本: {APP_VERSION}; 启动参数: {settings_summary(settings)}")
        self.relay = SerialNetworkRelay(settings, lambda kind, payload: self.event_queue.put((kind, payload)))
        self.relay.start()

    def _stop(self) -> None:
        if self.relay is not None:
            self._append_log("INFO", "正在停止服务")
            self.relay.stop()

    def _collect_settings(self) -> RelaySettings:
        serial_port = self.serial_port_var.get().strip()
        if not serial_port:
            raise ValueError("请选择串口。")

        try:
            baudrate = int(self.baudrate_var.get())
            local_port = int(self.local_port_var.get())
            remote_port = int(self.remote_port_var.get())
            reconnect_interval = float(self.serial_reconnect_interval_var.get())
            network_reconnect_interval = float(self.network_reconnect_interval_var.get())
        except ValueError as exc:
            raise ValueError("波特率、本地端口、目标端口和重连间隔必须是数字。") from exc

        if reconnect_interval <= 0:
            raise ValueError("串口重连间隔必须大于 0 秒。")
        if network_reconnect_interval <= 0:
            raise ValueError("网络重连间隔必须大于 0 秒。")

        network_mode = self._network_mode_value()
        if network_mode in {"tcp_server", "udp_server"} and not 1 <= local_port <= 65535:
            raise ValueError("Server 模式的本地端口必须在 1 到 65535 之间。")
        if network_mode in {"tcp_client", "udp_client"}:
            if not self.remote_host_var.get().strip():
                raise ValueError("Client 模式必须填写目标地址。")
            if not 1 <= remote_port <= 65535:
                raise ValueError("Client 模式的目标端口必须在 1 到 65535 之间。")
        if not 0 <= local_port <= 65535:
            raise ValueError("本地端口必须在 0 到 65535 之间。")

        serial_settings = SerialSettings(
            port=serial_port,
            baudrate=baudrate,
            data_bits=int(self.data_bits_var.get()),
            parity=self.parity_var.get(),
            stop_bits=self.stop_bits_var.get(),
            dtr=self.dtr_var.get(),
            rts=self.rts_var.get(),
            reset_input=self.reset_input_var.get(),
            auto_reconnect=self.serial_auto_reconnect_var.get(),
            reconnect_interval=reconnect_interval,
        )

        access_rules = tuple(split_rules(self.access_text.get("1.0", "end")))
        return RelaySettings(
            serial=serial_settings,
            network_mode=network_mode,
            bind_host=bind_host_value(self.bind_host_var.get()),
            local_port=local_port,
            remote_host=self.remote_host_var.get().strip(),
            remote_port=remote_port,
            client_policy=CLIENT_POLICIES[self.client_policy_var.get()],
            access_mode=ACCESS_MODES[self.access_mode_var.get()],
            access_rules=access_rules,
            hex_log=self.hex_log_var.get(),
            network_auto_reconnect=self.network_auto_reconnect_var.get(),
            network_reconnect_interval=network_reconnect_interval,
        )

    def _set_controls_running(self, running: bool) -> None:
        self.running = running
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.status_label.configure(foreground="#15803d" if running else "#9a3412")
        self.status_var.set("运行中" if running else "已停止")

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(kind, payload)
        self.after(100, self._poll_events)

    def _handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "status":
            self._set_controls_running(bool(payload["running"]))
            self.status_var.set(str(payload["text"]))
        elif kind == "log":
            self._append_log(str(payload["level"]), str(payload["message"]), str(payload["time"]))
        elif kind == "traffic":
            self._handle_traffic(payload)
        elif kind == "record":
            self._insert_record(payload)
        elif kind == "clients":
            self._update_clients(payload["clients"])
        elif kind == "serial_status":
            self.serial_status_var.set(str(payload["text"]))
        elif kind == "notify":
            self._notify(str(payload["title"]), str(payload["message"]))

    def _handle_traffic(self, payload: dict[str, Any]) -> None:
        message = f"{payload['peer']} {payload['direction']} {payload['byte_count']} B"
        if self.hex_log_var.get():
            message += f"  {payload['hex']}"
        self.log_store.log_data(str(payload["peer"]), str(payload["direction"]), int(payload["byte_count"]), str(payload["hex"]))
        self._append_log("DATA", message)

    def _insert_record(self, payload: dict[str, Any]) -> None:
        self.records_tree.insert(
            "",
            "end",
            values=(payload["time"], payload["peer"], payload["event"], payload["detail"]),
        )
        children = self.records_tree.get_children()
        if len(children) > 1000:
            self.records_tree.delete(children[0])
        self.records_tree.yview_moveto(1)
        self.log_store.log_system(str(payload["event"]), str(payload["detail"]), "连接记录", str(payload["peer"]))

    def _update_clients(self, clients: list[dict[str, Any]]) -> None:
        for item in self.clients_tree.get_children():
            self.clients_tree.delete(item)
        for client in clients:
            connected = dt.datetime.fromtimestamp(client["connected_at"]).strftime("%H:%M:%S")
            self.clients_tree.insert(
                "",
                "end",
                values=(
                    client["peer"],
                    connected,
                    format_bytes_count(client["network_to_serial_bytes"]),
                    format_bytes_count(client["serial_to_network_bytes"]),
                ),
            )

    def _append_log(self, level: str, message: str, time_text: str | None = None) -> None:
        if time_text is None:
            time_text = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{time_text}] [{level}] {message}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        children = int(float(self.log_text.index("end-1c").split(".")[0]))
        if children > 2000:
            self.log_text.delete("1.0", "200.0")
        if self.autoscroll_var.get():
            self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if level != "DATA":
            self.log_store.log_system(level, message, "运行日志")

    def _clear_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for item in self.records_tree.get_children():
            self.records_tree.delete(item)

    def _export_logs(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出日志",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        text = self.log_text.get("1.0", "end")
        Path(path).write_text(text, encoding="utf-8")
        self._append_log("INFO", f"日志已导出: {path}")

    def _open_log_viewer(self) -> None:
        LogViewer(self, self.log_store)

    def _export_settings(self) -> None:
        path = filedialog.asksaveasfilename(
            title="导出配置",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        self._save_settings()
        Path(path).write_text(json.dumps(self._settings_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._append_log("INFO", f"配置已导出: {path}")

    def _import_settings(self) -> None:
        path = filedialog.askopenfilename(
            title="导入配置",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"导入配置失败: {exc}")
            return
        self._apply_settings_dict(data)
        self._save_settings()
        self._append_log("INFO", f"配置已导入: {path}")

    def _settings_dict(self) -> dict[str, Any]:
        return {
            "serial_port": self.serial_port_var.get(),
            "baudrate": self.baudrate_var.get(),
            "data_bits": self.data_bits_var.get(),
            "parity": self.parity_var.get(),
            "stop_bits": self.stop_bits_var.get(),
            "dtr": self.dtr_var.get(),
            "rts": self.rts_var.get(),
            "reset_input": self.reset_input_var.get(),
            "serial_auto_reconnect": self.serial_auto_reconnect_var.get(),
            "serial_reconnect_interval": self.serial_reconnect_interval_var.get(),
            "network_mode": self.network_mode_var.get(),
            "bind_host": bind_host_value(self.bind_host_var.get()),
            "local_port": self.local_port_var.get(),
            "remote_host": self.remote_host_var.get(),
            "remote_port": self.remote_port_var.get(),
            "client_policy": self.client_policy_var.get(),
            "access_mode": self.access_mode_var.get(),
            "access_rules": self.access_text.get("1.0", "end").strip(),
            "hex_log": self.hex_log_var.get(),
            "autoscroll": self.autoscroll_var.get(),
            "start_with_windows": self.start_with_windows_var.get(),
            "auto_start_service": self.auto_start_service_var.get(),
            "close_to_tray": self.close_to_tray_var.get(),
            "network_auto_reconnect": self.network_auto_reconnect_var.get(),
            "network_reconnect_interval": self.network_reconnect_interval_var.get(),
        }

    def _load_settings(self) -> None:
        path = settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        self._apply_settings_dict(data)

    def _apply_settings_dict(self, data: dict[str, Any]) -> None:
        self.serial_port_var.set(data.get("serial_port", self.serial_port_var.get()))
        self.baudrate_var.set(data.get("baudrate", self.baudrate_var.get()))
        self.data_bits_var.set(data.get("data_bits", self.data_bits_var.get()))
        self.parity_var.set(data.get("parity", self.parity_var.get()))
        self.stop_bits_var.set(data.get("stop_bits", self.stop_bits_var.get()))
        self.dtr_var.set(bool(data.get("dtr", self.dtr_var.get())))
        self.rts_var.set(bool(data.get("rts", self.rts_var.get())))
        self.reset_input_var.set(bool(data.get("reset_input", self.reset_input_var.get())))
        self.serial_auto_reconnect_var.set(bool(data.get("serial_auto_reconnect", self.serial_auto_reconnect_var.get())))
        self.serial_reconnect_interval_var.set(
            data.get("serial_reconnect_interval", self.serial_reconnect_interval_var.get())
        )

        network_mode = data.get("network_mode", self.network_mode_var.get())
        if network_mode not in NETWORK_MODES:
            network_mode = self.network_mode_var.get()
        self.network_mode_var.set(network_mode)
        self.bind_host_var.set(bind_host_display(data.get("bind_host", self.bind_host_var.get())))
        self.local_port_var.set(data.get("local_port", data.get("tcp_port", self.local_port_var.get())))
        self.remote_host_var.set(data.get("remote_host", self.remote_host_var.get()))
        self.remote_port_var.set(data.get("remote_port", data.get("tcp_port", self.remote_port_var.get())))
        self.client_policy_var.set(data.get("client_policy", self.client_policy_var.get()))
        self.access_mode_var.set(data.get("access_mode", self.access_mode_var.get()))
        self.hex_log_var.set(bool(data.get("hex_log", self.hex_log_var.get())))
        self.autoscroll_var.set(bool(data.get("autoscroll", self.autoscroll_var.get())))
        self.start_with_windows_var.set(bool(data.get("start_with_windows", self.start_with_windows_var.get())))
        self.auto_start_service_var.set(bool(data.get("auto_start_service", self.auto_start_service_var.get())))
        self.close_to_tray_var.set(bool(data.get("close_to_tray", self.close_to_tray_var.get())))
        self.network_auto_reconnect_var.set(
            bool(data.get("network_auto_reconnect", self.network_auto_reconnect_var.get()))
        )
        self.network_reconnect_interval_var.set(
            data.get("network_reconnect_interval", self.network_reconnect_interval_var.get())
        )
        self.access_text.delete("1.0", "end")
        self.access_text.insert("1.0", data.get("access_rules", ""))

    def _save_settings(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._settings_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        set_startup_registration(self.start_with_windows_var.get())

    def _on_close(self) -> None:
        if not self._allow_exit and self.close_to_tray_var.get():
            self._save_settings()
            self._hide_to_tray()
            return

        if self.running and not self._allow_exit:
            if not messagebox.askyesno(APP_NAME, "服务正在运行，是否停止并退出？"):
                return
        if self.running:
            self._stop()
        self._save_settings()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.log_store.close()
        self.destroy()


def detect_local_ipv4_addresses() -> list[str]:
    addresses: list[str] = []

    def add(ip: str) -> None:
        if not ip.startswith("127.") and ip not in addresses:
            addresses.append(ip)

    try:
        hostname = socket.gethostname()
        for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
            if family == socket.AF_INET:
                add(sockaddr[0])
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            add(probe.getsockname()[0])
    except OSError:
        pass

    return addresses


def resolve_ipv4_endpoint(host: str, port: int, socktype: int) -> tuple[str, int]:
    infos = socket.getaddrinfo(host, port, socket.AF_INET, socktype)
    if not infos:
        raise OSError(f"无法解析地址 {host}:{port}")
    return infos[0][4]


def format_runtime_error(exc: Exception, settings: RelaySettings) -> str:
    if isinstance(exc, OSError) and is_address_in_use(exc):
        owner = find_port_owner(settings.local_port)
        mode_label = NETWORK_MODE_LABELS.get(settings.network_mode, settings.network_mode)
        bind_label = bind_host_display(settings.bind_host)
        owner_text = f"当前占用进程: {owner}。" if owner else ""
        return (
            f"启动失败：{mode_label} 本地地址 {bind_label}:{settings.local_port} 已被占用。"
            f"{owner_text}请关闭占用程序，或改用其它本地端口。"
        )
    return f"运行失败: {exc}"


def settings_summary(settings: RelaySettings) -> str:
    mode_label = NETWORK_MODE_LABELS.get(settings.network_mode, settings.network_mode)
    serial_label = (
        f"{settings.serial.port} {settings.serial.baudrate}-"
        f"{settings.serial.data_bits}{settings.serial.parity}{settings.serial.stop_bits}"
    )
    if settings.network_mode in {"tcp_client", "udp_client"}:
        network_label = f"{mode_label} -> {settings.remote_host}:{settings.remote_port}"
        if settings.local_port > 0 or settings.bind_host != BIND_ALL_VALUE:
            network_label += f"，本地 {bind_host_display(settings.bind_host)}:{settings.local_port}"
    else:
        network_label = f"{mode_label} {bind_host_display(settings.bind_host)}:{settings.local_port}"
    access_label = ACCESS_MODE_LABELS.get(settings.access_mode, settings.access_mode)
    client_label = CLIENT_POLICY_LABELS.get(settings.client_policy, settings.client_policy)
    reconnect_label = "串口重连开" if settings.serial.auto_reconnect else "串口重连关"
    return f"{serial_label}，{network_label}，{client_label}，{access_label}，{reconnect_label}"


def is_address_in_use(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {98, 10048}


def find_port_owner(port: int) -> str:
    if os.name != "nt" or not port:
        return ""

    script = f"""
$portNumber = {port}
$owners = @()
$owners += Get-NetTCPConnection -LocalPort $portNumber -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess
$owners += Get-NetUDPEndpoint -LocalPort $portNumber -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess
$ownerPid = $owners | Where-Object {{ $_ }} | Select-Object -First 1
if ($ownerPid) {{
    $process = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($process) {{
        "$($process.ProcessName).exe (PID $ownerPid)"
    }} else {{
        "PID $ownerPid"
    }}
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


def bind_host_display(value: str) -> str:
    return BIND_ALL_LABEL if value == BIND_ALL_VALUE else value


def bind_host_value(display: str) -> str:
    return BIND_ALL_VALUE if display in {BIND_ALL_LABEL, BIND_ALL_VALUE} else display


def split_rules(text: str) -> list[str]:
    rules: list[str] = []
    for line in text.replace(",", "\n").replace(";", "\n").splitlines():
        rule = line.strip()
        if not rule:
            continue
        rules.append(rule)
    return rules


def settings_path() -> Path:
    return app_root() / SETTINGS_FILE_NAME


def log_dir_path() -> Path:
    return app_root() / LOG_DIR_NAME


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative_path: Path) -> Path:
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        return Path(base_path) / relative_path
    return Path(__file__).resolve().parent / relative_path


def format_bytes_count(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / 1024 / 1024:.1f} MB"


def parse_log_time(text: str, is_end: bool) -> float | None:
    value = text.strip()
    if not value:
        return None

    formats = (
        ("%Y-%m-%d %H:%M:%S", False),
        ("%Y-%m-%d %H:%M", False),
        ("%Y-%m-%d", True),
    )
    for fmt, date_only in formats:
        try:
            parsed = dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
        if date_only and is_end:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed.timestamp()

    raise ValueError("时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS。")


def truncate_text(text: str, limit: int) -> str:
    single_line = " ".join(text.splitlines())
    if len(single_line) <= limit:
        return single_line
    return single_line[: limit - 1] + "..."


def clean_tsv(value: object) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def set_startup_registration(enabled: bool) -> None:
    if os.name != "nt":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" "{Path(__file__).resolve()}" --minimized'


def safe_close(resource: object) -> None:
    try:
        close = getattr(resource, "close")
        close()
    except Exception:
        pass


_INSTANCE_MUTEX: int | None = None


def ensure_single_instance() -> bool:
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    mutex = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if not mutex:
        return True
    if ctypes.get_last_error() == 183:
        ctypes.windll.user32.MessageBoxW(None, f"{APP_NAME} 已在运行。", APP_NAME, 0x40)
        kernel32.CloseHandle(mutex)
        return False
    _INSTANCE_MUTEX = mutex
    return True


def release_single_instance() -> None:
    global _INSTANCE_MUTEX
    if os.name == "nt" and _INSTANCE_MUTEX:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(_INSTANCE_MUTEX)
        _INSTANCE_MUTEX = None


def main() -> int:
    if not ensure_single_instance():
        return 0
    try:
        app = SerialRelayApp(start_minimized="--minimized" in sys.argv)
        app.mainloop()
    finally:
        release_single_instance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
