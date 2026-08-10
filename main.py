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
import traceback
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


def _safe_event(func):
    """事件处理器装饰器：捕获并记录所有异常，防止 UI 崩溃。
    异常将带完整 traceback 写入日志文件（经 stderr Tee），并弹出提示框。
    若处理器接收 Qt 事件对象（含 accept 方法），异常时仍调用 accept 以避免阻塞窗口关闭。"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb_text = "".join(traceback.format_exc())
            _log_exception(type(e), e, e.__traceback__,
                           source=f"事件异常[{func.__qualname__}]")
            try:
                from PySide6.QtWidgets import QApplication
                if QApplication.instance() is not None:
                    QMessageBox.critical(None, "事件处理异常",
                        f"{func.__qualname__} 发生异常:\n\n{tb_text[-1500:]}")
            except Exception:
                pass
            # 若是 Qt 事件，确保 accept 以免阻塞（如 closeEvent）
            for a in args[1:]:
                if hasattr(a, "accept") and callable(getattr(a, "accept")):
                    try:
                        a.accept()
                    except Exception:
                        pass
                    break
    wrapper.__name__ = func.__name__
    wrapper.__qualname__ = func.__qualname__
    wrapper.__doc__ = func.__doc__
    return wrapper


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

    def get_latest_value(self, channel_id: str) -> Optional[float]:
        """获取通道的最新值"""
        with self._lock:
            ch = self._channels.get(channel_id)
            if ch and ch["values"]:
                return ch["values"][-1]
            return None

    def find_channel_by_prefix(self, prefix: str) -> Optional[str]:
        """根据 channel_prefix 查找第一个匹配的 channel_id"""
        with self._lock:
            for cid in self._channels:
                if cid.startswith(prefix):
                    return cid
            return None

    def find_channels_by_prefix(self, prefix: str) -> list:
        """根据 channel_prefix 查找所有匹配的 channel_id"""
        with self._lock:
            return [cid for cid in self._channels if cid.startswith(prefix)]

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
#  第二部分: 字节序解码器
# ================================================================
class ByteOrderDecoder:
    """
    字节序解码器 — 支持所有标准 Modbus 字节序和数据类型

    字节序说明 (以32位为例，4个字节 A B C D):
    - ABCD (big):    大端序，字节顺序 A→B→C→D (Motorola)
    - DCBA (little): 小端序，字节顺序 D→C→B→A (Intel)
    - BADC (swap16): 双字节交换，字节顺序 B→A→D→C
    - CDAB (swap32): 四字交换，字节顺序 C→D→A→B

    支持的数据类型:
    - int16, uint16    (1个寄存器 = 2字节)
    - int32, uint32    (2个寄存器 = 4字节)
    - float32          (2个寄存器 = 4字节)
    - int64, uint64    (4个寄存器 = 8字节)
    - float64          (4个寄存器 = 8字节)
    """

    _TYPE_INFO = {
        "int16":   {"size": 2, "fmt": ">h"},
        "uint16":  {"size": 2, "fmt": ">H"},
        "int32":   {"size": 4, "fmt": ">i"},
        "uint32":  {"size": 4, "fmt": ">I"},
        "float32": {"size": 4, "fmt": ">f"},
        "int64":   {"size": 8, "fmt": ">q"},
        "uint64":  {"size": 8, "fmt": ">Q"},
        "float64": {"size": 8, "fmt": ">d"},
    }

    @classmethod
    def decode(cls, raw_bytes: bytes, data_type: str, byte_order: str = "abcd") -> list:
        """
        将原始字节解码为指定数据类型和字节序的值列表

        Args:
            raw_bytes: 原始字节数据（每个寄存器2字节，网络字节序）
            data_type: 数据类型，如 "uint16", "int32", "float32", "float64" 等
            byte_order: 字节序，可选值 "abcd", "dcba", "badc", "cdab"

        Returns:
            解码后的值列表
        """
        type_info = cls._TYPE_INFO.get(data_type.lower())
        if type_info is None:
            raise ValueError(f"不支持的数据类型: {data_type}")

        value_size = type_info["size"]
        fmt = type_info["fmt"]
        results = []

        for i in range(0, len(raw_bytes), value_size):
            chunk = raw_bytes[i:i + value_size]
            if len(chunk) < value_size:
                break

            reordered = cls._reorder_bytes(chunk, byte_order.lower())
            value = struct.unpack(fmt, reordered)[0]
            results.append(float(value))

        return results

    @classmethod
    def _reorder_bytes(cls, data: bytes, byte_order: str) -> bytes:
        """
        根据字节序重新排列字节

        Args:
            data: 原始字节数据
            byte_order: 目标字节序

        Returns:
            重新排列后的字节
        """
        n = len(data)

        if byte_order == "abcd":
            return data
        elif byte_order == "dcba":
            return data[::-1]
        elif byte_order == "badc":
            if n >= 2:
                chunks = [data[i:i + 2][::-1] for i in range(0, n, 2)]
                return b"".join(chunks)
            return data
        elif byte_order == "cdab":
            if n >= 4:
                words = [data[i:i + 2] for i in range(0, n, 2)]
                swapped = []
                for j in range(0, len(words), 2):
                    if j + 1 < len(words):
                        swapped.append(words[j + 1])
                        swapped.append(words[j])
                    else:
                        swapped.append(words[j])
                return b"".join(swapped)
            return data
        else:
            raise ValueError(f"不支持的字节序: {byte_order}")

    @classmethod
    def encode(cls, value: float, data_type: str, byte_order: str = "abcd") -> bytes:
        """
        将单个值编码为指定数据类型和字节序的原始字节（用于写入寄存器）
        编码过程为 decode 的逆操作。

        Args:
            value: 要编码的数值
            data_type: 数据类型，如 "uint16", "int32", "float32", "float64" 等
            byte_order: 字节序，可选值 "abcd", "dcba", "badc", "cdab"

        Returns:
            编码后的字节数据（网络字节序，每个寄存器2字节）
        """
        type_info = cls._TYPE_INFO.get(data_type.lower())
        if type_info is None:
            raise ValueError(f"不支持的数据类型: {data_type}")

        fmt = type_info["fmt"]
        # 整数类型需将浮点形式转换为整数（用户在 UI 中可能输入 10.0）
        if fmt[-1] in ("h", "H", "i", "I", "q", "Q"):
            value = int(round(value))
        native_bytes = struct.pack(fmt, value)
        # _reorder_bytes 是对称变换（再次执行同种重排会还原），
        # 对 native 大端字节执行同样重排即可得到目标字节序的写入字节
        return cls._reorder_bytes(native_bytes, byte_order.lower())

    @classmethod
    def get_supported_types(cls) -> list:
        """返回支持的所有数据类型列表"""
        return list(cls._TYPE_INFO.keys())

    @classmethod
    def get_supported_byte_orders(cls) -> list:
        """返回支持的所有字节序列表"""
        return ["abcd", "dcba", "badc", "cdab"]

    @classmethod
    def test(cls):
        """单元测试 — 验证字节序解码正确性"""
        tests = [
            ("uint16", "abcd", b"\x00\x0A", [10.0]),
            ("uint16", "dcba", b"\x0A\x00", [10.0]),
            ("uint16", "badc", b"\x0A\x00", [10.0]),
            ("uint16", "cdab", b"\x00\x0A", [10.0]),
            ("int16", "abcd", b"\xFF\xF6", [-10.0]),
            ("uint32", "abcd", b"\x00\x00\x00\x64", [100.0]),
            ("uint32", "dcba", b"\x64\x00\x00\x00", [100.0]),
            ("uint32", "badc", b"\x00\x00\x64\x00", [100.0]),
            ("uint32", "cdab", b"\x00\x64\x00\x00", [100.0]),
            ("int32", "abcd", b"\xFF\xFF\xFF\x9C", [-100.0]),
            ("float32", "abcd", b"\x41\x20\x00\x00", [10.0]),
            ("float64", "abcd", b"\x40\x24\x00\x00\x00\x00\x00\x00", [10.0]),
        ]
        all_pass = True
        for data_type, byte_order, raw_bytes, expected in tests:
            try:
                result = cls.decode(raw_bytes, data_type, byte_order)
                if result == expected:
                    print(f"✓ {data_type}/{byte_order}: {raw_bytes.hex()} -> {result}")
                else:
                    print(f"✗ {data_type}/{byte_order}: {raw_bytes.hex()} -> {result}, expected {expected}")
                    all_pass = False
            except Exception as e:
                print(f"✗ {data_type}/{byte_order}: {e}")
                all_pass = False
        return all_pass


# ================================================================
#  第三部分: Modbus TCP 连接器 (原生 socket 实现)
# ================================================================
class ModbusTCPConnector:
    """Modbus TCP 连接器 — 纯 socket 实现，无 pymodbus 依赖"""

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01
    FUNC_WRITE_SINGLE_COIL   = 0x05
    FUNC_WRITE_SINGLE_REG    = 0x06
    FUNC_WRITE_MULTI_REGS    = 0x10

    def __init__(self, connection_id: str, host: str, port: int = 502,
                 slave_id: int = 1, timeout: float = 3.0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._txn_id = 0
        self._lock = threading.RLock()  # 可重入：允许 connect()→disconnect()、_send_request异常→disconnect() 同线程嵌套

    def connect(self) -> bool:
        with self._lock:
            # 释放可能残留的旧句柄，避免重连时旧 socket 泄漏/冲突
            self.disconnect()
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
        with self._lock:
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
        with self._lock:
            if not self._sock:
                return None
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

    def read_holding_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_HOLDING, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def read_input_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_INPUT_REG, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def _extract_raw_register_data(self, resp: bytes) -> Optional[bytes]:
        if len(resp) < 9:
            return None
        func_code = resp[7]
        if func_code & 0x80:
            exc_code = resp[8] if len(resp) > 8 else -1
            print(f"[Modbus] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        byte_count = resp[8]
        return resp[9:9 + byte_count]

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

    # ---- 写入接口 ----
    def _send_write_request(self, func_code: int, pdu_body: bytes) -> bool:
        """发送写请求并验证响应。返回 True 表示写入成功。"""
        with self._lock:
            if not self._sock:
                return False
            self._txn_id = (self._txn_id + 1) & 0xFFFF
            length = 2 + len(pdu_body)  # unit_id(1) + pdu
            mbap = struct.pack(">HHH", self._txn_id, 0, length)
            frame = mbap + pdu_body
            try:
                self._sock.sendall(frame)
                resp = self._recv_response()
                if resp is None or len(resp) < 9:
                    return False
                # 验证事务ID与功能码（异常码高位为1表示出错）
                resp_txn = struct.unpack(">H", resp[0:2])[0]
                if resp_txn != self._txn_id:
                    return False
                func_resp = resp[7]
                if func_resp & 0x80:
                    exc_code = resp[8] if len(resp) > 8 else -1
                    print(f"[Modbus TCP] 写入异常: func={func_resp:#x}, exc={exc_code}")
                    return False
                return True
            except Exception as e:
                print(f"[Modbus TCP] 写入通信错误: {e}")
                self.disconnect()
                return False

    def write_single_coil(self, addr: int, value: bool) -> bool:
        """写单个线圈 (功能码 0x05)。ON=0xFF00, OFF=0x0000"""
        coil_value = 0xFF00 if value else 0x0000
        pdu = struct.pack(">BBHH", self.slave_id, self.FUNC_WRITE_SINGLE_COIL,
                          addr, coil_value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_COIL, pdu)

    def write_single_register(self, addr: int, value: int) -> bool:
        """写单个保持寄存器 (功能码 0x06)。value 应为 0-65535。"""
        value = int(value) & 0xFFFF
        pdu = struct.pack(">BBHH", self.slave_id, self.FUNC_WRITE_SINGLE_REG,
                          addr, value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_REG, pdu)

    def write_multiple_registers(self, addr: int, values: list) -> bool:
        """写多个保持寄存器 (功能码 0x10)。values 为整数列表 (每个0-65535)。"""
        if not values:
            return False
        reg_count = len(values)
        byte_count = reg_count * 2
        regs_bytes = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
        pdu = struct.pack(">BBHHB", self.slave_id, self.FUNC_WRITE_MULTI_REGS,
                          addr, reg_count, byte_count) + regs_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu)

    def write_registers_raw(self, addr: int, raw_bytes: bytes) -> bool:
        """以原始字节写入连续保持寄存器 (功能码 0x10)。
        raw_bytes 长度必须为偶数（每2字节一个寄存器）。"""
        if not raw_bytes or len(raw_bytes) % 2 != 0:
            return False
        reg_count = len(raw_bytes) // 2
        byte_count = len(raw_bytes)
        pdu = struct.pack(">BBHHB", self.slave_id, self.FUNC_WRITE_MULTI_REGS,
                          addr, reg_count, byte_count) + raw_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu)


# ================================================================
#  第四部分: Modbus RTU 连接器 (串口实现)
# ================================================================
class ModbusRTUConnector:
    """Modbus RTU 连接器 — 使用 pyserial 实现串口通信"""

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01
    FUNC_WRITE_SINGLE_COIL   = 0x05
    FUNC_WRITE_SINGLE_REG    = 0x06
    FUNC_WRITE_MULTI_REGS    = 0x10

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
        self._lock = threading.RLock()  # 可重入：允许 connect()→disconnect()、_send_request异常→disconnect() 同线程嵌套

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
        with self._lock:
            # 释放可能残留的旧句柄，避免自身占用串口导致重新打开失败（串口连接冲突）
            self.disconnect()
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
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def is_connected(self) -> bool:
        ser = self._ser  # 单次读取引用，避免 connect/disconnect 并发时两次读 _ser 之间被置 None
        return ser is not None and ser.is_open

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        with self._lock:
            if not self._ser or not self._ser.is_open:
                return None
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

    def read_holding_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_HOLDING, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def read_input_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_INPUT_REG, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def _extract_raw_register_data(self, resp: bytes) -> Optional[bytes]:
        if len(resp) < 5:
            return None
        func_code = resp[1]
        if func_code & 0x80:
            exc_code = resp[2] if len(resp) > 2 else -1
            print(f"[Modbus RTU] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        byte_count = resp[2]
        return resp[3:3 + byte_count]

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

    # ---- 写入接口 ----
    def _build_write_frame(self, func_code: int, pdu_body: bytes) -> bytes:
        """构造 RTU 写帧：slave_id + func_code + pdu_body + CRC"""
        full = bytes([self.slave_id, func_code]) + pdu_body
        crc = self._calc_crc(full)
        return full + struct.pack("<H", crc)

    def _send_write_request(self, func_code: int, pdu_body: bytes) -> bool:
        """发送写请求并验证响应。返回 True 表示写入成功。"""
        with self._lock:
            if not self._ser or not self._ser.is_open:
                return False
            try:
                frame = self._build_write_frame(func_code, pdu_body)
                self._ser.flushInput()
                self._ser.write(frame)
                self._ser.flush()
                resp = self._recv_response()
                if resp is None or len(resp) < 5:
                    return False
                if not self._validate_frame(resp):
                    return False
                func_resp = resp[1]
                if func_resp & 0x80:
                    exc_code = resp[2] if len(resp) > 2 else -1
                    print(f"[Modbus RTU] 写入异常: func={func_resp:#x}, exc={exc_code}")
                    return False
                # 验证回显地址/功能码与请求一致
                if resp[0] != self.slave_id or resp[1] != func_code:
                    return False
                return True
            except Exception as e:
                print(f"[Modbus RTU] 写入通信错误: {e}")
                self.disconnect()
                return False

    def write_single_coil(self, addr: int, value: bool) -> bool:
        """写单个线圈 (功能码 0x05)。ON=0xFF00, OFF=0x0000"""
        coil_value = 0xFF00 if value else 0x0000
        pdu_body = struct.pack(">HH", addr, coil_value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_COIL, pdu_body)

    def write_single_register(self, addr: int, value: int) -> bool:
        """写单个保持寄存器 (功能码 0x06)。value 应为 0-65535。"""
        value = int(value) & 0xFFFF
        pdu_body = struct.pack(">HH", addr, value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_REG, pdu_body)

    def write_multiple_registers(self, addr: int, values: list) -> bool:
        """写多个保持寄存器 (功能码 0x10)。values 为整数列表 (每个0-65535)。"""
        if not values:
            return False
        reg_count = len(values)
        byte_count = reg_count * 2
        regs_bytes = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
        pdu_body = struct.pack(">HHB", addr, reg_count, byte_count) + regs_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu_body)

    def write_registers_raw(self, addr: int, raw_bytes: bytes) -> bool:
        """以原始字节写入连续保持寄存器 (功能码 0x10)。
        raw_bytes 长度必须为偶数（每2字节一个寄存器）。"""
        if not raw_bytes or len(raw_bytes) % 2 != 0:
            return False
        reg_count = len(raw_bytes) // 2
        byte_count = len(raw_bytes)
        pdu_body = struct.pack(">HHB", addr, reg_count, byte_count) + raw_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu_body)


# ================================================================
#  第四部分: Modbus ASCII 连接器 (串口实现)
# ================================================================
class ModbusASCIIConnector:
    """Modbus ASCII 连接器 — 使用 pyserial 实现串口通信"""

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01
    FUNC_WRITE_SINGLE_COIL   = 0x05
    FUNC_WRITE_SINGLE_REG    = 0x06
    FUNC_WRITE_MULTI_REGS    = 0x10

    def __init__(self, connection_id: str, port: str = "COM1",
                 baudrate: int = 9600, slave_id: int = 1,
                 timeout: float = 3.0, parity: str = "N",
                 stopbits: int = 1, bytesize: int = 7):
        self.connection_id = connection_id
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self._ser = None
        self._lock = threading.RLock()  # 可重入：允许 connect()→disconnect()、_send_request异常→disconnect() 同线程嵌套

    def _calc_lrc(self, data: bytes) -> int:
        lrc = 0
        for byte in data:
            lrc = (lrc + byte) & 0xFF
        lrc = (~lrc + 1) & 0xFF
        return lrc

    def _build_frame(self, func_code: int, start_addr: int, quantity: int) -> bytes:
        pdu = struct.pack(">BBHH", self.slave_id, func_code, start_addr, quantity)
        lrc = self._calc_lrc(pdu)
        hex_str = ":" + pdu.hex().upper() + f"{lrc:02X}" + "\r\n"
        return hex_str.encode("ascii")

    def _validate_frame(self, frame: str) -> bool:
        if not frame.startswith(":") or not frame.endswith("\r\n"):
            return False
        hex_data = frame[1:-2]
        if len(hex_data) % 2 != 0:
            return False
        try:
            raw_bytes = bytes.fromhex(hex_data)
        except ValueError:
            return False
        if len(raw_bytes) < 4:
            return False
        data = raw_bytes[:-1]
        received_lrc = raw_bytes[-1]
        expected_lrc = self._calc_lrc(data)
        return received_lrc == expected_lrc

    def _hex_to_bytes(self, hex_str: str) -> Optional[bytes]:
        try:
            return bytes.fromhex(hex_str)
        except ValueError:
            return None

    def connect(self) -> bool:
        if serial is None:
            print(f"[Modbus ASCII] 连接失败 {self.port} -> 缺少 pyserial 依赖，请安装: pip install pyserial")
            return False
        with self._lock:
            # 释放可能残留的旧句柄，避免自身占用串口导致重新打开失败（串口连接冲突）
            self.disconnect()
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
                print(f"[Modbus ASCII] 连接失败 {self.port} -> {e}")
                self._ser = None
                return False

    def disconnect(self):
        with self._lock:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def is_connected(self) -> bool:
        ser = self._ser  # 单次读取引用，避免 connect/disconnect 并发时两次读 _ser 之间被置 None
        return ser is not None and ser.is_open

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        with self._lock:
            if not self._ser or not self._ser.is_open:
                return None
            try:
                frame = self._build_frame(func_code, start_addr, quantity)
                self._ser.flushInput()
                self._ser.write(frame)
                self._ser.flush()
                return self._recv_response()
            except Exception as e:
                print(f"[Modbus ASCII] 通信错误: {e}")
                self.disconnect()
                return None

    def _recv_response(self) -> Optional[bytes]:
        try:
            time.sleep(0.05)
            resp = b""
            start_time = time.time()
            timeout = self.timeout
            while True:
                if self._ser.in_waiting > 0:
                    chunk = self._ser.read(self._ser.in_waiting)
                    resp += chunk
                    if b"\r\n" in resp:
                        break
                else:
                    time.sleep(0.05)
                    if len(resp) > 0 and self._ser.in_waiting == 0:
                        break
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    break
            if len(resp) < 10:
                return None
            resp_str = resp.decode("ascii", errors="replace")
            if not self._validate_frame(resp_str):
                return None
            hex_data = resp_str[1:-2]
            raw_bytes = self._hex_to_bytes(hex_data)
            if raw_bytes is None:
                return None
            return raw_bytes[:-1]
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

    def read_holding_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_HOLDING, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def read_input_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        resp = self._send_request(self.FUNC_READ_INPUT_REG, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    def _extract_raw_register_data(self, resp: bytes) -> Optional[bytes]:
        if len(resp) < 5:
            return None
        func_code = resp[1]
        if func_code & 0x80:
            exc_code = resp[2] if len(resp) > 2 else -1
            print(f"[Modbus ASCII] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        byte_count = resp[2]
        return resp[3:3 + byte_count]

    def _parse_register_response(self, resp: bytes):
        if len(resp) < 5:
            return None
        func_code = resp[1]
        if func_code & 0x80:
            exc_code = resp[2] if len(resp) > 2 else -1
            print(f"[Modbus ASCII] 异常响应: func={func_code:#x}, exc={exc_code}")
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

    # ---- 写入接口 ----
    def _build_write_frame(self, func_code: int, pdu_body: bytes) -> bytes:
        """构造 ASCII 写帧：以 ':' 开头，slave_id + func_code + pdu_body + LRC，
        以 CRLF 结尾，返回 ASCII 编码的字节串。"""
        full = bytes([self.slave_id, func_code]) + pdu_body
        lrc = self._calc_lrc(full)
        hex_str = ":" + full.hex().upper() + f"{lrc:02X}" + "\r\n"
        return hex_str.encode("ascii")

    def _send_write_request(self, func_code: int, pdu_body: bytes) -> bool:
        """发送写请求并验证响应。返回 True 表示写入成功。"""
        with self._lock:
            if not self._ser or not self._ser.is_open:
                return False
            try:
                frame = self._build_write_frame(func_code, pdu_body)
                self._ser.flushInput()
                self._ser.write(frame)
                self._ser.flush()
                # 复用读取路径获取响应
                resp = self._recv_response()
                if resp is None or len(resp) < 4:
                    return False
                # _recv_response 已返回 raw_bytes（不含 LRC）
                func_resp = resp[1]
                if func_resp & 0x80:
                    exc_code = resp[2] if len(resp) > 2 else -1
                    print(f"[Modbus ASCII] 写入异常: func={func_resp:#x}, exc={exc_code}")
                    return False
                if resp[0] != self.slave_id or resp[1] != func_code:
                    return False
                return True
            except Exception as e:
                print(f"[Modbus ASCII] 写入通信错误: {e}")
                self.disconnect()
                return False

    def write_single_coil(self, addr: int, value: bool) -> bool:
        """写单个线圈 (功能码 0x05)。ON=0xFF00, OFF=0x0000"""
        coil_value = 0xFF00 if value else 0x0000
        pdu_body = struct.pack(">HH", addr, coil_value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_COIL, pdu_body)

    def write_single_register(self, addr: int, value: int) -> bool:
        """写单个保持寄存器 (功能码 0x06)。value 应为 0-65535。"""
        value = int(value) & 0xFFFF
        pdu_body = struct.pack(">HH", addr, value)
        return self._send_write_request(self.FUNC_WRITE_SINGLE_REG, pdu_body)

    def write_multiple_registers(self, addr: int, values: list) -> bool:
        """写多个保持寄存器 (功能码 0x10)。values 为整数列表 (每个0-65535)。"""
        if not values:
            return False
        reg_count = len(values)
        byte_count = reg_count * 2
        regs_bytes = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in values)
        pdu_body = struct.pack(">HHB", addr, reg_count, byte_count) + regs_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu_body)

    def write_registers_raw(self, addr: int, raw_bytes: bytes) -> bool:
        """以原始字节写入连续保持寄存器 (功能码 0x10)。
        raw_bytes 长度必须为偶数（每2字节一个寄存器）。"""
        if not raw_bytes or len(raw_bytes) % 2 != 0:
            return False
        reg_count = len(raw_bytes) // 2
        byte_count = len(raw_bytes)
        pdu_body = struct.pack(">HHB", addr, reg_count, byte_count) + raw_bytes
        return self._send_write_request(self.FUNC_WRITE_MULTI_REGS, pdu_body)


# ================================================================
#  第五部分: 基恩士(Keyence) PLC 连接器
# ================================================================
class KeyencePLCConnector:
    """
    基恩士 PLC 上位链接协议连接器
    适用型号: KV-5500/7500/8000/Nano 等
    默认端口: 3000 (以太网上位链接)

    数据类型后缀说明:
    - .U:  无符号16位整数（默认，占1个寄存器）
    - .S:  有符号16位整数（占1个寄存器）
    - .UD: 无符号32位整数（占2个寄存器）
    - .D:  有符号32位整数（占2个寄存器）
    - .F:  32位浮点数（占2个寄存器）
    """

    KEYENCE_TYPE_MAP = {
        "uint16":  ".U",
        "int16":   ".S",
        "uint32":  ".UD",
        "int32":   ".D",
        "float32": ".F",
        "uint64":  ".UL",
        "int64":   ".L",
        "float64": ".LF",
    }

    def __init__(self, connection_id: str, host: str, port: int = 3000,
                 timeout: float = 3.0, unit: int = 0):
        self.connection_id = connection_id
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit = unit
        self._sock: Optional[socket.socket] = None
        self._lock = threading.RLock()  # 可重入：允许 connect()→disconnect()、_send_command异常→disconnect() 同线程嵌套

    def connect(self) -> bool:
        with self._lock:
            # 释放可能残留的旧句柄，避免重连时旧 socket 泄漏/冲突
            self.disconnect()
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
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    def is_connected(self) -> bool:
        return self._sock is not None

    def _send_command(self, cmd: str) -> Optional[str]:
        with self._lock:
            if not self._sock:
                return None
            full_cmd = cmd + "\r\n"
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

    def read_device(self, device_type: str, start_addr: int, count: int = 1, data_type: str = ""):
        """
        通用设备读取
        device_type: DM / MR / LR / TIM / CNT / VR 等
        data_type:   数据类型，如 "uint16", "float32" 等，用于生成类型后缀

        命令格式 (Keyence 上位链接协议):
          - 读取1个点:   RD  <设备类型><起始地址>.<数据类型后缀>
          - 读取多个点:  RDS <设备类型><起始地址>.<数据类型后缀> <数量>
        示例:     RD DM6000.U        -> 从DM6000读取1个无符号16位值
                  RDS DM300.U 5      -> 从DM300连续读取5个无符号16位值
                  RD DM300.F         -> 从DM300读取1个float32值

        注: RD 仅支持单点读取；count > 1 时必须使用 RDS（连续读取）命令，
            否则 PLC 将返回 E1（命令异常）错误。
        """
        dt = device_type.upper()[:3]
        type_suffix = self.KEYENCE_TYPE_MAP.get(data_type.lower(), ".U")
        # 不发送类型后缀：让 PLC 返回 16 位寄存器原始值，再由 _poll_one 中的
        # ByteOrderDecoder 按 data_type/byte_order 本地解码（与 Modbus 路径一致，
        # 且使 byte_order 配置对 Keyence 也生效）。
        type_suffix = ''
        if count == 1:
            cmd = f"RD {dt}{start_addr}{type_suffix}"
        else:
            cmd = f"RDS {dt}{start_addr}{type_suffix} {count}"
        resp = self._send_command(cmd)
        if resp is None:
            return None
        if resp.startswith("E"):
            print(f"[Keyence] PLC错误: {resp} (命令: {cmd})")
            return None
        values = resp.split()
        if not values:
            return None
        try:
            # PLC 返回 16 位寄存器原始值（5 位零填充十进制，如 "03932"）。
            # 显式按十进制解析，避免前导零被 int(v, 0) 拒绝。
            # 类型转换与字节序解码交给 _poll_one 中的 ByteOrderDecoder，
            # 与 Modbus 路径保持一致。
            output = [int(v, 10) for v in values]
            print(f"[Keyence] 命令: {cmd} 读取 {count} 个寄存器原始值: {output}")
            return output
        except ValueError:
            print(f"[Keyence] 响应解析失败: {resp} (命令: {cmd})")
            return None

    @staticmethod
    def parse_words(words: list, data_type: str = "float64",
                    byte_order: str = "abcd"):
        """
        将 Keyence PLC 返回的 16 位寄存器值列表解析为指定数据类型的数值。

        适用于调试或手动解析场景：将 RDS/RD 命令返回的原始十进制寄存器值
        （如 [55050, 28835, 2621, 49202]）直接解码为目标数值。

        Args:
            words:      16 位寄存器值列表（十进制），如 [55050, 28835, 2621, 49202]
            data_type:  目标数据类型，可选 "uint16"/"int16"/"uint32"/"int32"/
                        "float32"/"uint64"/"int64"/"float64"
            byte_order: 字节序，可选 "abcd"(大端)/"dcba"(小端)/"badc"(字内交换)/"cdab"(双字交换)

        Returns:
            解码后的数值列表（float），或在解析失败时返回 None

        示例:
            # 解析 LREAL (64位浮点, 4个寄存器)
            val = KeyencePLCConnector.parse_words(
                [55050, 28835, 2621, 49202], "float64", "abcd")

            # 解析 REAL (32位浮点, 2个寄存器)
            val = KeyencePLCConnector.parse_words(
                [16286, 17225], "float32", "abcd")

            # 解析 UDInt (32位无符号, 2个寄存器)
            val = KeyencePLCConnector.parse_words(
                [0, 1000], "uint32", "abcd")
        """
        try:
            raw_bytes = b"".join(
                struct.pack(">H", int(v) & 0xFFFF) for v in words)
            return ByteOrderDecoder.decode(raw_bytes, data_type, byte_order)
        except Exception as e:
            print(f"[KeyencePLCConnector] 解析失败: {e}")
            return None

    @staticmethod
    def parse_lreal(word_list: list, byte_order: str = "abcd"):
        """
        将 4 个 16 位寄存器值解析为 1 个 LREAL (64位 IEEE 754 双精度浮点数)。

        Keyence PLC 的 LREAL 类型占用 4 个连续寄存器（8字节），
        按高位寄存器在前的顺序返回。本方法支持 4 种字节序以适配
        不同 PLC 型号或传输场景。

        Args:
            word_list:  4 个 16 位整数的列表，如 [55050, 28835, 2621, 49202]
            byte_order: 字节序，默认 "abcd"（大端序，Keyence PLC 原生格式）
                        "abcd" - 大端 (Big-Endian, ABCD) ：PLC 原生格式
                        "dcba" - 小端 (Little-Endian, DCBA) ：x86 内存映射
                        "badc" - 字内交换 (BADC) ：某些特殊格式
                        "cdab" - 双字交换 (CDAB) ：另类字序

        Returns:
            解析后的浮点数值，或在解析失败时返回 None

        示例:
            >>> words = [55050, 28835, 2621, 49202]
            >>> val = KeyencePLCConnector.parse_lreal(words)
            >>> val = KeyencePLCConnector.parse_lreal(words, "dcba")
        """
        if len(word_list) != 4:
            raise ValueError(
                f"需要恰好 4 个 16 位整数来组成 LREAL (当前 {len(word_list)} 个)")
        result = KeyencePLCConnector.parse_words(
            word_list, "float64", byte_order)
        if result and len(result) > 0:
            return result[0]
        return None

    @staticmethod
    def parse_real(word_list: list, byte_order: str = "abcd"):
        """
        将 2 个 16 位寄存器值解析为 1 个 REAL (32位 IEEE 754 单精度浮点数)。

        Args:
            word_list:  2 个 16 位整数的列表，如 [16286, 17225]
            byte_order: 字节序，默认 "abcd"（大端序）

        Returns:
            解析后的浮点数值，或在解析失败时返回 None
        """
        if len(word_list) != 2:
            raise ValueError(
                f"需要恰好 2 个 16 位整数来组成 REAL (当前 {len(word_list)} 个)")
        result = KeyencePLCConnector.parse_words(
            word_list, "float32", byte_order)
        if result and len(result) > 0:
            return result[0]
        return None

    def write_device(self, device_type: str, start_addr: int,
                     value, data_type: str = "") -> bool:
        """
        通用设备写入（Keyence 上位链接协议 WR 命令）
        device_type: DM / MR / LR / TIM / CNT / VR 等
        data_type:   数据类型，用于生成类型后缀（同 read_device）
        value:       要写入的值（数字）

        命令格式: WR <设备类型><起始地址>.<类型后缀> <值>
        示例:     WR DM6000.U 100     -> 将100写入DM6000(无符号16位)
                  WR DM300.F 3.14     -> 将3.14写入DM300(float32)
        返回 True 表示写入成功，False 表示失败。

        注: 单点写入使用 WR；连续多点写入应使用 WRS（本方法仅支持单点）。
        """
        dt = device_type.upper()[:3]
        type_suffix = self.KEYENCE_TYPE_MAP.get(data_type.lower(), ".U")
        # 浮点类型保留浮点字面量；整数类型直接输出整数
        if type_suffix in (".F", ".LF"):
            value_str = f"{float(value)}"
        else:
            value_str = f"{int(round(float(value)))}"
        cmd = f"WR {dt}{start_addr}{type_suffix} {value_str}"
        resp = self._send_command(cmd)
        if resp is None:
            print(f"[Keyence] PLC写入超时: {cmd}")
            return False
        if resp.startswith("E"):
            print(f"[Keyence] PLC写入错误: {resp} (命令: {cmd})")
            return False
        # 成功响应为 "OK" 或空字符串（视型号而定），不含 'E' 即视为成功
        print(f"[Keyence] PLC写入成功: {resp} (命令: {cmd})")
        return True


# ================================================================
#  第四部分: 采集任务与后台采集线程
# ================================================================
class PollingTask:
    """单个采集任务配置"""

    _TYPE_TO_REGISTERS = {
        "int16":   1,
        "uint16":  1,
        "int32":   2,
        "uint32":  2,
        "float32": 2,
        "int64":   4,
        "uint64":  4,
        "float64": 4,
    }

    def __init__(self, task_id: str, connection_id: str,
                 connection_type: str, device_type: str,
                 start_addr: int, quantity: int,
                 channel_prefix: str, channel_name: str,
                 unit: str = "", scale: float = 1.0,
                 offset: float = 0.0, data_type: str = "uint16",
                 byte_order: str = "abcd"):
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
        self.byte_order = byte_order

    def get_registers_per_value(self) -> int:
        """获取每个值需要的寄存器数量"""
        return self._TYPE_TO_REGISTERS.get(self.data_type.lower(), 1)

    def get_total_registers(self) -> int:
        """获取总共需要读取的寄存器数量"""
        return self.quantity * self.get_registers_per_value()

    def get_channel_ids(self):
        reg_per_val = self.get_registers_per_value()
        return [f"{self.channel_prefix}_{self.start_addr + i * reg_per_val}"
                for i in range(self.quantity)]

    def get_channel_names(self):
        if self.quantity == 1:
            return [self.channel_name]
        reg_per_val = self.get_registers_per_value()
        return [f"{self.channel_name}[{self.start_addr + i * reg_per_val}]"
                for i in range(self.quantity)]

    def to_dict(self):
        return {k: getattr(self, k) for k in [
            "task_id", "connection_id", "connection_type",
            "device_type", "start_addr", "quantity",
            "channel_prefix", "channel_name", "unit",
            "scale", "offset", "data_type", "byte_order"
        ]}

    @classmethod
    def from_dict(cls, d):
        byte_order = d.pop("byte_order", "abcd")
        return cls(byte_order=byte_order, **d)


class WriteTask:
    """单个写入任务配置 — 周期性向设备写入指定值

    属性:
      task_id          任务唯一ID
      connection_id    所属连接ID
      connection_type  连接类型（modbus_tcp/modbus_rtu/modbus_ascii/keyence）
      device_type      设备类型/区域（modbus: holding/input/coil；keyence: DM/MR/LR/...）
      start_addr       起始地址
      value            要写入的值（数字）
      write_interval   写入频率（秒），即每隔多久写入一次
      data_type        数据类型（uint16/int32/float32/...）
      byte_order       字节序（仅 Modbus 多寄存器写入时使用）
      name             任务显示名称（可选）
    """

    _TYPE_TO_REGISTERS = PollingTask._TYPE_TO_REGISTERS

    def __init__(self, task_id: str, connection_id: str,
                 connection_type: str, device_type: str,
                 start_addr: int, value: float,
                 write_interval: float = 1.0,
                 data_type: str = "uint16",
                 byte_order: str = "abcd",
                 name: str = ""):
        self.task_id = task_id
        self.connection_id = connection_id
        self.connection_type = connection_type
        self.device_type = device_type
        self.start_addr = int(start_addr)
        self.value = float(value)
        self.write_interval = float(write_interval)
        self.data_type = data_type
        self.byte_order = byte_order
        self.name = name or f"写{device_type}{start_addr}"

    def get_registers_per_value(self) -> int:
        return self._TYPE_TO_REGISTERS.get(self.data_type.lower(), 1)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "connection_id": self.connection_id,
            "connection_type": self.connection_type,
            "device_type": self.device_type,
            "start_addr": self.start_addr,
            "value": self.value,
            "write_interval": self.write_interval,
            "data_type": self.data_type,
            "byte_order": self.byte_order,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ================================================================
#  计算写入任务 — 将指定计算任务/采集任务的值周期性写入设备
# ================================================================
class CalcWriteTask:
    """计算写入任务 — 读取指定 task_id 的实时值, 周期性写入设备

    与 WriteTask 的区别: value 不是固定值, 而是从 source_task_id
    (calc_task 或 polling_task) 动态读取的最新值.
    """

    _TYPE_TO_REGISTERS = WriteTask._TYPE_TO_REGISTERS

    def __init__(self, task_id: str, source_task_id: str,
                 connection_id: str, connection_type: str,
                 device_type: str, start_addr: int,
                 write_interval: float = 1.0,
                 data_type: str = "uint16",
                 byte_order: str = "abcd",
                 name: str = ""):
        self.task_id = task_id
        self.source_task_id = source_task_id
        self.connection_id = connection_id
        self.connection_type = connection_type
        self.device_type = device_type
        self.start_addr = int(start_addr)
        self.write_interval = float(write_interval)
        self.data_type = data_type
        self.byte_order = byte_order
        self.name = name or f"写{device_type}{start_addr}"

    def get_registers_per_value(self) -> int:
        return self._TYPE_TO_REGISTERS.get(self.data_type.lower(), 1)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "source_task_id": self.source_task_id,
            "connection_id": self.connection_id,
            "connection_type": self.connection_type,
            "device_type": self.device_type,
            "start_addr": self.start_addr,
            "write_interval": self.write_interval,
            "data_type": self.data_type,
            "byte_order": self.byte_order,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ================================================================
#  计算任务 — 根据公式实时计算通道值
# ================================================================
class CalcTask:
    """计算任务 — 基于公式引用其他通道的实时值进行计算

    公式中可使用的变量引用方式:
      - task_id:  如 task_out2_ch8  (引用该任务的第一个通道值)
      - channel_prefix: 如 out2_ch8  (引用该前缀下第一个通道值)
      - channel_id: 如 out2_ch8_4223 (直接引用完整通道ID)

    支持的运算符: +  -  *  /  %  **  ()
    支持的函数: abs, min, max, sqrt, sin, cos, tan, log, exp
    """

    import re as _re
    _FUNC_MAP = {
        "abs": abs, "min": min, "max": max,
        "sqrt": __import__("math").sqrt,
        "sin": __import__("math").sin,
        "cos": __import__("math").cos,
        "tan": __import__("math").tan,
        "log": __import__("math").log,
        "exp": __import__("math").exp,
        "pow": pow,
    }

    def __init__(self, task_id: str, channel_prefix: str,
                 channel_name: str, formula: str,
                 unit: str = "", scale: float = 1.0,
                 offset: float = 0.0):
        self.task_id = task_id
        self.channel_prefix = channel_prefix
        self.channel_name = channel_name
        self.formula = formula
        self.unit = unit
        self.scale = scale
        self.offset = offset

    def get_channel_id(self):
        """计算通道ID (使用 channel_prefix 作为唯一标识)"""
        return self.channel_prefix

    def get_channel_ids(self):
        return [self.channel_prefix]

    def get_channel_names(self):
        return [self.channel_name]

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "channel_prefix": self.channel_prefix,
            "channel_name": self.channel_name,
            "formula": self.formula,
            "unit": self.unit,
            "scale": self.scale,
            "offset": self.offset,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**d)

    @classmethod
    def extract_variables(cls, formula: str) -> list:
        """从公式中提取所有变量名 (标识符)"""
        return cls._re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', formula)

    @classmethod
    def evaluate(cls, formula: str, variable_values: dict) -> float:
        """使用给定变量值计算公式

        Args:
            formula: 公式字符串, 如 "(a-b)/2"
            variable_values: {变量名: 数值} 字典

        Returns:
            计算结果 (float)

        Raises:
            ValueError: 公式中包含未定义变量
        """
        # 合并内置函数
        namespace = {**cls._FUNC_MAP, **variable_values}
        # 检查未定义的变量
        found_vars = cls.extract_variables(formula)
        builtin_names = set(cls._FUNC_MAP.keys())
        for v in found_vars:
            if v not in variable_values and v not in builtin_names:
                raise ValueError(f"公式中包含未定义变量: '{v}'")
        try:
            result = eval(formula, {"__builtins__": {}}, namespace)
            return float(result)
        except Exception as e:
            raise ValueError(f"公式计算错误: {e}") from e


class AcquisitionWorker(QObject):
    """后台采集线程 — 逐任务轮询所有连接"""
    data_acquired = Signal(str, float, float)
    connection_status = Signal(str, bool, str)
    error_occurred = Signal(str, str)
    write_status = Signal(str, bool, str)  # (task_id, success, message)

    def __init__(self, store: DataStore):
        super().__init__()
        self.store = store
        self._connections = {}
        self._tasks = []
        self._write_tasks = []  # WriteTask 列表
        self._calc_tasks = []   # CalcTask 列表
        self._calc_write_tasks = []  # CalcWriteTask 列表
        self._running = False
        self._poll_interval = 0.5
        self._thread = None
        self._write_thread = None
        # 记录每个写入任务下一次应触发的时间戳
        self._write_next_time: dict = {}
        # task_id → PollingTask 映射 (用于公式变量解析)
        self._task_id_to_task: dict = {}
        # task_id → CalcTask 映射 (用于动态值解析)
        self._calc_task_id_to_task: dict = {}

    def add_connection(self, conn_id: str, conn_type: str, **kwargs):
        if conn_type == "modbus_tcp":
            conn = ModbusTCPConnector(conn_id, **kwargs)
        elif conn_type == "modbus_rtu":
            conn = ModbusRTUConnector(conn_id, **kwargs)
        elif conn_type == "modbus_ascii":
            conn = ModbusASCIIConnector(conn_id, **kwargs)
        elif conn_type == "keyence":
            conn = KeyencePLCConnector(conn_id, **kwargs)
        else:
            raise ValueError(f"未知连接类型: {conn_type}")
        self._connections[conn_id] = {"type": conn_type, "connector": conn}

    def add_task(self, task: PollingTask):
        self._tasks.append(task)
        self._task_id_to_task[task.task_id] = task
        ch_ids = task.get_channel_ids()
        ch_names = task.get_channel_names()
        for cid, cname in zip(ch_ids, ch_names):
            self.store.register_channel(
                cid, cname, task.unit, task.connection_id,
                task.scale, task.offset, task.data_type
            )

    def add_calc_task(self, task: CalcTask):
        self._calc_tasks.append(task)
        self._calc_task_id_to_task[task.task_id] = task
        ch_ids = task.get_channel_ids()
        ch_names = task.get_channel_names()
        for cid, cname in zip(ch_ids, ch_names):
            self.store.register_channel(
                cid, cname, task.unit, "",
                task.scale, task.offset, "float64"
            )

    def remove_calc_task(self, task_id: str):
        self._calc_tasks = [t for t in self._calc_tasks if t.task_id != task_id]
        self._calc_task_id_to_task.pop(task_id, None)

    def add_write_task(self, task: WriteTask):
        self._write_tasks.append(task)

    def remove_write_task(self, task_id: str):
        self._write_tasks = [t for t in self._write_tasks if t.task_id != task_id]
        self._write_next_time.pop(task_id, None)

    def add_calc_write_task(self, task: CalcWriteTask):
        self._calc_write_tasks.append(task)

    def remove_calc_write_task(self, task_id: str):
        self._calc_write_tasks = [t for t in self._calc_write_tasks if t.task_id != task_id]
        self._write_next_time.pop(task_id, None)

    def clear_all(self):
        self._tasks.clear()
        self._calc_tasks.clear()
        self._write_tasks.clear()
        self._calc_write_tasks.clear()
        self._write_next_time.clear()
        self._task_id_to_task.clear()
        self._calc_task_id_to_task.clear()
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
        # 启动写入线程（存在写入任务或计算写入任务时）
        if self._write_tasks or self._calc_write_tasks:
            self._write_thread = threading.Thread(target=self._run_write_loop, daemon=True)
            self._write_thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        if self._write_thread:
            self._write_thread.join(timeout=5)
        self._write_next_time.clear()

    def _run_loop(self):
        # 连接所有设备
        for conn_id, info in list(self._connections.items()):
            connector = info["connector"]
            ok = connector.connect()
            if isinstance(connector, ModbusRTUConnector) or isinstance(connector, ModbusASCIIConnector):
                conn_info = f"{connector.port} @ {connector.baudrate}"
            else:
                conn_info = f"{connector.host}:{connector.port}"
            self.connection_status.emit(conn_id, ok,
                f"{'连接成功' if ok else '连接失败'} {conn_info}")

        # 主循环
        while self._running:
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
                    # 完整 traceback 写入日志，UI 仅显示简短信息
                    _log_exception(type(e), e, e.__traceback__,
                                   source=f"采集异常[{task.connection_id}]")
                    self.error_occurred.emit(task.connection_id, str(e))

            # ---- 计算任务评估 ----
            if self._calc_tasks:
                self._evaluate_calc_tasks()

            time.sleep(self._poll_interval)

        # 清理
        for conn_id, info in list(self._connections.items()):
            info["connector"].disconnect()
            self.connection_status.emit(conn_id, False, "已断开")

    def _poll_one(self, connector, task: PollingTask):
        if isinstance(connector, ModbusTCPConnector) or isinstance(connector, ModbusRTUConnector) or isinstance(connector, ModbusASCIIConnector):
            if task.device_type == "coil":
                return connector.read_coils(task.start_addr, task.quantity)
            else:
                total_registers = task.get_total_registers()
                if task.device_type == "holding":
                    raw_bytes = connector.read_holding_registers_raw(task.start_addr, total_registers)
                elif task.device_type == "input":
                    raw_bytes = connector.read_input_registers_raw(task.start_addr, total_registers)
                else:
                    raw_bytes = connector.read_holding_registers_raw(task.start_addr, total_registers)

                if raw_bytes is None:
                    return None
                try:
                    return ByteOrderDecoder.decode(raw_bytes, task.data_type, task.byte_order)
                except Exception as e:
                    print(f"[ByteOrderDecoder] 解码失败: {e}")
                    return None
        elif isinstance(connector, KeyencePLCConnector):
            raw_values = connector.read_device(task.device_type, task.start_addr, task.quantity, task.data_type)
            if raw_values is None:
                return None
            try:
                # Keyence 返回 16 位寄存器原始值列表（十进制），按 Modbus 约定
                # （每个寄存器大端 2 字节）打包为原始字节，再用 ByteOrderDecoder
                # 按 data_type/byte_order 解码：4 寄存器 -> 1 个 float64，
                # 2 寄存器 -> 1 个 uint32，1 寄存器 -> 1 个 uint16，依此类推。
                raw_bytes = b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in raw_values)
                return ByteOrderDecoder.decode(raw_bytes, task.data_type, task.byte_order)
            except Exception as e:
                print(f"[ByteOrderDecoder] 解码失败: {e}")
                return None
        return None

    # ---- 计算任务评估 ----
    def _evaluate_calc_tasks(self):
        """评估所有计算任务, 将结果存入 DataStore 并发出信号.
        执行两轮以支持计算任务之间的相互依赖."""
        if not self._calc_tasks:
            return
        for _pass in range(2):
            for calc_task in self._calc_tasks:
                try:
                    ts = time.time()
                    formula = calc_task.formula

                    # 1. 提取公式中的变量名
                    var_names = CalcTask.extract_variables(formula)

                    # 2. 解析每个变量为实际通道值
                    variable_values = {}
                    for var_name in var_names:
                        if var_name in CalcTask._FUNC_MAP:
                            continue
                        # 策略1: 直接匹配 task_id
                        polling_task = self._task_id_to_task.get(var_name)
                        if polling_task:
                            ch_ids = polling_task.get_channel_ids()
                            if ch_ids:
                                val = self.store.get_latest_value(ch_ids[0])
                                if val is not None:
                                    variable_values[var_name] = val
                                    continue
                        # 策略2: 匹配 channel_prefix (查找以此前缀开头的通道)
                        channel_id = self.store.find_channel_by_prefix(var_name)
                        if channel_id:
                            val = self.store.get_latest_value(channel_id)
                            if val is not None:
                                variable_values[var_name] = val
                                continue
                        # 策略3: 匹配完整 channel_id
                        val = self.store.get_latest_value(var_name)
                        if val is not None:
                            variable_values[var_name] = val
                            continue
                        # 找不到则跳过此变量 (保持为0)
                        variable_values[var_name] = 0.0

                    # 3. 计算公式值
                    result = CalcTask.evaluate(formula, variable_values)

                    # 4. 存入 DataStore (使用 calc task 的 channel_prefix 作为通道ID)
                    ch_id = calc_task.channel_prefix
                    self.store.add_data(ch_id, ts, result)

                    # 仅在最后一轮发送信号
                    if _pass == 1:
                        self.data_acquired.emit(ch_id, ts, result)

                except Exception as e:
                    err_msg = f"计算任务[{calc_task.task_id}]错误: {e}"
                    self.error_occurred.emit(calc_task.task_id, err_msg)

    # ---- 写入循环 ----
    def _resolve_source_value(self, source_task_id: str) -> float:
        """根据 source_task_id 解析最新值.
        策略: 1) 匹配 polling task_id → 取第一个通道值
               2) 匹配 calc task_id → 取其 channel_prefix 对应值
               3) 匹配 channel_prefix → 查找通道值
               4) 匹配完整 channel_id → 直接取值
        找不到返回 None."""
        # 策略1: polling task
        polling_task = self._task_id_to_task.get(source_task_id)
        if polling_task:
            ch_ids = polling_task.get_channel_ids()
            if ch_ids:
                val = self.store.get_latest_value(ch_ids[0])
                if val is not None:
                    return val
        # 策略2: calc task (channel_prefix 即通道ID)
        calc_task = self._calc_task_id_to_task.get(source_task_id)
        if calc_task:
            ch_id = calc_task.channel_prefix
            val = self.store.get_latest_value(ch_id)
            if val is not None:
                return val
        # 策略3: 匹配 channel_prefix
        channel_id = self.store.find_channel_by_prefix(source_task_id)
        if channel_id:
            val = self.store.get_latest_value(channel_id)
            if val is not None:
                return val
        # 策略4: 匹配完整 channel_id
        val = self.store.get_latest_value(source_task_id)
        if val is not None:
            return val
        return None

    def _run_write_loop(self):
        """独立线程：按每个写入任务的频率周期性写入指定值。
        不同任务可有不同的写入频率；线程按 50ms 粒度检查到期任务。"""
        # 初始化下次触发时间为当前时间（启动后立即执行一次）
        now = time.time()
        for t in self._write_tasks:
            self._write_next_time[t.task_id] = now
        for t in self._calc_write_tasks:
            self._write_next_time[t.task_id] = now

        while self._running:
            now = time.time()
            # 处理固定值写入任务
            for task in list(self._write_tasks):
                if not self._running:
                    break
                next_t = self._write_next_time.get(task.task_id)
                if next_t is None:
                    self._write_next_time[task.task_id] = now
                    continue
                if now < next_t:
                    continue
                # 到期，执行写入
                ok, msg = self._write_one(task)
                self.write_status.emit(task.task_id, ok, msg)
                # 安排下一次触发
                self._write_next_time[task.task_id] = time.time() + max(0.05, task.write_interval)

            # 处理计算写入任务 (动态解析值)
            for task in list(self._calc_write_tasks):
                if not self._running:
                    break
                next_t = self._write_next_time.get(task.task_id)
                if next_t is None:
                    self._write_next_time[task.task_id] = now
                    continue
                if now < next_t:
                    continue
                # 动态解析源值
                value = self._resolve_source_value(task.source_task_id)
                if value is None:
                    self.write_status.emit(task.task_id, False,
                                           f"源任务[{task.source_task_id}]无数据")
                    self._write_next_time[task.task_id] = time.time() + max(0.05, task.write_interval)
                    continue
                # 使用解析到的值执行写入
                ok, msg = self._write_one(task, value=value)
                self.write_status.emit(task.task_id, ok, msg)
                # 安排下一次触发
                self._write_next_time[task.task_id] = time.time() + max(0.05, task.write_interval)

            time.sleep(0.05)

    def _write_one(self, task, value=None):
        """执行一次写入。返回 (success, message)
        value 参数: 若提供则使用该值, 否则使用 task.value (用于 WriteTask)"""
        if value is None:
            value = task.value
        conn_info = self._connections.get(task.connection_id)
        if not conn_info:
            return False, f"连接ID '{task.connection_id}' 不存在"
        connector = conn_info["connector"]
        if not connector.is_connected():
            if not connector.connect():
                return False, "设备未连接且重连失败"
        try:
            print(f"[写入] 开始写入: {value} 到 {task.connection_id}")
            if isinstance(connector, (ModbusTCPConnector, ModbusRTUConnector, ModbusASCIIConnector)):
                if task.device_type == "coil":
                    # 线圈：value 非0视为 ON
                    ok = connector.write_single_coil(task.start_addr, bool(value))
                else:
                    # 保持寄存器：按数据类型/字节序编码后写入
                    # input 区域不可写，自动回退到 holding
                    raw = ByteOrderDecoder.encode(value, task.data_type, task.byte_order)
                    # uint16/int16 单寄存器使用 0x06，其他多寄存器使用 0x10
                    if task.get_registers_per_value() == 1:
                        ok = connector.write_single_register(task.start_addr, int.from_bytes(raw, "big"))
                    else:
                        ok = connector.write_registers_raw(task.start_addr, raw)
                if ok:
                    return True, f"写入成功: {value}"
                return False, "写入失败（设备返回异常或无响应）"
            elif isinstance(connector, KeyencePLCConnector):
                ok = connector.write_device(task.device_type, task.start_addr,
                                            value, task.data_type)
                if ok:
                    return True, f"写入成功: {value}"
                return False, "写入失败（PLC返回错误或无响应）"
            else:
                print(f"[警告] 不支持的连接器类型: {type(connector).__name__}")
                return False, f"不支持的连接器类型: {type(connector).__name__}"
        except Exception as e:
            # 完整 traceback 写入日志，调用方仅接收简短信息
            _log_exception(type(e), e, e.__traceback__,
                           source=f"写入异常[{task.connection_id}]")
            return False, f"写入异常: {e}"


# ================================================================
#  第五部分: 图表组件
# ================================================================
class ChartWidget(pg.PlotWidget):
    """单个折线图组件"""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setBackground("#eff1f5")
        self.setTitle(title, color="#4c4f69", size="10pt")
        self.setLabel("left", color="#4c4f69")
        self.setLabel("bottom", "时间(s)", color="#4c4f69")
        self.showGrid(x=True, y=True, alpha=0.3)
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
        self.cmb_type.addItems(["Modbus TCP", "Modbus RTU", "Modbus ASCII", "Keyence PLC"])
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

    @_safe_event
    def _on_type_changed(self):
        conn_type = self.cmb_type.currentText()
        is_modbus_tcp = conn_type == "Modbus TCP"
        is_modbus_rtu = conn_type == "Modbus RTU"
        is_modbus_ascii = conn_type == "Modbus ASCII"
        is_keyence = conn_type == "Keyence PLC"

        is_serial = is_modbus_rtu or is_modbus_ascii

        self.lbl_host.setVisible(is_modbus_tcp or is_keyence)
        self.edit_host.setVisible(is_modbus_tcp or is_keyence)
        self.lbl_serial_port.setVisible(is_serial)
        self.edit_serial_port.setVisible(is_serial)

        self.spin_port.setVisible(is_modbus_tcp or is_keyence)
        self.lbl_baudrate.setVisible(is_serial)
        self.cmb_baudrate.setVisible(is_serial)

        self.lbl_slave.setVisible(is_modbus_tcp or is_serial)
        self.spin_slave.setVisible(is_modbus_tcp or is_serial)
        self.lbl_unit.setVisible(is_keyence)
        self.spin_unit.setVisible(is_keyence)

        self.lbl_parity.setVisible(is_serial)
        self.cmb_parity.setVisible(is_serial)
        self.lbl_stopbits.setVisible(is_serial)
        self.spin_stopbits.setVisible(is_serial)

        if is_modbus_tcp:
            self.spin_port.setValue(502)
        elif is_keyence:
            self.spin_port.setValue(3000)

    @_safe_event
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
        elif conn_type_text == "Modbus ASCII":
            conn_type = "modbus_ascii"
            params = {
                "port": self.edit_serial_port.text().strip(),
                "baudrate": int(self.cmb_baudrate.currentText()),
                "slave_id": self.spin_slave.value(),
                "parity": self.cmb_parity.currentText(),
                "stopbits": self.spin_stopbits.value(),
                "bytesize": 7
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
            elif info['type'] in ['modbus_rtu', 'modbus_ascii']:
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

        layout.addWidget(QLabel("数据类型:"), 9, 0)
        self.cmb_data_type = QComboBox()
        self.cmb_data_type.addItems([
            "uint16", "int16", "uint32", "int32",
            "float32", "uint64", "int64", "float64"
        ])
        self.cmb_data_type.setCurrentText("uint16")
        layout.addWidget(self.cmb_data_type, 9, 1)

        layout.addWidget(QLabel("字节序:"), 10, 0)
        self.cmb_byte_order = QComboBox()
        self.cmb_byte_order.addItems([
            "abcd (大端)", "dcba (小端)",
            "badc (双字节交换)", "cdab (四字交换)"
        ])
        self.cmb_byte_order.setCurrentText("abcd (大端)")
        layout.addWidget(self.cmb_byte_order, 10, 1)

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 11, 0, 1, 2)

    @_safe_event
    def _on_conn_changed(self, *args):
        cid = self.cmb_conn.currentData()
        if cid is None:
            return
        info = self._connections.get(cid)
        self.cmb_device.clear()
        if info and info["type"] in ("modbus_tcp", "modbus_rtu"):
            self.cmb_device.addItems(["holding", "input", "coil"])
        elif info and info["type"] == "keyence":
            self.cmb_device.addItems(["DM", "MR", "LR", "TIM", "CNT", "VR"])

    @_safe_event
    def _on_ok(self):
        prefix = self.edit_prefix.text().strip()
        name = self.edit_name.text().strip()
        if not prefix or not name:
            QMessageBox.warning(self, "警告", "请填写通道前缀和名称")
            return
        cid = self.cmb_conn.currentData()
        
        data_type = self.cmb_data_type.currentText()
        byte_order_text = self.cmb_byte_order.currentText()
        byte_order = byte_order_text.split()[0]
        
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
            "data_type": data_type,
            "byte_order": byte_order,
        }
        self.task_added.emit(task_dict)
        self.close()


class WriteTaskConfigDialog(QWidget):
    """写入任务配置对话框 — 配置写入频率与写入值"""
    write_task_added = Signal(dict)

    def __init__(self, connections: dict):
        super().__init__()
        self._connections = connections
        self.setWindowTitle("添加写入任务")
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
            elif info['type'] in ['modbus_rtu', 'modbus_ascii']:
                label += f" {info['params'].get('port','')} @ {info['params'].get('baudrate',9600)}"
            else:
                label += f" {info['params'].get('host','')}:{info['params'].get('port',3000)}"
            self.cmb_conn.addItem(label, cid)
        layout.addWidget(self.cmb_conn, 0, 1)

        layout.addWidget(QLabel("任务名称:"), 1, 0)
        self.edit_name = QLineEdit("写值任务")
        layout.addWidget(self.edit_name, 1, 1)

        layout.addWidget(QLabel("设备类型:"), 2, 0)
        self.cmb_device = QComboBox()
        layout.addWidget(self.cmb_device, 2, 1)

        layout.addWidget(QLabel("起始地址:"), 3, 0)
        self.spin_addr = QSpinBox()
        self.spin_addr.setRange(0, 999999)
        layout.addWidget(self.spin_addr, 3, 1)

        layout.addWidget(QLabel("写入值:"), 4, 0)
        self.spin_value = QDoubleSpinBox()
        self.spin_value.setRange(-999999999, 999999999)
        self.spin_value.setDecimals(6)
        self.spin_value.setValue(0.0)
        layout.addWidget(self.spin_value, 4, 1)

        layout.addWidget(QLabel("写入频率(秒):"), 5, 0)
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.05, 3600.0)
        self.spin_interval.setSingleStep(0.1)
        self.spin_interval.setDecimals(3)
        self.spin_interval.setValue(1.0)
        layout.addWidget(self.spin_interval, 5, 1)

        layout.addWidget(QLabel("数据类型:"), 6, 0)
        self.cmb_data_type = QComboBox()
        self.cmb_data_type.addItems([
            "uint16", "int16", "uint32", "int32",
            "float32", "uint64", "int64", "float64"
        ])
        self.cmb_data_type.setCurrentText("uint16")
        layout.addWidget(self.cmb_data_type, 6, 1)

        layout.addWidget(QLabel("字节序:"), 7, 0)
        self.cmb_byte_order = QComboBox()
        self.cmb_byte_order.addItems([
            "abcd (大端)", "dcba (小端)",
            "badc (双字节交换)", "cdab (四字交换)"
        ])
        self.cmb_byte_order.setCurrentText("abcd (大端)")
        layout.addWidget(self.cmb_byte_order, 7, 1)

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 8, 0, 1, 2)

    @_safe_event
    def _on_conn_changed(self, *args):
        cid = self.cmb_conn.currentData()
        if cid is None:
            return
        info = self._connections.get(cid)
        self.cmb_device.clear()
        if info and info["type"] in ("modbus_tcp", "modbus_rtu", "modbus_ascii"):
            # input 区域不可写，仅提供 holding 与 coil
            self.cmb_device.addItems(["holding", "coil"])
        elif info and info["type"] == "keyence":
            self.cmb_device.addItems(["DM", "MR", "LR", "TIM", "CNT", "VR"])

    @_safe_event
    def _on_ok(self):
        name = self.edit_name.text().strip()
        if not name:
            QMessageBox.warning(self, "警告", "请填写任务名称")
            return
        cid = self.cmb_conn.currentData()
        if cid is None:
            QMessageBox.warning(self, "警告", "请选择所属连接")
            return

        data_type = self.cmb_data_type.currentText()
        byte_order_text = self.cmb_byte_order.currentText()
        byte_order = byte_order_text.split()[0]

        task_dict = {
            "task_id": f"wtask_{int(time.time()*1000)}",
            "connection_id": cid,
            "connection_type": self._connections[cid]["type"],
            "device_type": self.cmb_device.currentText(),
            "start_addr": self.spin_addr.value(),
            "value": self.spin_value.value(),
            "write_interval": self.spin_interval.value(),
            "data_type": data_type,
            "byte_order": byte_order,
            "name": name,
        }
        self.write_task_added.emit(task_dict)
        self.close()


# ================================================================
#  计算任务配置对话框
# ================================================================
class CalcTaskConfigDialog(QWidget):
    """计算任务配置对话框 — 通过公式计算通道值"""
    calc_task_added = Signal(dict)

    def __init__(self, connections: dict, tasks: list):
        super().__init__()
        self._connections = connections
        self._tasks = tasks  # PollingTask 列表, 用于提示可用变量
        self.setWindowTitle("添加计算任务")
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(480)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout(self)

        layout.addWidget(QLabel("通道ID(前缀):"), 0, 0)
        self.edit_prefix = QLineEdit("calc_ch1")
        layout.addWidget(self.edit_prefix, 0, 1)

        layout.addWidget(QLabel("通道名称:"), 1, 0)
        self.edit_name = QLineEdit("计算通道1")
        layout.addWidget(self.edit_name, 1, 1)

        layout.addWidget(QLabel("公式:"), 2, 0)
        self.edit_formula = QLineEdit("(task_out2_ch8 - task_out2_ch7) / 2")
        self.edit_formula.setPlaceholderText(
            "支持 + - * / % () 及 abs, min, max, sqrt, sin, cos, tan, log, exp, pow"
        )
        layout.addWidget(self.edit_formula, 2, 1)

        layout.addWidget(QLabel("单位:"), 3, 0)
        self.edit_unit = QLineEdit("")
        layout.addWidget(self.edit_unit, 3, 1)

        layout.addWidget(QLabel("缩放系数:"), 4, 0)
        self.spin_scale = QDoubleSpinBox()
        self.spin_scale.setRange(-999999, 999999)
        self.spin_scale.setDecimals(6)
        self.spin_scale.setValue(1.0)
        layout.addWidget(self.spin_scale, 4, 1)

        layout.addWidget(QLabel("偏移量:"), 5, 0)
        self.spin_offset = QDoubleSpinBox()
        self.spin_offset.setRange(-999999, 999999)
        self.spin_offset.setDecimals(6)
        layout.addWidget(self.spin_offset, 5, 1)

        # 可用变量提示
        var_hint_text = self._build_variable_hint()
        lbl_hint = QLabel(var_hint_text)
        lbl_hint.setStyleSheet("color: #6c7086; font-size: 11px;")
        lbl_hint.setWordWrap(True)
        layout.addWidget(lbl_hint, 6, 0, 1, 2)

        # 测试按钮
        self.btn_test = QPushButton("测试公式")
        self.btn_test.clicked.connect(self._on_test_formula)
        layout.addWidget(self.btn_test, 7, 0)

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 7, 1)

    def _build_variable_hint(self) -> str:
        """构建可用变量提示文本"""
        lines = ["📋 可用变量 (task_id / channel_prefix):"]
        if not self._tasks:
            lines.append("  (暂无采集任务, 请先添加采集任务)")
        else:
            for t in self._tasks[:20]:  # 最多显示20个
                ch_ids = t.get_channel_ids()
                prefix_info = f", 前缀: {t.channel_prefix}" if t.channel_prefix else ""
                lines.append(
                    f"  • {t.task_id} → {ch_ids[0] if ch_ids else '?'} "
                    f"({t.channel_name}{prefix_info})"
                )
            if len(self._tasks) > 20:
                lines.append(f"  ... 共 {len(self._tasks)} 个任务")
        return "\n".join(lines)

    @_safe_event
    def _on_test_formula(self):
        """测试公式 (仅做语法检查, 不实际连接设备)"""
        formula = self.edit_formula.text().strip()
        if not formula:
            QMessageBox.warning(self, "警告", "请输入公式")
            return
        try:
            # 尝试用虚拟变量 (值为1) 来测试公式语法
            var_names = CalcTask.extract_variables(formula)
            test_vars = {v: 1.0 for v in var_names
                        if v not in CalcTask._FUNC_MAP}
            result = CalcTask.evaluate(formula, test_vars)
            QMessageBox.information(self, "测试通过",
                                    f"公式语法正确!\n测试结果 (变量=1时): {result}")
        except ValueError as e:
            QMessageBox.warning(self, "公式错误", str(e))
        except Exception as e:
            QMessageBox.warning(self, "公式错误", f"公式解析失败: {e}")

    @_safe_event
    def _on_ok(self):
        prefix = self.edit_prefix.text().strip()
        name = self.edit_name.text().strip()
        formula = self.edit_formula.text().strip()
        if not prefix or not name:
            QMessageBox.warning(self, "警告", "请填写通道ID和名称")
            return
        if not formula:
            QMessageBox.warning(self, "警告", "请输入计算公式")
            return
        # 验证公式
        try:
            var_names = CalcTask.extract_variables(formula)
            test_vars = {v: 1.0 for v in var_names
                        if v not in CalcTask._FUNC_MAP}
            CalcTask.evaluate(formula, test_vars)
        except ValueError as e:
            QMessageBox.warning(self, "公式错误", str(e))
            return

        task_dict = {
            "task_id": f"calc_{int(time.time()*1000)}",
            "channel_prefix": prefix,
            "channel_name": name,
            "formula": formula,
            "unit": self.edit_unit.text().strip(),
            "scale": self.spin_scale.value(),
            "offset": self.spin_offset.value(),
        }
        self.calc_task_added.emit(task_dict)
        self.close()


# ================================================================
#  计算写入任务配置对话框
# ================================================================
class CalcWriteTaskConfigDialog(QWidget):
    """计算写入任务配置对话框 — 将指定任务的实时值写入设备"""
    calc_write_task_added = Signal(dict)

    def __init__(self, connections: dict, calc_tasks: list, polling_tasks: list):
        super().__init__()
        self._connections = connections
        self._calc_tasks = calc_tasks
        self._polling_tasks = polling_tasks
        self.setWindowTitle("添加计算写入任务")
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(520)
        self._build_ui()

    def _build_ui(self):
        layout = QGridLayout(self)

        # 源任务选择
        layout.addWidget(QLabel("数据源任务:"), 0, 0)
        self.cmb_source = QComboBox()
        self._build_source_combo()
        layout.addWidget(self.cmb_source, 0, 1)

        # 所属连接
        layout.addWidget(QLabel("写入连接:"), 1, 0)
        self.cmb_conn = QComboBox()
        for cid, info in self._connections.items():
            label = f"{cid} ({info['type']})"
            if info['type'] == 'modbus_tcp':
                label += f" {info['params'].get('host','')}:{info['params'].get('port',502)}"
            elif info['type'] in ['modbus_rtu', 'modbus_ascii']:
                label += f" {info['params'].get('port','')} @ {info['params'].get('baudrate',9600)}"
            self.cmb_conn.addItem(label, cid)
        layout.addWidget(self.cmb_conn, 1, 1)

        # 任务名称
        layout.addWidget(QLabel("任务名称:"), 2, 0)
        self.edit_name = QLineEdit("计算写入任务")
        layout.addWidget(self.edit_name, 2, 1)

        # 设备类型
        layout.addWidget(QLabel("设备类型:"), 3, 0)
        self.cmb_device = QComboBox()
        layout.addWidget(self.cmb_device, 3, 1)

        # 起始地址
        layout.addWidget(QLabel("起始地址:"), 4, 0)
        self.spin_addr = QSpinBox()
        self.spin_addr.setRange(0, 999999)
        layout.addWidget(self.spin_addr, 4, 1)

        # 写入频率
        layout.addWidget(QLabel("写入频率(秒):"), 5, 0)
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.05, 3600.0)
        self.spin_interval.setSingleStep(0.1)
        self.spin_interval.setDecimals(3)
        self.spin_interval.setValue(1.0)
        layout.addWidget(self.spin_interval, 5, 1)

        # 数据类型
        layout.addWidget(QLabel("数据类型:"), 6, 0)
        self.cmb_data_type = QComboBox()
        self.cmb_data_type.addItems([
            "uint16", "int16", "uint32", "int32",
            "float32", "uint64", "int64", "float64"
        ])
        self.cmb_data_type.setCurrentText("uint16")
        layout.addWidget(self.cmb_data_type, 6, 1)

        # 字节序
        layout.addWidget(QLabel("字节序:"), 7, 0)
        self.cmb_byte_order = QComboBox()
        self.cmb_byte_order.addItems([
            "abcd (大端)", "dcba (小端)",
            "badc (双字节交换)", "cdab (四字交换)"
        ])
        self.cmb_byte_order.setCurrentText("abcd (大端)")
        layout.addWidget(self.cmb_byte_order, 7, 1)

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        btn_layout = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self._on_ok)
        btn_cancel.clicked.connect(self.close)
        btn_layout.addWidget(btn_ok)
        btn_layout.addWidget(btn_cancel)
        layout.addLayout(btn_layout, 8, 0, 1, 2)

    def _build_source_combo(self):
        """构建源任务下拉列表, 包含计算任务和采集任务"""
        self.cmb_source.clear()
        self.cmb_source.addItem("--- 选择数据源 ---", "")
        # 添加计算任务
        if self._calc_tasks:
            self.cmb_source.addItem("── 计算任务 ──", "")
            for t in self._calc_tasks:
                label = f"[calc] {t.task_id} → {t.channel_prefix} ({t.channel_name})"
                self.cmb_source.addItem(label, t.task_id)
        # 添加采集任务
        if self._polling_tasks:
            self.cmb_source.addItem("── 采集任务 ──", "")
            for t in self._polling_tasks:
                ch_ids = t.get_channel_ids()
                label = f"[task] {t.task_id} → {ch_ids[0] if ch_ids else '?'} ({t.channel_name})"
                self.cmb_source.addItem(label, t.task_id)
        if not self._calc_tasks and not self._polling_tasks:
            self.cmb_source.setItemText(0, "(无可用任务, 请先添加采集或计算任务)")

    @_safe_event
    def _on_conn_changed(self, *args):
        cid = self.cmb_conn.currentData()
        if cid is None:
            return
        info = self._connections.get(cid)
        self.cmb_device.clear()
        if info and info["type"] in ("modbus_tcp", "modbus_rtu", "modbus_ascii"):
            self.cmb_device.addItems(["holding", "coil"])
        elif info and info["type"] == "keyence":
            self.cmb_device.addItems(["DM", "MR", "LR", "TIM", "CNT", "VR"])

    @_safe_event
    def _on_ok(self):
        source_task_id = self.cmb_source.currentData()
        if not source_task_id:
            QMessageBox.warning(self, "警告", "请选择数据源任务")
            return
        cid = self.cmb_conn.currentData()
        if cid is None:
            QMessageBox.warning(self, "警告", "请选择写入连接")
            return
        name = self.edit_name.text().strip() or "计算写入任务"

        data_type = self.cmb_data_type.currentText()
        byte_order_text = self.cmb_byte_order.currentText()
        byte_order = byte_order_text.split()[0]

        task_dict = {
            "task_id": f"calcwrite_{int(time.time()*1000)}",
            "source_task_id": source_task_id,
            "connection_id": cid,
            "connection_type": self._connections[cid]["type"],
            "device_type": self.cmb_device.currentText(),
            "start_addr": self.spin_addr.value(),
            "write_interval": self.spin_interval.value(),
            "data_type": data_type,
            "byte_order": byte_order,
            "name": name,
        }
        self.calc_write_task_added.emit(task_dict)
        self.close()


# ================================================================
#  第八部分: 主窗口
# ================================================================
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("多通道工业数据采集系统 — Modbus TCP/RTU/ASCII / Keyence PLC")
        self.resize(1400, 900)
        self.setMinimumSize(800, 600)

        self.store = DataStore(max_points=10000)
        self.worker = AcquisitionWorker(self.store)
        self._connections = {}
        self._tasks = []
        self._write_tasks = []  # WriteTask 列表
        self._calc_tasks = []   # CalcTask 列表
        self._calc_write_tasks = []  # CalcWriteTask 列表
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

        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        self._config_file = os.path.join(app_dir, "daq_config.yml")
        self._load_config()

        # 软件启动时自动开始采集（如果已配置采集任务或写入任务）
        if self._tasks or self._write_tasks or self._calc_tasks or self._calc_write_tasks:
            self._on_start()
            self.status_bar.showMessage("已自动开始采集", 3000)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # ---- 工具栏 ----
        toolbar = QHBoxLayout()
        self.btn_add_conn = QPushButton("➕ 添加连接")
        self.btn_add_task = QPushButton("➕ 添加采集任务")
        self.btn_add_calc_task = QPushButton("ƒ 添加计算任务")
        self.btn_add_write_task = QPushButton("✏ 添加写入任务")
        self.btn_add_calc_write_task = QPushButton("⇌ 计算值写入")
        self.btn_del_write_task = QPushButton("🗑 删除写入任务")
        self.btn_del_calc_write_task = QPushButton("🗑 删除计算写入")
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
        toolbar.addWidget(self.btn_add_calc_task)
        toolbar.addWidget(self.btn_add_write_task)
        toolbar.addWidget(self.btn_add_calc_write_task)
        toolbar.addWidget(self.btn_del_write_task)
        toolbar.addWidget(self.btn_del_calc_write_task)
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
        self.table_conn.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        conn_layout.addWidget(self.table_conn)
        conn_group.setLayout(conn_layout)
        left_layout.addWidget(conn_group)

        task_group = QGroupBox("已配置采集任务")
        task_layout = QVBoxLayout()
        self.table_task = QTableWidget(0, 8)
        self.table_task.setHorizontalHeaderLabels(
            ["连接ID", "类型", "设备类型", "起始地址", "数量", "通道前缀", "通道名称", "单位"])
        self.table_task.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_task.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        task_layout.addWidget(self.table_task)
        task_group.setLayout(task_layout)
        left_layout.addWidget(task_group)

        write_group = QGroupBox("已配置写入任务")
        write_layout = QVBoxLayout()
        self.table_write = QTableWidget(0, 8)
        self.table_write.setHorizontalHeaderLabels(
            ["任务名称", "连接ID", "类型", "设备类型", "起始地址",
             "写入值", "写入频率(s)", "状态"])
        self.table_write.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_write.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_write.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_write.setSelectionMode(QTableWidget.SingleSelection)
        write_layout.addWidget(self.table_write)
        write_group.setLayout(write_layout)
        left_layout.addWidget(write_group)

        # 计算任务表格
        calc_group = QGroupBox("已配置计算任务")
        calc_layout = QVBoxLayout()
        calc_btn_layout = QHBoxLayout()
        self.btn_del_calc_task = QPushButton("🗑 删除计算任务")
        calc_btn_layout.addWidget(self.btn_del_calc_task)
        calc_btn_layout.addStretch()
        calc_layout.addLayout(calc_btn_layout)
        self.table_calc = QTableWidget(0, 5)
        self.table_calc.setHorizontalHeaderLabels(
            ["通道ID", "通道名称", "公式", "单位", "缩放系数"])
        self.table_calc.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_calc.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_calc.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_calc.setSelectionMode(QTableWidget.SingleSelection)
        calc_layout.addWidget(self.table_calc)
        calc_group.setLayout(calc_layout)
        left_layout.addWidget(calc_group)

        # 计算写入任务表格
        cw_group = QGroupBox("已配置计算写入任务")
        cw_layout = QVBoxLayout()
        self.table_calc_write = QTableWidget(0, 8)
        self.table_calc_write.setHorizontalHeaderLabels(
            ["任务名称", "源任务ID", "连接ID", "设备类型", "起始地址",
             "写入频率(s)", "数据类型", "状态"])
        self.table_calc_write.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_calc_write.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_calc_write.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_calc_write.setSelectionMode(QTableWidget.SingleSelection)
        cw_layout.addWidget(self.table_calc_write)
        cw_group.setLayout(cw_layout)
        left_layout.addWidget(cw_group)

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
            "  1. 点击「添加连接」配置 Modbus TCP/RTU/ASCII / Keyence 设备\n"
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
        self.btn_add_calc_task.clicked.connect(self._on_add_calc_task)
        self.btn_add_write_task.clicked.connect(self._on_add_write_task)
        self.btn_add_calc_write_task.clicked.connect(self._on_add_calc_write_task)
        self.btn_del_write_task.clicked.connect(self._on_del_write_task)
        self.btn_del_calc_write_task.clicked.connect(self._on_del_calc_write_task)
        self.btn_del_calc_task.clicked.connect(self._on_del_calc_task)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_export.clicked.connect(self._on_export)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_save_cfg.clicked.connect(self._save_config)
        self.btn_load_cfg.clicked.connect(self._load_config)
        self.worker.data_acquired.connect(self._on_data_acquired)
        self.worker.connection_status.connect(self._on_conn_status)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.write_status.connect(self._on_write_status)
        self.table_conn.cellChanged.connect(self._on_conn_cell_changed)
        self.table_task.cellChanged.connect(self._on_task_cell_changed)

    # ---- 连接管理 ----
    @_safe_event
    def _on_add_connection(self):
        self._conn_dialog = ConnectionConfigDialog()
        self._conn_dialog.connection_added.connect(self._add_connection)
        self._conn_dialog.show()

    @_safe_event
    def _add_connection(self, conn_id, conn_type, params):
        if conn_id in self._connections:
            QMessageBox.warning(self, "警告", f"连接ID '{conn_id}' 已存在")
            return
        # 串口冲突检测：避免多个连接指向同一物理串口，否则后打开者会因自身占用而失败
        if conn_type in ("modbus_rtu", "modbus_ascii"):
            new_port = str(params.get("port", "")).strip().lower()
            if new_port:
                for cid, info in self._connections.items():
                    if info["type"] in ("modbus_rtu", "modbus_ascii") and \
                       str(info["params"].get("port", "")).strip().lower() == new_port:
                        QMessageBox.warning(self, "串口冲突",
                            f"串口 '{params.get('port')}' 已被连接 '{cid}' 占用。\n\n"
                            f"同一物理串口同一时刻只能被一个连接打开，"
                            f"否则后打开的连接会因自身占用导致连接失败。")
                        return
        self._connections[conn_id] = {"type": conn_type, "params": params}
        self.worker.add_connection(conn_id, conn_type, **params)
        self._refresh_conn_table()
        self.status_bar.showMessage(f"已添加连接: {conn_id}", 3000)

    @_safe_event
    def _on_conn_cell_changed(self, row, col):
        if row < 0 or row >= len(self._connections):
            return
        conn_ids = list(self._connections.keys())
        conn_id = conn_ids[row]
        new_value = self.table_conn.item(row, col).text().strip()
        
        if col == 0:
            if not new_value:
                QMessageBox.warning(self, "警告", "连接ID不能为空")
                self.table_conn.blockSignals(True)
                self._refresh_conn_table()
                self.table_conn.blockSignals(False)
                return
            if new_value != conn_id and new_value in self._connections:
                QMessageBox.warning(self, "警告", f"连接ID '{new_value}' 已存在")
                self.table_conn.blockSignals(True)
                self._refresh_conn_table()
                self.table_conn.blockSignals(False)
                return
            worker_conn = self.worker._connections.pop(conn_id, None)
            if worker_conn:
                self.worker._connections[new_value] = worker_conn
            self._connections[new_value] = self._connections.pop(conn_id)
            self.table_conn.blockSignals(True)
            self._refresh_conn_table()
            self.table_conn.blockSignals(False)
            self.status_bar.showMessage(f"连接ID已修改为: {new_value}", 3000)
        elif col == 1:
            conn_type = new_value.lower()
            if conn_type not in ["modbus_tcp", "modbus_rtu", "modbus_ascii", "keyence"]:
                QMessageBox.warning(self, "警告", f"不支持的连接类型: {new_value}")
                self.table_conn.blockSignals(True)
                self._refresh_conn_table()
                self.table_conn.blockSignals(False)
                return
            self._connections[conn_id]["type"] = conn_type
            self.table_conn.blockSignals(True)
            self._refresh_conn_table()
            self.table_conn.blockSignals(False)
            self.status_bar.showMessage(f"连接类型已修改为: {new_value}", 3000)
        elif col == 2:
            info = self._connections[conn_id]
            worker_conn = self.worker._connections.get(conn_id)
            if info["type"] in ["modbus_rtu", "modbus_ascii"]:
                info["params"]["port"] = new_value
                if worker_conn:
                    worker_conn["connector"].port = new_value
            else:
                info["params"]["host"] = new_value
                if worker_conn:
                    worker_conn["connector"].host = new_value
            self.table_conn.blockSignals(True)
            self._refresh_conn_table()
            self.table_conn.blockSignals(False)
        elif col == 3:
            info = self._connections[conn_id]
            try:
                val = int(new_value)
                if info["type"] in ["modbus_rtu", "modbus_ascii"]:
                    info["params"]["baudrate"] = val
                    worker_conn = self.worker._connections.get(conn_id)
                    if worker_conn:
                        worker_conn["connector"].baudrate = val
                else:
                    info["params"]["port"] = val
                    worker_conn = self.worker._connections.get(conn_id)
                    if worker_conn:
                        worker_conn["connector"].port = val
                self.table_conn.blockSignals(True)
                self._refresh_conn_table()
                self.table_conn.blockSignals(False)
            except ValueError:
                QMessageBox.warning(self, "警告", "端口/波特率必须为整数")
                self.table_conn.blockSignals(True)
                self._refresh_conn_table()
                self.table_conn.blockSignals(False)

    # ---- 任务管理 ----
    @_safe_event
    def _on_add_task(self):
        if not self._connections:
            QMessageBox.warning(self, "提示", "请先添加至少一个连接")
            return
        self._task_dialog = TaskConfigDialog(self._connections)
        self._task_dialog.task_added.connect(self._add_task)
        self._task_dialog.show()

    @_safe_event
    def _add_task(self, task_dict):
        task = PollingTask.from_dict(task_dict)
        self._tasks.append(task)
        self.worker.add_task(task)
        self._refresh_task_table()
        self._ensure_chart_for_task(task)
        self.status_bar.showMessage(f"已添加采集任务: {task.channel_name}", 3000)

    # ---- 计算任务管理 ----
    @_safe_event
    def _on_add_calc_task(self):
        if not self._tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务 (计算任务需要引用采集任务的数据)")
            return
        self._calc_task_dialog = CalcTaskConfigDialog(self._connections, self._tasks)
        self._calc_task_dialog.calc_task_added.connect(self._add_calc_task)
        self._calc_task_dialog.show()

    @_safe_event
    def _add_calc_task(self, task_dict):
        task = CalcTask.from_dict(task_dict)
        self._calc_tasks.append(task)
        self.worker.add_calc_task(task)
        self._refresh_calc_table()
        self._ensure_chart_for_calc_task(task)
        self.status_bar.showMessage(f"已添加计算任务: {task.channel_name}", 3000)

    @_safe_event
    def _on_del_calc_task(self):
        row = self.table_calc.currentRow()
        if row < 0 or row >= len(self._calc_tasks):
            QMessageBox.warning(self, "提示", "请先在计算任务表中选择要删除的任务")
            return
        task = self._calc_tasks.pop(row)
        self.worker.remove_calc_task(task.task_id)
        self._refresh_calc_table()
        # 删除对应图表
        chart_id = task.channel_prefix
        if chart_id in self._chart_widgets:
            chart = self._chart_widgets.pop(chart_id)
            chart.deleteLater()
        # 清理 channel_to_chart 映射
        self._channel_to_chart = {
            k: v for k, v in self._channel_to_chart.items()
            if v != chart_id
        }
        self.status_bar.showMessage(f"已删除计算任务: {task.channel_name}", 3000)

    @_safe_event
    def _refresh_calc_table(self):
        self.table_calc.blockSignals(True)
        self.table_calc.setRowCount(len(self._calc_tasks))
        for i, task in enumerate(self._calc_tasks):
            self.table_calc.setItem(i, 0, QTableWidgetItem(task.channel_prefix))
            self.table_calc.setItem(i, 1, QTableWidgetItem(task.channel_name))
            self.table_calc.setItem(i, 2, QTableWidgetItem(task.formula))
            self.table_calc.setItem(i, 3, QTableWidgetItem(task.unit))
            self.table_calc.setItem(i, 4, QTableWidgetItem(str(task.scale)))
        self.table_calc.blockSignals(False)

    @_safe_event
    def _ensure_chart_for_calc_task(self, task: CalcTask):
        self._placeholder_label.setVisible(False)
        chart_id = task.channel_prefix
        if chart_id not in self._chart_widgets:
            chart = ChartWidget(title=task.channel_name)
            self.chart_container.addWidget(chart)
            self._chart_widgets[chart_id] = chart
        chart = self._chart_widgets[chart_id]
        for cid, cname in zip(task.get_channel_ids(), task.get_channel_names()):
            if cid not in self._channel_to_chart:
                chart.add_channel(cid, cname)
                self._channel_to_chart[cid] = chart_id

    # ---- 写入任务管理 ----
    @_safe_event
    def _on_add_write_task(self):
        if not self._connections:
            QMessageBox.warning(self, "提示", "请先添加至少一个连接")
            return
        self._write_task_dialog = WriteTaskConfigDialog(self._connections)
        self._write_task_dialog.write_task_added.connect(self._add_write_task)
        self._write_task_dialog.show()

    @_safe_event
    def _add_write_task(self, task_dict):
        task = WriteTask.from_dict(task_dict)
        self._write_tasks.append(task)
        self.worker.add_write_task(task)
        self._refresh_write_table()
        self.status_bar.showMessage(f"已添加写入任务: {task.name}", 3000)
        # 若采集已在运行，写入任务添加后需重启 worker 以启动写入线程
        if self.worker._running and self.worker._write_thread is None:
            self.worker._write_thread = threading.Thread(
                target=self.worker._run_write_loop, daemon=True)
            self.worker._write_thread.start()

    @_safe_event
    def _on_del_write_task(self):
        row = self.table_write.currentRow()
        if row < 0 or row >= len(self._write_tasks):
            QMessageBox.warning(self, "提示", "请先在写入任务表中选择要删除的任务")
            return
        task = self._write_tasks.pop(row)
        self.worker.remove_write_task(task.task_id)
        self._refresh_write_table()
        self.status_bar.showMessage(f"已删除写入任务: {task.name}", 3000)

    @_safe_event
    def _on_write_status(self, task_id, success, message):
        # 先检查是否为计算写入任务
        for row, task in enumerate(self._calc_write_tasks):
            if task.task_id == task_id:
                status_text = "✓ 成功" if success else f"✗ {message}"
                item = self.table_calc_write.item(row, 7)
                if item is None:
                    item = QTableWidgetItem(status_text)
                    self.table_calc_write.setItem(row, 7, item)
                else:
                    item.setText(status_text)
                color = QColor("#a6e3a1") if success else QColor("#f38ba8")
                item.setForeground(color)
                break
        else:
            # 固定值写入任务
            for row, task in enumerate(self._write_tasks):
                if task.task_id == task_id:
                    status_text = "✓ 成功" if success else "✗ 失败"
                    item = self.table_write.item(row, 7)
                    if item is None:
                        item = QTableWidgetItem(status_text)
                        self.table_write.setItem(row, 7, item)
                    else:
                        item.setText(status_text)
                    color = QColor("#a6e3a1") if success else QColor("#f38ba8")
                    item.setForeground(color)
                    break
        if not success:
            self.status_bar.showMessage(f"写入任务失败 [{task_id}]: {message}", 5000)

    @_safe_event
    def _refresh_write_table(self):
        self.table_write.blockSignals(True)
        self.table_write.setRowCount(len(self._write_tasks))
        for i, task in enumerate(self._write_tasks):
            self.table_write.setItem(i, 0, QTableWidgetItem(task.name))
            self.table_write.setItem(i, 1, QTableWidgetItem(task.connection_id))
            self.table_write.setItem(i, 2, QTableWidgetItem(task.connection_type))
            self.table_write.setItem(i, 3, QTableWidgetItem(task.device_type))
            self.table_write.setItem(i, 4, QTableWidgetItem(str(task.start_addr)))
            self.table_write.setItem(i, 5, QTableWidgetItem(str(task.value)))
            self.table_write.setItem(i, 6, QTableWidgetItem(f"{task.write_interval:g}"))
            self.table_write.setItem(i, 7, QTableWidgetItem("—"))
        self.table_write.blockSignals(False)

    # ---- 计算写入任务管理 ----
    @_safe_event
    def _on_add_calc_write_task(self):
        if not self._connections:
            QMessageBox.warning(self, "提示", "请先添加至少一个连接")
            return
        if not self._calc_tasks and not self._tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务或计算任务 (计算写入任务需要引用数据源)")
            return
        self._calc_write_task_dialog = CalcWriteTaskConfigDialog(
            self._connections, self._calc_tasks, self._tasks)
        self._calc_write_task_dialog.calc_write_task_added.connect(self._add_calc_write_task)
        self._calc_write_task_dialog.show()

    @_safe_event
    def _add_calc_write_task(self, task_dict):
        task = CalcWriteTask.from_dict(task_dict)
        self._calc_write_tasks.append(task)
        self.worker.add_calc_write_task(task)
        self._refresh_calc_write_table()
        self.status_bar.showMessage(f"已添加计算写入任务: {task.name}", 3000)
        # 若采集已在运行，需确保写入线程已启动
        if self.worker._running and self.worker._write_thread is None:
            self.worker._write_thread = threading.Thread(
                target=self.worker._run_write_loop, daemon=True)
            self.worker._write_thread.start()

    @_safe_event
    def _on_del_calc_write_task(self):
        row = self.table_calc_write.currentRow()
        if row < 0 or row >= len(self._calc_write_tasks):
            QMessageBox.warning(self, "提示", "请先在计算写入任务表中选择要删除的任务")
            return
        task = self._calc_write_tasks.pop(row)
        self.worker.remove_calc_write_task(task.task_id)
        self._refresh_calc_write_table()
        self.status_bar.showMessage(f"已删除计算写入任务: {task.name}", 3000)

    @_safe_event
    def _on_calc_write_status(self, task_id, success, message):
        # 更新计算写入任务表中的"状态"列
        for row, task in enumerate(self._calc_write_tasks):
            if task.task_id == task_id:
                status_text = "✓ 成功" if success else f"✗ {message}"
                item = self.table_calc_write.item(row, 7)
                if item is None:
                    item = QTableWidgetItem(status_text)
                    self.table_calc_write.setItem(row, 7, item)
                else:
                    item.setText(status_text)
                color = QColor("#a6e3a1") if success else QColor("#f38ba8")
                item.setForeground(color)
                break
        if not success:
            self.status_bar.showMessage(f"计算写入任务失败 [{task_id}]: {message}", 5000)

    @_safe_event
    def _refresh_calc_write_table(self):
        self.table_calc_write.blockSignals(True)
        self.table_calc_write.setRowCount(len(self._calc_write_tasks))
        for i, task in enumerate(self._calc_write_tasks):
            self.table_calc_write.setItem(i, 0, QTableWidgetItem(task.name))
            self.table_calc_write.setItem(i, 1, QTableWidgetItem(task.source_task_id))
            self.table_calc_write.setItem(i, 2, QTableWidgetItem(task.connection_id))
            self.table_calc_write.setItem(i, 3, QTableWidgetItem(task.device_type))
            self.table_calc_write.setItem(i, 4, QTableWidgetItem(str(task.start_addr)))
            self.table_calc_write.setItem(i, 5, QTableWidgetItem(f"{task.write_interval:g}"))
            self.table_calc_write.setItem(i, 6, QTableWidgetItem(task.data_type))
            self.table_calc_write.setItem(i, 7, QTableWidgetItem("—"))
        self.table_calc_write.blockSignals(False)

    @_safe_event
    def _on_task_cell_changed(self, row, col):
        if row < 0 or row >= len(self._tasks):
            return
        task = self._tasks[row]
        new_value = self.table_task.item(row, col).text().strip()
        
        if col == 0:
            if new_value not in self._connections:
                QMessageBox.warning(self, "警告", f"连接ID '{new_value}' 不存在")
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
                return
            task.connection_id = new_value
            task.connection_type = self._connections[new_value]["type"]
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)
            self.status_bar.showMessage(f"任务连接已修改为: {new_value}", 3000)
        elif col == 1:
            conn_type = new_value.lower()
            if conn_type not in ["modbus_tcp", "modbus_rtu", "modbus_ascii", "keyence"]:
                QMessageBox.warning(self, "警告", f"不支持的连接类型: {new_value}")
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
                return
            task.connection_type = conn_type
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)
        elif col == 2:
            task.device_type = new_value
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)
        elif col == 3:
            try:
                task.start_addr = int(new_value)
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
            except ValueError:
                QMessageBox.warning(self, "警告", "起始地址必须为整数")
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
        elif col == 4:
            try:
                qty = int(new_value)
                if qty < 1 or qty > 125:
                    QMessageBox.warning(self, "警告", "读取数量必须在 1-125 之间")
                    self.table_task.blockSignals(True)
                    self._refresh_task_table()
                    self.table_task.blockSignals(False)
                    return
                task.quantity = qty
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
            except ValueError:
                QMessageBox.warning(self, "警告", "读取数量必须为整数")
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
        elif col == 5:
            QMessageBox.warning(self, "提示", "通道前缀不可编辑，请删除任务后重新添加")
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)
        elif col == 6:
            if not new_value:
                QMessageBox.warning(self, "警告", "通道名称不能为空")
                self.table_task.blockSignals(True)
                self._refresh_task_table()
                self.table_task.blockSignals(False)
                return
            old_name = task.channel_name
            task.channel_name = new_value
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)
            self._update_chart_for_task(task)
            self.status_bar.showMessage(f"通道名称已修改: {old_name} -> {new_value}", 3000)
        elif col == 7:
            task.unit = new_value
            self.table_task.blockSignals(True)
            self._refresh_task_table()
            self.table_task.blockSignals(False)

    @_safe_event
    def _update_chart_for_task(self, task):
        chart_id = task.task_id
        if chart_id in self._chart_widgets:
            chart = self._chart_widgets[chart_id]
            chart.setTitle(task.channel_name)

    @_safe_event
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
    @_safe_event
    def _on_start(self):
        if not self._tasks and not self._write_tasks and not self._calc_tasks and not self._calc_write_tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务、写入任务、计算任务或计算写入任务")
            return
        self.worker.set_poll_interval(self.spin_interval.value())
        self.worker.start()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_add_conn.setEnabled(False)
        self.btn_add_task.setEnabled(False)
        self.btn_add_calc_task.setEnabled(False)
        self.btn_add_write_task.setEnabled(False)
        self.btn_add_calc_write_task.setEnabled(False)
        self.btn_del_write_task.setEnabled(False)
        self.btn_del_calc_write_task.setEnabled(False)
        self.btn_del_calc_task.setEnabled(False)
        self.status_bar.showMessage("采集已启动", 3000)

    @_safe_event
    def _on_stop(self):
        self.worker.stop()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_add_conn.setEnabled(True)
        self.btn_add_task.setEnabled(True)
        self.btn_add_calc_task.setEnabled(True)
        self.btn_add_write_task.setEnabled(True)
        self.btn_add_calc_write_task.setEnabled(True)
        self.btn_del_write_task.setEnabled(True)
        self.btn_del_calc_write_task.setEnabled(True)
        self.btn_del_calc_task.setEnabled(True)
        self.status_bar.showMessage("采集已停止", 3000)

    # ---- 事件回调 ----
    @_safe_event
    def _on_data_acquired(self, channel_id, timestamp, value):
        pass

    @_safe_event
    def _on_conn_status(self, conn_id, connected, message):
        self.status_bar.showMessage(f"[{conn_id}] {message}", 3000)
        self._refresh_conn_table()
        if not connected and "连接失败" in message:
            QMessageBox.warning(self, "连接失败",
                f"设备 [{conn_id}] 连接失败!\n\n{message}\n\n请检查:\n• 设备IP地址和端口是否正确\n• 设备是否已开机\n• 网络是否通畅\n• 防火墙是否允许连接")

    @_safe_event
    def _on_error(self, conn_id, error):
        self.status_bar.showMessage(f"[{conn_id}] 错误: {error}", 5000)
        QMessageBox.warning(self, "通信错误",
            f"设备 [{conn_id}] 通信异常:\n\n{error}")

    # ---- 导出/清空 ----
    @_safe_event
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

    @_safe_event
    def _on_clear(self):
        reply = QMessageBox.question(self, "确认", "确定清空所有采集数据?")
        if reply == QMessageBox.Yes:
            self.store.clear()
            for chart in self._chart_widgets.values():
                chart.clear_all()
            self._refresh_status()
            self.status_bar.showMessage("数据已清空", 3000)

    # ---- 定时刷新 ----
    @_safe_event
    def _refresh_charts(self):
        for chart_id, chart in self._chart_widgets.items():
            for ch_id, cid in list(self._channel_to_chart.items()):
                if cid == chart_id:
                    ts, vals = self.store.get_channel_data(ch_id)
                    if ts:
                        chart.update_channel(ch_id, ts, vals)

    @_safe_event
    def _refresh_status(self):
        self.lbl_record_count.setText(f"记录数: {self.store.get_record_count()}")
        active = sum(1 for c in self.worker._connections.values()
                     if c["connector"].is_connected())
        self.lbl_active_conn.setText(
            f"活跃连接: {active}/{len(self._connections)}")

    @_safe_event
    def _refresh_conn_table(self):
        self.table_conn.blockSignals(True)
        self.table_conn.setRowCount(len(self._connections))
        for i, (cid, info) in enumerate(self._connections.items()):
            p = info["params"]
            self.table_conn.setItem(i, 0, QTableWidgetItem(cid))
            conn_type = info["type"]
            self.table_conn.setItem(i, 1, QTableWidgetItem(conn_type))
            
            if conn_type in ["modbus_rtu", "modbus_ascii"]:
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
            status_item = QTableWidgetItem(status)
            status_item.setFlags(status_item.flags() & ~Qt.ItemIsEditable)
            self.table_conn.setItem(i, 4, status_item)
        self.table_conn.blockSignals(False)

    @_safe_event
    def _refresh_task_table(self):
        self.table_task.blockSignals(True)
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
        self.table_task.blockSignals(False)

    # ---- 配置持久化 ----
    @_safe_event
    def _save_config(self):
        config = {
            "connections": {
                cid: {"type": info["type"], "params": info["params"]}
                for cid, info in self._connections.items()
            },
            "tasks": [t.to_dict() for t in self._tasks],
            "calc_tasks": [t.to_dict() for t in self._calc_tasks],
            "write_tasks": [t.to_dict() for t in self._write_tasks],
            "calc_write_tasks": [t.to_dict() for t in self._calc_write_tasks],
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

    @_safe_event
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
        self._calc_tasks.clear()
        self._write_tasks.clear()
        self._calc_write_tasks.clear()
        for chart in self._chart_widgets.values():
            chart.deleteLater()
        self._chart_widgets.clear()
        self._channel_to_chart.clear()

        for cid, info in config.get("connections", {}).items():
            self._add_connection(cid, info["type"], info["params"])
        for td in config.get("tasks", []):
            self._add_task(td)
        for td in config.get("calc_tasks", []):
            self._add_calc_task(td)
        for td in config.get("write_tasks", []):
            self._add_write_task(td)
        for td in config.get("calc_write_tasks", []):
            self._add_calc_write_task(td)
        self.spin_interval.setValue(config.get("poll_interval", 0.5))

        self._refresh_conn_table()
        self._refresh_task_table()
        self._refresh_calc_table()
        self._refresh_write_table()
        self._refresh_calc_write_table()
        self.status_bar.showMessage("配置已加载", 3000)

    @_safe_event
    def closeEvent(self, event):
        if self.worker._running:
            self.worker.stop()
        event.accept()


# ================================================================
#  第九部分: 程序入口
# ================================================================
class _TeeStream:
    """同时写入原流与日志文件的分流器"""

    def __init__(self, original, log_file):
        self._original = original
        self._log_file = log_file
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return
        try:
            self._original.write(text)
            self._original.flush()
        except Exception:
            pass
        with self._lock:
            try:
                self._log_file.write(text)
                self._log_file.flush()
            except Exception:
                pass

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        with self._lock:
            try:
                self._log_file.flush()
            except Exception:
                pass

    def reconfigure(self, **kwargs):
        # 兼容 sys.stdout.reconfigure 调用（如 build.py 中的 UTF-8 设置）
        try:
            self._original.reconfigure(**kwargs)
        except Exception:
            pass


def _log_exception(exc_type, exc_value, exc_tb, source="未处理异常"):
    """将异常完整 traceback 写入日志文件（经 stderr Tee 落盘）。

    所有异常（主线程/工作线程/被捕获的处理异常）统一经此入口记录，
    确保即使 GUI 或控制台不可用也能持久化完整堆栈。
    日志通道本身故障时静默，避免抛出二次异常导致递归。"""
    try:
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(f"\n[{source}] {exc_type.__name__}: {exc_value}\n{tb_text}\n")
    except Exception:
        pass


def _setup_file_logging():
    """将 stdout/stderr 同时写入控制台和当前用户 Downloads/daq_logs 下的时间戳日志文件，
    并安装全局异常钩子以捕获所有未处理异常（主线程与工作线程）。"""
    # 日志写入当前 Windows 用户的 Downloads 文件夹下的 daq_logs 子目录
    log_dir = os.path.join(os.path.expanduser("~"), "Downloads", "daq_logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"daq_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

    try:
        log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    except Exception as e:
        print(f"[警告] 无法创建日志文件: {e}")
        return

    print(f"[日志] 输出文件: {log_path}")
    sys.stdout = _TeeStream(sys.stdout, log_file)
    sys.stderr = _TeeStream(sys.stderr, log_file)

    def _excepthook(exc_type, exc_value, exc_tb):
        # 先写日志（通过 stderr tee 落盘）
        _log_exception(exc_type, exc_value, exc_tb, source="未处理异常")
        # 同时弹窗提示用户（GUI 环境）
        try:
            from PySide6.QtWidgets import QMessageBox
            tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            QMessageBox.critical(None, "未处理的异常", tb_text[-2000:])
        except Exception:
            pass

    sys.excepthook = _excepthook

    # 捕获工作线程中未处理的异常，确保完整 traceback 写入日志
    def _threading_excepthook(args):
        _log_exception(args.exc_type, args.exc_value, args.exc_traceback,
                       source=f"线程异常[{args.thread.name}]")

    try:
        threading.excepthook = _threading_excepthook
    except AttributeError:
        # 老版本 Python 无 threading.excepthook（3.8 之前）
        pass


def main():
    _setup_file_logging()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Catppuccin Latte 亮色主题
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor("#eff1f5"))
    palette.setColor(palette.ColorRole.WindowText, QColor("#4c4f69"))
    palette.setColor(palette.ColorRole.Base, QColor("#e6e9ef"))
    palette.setColor(palette.ColorRole.AlternateBase, QColor("#eff1f5"))
    palette.setColor(palette.ColorRole.Text, QColor("#4c4f69"))
    palette.setColor(palette.ColorRole.Button, QColor("#dce0e8"))
    palette.setColor(palette.ColorRole.ButtonText, QColor("#4c4f69"))
    palette.setColor(palette.ColorRole.Highlight, QColor("#bcc0cc"))
    palette.setColor(palette.ColorRole.HighlightedText, QColor("#4c4f69"))
    app.setPalette(palette)

    pg.setConfigOption("background", "#eff1f5")
    pg.setConfigOption("foreground", "#4c4f69")

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
