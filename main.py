#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多通道工业数据采集系统
============================================================
功能:
  1. 支持多个 Modbus TCP 连接
  2. 支持多个基恩士(Keyence) PLC 连接（上位链接协议）
  3. 实时多通道折线图显示
  4. 全量数据 CSV 导出
  5. 配置文件持久化

依赖:  pip install PySide6 pyqtgraph
运行:  python daq_system.py
============================================================
"""

import sys
import os
import csv
import time
import json
import struct
import socket
import threading
from datetime import datetime
from collections import deque
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

try:
    import serial
except ImportError:
    serial = None

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QGroupBox,
    QMessageBox, QFileDialog, QSpinBox, QDoubleSpinBox,
    QStatusBar, QSplitter, QScrollArea
)
from PySide6.QtGui import QColor
import pyqtgraph as pg


# ================================================================
#  第一部分: 线程安全数据存储
# ================================================================
class DataStore:
    """线程安全的数据存储管理器，支持实时折线图和CSV导出"""

    def __init__(self, max_points: int = 10000):
        self._lock = threading.Lock()
        self._max_points = max_points
        self._channels: dict = {}
        self._all_records: list = []

    def register_channel(self, channel_id: str, name: str,
                         unit: str = "", connection_id: str = "",
                         scale: float = 1.0, offset: float = 0.0,
                         data_type: str = "uint16"):
        with self._lock:
            if channel_id not in self._channels:
                self._channels[channel_id] = {
                    "timestamps": deque(maxlen=self._max_points),
                    "values": deque(maxlen=self._max_points),
                    "meta": {
                        "name": name, "unit": unit,
                        "connection_id": connection_id,
                        "scale": scale, "offset": offset,
                        "data_type": data_type,
                        "registered_at": datetime.now().isoformat()
                    }
                }

    def add_data(self, channel_id: str, timestamp: float, raw_value: float):
        with self._lock:
            if channel_id not in self._channels:
                return
            ch = self._channels[channel_id]
            meta = ch["meta"]
            eng_value = raw_value * meta["scale"] + meta["offset"]
            ch["timestamps"].append(timestamp)
            ch["values"].append(eng_value)
            self._all_records.append({
                "timestamp": datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S.%f"),
                "channel_id": channel_id,
                "channel_name": meta["name"],
                "raw_value": raw_value,
                "value": round(eng_value, 6),
                "unit": meta["unit"],
                "connection_id": meta["connection_id"],
                "scale": meta["scale"],
                "offset": meta["offset"]
            })

    def get_channel_data(self, channel_id: str):
        with self._lock:
            ch = self._channels.get(channel_id)
            if ch:
                return list(ch["timestamps"]), list(ch["values"])
            return [], []

    def get_all_channel_ids(self):
        with self._lock:
            return list(self._channels.keys())

    def get_channel_meta(self, channel_id: str):
        with self._lock:
            ch = self._channels.get(channel_id)
            return ch["meta"].copy() if ch else {}

    def export_csv(self, filepath: str) -> int:
        """全量导出所有采集数据到CSV"""
        with self._lock:
            records = list(self._all_records)
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "timestamp", "connection_id", "channel_id",
                "channel_name", "raw_value", "value",
                "unit", "scale", "offset"
            ])
            writer.writeheader()
            writer.writerows(records)
        return len(records)

    def clear(self):
        with self._lock:
            self._channels.clear()
            self._all_records.clear()

    def get_record_count(self) -> int:
        with self._lock:
            return len(self._all_records)


# ================================================================
#  第二部分: Modbus TCP 连接器 (原生 socket 实现)
# ================================================================
class ModbusTCPConnector:
    """Modbus TCP 连接器 — 纯 socket 实现，无 pymodbus 依赖"""

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01

    def __init__(self, connection_id: str, host: str, port: int = 502,
                 slave_id: int = 1, timeout: float = 3.0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._txn_id = 0
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"[Modbus] 连接失败 {self.host}:{self.port} -> {e}")
            self._sock = None
            return False

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        if not self._sock:
            return None
        with self._lock:
            self._txn_id = (self._txn_id + 1) & 0xFFFF
            length = 6
            mbap = struct.pack(">HHH", self._txn_id, 0, length)
            pdu = struct.pack(">BBHH", self.slave_id, func_code,
                              start_addr, quantity)
            frame = mbap + pdu
            try:
                self._sock.sendall(frame)
                return self._recv_response()
            except Exception as e:
                print(f"[Modbus] 通信错误: {e}")
                self.disconnect()
                return None

    def _recv_response(self) -> Optional[bytes]:
        try:
            header = b""
            while len(header) < 6:
                chunk = self._sock.recv(6 - len(header))
                if not chunk:
                    return None
                header += chunk
            _txn, _proto, length = struct.unpack(">HHH", header[:6])
            body = b""
            while len(body) < length:
                chunk = self._sock.recv(length - len(body))
                if not chunk:
                    break
                body += chunk
            return header + body
        except socket.timeout:
            return None

    def read_holding_registers(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_HOLDING, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_register_response(resp)

    def read_input_registers(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_INPUT_REG, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_register_response(resp)

    def read_coils(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_COILS, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_bit_response(resp, quantity)

    def _parse_register_response(self, resp: bytes):
        if len(resp) < 9:
            return None
        func_code = resp[7]
        if func_code & 0x80:
            exc_code = resp[8] if len(resp) > 8 else -1
            print(f"[Modbus] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        byte_count = resp[8]
        reg_data = resp[9:9 + byte_count]
        values = []
        for i in range(0, len(reg_data), 2):
            chunk = reg_data[i:i + 2]
            if len(chunk) == 2:
                values.append(struct.unpack(">H", chunk)[0])
            elif len(chunk) == 1:
                values.append(chunk[0])
        return values

    def _parse_bit_response(self, resp: bytes, quantity: int):
        if len(resp) < 9:
            return None
        func_code = resp[7]
        if func_code & 0x80:
            return None
        byte_count = resp[8]
        bit_data = resp[9:9 + byte_count]
        values = []
        for i in range(quantity):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx < len(bit_data):
                values.append((bit_data[byte_idx] >> bit_idx) & 0x01)
            else:
                values.append(0)
        return values


# ================================================================
#  第三部分: Modbus RTU 连接器 (串口实现)
# ================================================================
class ModbusRTUConnector:
    """Modbus RTU 连接器 — 使用 pyserial 实现串口通信"""

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01

    _CRC_TABLE = [
        0x0000, 0xC0C1, 0xC181, 0x0140, 0xC301, 0x03C0, 0x0280, 0xC241,
        0xC601, 0x06C0, 0x0780, 0xC741, 0x0500, 0xC5C1, 0xC481, 0x0440,
        0xCC01, 0x0CC0, 0x0D80, 0xCD41, 0x0F00, 0xCFC1, 0xCE81, 0x0E40,
        0x0A00, 0xCAC1, 0xCB81, 0x0B40, 0xC901, 0x09C0, 0x0880, 0xC841,
        0xD801, 0x18C0, 0x1980, 0xD941, 0x1B00, 0xDBC1, 0xDA81, 0x1A40,
        0x1E00, 0xDEC1, 0xDF81, 0x1F40, 0xDD01, 0x1DC0, 0x1C80, 0xDC41,
        0x1400, 0xD4C1, 0xD581, 0x1540, 0xD701, 0x17C0, 0x1680, 0xD641,
        0xD201, 0x12C0, 0x1380, 0xD341, 0x1100, 0xD1C1, 0xD081, 0x1040,
        0xF001, 0x30C0, 0x3180, 0xF141, 0x3300, 0xF3C1, 0xF281, 0x3240,
        0x3600, 0xF6C1, 0xF781, 0x3740, 0xF501, 0x35C0, 0x3480, 0xF441,
        0x3C00, 0xFCC1, 0xFD81, 0x3D40, 0xFF01, 0x3FC0, 0x3E80, 0xFE41,
        0xFA01, 0x3AC0, 0x3B80, 0xFB41, 0x3900, 0xF9C1, 0xF881, 0x3840,
        0x2800, 0xE8C1, 0xE981, 0x2940, 0xEB01, 0x2BC0, 0x2A80, 0xEA41,
        0xEE01, 0x2EC0, 0x2F80, 0xEF41, 0x2D00, 0xEDC1, 0xEC81, 0x2C40,
        0xE401, 0x24C0, 0x2580, 0xE541, 0x2700, 0xE7C1, 0xE681, 0x2640,
        0x2200, 0xE2C1, 0xE381, 0x2340, 0xE101, 0x21C0, 0x2080, 0xE041,
        0xA001, 0x60C0, 0x6180, 0xA141, 0x6300, 0xA3C1, 0xA281, 0x6240,
        0x6600, 0xA6C1, 0xA781, 0x6740, 0xA501, 0x65C0, 0x6480, 0xA441,
        0x6C00, 0xACC1, 0xAD81, 0x6D40, 0xAF01, 0x6FC0, 0x6E80, 0xAE41,
        0xAA01, 0x6AC0, 0x6B80, 0xAB41, 0x6900, 0xA9C1, 0xA881, 0x6840,
        0x7800, 0xB8C1, 0xB981, 0x7940, 0xBB01, 0x7BC0, 0x7A80, 0xBA41,
        0xBE01, 0x7EC0, 0x7F80, 0xBF41, 0x7D00, 0xBDC1, 0xBC81, 0x7C40,
        0xB401, 0x74C0, 0x7580, 0xB541, 0x7700, 0xB7C1, 0xB681, 0x7640,
        0x7200, 0xB2C1, 0xB381, 0x7340, 0xB101, 0x71C0, 0x7080, 0xB041,
        0x5000, 0x90C1, 0x9181, 0x5140, 0x9301, 0x53C0, 0x5280, 0x9241,
        0x9601, 0x56C0, 0x5780, 0x9741, 0x5500, 0x95C1, 0x9481, 0x5440,
        0x9C01, 0x5CC0, 0x5D80, 0x9D41, 0x5F00, 0x9FC1, 0x9E81, 0x5E40,
        0x5A00, 0x9AC1, 0x9B81, 0x5B40, 0x9901, 0x59C0, 0x5880, 0x9841,
        0x8801, 0x48C0, 0x4980, 0x8941, 0x4B00, 0x8BC1, 0x8A81, 0x4A40,
        0x4E00, 0x8EC1, 0x8F81, 0x4F40, 0x8D01, 0x4DC0, 0x4C80, 0x8C41,
        0x4400, 0x84C1, 0x8581, 0x4540, 0x8701, 0x47C0, 0x4680, 0x8641,
        0x8201, 0x42C0, 0x4380, 0x8341, 0x4100, 0x81C1, 0x8081, 0x4040,
    ]

    def __init__(self, connection_id: str, port: str = "COM1",
                 baudrate: int = 9600, slave_id: int = 1,
                 timeout: float = 3.0, parity: str = "N",
                 stopbits: int = 1, bytesize: int = 8):
        self.connection_id = connection_id
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self._ser = None
        self._lock = threading.Lock()

    def _calc_crc(self, data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc = ((crc >> 8) & 0xFF) ^ self._CRC_TABLE[(crc ^ byte) & 0xFF]
        return crc

    def _build_frame(self, func_code: int, start_addr: int, quantity: int) -> bytes:
        pdu = struct.pack(">BBHH", self.slave_id, func_code, start_addr, quantity)
        crc = self._calc_crc(pdu)
        return pdu + struct.pack("<H", crc)

    def _validate_frame(self, frame: bytes) -> bool:
        if len(frame) < 4:
            return False
        data = frame[:-2]
        received_crc = struct.unpack("<H", frame[-2:])[0]
        expected_crc = self._calc_crc(data)
        return received_crc == expected_crc

    def connect(self) -> bool:
        if serial is None:
            print(f"[Modbus RTU] 连接失败 {self.port} -> 缺少 pyserial 依赖，请安装: pip install pyserial")
            return False
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                parity=self.parity,
                stopbits=self.stopbits,
                bytesize=self.bytesize,
                timeout=self.timeout,
                write_timeout=self.timeout
            )
            return True
        except Exception as e:
            print(f"[Modbus RTU] 连接失败 {self.port} -> {e}")
            self._ser = None
            return False

    def disconnect(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def is_connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        if not self._ser or not self._ser.is_open:
            return None
        with self._lock:
            try:
                frame = self._build_frame(func_code, start_addr, quantity)
                self._ser.flushInput()
                self._ser.write(frame)
                self._ser.flush()
                return self._recv_response()
            except Exception as e:
                print(f"[Modbus RTU] 通信错误: {e}")
                self.disconnect()
                return None

    def _recv_response(self) -> Optional[bytes]:
        try:
            time.sleep(0.05)
            if self._ser.in_waiting < 4:
                time.sleep(0.1)
            if self._ser.in_waiting < 4:
                return None
            resp = self._ser.read(self._ser.in_waiting)
            if len(resp) < 4:
                return None
            if not self._validate_frame(resp):
                return None
            return resp
        except Exception:
            return None

    def read_holding_registers(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_HOLDING, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_register_response(resp)

    def read_input_registers(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_INPUT_REG, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_register_response(resp)

    def read_coils(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_COILS, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_bit_response(resp, quantity)

    def _parse_register_response(self, resp: bytes):
        if len(resp) < 5:
            return None
        func_code = resp[1]
        if func_code & 0x80:
            exc_code = resp[2] if len(resp) > 2 else -1
            print(f"[Modbus RTU] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        byte_count = resp[2]
        reg_data = resp[3:3 + byte_count]
        values = []
        for i in range(0, len(reg_data), 2):
            chunk = reg_data[i:i + 2]
            if len(chunk) == 2:
                values.append(struct.unpack(">H", chunk)[0])
            elif len(chunk) == 1:
                values.append(chunk[0])
        return values

    def _parse_bit_response(self, resp: bytes, quantity: int):
        if len(resp) < 5:
            return None
        func_code = resp[1]
        if func_code & 0x80:
            return None
        byte_count = resp[2]
        bit_data = resp[3:3 + byte_count]
        values = []
        for i in range(quantity):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx < len(bit_data):
                values.append((bit_data[byte_idx] >> bit_idx) & 0x01)
            else:
                values.append(0)
        return values


# ================================================================
#  第四部分: 基恩士(Keyence) PLC 连接器
# ================================================================
class KeyencePLCConnector:
    """
    基恩士 PLC 上位链接协议连接器
    适用型号: KV-5500/7500/8000/Nano 等
    默认端口: 3000 (以太网上位链接)
    """

    def __init__(self, connection_id: str, host: str, port: int = 3000,
                 timeout: float = 3.0, unit: int = 0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit = unit
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(self.timeout)
            self._sock.connect((self.host, self.port))
            return True
        except Exception as e:
            print(f"[Keyence] 连接失败 {self.host}:{self.port} -> {e}")
            self._sock = None
            return False

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None

    def _send_command(self, cmd: str) -> Optional[str]:
        if not self._sock:
            return None
        with self._lock:
            full_cmd = cmd + "\r"
            try:
                self._sock.sendall(full_cmd.encode("ascii"))
                resp = b""
                while True:
                    chunk = self._sock.recv(1024)
                    if not chunk:
                        break
                    resp += chunk
                    if b"\r" in resp or b"\n" in resp:
                        break
                return resp.decode("ascii", errors="replace").strip("\r\n")
            except socket.timeout:
                print(f"[Keyence] 响应超时: {cmd}")
                return None
            except Exception as e:
                print(f"[Keyence] 通信错误: {e}")
                self.disconnect()
                return None

    def read_device(self, device_type: str, start_addr: int, count: int = 1):
        """
        通用设备读取
        device_type: DM / MR / LR / TIM / CNT / VR 等
        命令格式: R<设备类型><起始地址6位><读取数量2位>
        示例:     RDM00000005  -> 从DM0读取5个寄存器
        """
        dt = device_type.upper()[:3]
        cmd = f"R{dt}{start_addr:06d}{count:02d}"
        resp = self._send_command(cmd)
        if resp is None:
            return None
        if resp.startswith("E"):
            print(f"[Keyence] PLC错误: {resp} (命令: {cmd})")
            return None
        values = resp.split()
        try:
            return [int(v, 0) for v in values] if values else None
        except ValueError:
            return None

    def read_dm(self, start_addr: int, count: int = 1):
        return self.read_device("DM", start_addr, count)

    def read_mr(self, start_addr: int, count: int = 1):
        return self.read_device("MR", start_addr, count)

    def read_lr(self, start_addr: int, count: int = 1):
        return self.read_device("LR", start_addr, count)


# ================================================================
#  第四部分: 采集任务与后台采集线程
# ================================================================
class PollingTask:
    """单个采集任务配置"""

    def __init__(self, task_id: str, connection_id: str,
                 connection_type: str, device_type: str,
                 start_addr: int, quantity: int,
                 channel_prefix: str, channel_name: str,
                 unit: str = "", scale: float = 1.0,
                 offset: float = 0.0, data_type: str = "uint16"):
        self.task_id = task_id
        self.connection_id = connection_id
        self.connection_type = connection_type
        self.device_type = device_type
        self.start_addr = start_addr
        self.quantity = quantity
        self.channel_prefix = channel_prefix
        self.channel_name = channel_name
        self.unit = unit
        self.scale = scale
        self.offset = offset
        self.data_type = data_type

    def get_channel_ids(self):
        return [f"{self.channel_prefix}_{self.start_addr + i}"
                for i in range(self.quantity)]

    def get_channel_names(self):
        if self.quantity == 1:
            return [self.channel_name]
        return [f"{self.channel_name}[{self.start_addr + i}]"
                for i in range(self.quantity)]

    def to_dict(self):
        return {k: getattr(self, k) for k in [
            "task_id", "connection_id", "connection_type",
            "device_type", "start_addr", "quantity",
            "channel_prefix", "channel_name", "unit",
            "scale", "offset", "data_type"
        ]}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class AcquisitionWorker(QObject):
    """后台采集线程 — 逐任务轮询所有连接"""
    data_acquired = Signal(str, float, float)
    connection_status = Signal(str, bool, str)
    error_occurred = Signal(str, str)

    def __init__(self, store: DataStore):
        super().__init__()
        self.store = store
        self._connections = {}
        self._tasks = []
        self._running = False
        self._poll_interval = 0.5
        self._thread = None

    def add_connection(self, conn_id: str, conn_type: str, **kwargs):
        if conn_type == "modbus_tcp":
            conn = ModbusTCPConnector(conn_id, **kwargs)
        elif conn_type == "modbus_rtu":
            conn = ModbusRTUConnector(conn_id, **kwargs)
        elif conn_type == "keyence":
            conn = KeyencePLCConnector(conn_id, **kwargs)
        else:
            raise ValueError(f"未知连接类型: {conn_type}")
        self._connections[conn_id] = {"type": conn_type, "connector": conn}

    def add_task(self, task: PollingTask):
        self._tasks.append(task)
        ch_ids = task.get_channel_ids()
        ch_names = task.get_channel_names()
        for cid, cname in zip(ch_ids, ch_names):
            self.store.register_channel(
                cid, cname, task.unit, task.connection_id,
                task.scale, task.offset, task.data_type
            )

    def clear_all(self):
        self._tasks.clear()
        for conn in self._connections.values():
            conn["connector"].disconnect()
        self._connections.clear()

    def set_poll_interval(self, seconds: float):
        self._poll_interval = seconds

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        # 连接所有设备
        for conn_id, info in self._connections.items():
            connector = info["connector"]
            ok = connector.connect()
            if isinstance(connector, ModbusRTUConnector):
                conn_info = f"{connector.port} @ {connector.baudrate}"
            else:
                conn_info = f"{connector.host}:{connector.port}"
            self.connection_status.emit(conn_id, ok,
                f"{'连接成功' if ok else '连接失败'} {conn_info}")

        # 主循环
        while self._running:
            loop_start = time.time()
            for task in self._tasks:
                if not self._running:
                    break
                conn_info = self._connections.get(task.connection_id)
                if not conn_info:
                    continue
                connector = conn_info["connector"]
                if not connector.is_connected():
                    if connector.connect():
                        self.connection_status.emit(task.connection_id, True, "重连成功")
                    else:
                        continue
                try:
                    values = self._poll_one(connector, task)
                    if values is not None:
                        ts = time.time()
                        ch_ids = task.get_channel_ids()
                        for i, v in enumerate(values):
                            if i < len(ch_ids):
                                self.store.add_data(ch_ids[i], ts, float(v))
                                self.data_acquired.emit(ch_ids[i], ts, float(v))
                except Exception as e:
                    self.error_occurred.emit(task.connection_id, str(e))

            elapsed = time.time() - loop_start
            time.sleep(max(0, self._poll_interval - elapsed))

        # 清理
        for conn_id, info in self._connections.items():
            info["connector"].disconnect()
            self.connection_status.emit(conn_id, False, "已断开")

    def _poll_one(self, connector, task: PollingTask):
        if isinstance(connector, ModbusTCPConnector) or isinstance(connector, ModbusRTUConnector):
            if task.device_type == "holding":
                return connector.read_holding_registers(task.start_addr, task.quantity)
            elif task.device_type == "input":
                return connector.read_input_registers(task.start_addr, task.quantity)
            elif task.device_type == "coil":
                return connector.read_coils(task.start_addr, task.quantity)
            else:
                return connector.read_holding_registers(task.start_addr, task.quantity)
        elif isinstance(connector, KeyencePLCConnector):
            return connector.read_device(task.device_type, task.start_addr, task.quantity)
        return None


# ================================================================
#  第五部分: 图表组件
# ================================================================
class ChartWidget(pg.PlotWidget):
    """单个折线图组件"""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setBackground("#1e1e2e")
        self.setTitle(title, color="#cdd6f4", size="10pt")
        self.setLabel("left", color="#a6adc8")
        self.setLabel("bottom", "时间(s)", color="#a6adc8")
        self.showGrid(x=True, y=True, alpha=0.15)
        self._curves = {}
        self._colors = [
            "#f38ba8", "#fab387", "#f9e2af", "#a6e3a1",
            "#94e2d5", "#89dceb", "#b4befe", "#cba6f7"
        ]

    def add_channel(self, channel_id: str, name: str):
        color = self._colors[len(self._curves) % len(self._colors)]
        pen = pg.mkPen(color=color, width=2)
        curve = self.plot(pen=pen, name=name)
        self._curves[channel_id] = curve

    def update_channel(self, channel_id: str, timestamps: list, values: list):
        if channel_id in self._curves and timestamps:
            # 转换为相对时间
            if timestamps:
                t0 = timestamps[0]
                rel_ts = [t - t0 for t in timestamps]
                self._curves[channel_id].setData(rel_ts, values)

    def clear_all(self):
        for curve in self._curves.values():
            curve.setData([], [])
        self._curves.clear()


# ================================================================
#  第六部分: 连接配置对话框
# ================================================================
class ConnectionConfigDialog(QWidget):
    connection_added = Signal(str, str, dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("添加连接")
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(350)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout(self)

        layout.addWidget(QLabel("连接类型:"), 0, 0)
        self.cmb_type = QComboBox()
        self.cmb_type.addItems(["Modbus TCP", "Modbus RTU", "Keyence PLC"])
        layout.addWidget(self.cmb_type, 0, 1)

        layout.addWidget(QLabel("连接ID:"), 1, 0)
        self.edit_id = QLineEdit("conn1")
        layout.addWidget(self.edit_id, 1, 1)

        self.lbl_host = QLabel("IP地址:")
        self.edit_host = QLineEdit("192.168.1.100")
        layout.addWidget(self.lbl_host, 2, 0)
        layout.addWidget(self.edit_host, 2, 1)

        self.lbl_serial_port = QLabel("串口:")
        self.edit_serial_port = QLineEdit("COM1")
        layout.addWidget(self.lbl_serial_port, 2, 0)
        layout.addWidget(self.edit_serial_port, 2, 1)

        layout.addWidget(QLabel("端口/波特率:"), 3, 0)
        self.spin_port = QSpinBox()
        self.spin_port.setRange(1, 65535)
        layout.addWidget(self.spin_port, 3, 1)

        self.lbl_baudrate = QLabel("波特率:")
        self.cmb_baudrate = QComboBox()
        self.cmb_baudrate.addItems(["2400", "4800", "9600", "19200", "38400", "57600", "115200"])
        self.cmb_baudrate.setCurrentText("9600")
        layout.addWidget(self.lbl_baudrate, 3, 0)
        layout.addWidget(self.cmb_baudrate, 3, 1)

        self.lbl_slave = QLabel("Slave ID:")
        self.spin_slave = QSpinBox()
        self.spin_slave.setRange(0, 255)
        self.spin_slave.setValue(1)
        layout.addWidget(self.lbl_slave, 4, 0)
        layout.addWidget(self.spin_slave, 4, 1)

        self.lbl_unit = QLabel("单元号:")
        self.spin_unit = QSpinBox()
        self.spin_unit.setRange(0, 99)
        layout.addWidget(self.lbl_unit, 4, 0)
        layout.addWidget(self.spin_unit, 4, 1)

        self.lbl_parity = QLabel("校验位:")
        self.cmb_parity = QComboBox()
        self.cmb_parity.addItems(["N", "E", "O"])
        self.cmb_parity.setCurrentText("N")
        layout.addWidget(self.lbl_parity, 5, 0)
        layout.addWidget(self.cmb_parity, 5, 1)

        self.lbl_stopbits = QLabel("停止位:")
        self.spin_stopbits = QSpinBox()
        self.spin_stopbits.setRange(1, 2)
        self.spin_stopbits.setValue(1)
        layout.addWidget(self.lbl_stopbits, 6, 0)
        layout.addWidget(self.spin_stopbits, 6, 1)

        self.cmb_type.currentIndexChanged.connect(self._on_type_changed)
        self._on_type_changed()

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 7, 0, 1, 2)

        self.setMinimumWidth(380)

    def _on_type_changed(self):
        conn_type = self.cmb_type.currentText()
        is_modbus_tcp = conn_type == "Modbus TCP"
        is_modbus_rtu = conn_type == "Modbus RTU"
        is_keyence = conn_type == "Keyence PLC"

        self.lbl_host.setVisible(is_modbus_tcp or is_keyence)
        self.edit_host.setVisible(is_modbus_tcp or is_keyence)
        self.lbl_serial_port.setVisible(is_modbus_rtu)
        self.edit_serial_port.setVisible(is_modbus_rtu)

        self.spin_port.setVisible(is_modbus_tcp or is_keyence)
        self.lbl_baudrate.setVisible(is_modbus_rtu)
        self.cmb_baudrate.setVisible(is_modbus_rtu)

        self.lbl_slave.setVisible(is_modbus_tcp or is_modbus_rtu)
        self.spin_slave.setVisible(is_modbus_tcp or is_modbus_rtu)
        self.lbl_unit.setVisible(is_keyence)
        self.spin_unit.setVisible(is_keyence)

        self.lbl_parity.setVisible(is_modbus_rtu)
        self.cmb_parity.setVisible(is_modbus_rtu)
        self.lbl_stopbits.setVisible(is_modbus_rtu)
        self.spin_stopbits.setVisible(is_modbus_rtu)

        if is_modbus_tcp:
            self.spin_port.setValue(502)
        elif is_keyence:
            self.spin_port.setValue(3000)

    def _on_ok(self):
        conn_id = self.edit_id.text().strip()
        if not conn_id:
            QMessageBox.warning(self, "警告", "请输入连接ID")
            return
        conn_type_text = self.cmb_type.currentText()
        
        if conn_type_text == "Modbus TCP":
            conn_type = "modbus_tcp"
            params = {
                "host": self.edit_host.text().strip(),
                "port": self.spin_port.value(),
                "slave_id": self.spin_slave.value()
            }
        elif conn_type_text == "Modbus RTU":
            conn_type = "modbus_rtu"
            params = {
                "port": self.edit_serial_port.text().strip(),
                "baudrate": int(self.cmb_baudrate.currentText()),
                "slave_id": self.spin_slave.value(),
                "parity": self.cmb_parity.currentText(),
                "stopbits": self.spin_stopbits.value(),
                "bytesize": 8
            }
        else:
            conn_type = "keyence"
            params = {
                "host": self.edit_host.text().strip(),
                "port": self.spin_port.value(),
                "unit": self.spin_unit.value()
            }
        self.connection_added.emit(conn_id, conn_type, params)
        self.close()


# ================================================================
#  第七部分: 采集任务配置对话框
# ================================================================
class TaskConfigDialog(QWidget):
    task_added = Signal(dict)

    def __init__(self, connections: dict):
        super().__init__()
        self._connections = connections
        self.setWindowTitle("添加采集任务")
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(400)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout(self)

        layout.addWidget(QLabel("所属连接:"), 0, 0)
        self.cmb_conn = QComboBox()
        for cid, info in self._connections.items():
            label = f"{cid} ({info['type']})"
            if info['type'] == 'modbus_tcp':
                label += f" {info['params'].get('host','')}:{info['params'].get('port',502)}"
            elif info['type'] == 'modbus_rtu':
                label += f" {info['params'].get('port','')} @ {info['params'].get('baudrate',9600)}"
            else:
                label += f" {info['params'].get('host','')}:{info['params'].get('port',3000)}"
            self.cmb_conn.addItem(label, cid)
        layout.addWidget(self.cmb_conn, 0, 1)

        layout.addWidget(QLabel("通道前缀:"), 1, 0)
        self.edit_prefix = QLineEdit("temp")
        layout.addWidget(self.edit_prefix, 1, 1)

        layout.addWidget(QLabel("通道名称:"), 2, 0)
        self.edit_name = QLineEdit("温度")
        layout.addWidget(self.edit_name, 2, 1)

        layout.addWidget(QLabel("设备类型:"), 3, 0)
        self.cmb_device = QComboBox()
        layout.addWidget(self.cmb_device, 3, 1)

        layout.addWidget(QLabel("起始地址:"), 4, 0)
        self.spin_addr = QSpinBox()
        self.spin_addr.setRange(0, 999999)
        layout.addWidget(self.spin_addr, 4, 1)

        layout.addWidget(QLabel("读取数量:"), 5, 0)
        self.spin_qty = QSpinBox()
        self.spin_qty.setRange(1, 125)
        self.spin_qty.setValue(1)
        layout.addWidget(self.spin_qty, 5, 1)

        layout.addWidget(QLabel("单位:"), 6, 0)
        self.edit_unit = QLineEdit("")
        layout.addWidget(self.edit_unit, 6, 1)

        layout.addWidget(QLabel("缩放系数:"), 7, 0)
        self.spin_scale = QDoubleSpinBox()
        self.spin_scale.setRange(-999999, 999999)
        self.spin_scale.setDecimals(6)
        self.spin_scale.setValue(1.0)
        layout.addWidget(self.spin_scale, 7, 1)

        layout.addWidget(QLabel("偏移量:"), 8, 0)
        self.spin_offset = QDoubleSpinBox()
        self.spin_offset.setRange(-999999, 999999)
        self.spin_offset.setDecimals(6)
        layout.addWidget(self.spin_offset, 8, 1)

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 9, 0, 1, 2)

    def _on_conn_changed(self):
        cid = self.cmb_conn.currentData()
        if cid is None:
            return
        info = self._connections.get(cid)
        self.cmb_device.clear()
        if info and info["type"] in ("modbus_tcp", "modbus_rtu"):
            self.cmb_device.addItems(["holding", "input", "coil"])
        elif info and info["type"] == "keyence":
            self.cmb_device.addItems(["DM", "MR", "LR", "TIM", "CNT", "VR"])

    def _on_ok(self):
        prefix = self.edit_prefix.text().strip()
        name = self.edit_name.text().strip()
        if not prefix or not name:
            QMessageBox.warning(self, "警告", "请填写通道前缀和名称")
            return
        cid = self.cmb_conn.currentData()
        task_dict = {
            "task_id": f"task_{int(time.time()*1000)}",
            "connection_id": cid,
            "connection_type": self._connections[cid]["type"],
            "device_type": self.cmb_device.currentText(),
            "start_addr": self.spin_addr.value(),
            "quantity": self.spin_qty.value(),
            "channel_prefix": prefix,
            "channel_name": name,
            "unit": self.edit_unit.text().strip(),
            "scale": self.spin_scale.value(),
            "offset": self.spin_offset.value(),
            "data_type": "uint16",
        }
        self.task_added.emit(task_dict)
        self.close()


# ================================================================
#  第八部分: 主窗口
# ================================================================
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("多通道工业数据采集系统 — Modbus TCP/RTU / Keyence PLC")
        self.resize(1400, 900)
        self.setMinimumSize(800, 600)

        self.store = DataStore(max_points=10000)
        self.worker = AcquisitionWorker(self.store)
        self._connections = {}
        self._tasks = []
        self._chart_widgets = {}
        self._channel_to_chart = {}

        self._build_ui()
        self._connect_signals()

        self._refresh_timer = QTimer()
        self._refresh_timer.timeout.connect(self._refresh_charts)
        self._refresh_timer.start(500)

        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(1000)

        self._config_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "daq_config.yml")
        self._load_config()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.btn_add_conn = QPushButton("➕ 添加连接")
        self.btn_add_task = QPushButton("➕ 添加采集任务")
        self.btn_start = QPushButton("▶ 开始采集")
        self.btn_stop = QPushButton("⏹ 停止采集")
        self.btn_export = QPushButton("💾 导出CSV")
        self.btn_clear = QPushButton("🗑 清空数据")
        self.btn_save_cfg = QPushButton("💾 保存配置")
        self.btn_load_cfg = QPushButton("📂 加载配置")

        self.btn_start.setStyleSheet("background-color: #a6e3a1; font-weight: bold; padding: 6px 16px;")
        self.btn_stop.setStyleSheet("background-color: #f38ba8; font-weight: bold; padding: 6px 16px;")
        self.btn_stop.setEnabled(False)

        toolbar.addWidget(self.btn_add_conn)
        toolbar.addWidget(self.btn_add_task)
        toolbar.addSpacing(20)
        toolbar.addWidget(self.btn_start)
        toolbar.addWidget(self.btn_stop)
        toolbar.addSpacing(20)
        toolbar.addWidget(self.btn_export)
        toolbar.addWidget(self.btn_clear)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_save_cfg)
        toolbar.addWidget(self.btn_load_cfg)
        main_layout.addLayout(toolbar)

        # ---- 采集间隔 ----
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("采集间隔(秒):"))
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.1, 60.0)
        self.spin_interval.setSingleStep(0.1)
        self.spin_interval.setValue(0.5)
        interval_layout.addWidget(self.spin_interval)
        interval_layout.addStretch()
        main_layout.addLayout(interval_layout)

        # ---- 主区域 ----
        splitter = QSplitter(Qt.Horizontal)

        # 左: 配置表格
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        conn_group = QGroupBox("已配置连接")
        conn_layout = QVBoxLayout()
        self.table_conn = QTableWidget(0, 5)
        self.table_conn.setHorizontalHeaderLabels(["连接ID", "类型", "IP地址", "端口", "状态"])
        self.table_conn.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        conn_layout.addWidget(self.table_conn)
        conn_group.setLayout(conn_layout)
        left_layout.addWidget(conn_group)

        task_group = QGroupBox("已配置采集任务")
        task_layout = QVBoxLayout()
        self.table_task = QTableWidget(0, 8)
        self.table_task.setHorizontalHeaderLabels(
            ["连接ID", "类型", "设备类型", "起始地址", "数量", "通道前缀", "通道名称", "单位"])
        self.table_task.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        task_layout.addWidget(self.table_task)
        task_group.setLayout(task_layout)
        left_layout.addWidget(task_group)

        splitter.addWidget(left_panel)

        # 右: 图表
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll_widget = QWidget()
        self.chart_container = QVBoxLayout(self.chart_scroll_widget)
        self.chart_container.setAlignment(Qt.AlignTop)
        self.chart_scroll.setWidget(self.chart_scroll_widget)
        right_layout.addWidget(self.chart_scroll)

        self._placeholder_label = QLabel(
            "📝 请添加连接和采集任务后开始采集\n\n"
            "使用步骤:\n"
            "  1. 点击「添加连接」配置 Modbus TCP/RTU / Keyence 设备\n"
            "  2. 点击「添加采集任务」配置要读取的寄存器/区域\n"
            "  3. 点击「开始采集」启动实时数据采集\n"
            "  4. 点击「导出CSV」全量导出所有数据"
        )
        self._placeholder_label.setAlignment(Qt.AlignCenter)
        self._placeholder_label.setStyleSheet("color: #6c7086; font-size: 14px; padding: 80px;")
        self.chart_container.addWidget(self._placeholder_label)

        splitter.addWidget(right_panel)
        splitter.setSizes([450, 950])
        main_layout.addWidget(splitter, stretch=1)

        # ---- 状态栏 ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.lbl_record_count = QLabel("记录数: 0")
        self.lbl_active_conn = QLabel("活跃连接: 0")
        self.status_bar.addPermanentWidget(self.lbl_active_conn)
        self.status_bar.addPermanentWidget(self.lbl_record_count)

    def _connect_signals(self):
        self.btn_add_conn.clicked.connect(self._on_add_connection)
        self.btn_add_task.clicked.connect(self._on_add_task)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_export.clicked.connect(self._on_export)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_save_cfg.clicked.connect(self._save_config)
        self.btn_load_cfg.clicked.connect(self._load_config)
        self.worker.data_acquired.connect(self._on_data_acquired)
        self.worker.connection_status.connect(self._on_conn_status)
        self.worker.error_occurred.connect(self._on_error)

    # ---- 连接管理 ----
    def _on_add_connection(self):
        self._conn_dialog = ConnectionConfigDialog()
        self._conn_dialog.connection_added.connect(self._add_connection)
        self._conn_dialog.show()

    def _add_connection(self, conn_id, conn_type, params):
        if conn_id in self._connections:
            QMessageBox.warning(self, "警告", f"连接ID '{conn_id}' 已存在")
            return
        self._connections[conn_id] = {"type": conn_type, "params": params}
        self.worker.add_connection(conn_id, conn_type, **params)
        self._refresh_conn_table()
        self.status_bar.showMessage(f"已添加连接: {conn_id}", 3000)

    # ---- 任务管理 ----
    def _on_add_task(self):
        if not self._connections:
            QMessageBox.warning(self, "提示", "请先添加至少一个连接")
            return
        self._task_dialog = TaskConfigDialog(self._connections)
        self._task_dialog.task_added.connect(self._add_task)
        self._task_dialog.show()

    def _add_task(self, task_dict):
        task = PollingTask.from_dict(task_dict)
        self._tasks.append(task)
        self.worker.add_task(task)
        self._refresh_task_table()
        self._ensure_chart_for_task(task)
        self.status_bar.showMessage(f"已添加采集任务: {task.channel_name}", 3000)

    def _ensure_chart_for_task(self, task):
        self._placeholder_label.setVisible(False)
        chart_id = task.task_id
        if chart_id not in self._chart_widgets:
            chart = ChartWidget(title=task.channel_name)
            self.chart_container.addWidget(chart)
            self._chart_widgets[chart_id] = chart
        chart = self._chart_widgets[chart_id]
        for cid, cname in zip(task.get_channel_ids(), task.get_channel_names()):
            if cid not in self._channel_to_chart:
                chart.add_channel(cid, cname)
                self._channel_to_chart[cid] = chart_id

    # ---- 采集控制 ----
    def _on_start(self):
        if not self._tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务")
            return
        self.worker.set_poll_interval(self.spin_interval.value())
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_add_conn.setEnabled(False)
        self.btn_add_task.setEnabled(False)
        self.status_bar.showMessage("采集已启动", 3000)

    def _on_stop(self):
        self.worker.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_add_conn.setEnabled(True)
        self.btn_add_task.setEnabled(True)
        self.status_bar.showMessage("采集已停止", 3000)

    # ---- 事件回调 ----
    def _on_data_acquired(self, channel_id, timestamp, value):
        pass

    def _on_conn_status(self, conn_id, connected, message):
        self.status_bar.showMessage(f"[{conn_id}] {message}", 3000)
        self._refresh_conn_table()
        if not connected and "连接失败" in message:
            QMessageBox.warning(self, "连接失败",
                f"设备 [{conn_id}] 连接失败!\n\n{message}\n\n请检查:\n• 设备IP地址和端口是否正确\n• 设备是否已开机\n• 网络是否通畅\n• 防火墙是否允许连接")

    def _on_error(self, conn_id, error):
        self.status_bar.showMessage(f"[{conn_id}] 错误: {error}", 5000)
        QMessageBox.warning(self, "通信错误",
            f"设备 [{conn_id}] 通信异常:\n\n{error}")

    # ---- 导出/清空 ----
    def _on_export(self):
        if self.store.get_record_count() == 0:
            QMessageBox.information(self, "提示", "暂无数据可导出")
            return
        default_name = f"daq_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "选择导出路径", default_name, "CSV文件 (*.csv)")
        if filepath:
            count = self.store.export_csv(filepath)
            QMessageBox.information(self, "导出完成",
                f"已导出 {count} 条记录到:\n{filepath}")

    def _on_clear(self):
        reply = QMessageBox.question(self, "确认", "确定清空所有采集数据?")
        if reply == QMessageBox.Yes:
            self.store.clear()
            for chart in self._chart_widgets.values():
                chart.clear_all()
            self._refresh_status()
            self.status_bar.showMessage("数据已清空", 3000)

    # ---- 定时刷新 ----
    def _refresh_charts(self):
        for chart_id, chart in self._chart_widgets.items():
            for ch_id, cid in list(self._channel_to_chart.items()):
                if cid == chart_id:
                    ts, vals = self.store.get_channel_data(ch_id)
                    if ts:
                        chart.update_channel(ch_id, ts, vals)

    def _refresh_status(self):
        self.lbl_record_count.setText(f"记录数: {self.store.get_record_count()}")
        active = sum(1 for c in self.worker._connections.values()
                     if c["connector"].is_connected())
        self.lbl_active_conn.setText(
            f"活跃连接: {active}/{len(self._connections)}")

    def _refresh_conn_table(self):
        self.table_conn.setRowCount(len(self._connections))
        for i, (cid, info) in enumerate(self._connections.items()):
            p = info["params"]
            self.table_conn.setItem(i, 0, QTableWidgetItem(cid))
            conn_type = info["type"]
            self.table_conn.setItem(i, 1, QTableWidgetItem(conn_type))
            
            if conn_type == "modbus_rtu":
                host_text = p.get("port", "")
                port_text = f"{p.get('baudrate', 9600)}"
            else:
                host_text = p.get("host", "")
                port_text = str(p.get("port", ""))
            
            self.table_conn.setItem(i, 2, QTableWidgetItem(host_text))
            self.table_conn.setItem(i, 3, QTableWidgetItem(port_text))
            
            conn_obj = self.worker._connections.get(cid)
            if conn_obj:
                status = "🟢 连接中" if conn_obj["connector"].is_connected() else "⚪ 未连接"
            else:
                status = "⚪ 未连接"
            self.table_conn.setItem(i, 4, QTableWidgetItem(status))

    def _refresh_task_table(self):
        self.table_task.setRowCount(len(self._tasks))
        for i, task in enumerate(self._tasks):
            self.table_task.setItem(i, 0, QTableWidgetItem(task.connection_id))
            self.table_task.setItem(i, 1, QTableWidgetItem(task.connection_type))
            self.table_task.setItem(i, 2, QTableWidgetItem(task.device_type))
            self.table_task.setItem(i, 3, QTableWidgetItem(str(task.start_addr)))
            self.table_task.setItem(i, 4, QTableWidgetItem(str(task.quantity)))
            self.table_task.setItem(i, 5, QTableWidgetItem(task.channel_prefix))
            self.table_task.setItem(i, 6, QTableWidgetItem(task.channel_name))
            self.table_task.setItem(i, 7, QTableWidgetItem(task.unit))

    # ---- 配置持久化 ----
    def _save_config(self):
        config = {
            "connections": {
                cid: {"type": info["type"], "params": info["params"]}
                for cid, info in self._connections.items()
            },
            "tasks": [t.to_dict() for t in self._tasks],
            "poll_interval": self.spin_interval.value(),
        }
        try:
            with open(self._config_file, "w", encoding="utf-8") as f:
                if yaml is not None:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, indent=2)
                else:
                    json.dump(config, f, ensure_ascii=False, indent=2)
            self.status_bar.showMessage(f"配置已保存: {self._config_file}", 3000)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"保存配置失败: {e}")

    def _load_config(self):
        if not os.path.exists(self._config_file):
            return
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                if yaml is not None:
                    config = yaml.safe_load(f)
                else:
                    config = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "错误", f"加载配置失败: {e}")
            return

        self.worker.clear_all()
        self._connections.clear()
        self._tasks.clear()
        for chart in self._chart_widgets.values():
            chart.deleteLater()
        self._chart_widgets.clear()
        self._channel_to_chart.clear()

        for cid, info in config.get("connections", {}).items():
            self._add_connection(cid, info["type"], info["params"])
        for td in config.get("tasks", []):
            self._add_task(td)
        self.spin_interval.setValue(config.get("poll_interval", 0.5))

        self._refresh_conn_table()
        self._refresh_task_table()
        self.status_bar.showMessage("配置已加载", 3000)

    def closeEvent(self, event):
        if self.worker._running:
            self.worker.stop()
        event.accept()


# ================================================================
#  第九部分: 程序入口
# ================================================================
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Catppuccin Mocha 暗色主题
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor("#1e1e2e"))
    palette.setColor(palette.ColorRole.WindowText, QColor("#cdd6f4"))
    palette.setColor(palette.ColorRole.Base, QColor("#313244"))
    palette.setColor(palette.ColorRole.AlternateBase, QColor("#1e1e2e"))
    palette.setColor(palette.ColorRole.Text, QColor("#cdd6f4"))
    palette.setColor(palette.ColorRole.Button, QColor("#45475a"))
    palette.setColor(palette.ColorRole.ButtonText, QColor("#cdd6f4"))
    palette.setColor(palette.ColorRole.Highlight, QColor("#585b70"))
    palette.setColor(palette.ColorRole.HighlightedText, QColor("#cdd6f4"))
    app.setPalette(palette)

    pg.setConfigOption("background", "#1e1e2e")
    pg.setConfigOption("foreground", "#cdd6f4")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
