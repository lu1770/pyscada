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
import re
import math
import time
import json
import struct
import socket
import threading
import traceback
from contextlib import contextmanager
from datetime import datetime
from collections import deque
from typing import Optional

try:
    import yaml

    class _IndentDumper(yaml.Dumper):
        """强制块序列（列表）随父级键一起缩进。"""

        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)
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
    QStatusBar, QSplitter, QScrollArea, QTabWidget
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
        matches = self.find_channels_by_prefix(prefix)
        return matches[0] if matches else None

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
        """清空所有采集数据（保留通道注册信息，否则清空后重新采集时
        add_data 会因通道未注册而丢弃所有样本，导致界面无法刷新）"""
        with self._lock:
            for ch in self._channels.values():
                ch["timestamps"].clear()
                ch["values"].clear()
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
#  共享常量与辅助函数
# ================================================================

# 通道配色（图表曲线与磁贴卡片共用）
_CHANNEL_COLORS = [
    "#f38ba8", "#fab387", "#f9e2af", "#a6e3a1",
    "#94e2d5", "#89dceb", "#b4befe", "#cba6f7"
]

# 任务对话框共用的下拉选项
_DATA_TYPE_ITEMS = [
    "uint16", "int16", "uint32", "int32",
    "float32", "uint64", "int64", "float64"
]
_BYTE_ORDER_ITEMS = [
    "abcd (大端)", "dcba (小端)",
    "badc (双字节交换)", "cdab (四字交换)"
]

# 全部支持的连接类型（连接/任务表格内联编辑校验共用）
_MODBUS_CONN_TYPES = ("modbus_tcp", "modbus_rtu", "modbus_ascii")
_SERIAL_CONN_TYPES = ("modbus_rtu", "modbus_ascii")
_CONNECTION_TYPES = _MODBUS_CONN_TYPES + ("keyence",)

# 数据类型 → 每个值占用的 16 位寄存器数量（采集/写入任务共用）
_TYPE_REGISTER_COUNT = {
    "int16": 1, "uint16": 1,
    "int32": 2, "uint32": 2, "float32": 2,
    "int64": 4, "uint64": 4, "float64": 4,
}


def _registers_per_value(data_type: str) -> int:
    """数据类型对应的 16 位寄存器数量（未知类型按 1 个寄存器处理）"""
    return _TYPE_REGISTER_COUNT.get(data_type.lower(), 1)


def _words_to_bytes(words) -> bytes:
    """将 16 位寄存器值列表打包为大端原始字节（每个值 2 字节）。
    Modbus 多寄存器写入与 Keyence 寄存器值编解码共用。"""
    return b"".join(struct.pack(">H", int(v) & 0xFFFF) for v in words)


@contextmanager
def _block_signals(widget):
    """临时阻塞 Qt 控件信号的上下文管理器（批量刷新表格时避免触发 cellChanged）"""
    widget.blockSignals(True)
    try:
        yield
    finally:
        widget.blockSignals(False)


def _connection_display_label(cid: str, info: dict) -> str:
    """构造连接下拉项文本: 'id (类型) 地址描述'"""
    conn_type = info["type"]
    label = f"{cid} ({conn_type})"
    p = info["params"]
    if conn_type in _SERIAL_CONN_TYPES:
        label += f" {p.get('port', '')} @ {p.get('baudrate', 9600)}"
    else:
        default_port = 502 if conn_type == "modbus_tcp" else 3000
        label += f" {p.get('host', '')}:{p.get('port', default_port)}"
    return label


def _connector_address(connector) -> str:
    """连接器的地址描述（用于状态提示）：串口显示 'COMx @ 波特率'，网络显示 'host:port'"""
    if isinstance(connector, ModbusSerialConnector):
        return f"{connector.port} @ {connector.baudrate}"
    return f"{connector.host}:{connector.port}"


def _populate_device_combo(cmb_device, conn_type: str, writable: bool = False):
    """根据连接类型填充设备类型下拉框。
    writable=True 时仅提供可写区域（holding/coil），否则含 input 只读区域。"""
    cmb_device.clear()
    if conn_type in _MODBUS_CONN_TYPES:
        cmb_device.addItems(["holding", "coil"] if writable
                            else ["holding", "input", "coil"])
    elif conn_type == "keyence":
        cmb_device.addItems(["DM", "MR", "LR", "TIM", "CNT", "VR"])


def _refresh_device_combo(cmb_conn, cmb_device, connections: dict,
                          writable: bool):
    """连接下拉框切换时刷新设备类型下拉框。三个任务对话框共用。"""
    cid = cmb_conn.currentData()
    if cid is None:
        return
    info = connections.get(cid)
    if info:
        _populate_device_combo(cmb_device, info["type"], writable=writable)


def _create_data_type_combo() -> QComboBox:
    """创建数据类型下拉框（8 种 Modbus 标准类型）"""
    cmb = QComboBox()
    cmb.addItems(_DATA_TYPE_ITEMS)
    cmb.setCurrentText("uint16")
    return cmb


def _create_byte_order_combo() -> QComboBox:
    """创建字节序下拉框（4 种标准字节序）"""
    cmb = QComboBox()
    cmb.addItems(_BYTE_ORDER_ITEMS)
    cmb.setCurrentText("abcd (大端)")
    return cmb


def _combo_byte_order(cmb: QComboBox) -> str:
    """从字节序下拉框文本提取代码，如 'abcd (大端)' -> 'abcd'"""
    return cmb.currentText().split()[0]


def _make_ok_cancel_layout(parent: QWidget, on_ok) -> QHBoxLayout:
    """构造 '确定/取消' 按钮行（确定触发 on_ok，取消关闭父窗口）"""
    btn_layout = QHBoxLayout()
    btn_ok = QPushButton("确定")
    btn_cancel = QPushButton("取消")
    btn_ok.clicked.connect(on_ok)
    btn_cancel.clicked.connect(parent.close)
    btn_layout.addWidget(btn_ok)
    btn_layout.addWidget(btn_cancel)
    return btn_layout


def _create_connection_combo(connections: dict) -> QComboBox:
    """创建 '所属连接' 下拉框（采集/写入/计算写入对话框共用）"""
    cmb = QComboBox()
    for cid, info in connections.items():
        cmb.addItem(_connection_display_label(cid, info), cid)
    return cmb


def _create_addr_spin() -> QSpinBox:
    """创建起始地址输入框（0-999999，各任务对话框共用）"""
    spin = QSpinBox()
    spin.setRange(0, 999999)
    return spin


def _create_interval_spin() -> QDoubleSpinBox:
    """创建写入频率(秒)输入框（固定值写入/计算写入对话框共用）"""
    spin = QDoubleSpinBox()
    spin.setRange(0.05, 3600.0)
    spin.setSingleStep(0.1)
    spin.setDecimals(3)
    spin.setValue(1.0)
    return spin


def _create_scale_spin() -> QDoubleSpinBox:
    """创建缩放系数输入框（采集/计算任务对话框共用）"""
    spin = QDoubleSpinBox()
    spin.setRange(-999999, 999999)
    spin.setDecimals(6)
    spin.setValue(1.0)
    return spin


def _create_offset_spin() -> QDoubleSpinBox:
    """创建偏移量输入框（采集/计算任务对话框共用）"""
    spin = QDoubleSpinBox()
    spin.setRange(-999999, 999999)
    spin.setDecimals(6)
    return spin


# ================================================================
#  共享: 基于 socket 的连接生命周期混入（TCP 类连接器共用）
# ================================================================
class _TCPConnectorMixin:
    """提供基于 TCP socket 的 connect / disconnect / is_connected 实现。
    ModbusTCPConnector 与 KeyencePLCConnector 共用此逻辑，避免重复。
    子类需具备 self.host / self.port / self.timeout / self._sock / self._lock。"""

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
                print(f"[{getattr(self, 'LOG_TAG', 'TCP')}] 连接失败 "
                      f"{self.host}:{self.port} -> {e}")
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


# ================================================================
#  第三部分: Modbus 连接器 (TCP / RTU / ASCII)
# ================================================================
class ModbusBaseConnector:
    """Modbus 连接器基类 — 封装 TCP/RTU/ASCII 三种传输共用的
    功能码常量、读写接口与 PDU（slave_id + func_code + data）解析逻辑。

    子类需实现:
      - connect() / disconnect() / is_connected()
      - _send_request(func_code, start_addr, quantity) -> 统一 PDU 或 None
      - _send_write_request(func_code, pdu_body) -> bool
        （pdu_body 不含 slave_id/func_code，由传输层自行封装）
    """

    FUNC_READ_HOLDING   = 0x03
    FUNC_READ_INPUT_REG = 0x04
    FUNC_READ_COILS     = 0x01
    FUNC_WRITE_SINGLE_COIL   = 0x05
    FUNC_WRITE_SINGLE_REG    = 0x06
    FUNC_WRITE_MULTI_REGS    = 0x10

    LOG_TAG = "Modbus"

    def __init__(self, connection_id: str, slave_id: int = 1,
                 timeout: float = 3.0):
        self.connection_id = connection_id
        self.slave_id = slave_id
        self.timeout = timeout
        self._lock = threading.RLock()  # 可重入：允许 connect()→disconnect()、请求异常→disconnect() 同线程嵌套

    # ---- 传输层抽象（子类实现）----
    def connect(self) -> bool:
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        """发送读请求，返回统一 PDU（slave_id + func_code + data）；失败返回 None"""
        raise NotImplementedError

    def _send_write_request(self, func_code: int, pdu_body: bytes) -> bool:
        """发送写请求（pdu_body 不含 slave_id/func_code），返回是否成功"""
        raise NotImplementedError

    # ---- 读取接口（三种传输共用）----
    def _read_registers(self, func_code: int, start_addr: int,
                        quantity: int) -> Optional[bytes]:
        """读寄存器请求的统一入口：发送请求并提取原始字节，失败返回 None。"""
        resp = self._send_request(func_code, start_addr, quantity)
        if resp is None:
            return None
        return self._extract_raw_register_data(resp)

    @staticmethod
    def _parse_register_data(reg_data: bytes):
        """将原始寄存器字节解析为 16 位整数值列表。"""
        values = []
        for i in range(0, len(reg_data), 2):
            chunk = reg_data[i:i + 2]
            if len(chunk) == 2:
                values.append(struct.unpack(">H", chunk)[0])
            elif len(chunk) == 1:
                values.append(chunk[0])
        return values

    def read_holding_registers(self, start_addr: int, quantity: int):
        reg_data = self._read_registers(
            self.FUNC_READ_HOLDING, start_addr, quantity)
        return self._parse_register_data(reg_data) if reg_data is not None else None

    def read_input_registers(self, start_addr: int, quantity: int):
        reg_data = self._read_registers(
            self.FUNC_READ_INPUT_REG, start_addr, quantity)
        return self._parse_register_data(reg_data) if reg_data is not None else None

    def read_coils(self, start_addr: int, quantity: int):
        resp = self._send_request(self.FUNC_READ_COILS, start_addr, quantity)
        if resp is None:
            return None
        return self._parse_bit_response(resp, quantity)

    def read_holding_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        return self._read_registers(
            self.FUNC_READ_HOLDING, start_addr, quantity)

    def read_input_registers_raw(self, start_addr: int, quantity: int) -> Optional[bytes]:
        return self._read_registers(
            self.FUNC_READ_INPUT_REG, start_addr, quantity)

    # ---- PDU 解析（统一布局: [slave_id, func_code, byte_count, ...data]）----
    def _check_exception(self, pdu: bytes):
        """检查 PDU 是否为异常响应；正常返回 func_code，异常打印日志并返回 None"""
        func_code = pdu[1]
        if func_code & 0x80:
            exc_code = pdu[2] if len(pdu) > 2 else -1
            print(f"[{self.LOG_TAG}] 异常响应: func={func_code:#x}, exc={exc_code}")
            return None
        return func_code

    def _extract_raw_register_data(self, pdu: bytes) -> Optional[bytes]:
        if len(pdu) < 3 or self._check_exception(pdu) is None:
            return None
        byte_count = pdu[2]
        return pdu[3:3 + byte_count]

    def _parse_bit_response(self, pdu: bytes, quantity: int):
        if len(pdu) < 3 or self._check_exception(pdu) is None:
            return None
        byte_count = pdu[2]
        bit_data = pdu[3:3 + byte_count]
        values = []
        for i in range(quantity):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx < len(bit_data):
                values.append((bit_data[byte_idx] >> bit_idx) & 0x01)
            else:
                values.append(0)
        return values

    # ---- 写入接口（三种传输共用；pdu_body 不含 slave_id/func_code）----
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
        pdu_body = struct.pack(">HHB", addr, reg_count, byte_count) + _words_to_bytes(values)
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


class ModbusTCPConnector(_TCPConnectorMixin, ModbusBaseConnector):
    """Modbus TCP 连接器 — 纯 socket 实现，无 pymodbus 依赖
    connect/disconnect/is_connected 由 _TCPConnectorMixin 提供（MRO 中排在前面以覆盖基类的抽象实现）。"""

    LOG_TAG = "Modbus"

    def __init__(self, connection_id: str, host: str, port: int = 502,
                 slave_id: int = 1, timeout: float = 3.0):
        super().__init__(connection_id, slave_id, timeout)
        self.host = host
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._txn_id = 0

    def _send_request(self, func_code: int, start_addr: int,
                      quantity: int) -> Optional[bytes]:
        with self._lock:
            if not self._sock:
                return None
            self._txn_id = (self._txn_id + 1) & 0xFFFF
            mbap = struct.pack(">HHH", self._txn_id, 0, 6)
            pdu = struct.pack(">BBHH", self.slave_id, func_code,
                              start_addr, quantity)
            try:
                self._sock.sendall(mbap + pdu)
                resp = self._recv_response()
                if resp is None:
                    return None
                return resp[6:]  # 去掉 MBAP 头（6字节），返回统一 PDU
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

    # ---- 写入接口 ----
    def _send_write_request(self, func_code: int, pdu_body: bytes) -> bool:
        """发送写请求并验证响应。返回 True 表示写入成功。"""
        with self._lock:
            if not self._sock:
                return False
            self._txn_id = (self._txn_id + 1) & 0xFFFF
            full_pdu = bytes([self.slave_id, func_code]) + pdu_body
            mbap = struct.pack(">HHH", self._txn_id, 0, len(full_pdu))
            try:
                self._sock.sendall(mbap + full_pdu)
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


# ----------------------------------------------------------------
#  Modbus 串口连接器基类 (RTU / ASCII 共用)
# ----------------------------------------------------------------
class ModbusSerialConnector(ModbusBaseConnector):
    """Modbus 串口连接器基类 — RTU/ASCII 共用的串口生命周期、
    帧封装骨架与收发流程。

    子类需实现:
      - _encode_frame(full_pdu) -> 线路帧（含校验）
      - _recv_pdu() -> 统一 PDU（slave_id + func + data）或 None
    """

    LOG_TAG = "Modbus Serial"

    def __init__(self, connection_id: str, port: str = "COM1",
                 baudrate: int = 9600, slave_id: int = 1,
                 timeout: float = 3.0, parity: str = "N",
                 stopbits: int = 1, bytesize: int = 8):
        super().__init__(connection_id, slave_id, timeout)
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self._ser = None

    def connect(self) -> bool:
        if serial is None:
            print(f"[{self.LOG_TAG}] 连接失败 {self.port} -> 缺少 pyserial 依赖，请安装: pip install pyserial")
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
                print(f"[{self.LOG_TAG}] 连接失败 {self.port} -> {e}")
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

    # ---- 帧封装骨架 ----
    def _encode_frame(self, full_pdu: bytes) -> bytes:
        """将完整 PDU（slave_id + func + body）封装为线路帧（含校验）"""
        raise NotImplementedError

    def _build_frame(self, func_code: int, start_addr: int, quantity: int) -> bytes:
        full_pdu = struct.pack(">BBHH", self.slave_id, func_code, start_addr, quantity)
        return self._encode_frame(full_pdu)

    def _build_write_frame(self, func_code: int, pdu_body: bytes) -> bytes:
        """构造写帧：slave_id + func_code + pdu_body + 校验"""
        full_pdu = bytes([self.slave_id, func_code]) + pdu_body
        return self._encode_frame(full_pdu)

    # ---- 收发骨架 ----
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
                return self._recv_pdu()
            except Exception as e:
                print(f"[{self.LOG_TAG}] 通信错误: {e}")
                self.disconnect()
                return None

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
                pdu = self._recv_pdu()
                return self._check_write_pdu(func_code, pdu)
            except Exception as e:
                print(f"[{self.LOG_TAG}] 写入通信错误: {e}")
                self.disconnect()
                return False

    def _check_write_pdu(self, func_code: int, pdu: Optional[bytes]) -> bool:
        """校验写响应 PDU：长度、异常标志、slave_id/func 与请求回显一致"""
        if pdu is None or len(pdu) < 2:
            return False
        func_resp = pdu[1]
        if func_resp & 0x80:
            exc_code = pdu[2] if len(pdu) > 2 else -1
            print(f"[{self.LOG_TAG}] 写入异常: func={func_resp:#x}, exc={exc_code}")
            return False
        # 验证回显地址/功能码与请求一致
        if pdu[0] != self.slave_id or pdu[1] != func_code:
            return False
        return True


# ----------------------------------------------------------------
#  Modbus RTU 连接器 (CRC16 校验)
# ----------------------------------------------------------------
class ModbusRTUConnector(ModbusSerialConnector):
    """Modbus RTU 连接器 — 使用 pyserial 实现串口通信"""

    LOG_TAG = "Modbus RTU"

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

    def _calc_crc(self, data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc = ((crc >> 8) & 0xFF) ^ self._CRC_TABLE[(crc ^ byte) & 0xFF]
        return crc

    def _encode_frame(self, full_pdu: bytes) -> bytes:
        """RTU 帧：完整 PDU + CRC16（小端）"""
        crc = self._calc_crc(full_pdu)
        return full_pdu + struct.pack("<H", crc)

    def _validate_frame(self, frame: bytes) -> bool:
        if len(frame) < 4:
            return False
        received_crc = struct.unpack("<H", frame[-2:])[0]
        return received_crc == self._calc_crc(frame[:-2])

    def _recv_pdu(self) -> Optional[bytes]:
        """接收 RTU 响应帧并校验 CRC，返回去掉 CRC 的统一 PDU"""
        try:
            time.sleep(0.05)
            if self._ser.in_waiting < 4:
                time.sleep(0.1)
            if self._ser.in_waiting < 4:
                return None
            resp = self._ser.read(self._ser.in_waiting)
            if len(resp) < 4 or not self._validate_frame(resp):
                return None
            return resp[:-2]
        except Exception:
            return None


# ----------------------------------------------------------------
#  Modbus ASCII 连接器 (LRC 校验, ':' 起始 / CRLF 结束)
# ----------------------------------------------------------------
class ModbusASCIIConnector(ModbusSerialConnector):
    """Modbus ASCII 连接器 — 使用 pyserial 实现串口通信（默认 7 数据位）"""

    LOG_TAG = "Modbus ASCII"

    def __init__(self, connection_id: str, port: str = "COM1",
                 baudrate: int = 9600, slave_id: int = 1,
                 timeout: float = 3.0, parity: str = "N",
                 stopbits: int = 1, bytesize: int = 7):
        super().__init__(connection_id, port, baudrate, slave_id,
                         timeout, parity, stopbits, bytesize)

    def _calc_lrc(self, data: bytes) -> int:
        lrc = 0
        for byte in data:
            lrc = (lrc + byte) & 0xFF
        lrc = (~lrc + 1) & 0xFF
        return lrc

    def _encode_frame(self, full_pdu: bytes) -> bytes:
        """ASCII 帧：':' + HEX(PDU) + LRC(2位HEX) + CRLF"""
        lrc = self._calc_lrc(full_pdu)
        hex_str = ":" + full_pdu.hex().upper() + f"{lrc:02X}" + "\r\n"
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
        return raw_bytes[-1] == self._calc_lrc(raw_bytes[:-1])

    @staticmethod
    def _hex_to_bytes(hex_str: str) -> Optional[bytes]:
        try:
            return bytes.fromhex(hex_str)
        except ValueError:
            return None

    def _recv_pdu(self) -> Optional[bytes]:
        """接收 ASCII 响应帧（':' 开头、CRLF 结尾），校验 LRC 后
        返回去掉 LRC 的统一 PDU（slave_id + func + data）"""
        try:
            time.sleep(0.05)
            resp = b""
            start_time = time.time()
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
                if time.time() - start_time >= self.timeout:
                    break
            if len(resp) < 10:
                return None
            resp_str = resp.decode("ascii", errors="replace")
            if not self._validate_frame(resp_str):
                return None
            raw_bytes = self._hex_to_bytes(resp_str[1:-2])
            if raw_bytes is None:
                return None
            return raw_bytes[:-1]  # 去掉 LRC，返回 PDU
        except Exception:
            return None


# ================================================================
#  第四部分: 基恩士(Keyence) PLC 连接器
# ================================================================
class KeyencePLCConnector(_TCPConnectorMixin):
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

    connect/disconnect/is_connected 由 _TCPConnectorMixin 提供。
    """

    LOG_TAG = "Keyence"

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
            raw_bytes = KeyencePLCConnector.pack_words(words)
            return ByteOrderDecoder.decode(raw_bytes, data_type, byte_order)
        except Exception as e:
            print(f"[KeyencePLCConnector] 解析失败: {e}")
            return None

    @staticmethod
    def pack_words(words: list) -> bytes:
        """将 16 位寄存器值列表（十进制）打包为大端原始字节（每值2字节）。
        parse_words 解码与采集轮询打包共用。"""
        return _words_to_bytes(words)

    @staticmethod
    def _parse_scalar(word_list: list, expected_len: int,
                      data_type: str, type_name: str,
                      byte_order: str = "abcd"):
        """将固定数量的 16 位寄存器值解析为单个标量（LREAL/REAL 共用）。
        寄存器数量不符时抛出 ValueError，解析失败返回 None。"""
        if len(word_list) != expected_len:
            raise ValueError(
                f"需要恰好 {expected_len} 个 16 位整数来组成 {type_name} "
                f"(当前 {len(word_list)} 个)")
        result = KeyencePLCConnector.parse_words(
            word_list, data_type, byte_order)
        return result[0] if result else None

    @staticmethod
    def parse_lreal(word_list: list, byte_order: str = "abcd"):
        """将 4 个 16 位寄存器值解析为 1 个 LREAL
        (64位 IEEE 754 双精度浮点数，Keyence PLC 原生大端序)。
        字节序可选 "abcd"/"dcba"/"badc"/"cdab"；解析失败返回 None。"""
        return KeyencePLCConnector._parse_scalar(
            word_list, 4, "float64", "LREAL", byte_order)

    @staticmethod
    def parse_real(word_list: list, byte_order: str = "abcd"):
        """将 2 个 16 位寄存器值解析为 1 个 REAL
        (32位 IEEE 754 单精度浮点数)。
        字节序可选 "abcd"/"dcba"/"badc"/"cdab"；解析失败返回 None。"""
        return KeyencePLCConnector._parse_scalar(
            word_list, 2, "float32", "REAL", byte_order)

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
#  第五部分: 采集任务与后台采集线程
# ================================================================
class PollingTask:
    """单个采集任务配置"""

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
        return _registers_per_value(self.data_type)

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


class _BaseWriteTask:
    """写入任务基类 — WriteTask（固定值）与 CalcWriteTask（动态值）的公共字段

    属性:
      task_id          任务唯一ID
      connection_id    所属连接ID
      connection_type  连接类型（modbus_tcp/modbus_rtu/modbus_ascii/keyence）
      device_type      设备类型/区域（modbus: holding/input/coil；keyence: DM/MR/LR/...）
      start_addr       起始地址
      write_interval   写入频率（秒），即每隔多久写入一次
      data_type        数据类型（uint16/int32/float32/...）
      byte_order       字节序（仅 Modbus 多寄存器写入时使用）
      name             任务显示名称（可选）
    """

    def __init__(self, task_id: str, connection_id: str,
                 connection_type: str, device_type: str,
                 start_addr: int, write_interval: float = 1.0,
                 data_type: str = "uint16", byte_order: str = "abcd",
                 name: str = ""):
        self.task_id = task_id
        self.connection_id = connection_id
        self.connection_type = connection_type
        self.device_type = device_type
        self.start_addr = int(start_addr)
        self.write_interval = float(write_interval)
        self.data_type = data_type
        self.byte_order = byte_order
        self.name = name or f"写{device_type}{start_addr}"

    def get_registers_per_value(self) -> int:
        return _registers_per_value(self.data_type)

    def _base_dict(self) -> dict:
        """公共字段的字典形式（子类 to_dict 在此基础上追加特有字段）"""
        return {
            "task_id": self.task_id,
            "connection_id": self.connection_id,
            "connection_type": self.connection_type,
            "device_type": self.device_type,
            "start_addr": self.start_addr,
            "write_interval": self.write_interval,
            "data_type": self.data_type,
            "byte_order": self.byte_order,
            "name": self.name,
        }


class WriteTask(_BaseWriteTask):
    """单个写入任务配置 — 周期性向设备写入固定值"""

    def __init__(self, task_id: str, connection_id: str,
                 connection_type: str, device_type: str,
                 start_addr: int, value: float,
                 write_interval: float = 1.0,
                 data_type: str = "uint16",
                 byte_order: str = "abcd",
                 name: str = ""):
        super().__init__(task_id, connection_id, connection_type,
                         device_type, start_addr, write_interval,
                         data_type, byte_order, name)
        self.value = float(value)

    def to_dict(self):
        d = self._base_dict()
        d["value"] = self.value
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


# ================================================================
#  计算写入任务 — 将指定计算任务/采集任务的值周期性写入设备
# ================================================================
class CalcWriteTask(_BaseWriteTask):
    """计算写入任务 — 读取指定 task_id 的实时值, 周期性写入设备

    与 WriteTask 的区别: value 不是固定值, 而是从 source_task_id
    (calc_task 或 polling_task) 动态读取的最新值.
    """

    def __init__(self, task_id: str, source_task_id: str,
                 connection_id: str, connection_type: str,
                 device_type: str, start_addr: int,
                 write_interval: float = 1.0,
                 data_type: str = "uint16",
                 byte_order: str = "abcd",
                 name: str = ""):
        super().__init__(task_id, connection_id, connection_type,
                         device_type, start_addr, write_interval,
                         data_type, byte_order, name)
        self.source_task_id = source_task_id

    def to_dict(self):
        d = self._base_dict()
        d["source_task_id"] = self.source_task_id
        return d

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

    _FUNC_MAP = {
        "abs": abs, "min": min, "max": max,
        "sqrt": math.sqrt,
        "sin": math.sin,
        "cos": math.cos,
        "tan": math.tan,
        "log": math.log,
        "exp": math.exp,
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
        return re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', formula)

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

    # 连接类型 → 连接器类（add_connection 工厂映射）
    _CONNECTOR_CLASSES = {
        "modbus_tcp": ModbusTCPConnector,
        "modbus_rtu": ModbusRTUConnector,
        "modbus_ascii": ModbusASCIIConnector,
        "keyence": KeyencePLCConnector,
    }

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
        cls = self._CONNECTOR_CLASSES.get(conn_type)
        if cls is None:
            raise ValueError(f"未知连接类型: {conn_type}")
        self._connections[conn_id] = {
            "type": conn_type, "connector": cls(conn_id, **kwargs)}

    def add_task(self, task: PollingTask):
        self._tasks.append(task)
        self._task_id_to_task[task.task_id] = task
        self._register_task_channels(task, task.connection_id, task.data_type)

    def add_calc_task(self, task: CalcTask):
        self._calc_tasks.append(task)
        self._calc_task_id_to_task[task.task_id] = task
        self._register_task_channels(task, "", "float64")

    def _register_task_channels(self, task, connection_id: str, data_type: str):
        """将任务产生的通道注册到 DataStore（采集任务与计算任务共用）"""
        for cid, cname in zip(task.get_channel_ids(), task.get_channel_names()):
            self.store.register_channel(
                cid, cname, task.unit, connection_id,
                task.scale, task.offset, data_type
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
        self.ensure_write_thread()

    def ensure_write_thread(self):
        """确保写入线程正在运行（采集运行中且存在任一写入任务时）。
        启动后动态新增写入任务时也可调用。"""
        if self._running and self._write_thread is None and \
                (self._write_tasks or self._calc_write_tasks):
            self._write_thread = threading.Thread(
                target=self._run_write_loop, daemon=True)
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
            self.connection_status.emit(conn_id, ok,
                f"{'连接成功' if ok else '连接失败'} {_connector_address(connector)}")

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

    @staticmethod
    def _decode_raw(raw_bytes, data_type: str, byte_order: str):
        """用 ByteOrderDecoder 解码原始字节；输入 None 或解码失败时返回 None"""
        if raw_bytes is None:
            return None
        try:
            return ByteOrderDecoder.decode(raw_bytes, data_type, byte_order)
        except Exception as e:
            print(f"[ByteOrderDecoder] 解码失败: {e}")
            return None

    def _poll_one(self, connector, task: PollingTask):
        if isinstance(connector, ModbusBaseConnector):
            if task.device_type == "coil":
                return connector.read_coils(task.start_addr, task.quantity)
            total_registers = task.get_total_registers()
            if task.device_type == "input":
                raw_bytes = connector.read_input_registers_raw(
                    task.start_addr, total_registers)
            else:
                # holding 及其他区域（未知类型回退 holding）
                raw_bytes = connector.read_holding_registers_raw(
                    task.start_addr, total_registers)
            return self._decode_raw(raw_bytes, task.data_type, task.byte_order)
        elif isinstance(connector, KeyencePLCConnector):
            raw_values = connector.read_device(
                task.device_type, task.start_addr, task.quantity, task.data_type)
            if raw_values is None:
                return None
            # Keyence 返回 16 位寄存器原始值列表（十进制），按 Modbus 约定
            # （每个寄存器大端 2 字节）打包为原始字节，再用 ByteOrderDecoder
            # 按 data_type/byte_order 解码：4 寄存器 -> 1 个 float64，
            # 2 寄存器 -> 1 个 uint32，1 寄存器 -> 1 个 uint16，依此类推。
            raw_bytes = _words_to_bytes(raw_values)
            return self._decode_raw(raw_bytes, task.data_type, task.byte_order)
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
                    #    （函数名跳过；找不到的变量按 0.0 参与计算）
                    variable_values = {}
                    for var_name in var_names:
                        if var_name in CalcTask._FUNC_MAP:
                            continue
                        variable_values[var_name] = \
                            self._resolve_channel_value(var_name, 0.0)

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

    # ---- 通道值解析（公式变量与写入源任务共用）----
    def _resolve_channel_value(self, name: str, default=None):
        """按名称解析最新通道值。
        策略: 1) 匹配 polling task_id → 取第一个通道值
               2) 匹配 calc task_id → 取其 channel_prefix 对应值
               3) 匹配 channel_prefix → 查找以此前缀开头的通道值
               4) 匹配完整 channel_id → 直接取值
        全部未命中时返回 default（公式变量传 0.0，写入源任务传 None）。"""
        # 策略1: polling task
        polling_task = self._task_id_to_task.get(name)
        if polling_task:
            ch_ids = polling_task.get_channel_ids()
            if ch_ids:
                val = self.store.get_latest_value(ch_ids[0])
                if val is not None:
                    return val
        # 策略2: calc task (channel_prefix 即通道ID)
        calc_task = self._calc_task_id_to_task.get(name)
        if calc_task:
            val = self.store.get_latest_value(calc_task.channel_prefix)
            if val is not None:
                return val
        # 策略3: 匹配 channel_prefix
        channel_id = self.store.find_channel_by_prefix(name)
        if channel_id:
            val = self.store.get_latest_value(channel_id)
            if val is not None:
                return val
        # 策略4: 匹配完整 channel_id
        val = self.store.get_latest_value(name)
        if val is not None:
            return val
        return default

    def _resolve_source_value(self, source_task_id: str) -> float:
        """根据 source_task_id 解析写入源的最新值，找不到返回 None。"""
        return self._resolve_channel_value(source_task_id, None)

    def _iter_write_tasks(self):
        """全部写入任务（固定值 WriteTask + 动态值 CalcWriteTask，
        均为 _BaseWriteTask 子类），供写循环统一调度。"""
        return self._write_tasks + self._calc_write_tasks

    def _write_task_due(self, task, now: float) -> bool:
        """判断任务是否到期；首次出现的任务先登记为当前时间（下一轮到期）。"""
        next_t = self._write_next_time.get(task.task_id)
        if next_t is None:
            self._write_next_time[task.task_id] = now
            return False
        return now >= next_t

    def _reschedule_write(self, task):
        """按写入间隔安排任务下一次触发时间（间隔下限 50ms）"""
        self._write_next_time[task.task_id] = \
            time.time() + max(0.05, task.write_interval)

    def _process_write_task(self, task):
        """执行单个到期写入任务并发出状态信号。
        CalcWriteTask 先动态解析源值，源无数据时发失败状态并跳过本次写入。"""
        value = None
        if isinstance(task, CalcWriteTask):
            value = self._resolve_source_value(task.source_task_id)
            if value is None:
                self.write_status.emit(
                    task.task_id, False,
                    f"源任务[{task.source_task_id}]无数据")
                self._reschedule_write(task)
                return
        ok, msg = self._write_one(task, value=value)
        self.write_status.emit(task.task_id, ok, msg)
        self._reschedule_write(task)

    def _run_write_loop(self):
        """独立线程：按每个写入任务的频率周期性写入指定值。
        不同任务可有不同的写入频率；线程按 50ms 粒度检查到期任务。"""
        # 初始化下次触发时间为当前时间（启动后立即执行一次）
        now = time.time()
        for t in self._iter_write_tasks():
            self._write_next_time[t.task_id] = now

        while self._running:
            now = time.time()
            for task in list(self._iter_write_tasks()):
                if not self._running:
                    break
                if self._write_task_due(task, now):
                    self._process_write_task(task)
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
            if isinstance(connector, ModbusBaseConnector):
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
#  第六部分: 图表组件
# ================================================================
class ChartWidget(pg.PlotWidget):
    """单个折线图组件"""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setBackground("#eff1f5")
        self.setTitle(title, color="#4c4f69", size="10pt")
        self.setLabel("right", color="#4c4f69")
        self.setLabel("bottom", "时间(s)", color="#4c4f69")
        self.showAxis("right")
        self.hideAxis("left")
        self.showGrid(x=True, y=True, alpha=0.3)
        self._curves = {}

    def add_channel(self, channel_id: str, name: str):
        color = _CHANNEL_COLORS[len(self._curves) % len(_CHANNEL_COLORS)]
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
#  第六部分(续): 磁贴显示组件
# ================================================================
class TileWidget(QWidget):
    """单个通道磁贴卡片，显示通道名称、最新值和单位"""

    def __init__(self, channel_id: str, name: str, unit: str = "",
                 color_index: int = 0, parent=None):
        super().__init__(parent)
        self.channel_id = channel_id
        self.unit = unit or ""
        accent = _CHANNEL_COLORS[color_index % len(_CHANNEL_COLORS)]

        # QWidget 子类需启用 styledBackground 才会绘制样式表背景
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("TileFrame")
        self.setStyleSheet(f"""
            QWidget#TileFrame {{
                background-color: #ffffff;
                border: 1px solid #bcc0cc;
                border-left: 4px solid {accent};
                border-radius: 8px;
            }}
            QLabel#TileName {{ color: #4c4f69; font-size: 12px; font-weight: 600; }}
            QLabel#TileValue {{ color: {accent}; font-size: 24px; font-weight: bold; }}
            QLabel#TileUnit {{ color: #6c7086; font-size: 11px; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        self.name_label = QLabel(name)
        self.name_label.setObjectName("TileName")
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)

        self.value_label = QLabel("--")
        self.value_label.setObjectName("TileValue")
        self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label, stretch=1)

        unit_row = QHBoxLayout()
        unit_row.addStretch()
        self.unit_label = QLabel(self.unit)
        self.unit_label.setObjectName("TileUnit")
        unit_row.addWidget(self.unit_label)
        unit_row.addStretch()
        layout.addLayout(unit_row)

        self.setMinimumSize(160, 110)

    def update_value(self, value):
        if value is None:
            self.value_label.setText("--")
            return
        try:
            self.value_label.setText(f"{float(value):.3f}")
        except (TypeError, ValueError):
            self.value_label.setText(str(value))

    def set_name(self, name: str):
        self.name_label.setText(name or "")


class TileDisplayWidget(QWidget):
    """磁贴显示容器，按网格排列各通道磁贴；与统计图选项卡共享数据源"""

    def __init__(self, columns: int = 4, parent=None):
        super().__init__(parent)
        self._columns = max(1, columns)
        self._tiles = {}            # channel_id -> TileWidget
        self._color_counter = 0    # 颜色循环计数

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._placeholder = QLabel(
            "📝 暂无通道数据\n\n"
            "请添加采集任务并开始采集后查看磁贴显示\n"
            "可切换到「统计图」选项卡查看实时折线图"
        )
        self._placeholder.setAlignment(Qt.AlignCenter)
        self._placeholder.setStyleSheet("color: #6c7086; font-size: 14px; padding: 80px;")
        outer.addWidget(self._placeholder, stretch=1)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.container.setStyleSheet("background-color: #e6e9ef;")
        self.grid = QGridLayout(self.container)
        self.grid.setAlignment(Qt.AlignTop)
        self.grid.setSpacing(10)
        self.grid.setContentsMargins(10, 10, 10, 10)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll, stretch=1)
        self.scroll.setVisible(False)  # 初始无通道时隐藏滚动区，占位提示居中

    def add_channel(self, channel_id: str, name: str, unit: str = ""):
        if channel_id in self._tiles:
            return
        # 首个通道加入时切换为滚动区显示
        if not self._tiles:
            self._placeholder.setVisible(False)
            self.scroll.setVisible(True)
        tile = TileWidget(channel_id, name, unit,
                          color_index=self._color_counter)
        self._color_counter += 1
        idx = len(self._tiles)
        row, col = idx // self._columns, idx % self._columns
        self.grid.addWidget(tile, row, col)
        self._tiles[channel_id] = tile

    def has_channel(self, channel_id: str) -> bool:
        return channel_id in self._tiles

    def update_channel(self, channel_id: str, value):
        tile = self._tiles.get(channel_id)
        if tile is not None:
            tile.update_value(value)

    def set_channel_name(self, channel_id: str, name: str):
        tile = self._tiles.get(channel_id)
        if tile is not None:
            tile.set_name(name)

    def remove_channel(self, channel_id: str):
        tile = self._tiles.pop(channel_id, None)
        if tile is None:
            return
        self.grid.removeWidget(tile)
        tile.setParent(None)
        tile.deleteLater()
        self._rebuild_grid()

    def _rebuild_grid(self):
        # 重新铺排剩余磁贴到紧凑网格位置，避免删除后留空位
        tiles = list(self._tiles.values())
        for tile in tiles:
            self.grid.removeWidget(tile)
        for idx, tile in enumerate(tiles):
            row, col = idx // self._columns, idx % self._columns
            self.grid.addWidget(tile, row, col)
        if not tiles:
            self._placeholder.setVisible(True)
            self.scroll.setVisible(False)

    def clear_all(self):
        for tile in list(self._tiles.values()):
            tile.setParent(None)
            tile.deleteLater()
        self._tiles.clear()
        self._color_counter = 0
        self._placeholder.setVisible(True)
        self.scroll.setVisible(False)

    def reset_values(self):
        """仅清空磁贴显示值（保留磁贴卡片本身），与图表清空曲线语义一致"""
        for tile in self._tiles.values():
            tile.update_value(None)


# ================================================================
#  第七部分: 连接配置对话框
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

        layout.addLayout(_make_ok_cancel_layout(self, self._on_ok), 7, 0, 1, 2)

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
#  第八部分: 任务配置对话框（采集 / 写入 / 计算写入）
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
        self.cmb_conn = _create_connection_combo(self._connections)
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
        self.spin_addr = _create_addr_spin()
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
        self.spin_scale = _create_scale_spin()
        layout.addWidget(self.spin_scale, 7, 1)

        layout.addWidget(QLabel("偏移量:"), 8, 0)
        self.spin_offset = _create_offset_spin()
        layout.addWidget(self.spin_offset, 8, 1)

        layout.addWidget(QLabel("数据类型:"), 9, 0)
        self.cmb_data_type = _create_data_type_combo()
        layout.addWidget(self.cmb_data_type, 9, 1)

        layout.addWidget(QLabel("字节序:"), 10, 0)
        self.cmb_byte_order = _create_byte_order_combo()
        layout.addWidget(self.cmb_byte_order, 10, 1)

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        layout.addLayout(_make_ok_cancel_layout(self, self._on_ok), 11, 0, 1, 2)

    @_safe_event
    def _on_conn_changed(self, *args):
        _refresh_device_combo(self.cmb_conn, self.cmb_device,
                              self._connections, writable=False)

    @_safe_event
    def _on_ok(self):
        prefix = self.edit_prefix.text().strip()
        name = self.edit_name.text().strip()
        if not prefix or not name:
            QMessageBox.warning(self, "警告", "请填写通道前缀和名称")
            return
        cid = self.cmb_conn.currentData()

        data_type = self.cmb_data_type.currentText()
        byte_order = _combo_byte_order(self.cmb_byte_order)

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


class _BaseWriteTaskDialog(QWidget):
    """写入类任务对话框基类 — 固定值写入(WriteTaskConfigDialog)与
    计算值写入(CalcWriteTaskConfigDialog)共用：所属连接、任务名称、设备类型、
    起始地址、写入频率、数据类型、字节序等公共行，以及任务字典构造与校验流程。

    子类通过钩子定制差异部分:
      _conn_label          连接行标签（"所属连接:" / "写入连接:"）
      _name_required       任务名称为空时是否弹警告（否则回退 _default_name()）
      _default_name()      名称输入框默认值/空名称回退值
      _task_id_prefix()    任务ID前缀（"wtask" / "calcwrite"）
      _add_extra_rows()    在起始地址行之后插入特有行，返回下一可用行号
      _validate()          附加校验，返回错误信息字符串或 None
      _extend_task_dict()  向任务字典追加特有字段（value / source_task_id）
    """

    task_submitted = Signal(dict)

    _conn_label = "所属连接:"
    _name_required = False

    def __init__(self, connections: dict, title: str, min_width: int = 400):
        super().__init__()
        self._connections = connections
        self.setWindowTitle(title)
        self.setWindowModality(Qt.ApplicationModal)
        self.setMinimumWidth(min_width)
        self._build_ui()

    def _build_ui(self):
        """子类实现：创建布局并调用 _add_common_write_rows 填充公共行"""
        raise NotImplementedError

    def _add_common_write_rows(self, layout: QGridLayout, start_row: int) -> int:
        """添加写入任务公共行，返回下一个可用行号。
        子类特有行经 _add_extra_rows 钩子插在起始地址与写入频率之间。"""
        r = start_row

        layout.addWidget(QLabel(self._conn_label), r, 0)
        self.cmb_conn = _create_connection_combo(self._connections)
        layout.addWidget(self.cmb_conn, r, 1)
        r += 1

        layout.addWidget(QLabel("任务名称:"), r, 0)
        self.edit_name = QLineEdit(self._default_name())
        layout.addWidget(self.edit_name, r, 1)
        r += 1

        layout.addWidget(QLabel("设备类型:"), r, 0)
        self.cmb_device = QComboBox()
        layout.addWidget(self.cmb_device, r, 1)
        r += 1

        layout.addWidget(QLabel("起始地址:"), r, 0)
        self.spin_addr = _create_addr_spin()
        layout.addWidget(self.spin_addr, r, 1)
        r += 1

        r = self._add_extra_rows(layout, r)

        layout.addWidget(QLabel("写入频率(秒):"), r, 0)
        self.spin_interval = _create_interval_spin()
        layout.addWidget(self.spin_interval, r, 1)
        r += 1

        layout.addWidget(QLabel("数据类型:"), r, 0)
        self.cmb_data_type = _create_data_type_combo()
        layout.addWidget(self.cmb_data_type, r, 1)
        r += 1

        layout.addWidget(QLabel("字节序:"), r, 0)
        self.cmb_byte_order = _create_byte_order_combo()
        layout.addWidget(self.cmb_byte_order, r, 1)
        r += 1

        self.cmb_conn.currentIndexChanged.connect(self._on_conn_changed)
        self._on_conn_changed()

        layout.addLayout(_make_ok_cancel_layout(self, self._on_ok),
                         r, 0, 1, 2)
        return r + 1

    # ---- 子类钩子 ----
    def _default_name(self) -> str:
        return "写值任务"

    def _task_id_prefix(self) -> str:
        return "wtask"

    def _add_extra_rows(self, layout: QGridLayout, row: int) -> int:
        """在起始地址行之后、写入频率行之前插入子类特有行"""
        return row

    def _validate(self):
        """附加校验：返回错误信息字符串；无错误返回 None"""
        return None

    def _extend_task_dict(self, task_dict: dict):
        """向任务字典追加子类特有字段"""

    @_safe_event
    def _on_conn_changed(self, *args):
        _refresh_device_combo(self.cmb_conn, self.cmb_device,
                              self._connections, writable=True)

    @_safe_event
    def _on_ok(self):
        cid = self.cmb_conn.currentData()
        if cid is None:
            QMessageBox.warning(self, "警告", "请选择所属连接")
            return
        name = self.edit_name.text().strip()
        if not name:
            if self._name_required:
                QMessageBox.warning(self, "警告", "请填写任务名称")
                return
            name = self._default_name()
        error = self._validate()
        if error:
            QMessageBox.warning(self, "警告", error)
            return

        task_dict = {
            "task_id": f"{self._task_id_prefix()}_{int(time.time()*1000)}",
            "connection_id": cid,
            "connection_type": self._connections[cid]["type"],
            "device_type": self.cmb_device.currentText(),
            "start_addr": self.spin_addr.value(),
            "write_interval": self.spin_interval.value(),
            "data_type": self.cmb_data_type.currentText(),
            "byte_order": _combo_byte_order(self.cmb_byte_order),
            "name": name,
        }
        self._extend_task_dict(task_dict)
        self.task_submitted.emit(task_dict)
        self.close()


class WriteTaskConfigDialog(_BaseWriteTaskDialog):
    """写入任务配置对话框 — 配置写入频率与固定写入值"""

    _name_required = True  # 固定值写入必须填写任务名称

    def __init__(self, connections: dict):
        super().__init__(connections, "添加写入任务")

    def _build_ui(self):
        layout = QGridLayout(self)
        self._add_common_write_rows(layout, 0)

    def _add_extra_rows(self, layout, row):
        layout.addWidget(QLabel("写入值:"), row, 0)
        self.spin_value = QDoubleSpinBox()
        self.spin_value.setRange(-999999999, 999999999)
        self.spin_value.setDecimals(6)
        self.spin_value.setValue(0.0)
        layout.addWidget(self.spin_value, row, 1)
        return row + 1

    def _extend_task_dict(self, task_dict):
        task_dict["value"] = self.spin_value.value()


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
        self.spin_scale = _create_scale_spin()
        layout.addWidget(self.spin_scale, 4, 1)

        layout.addWidget(QLabel("偏移量:"), 5, 0)
        self.spin_offset = _create_offset_spin()
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

        layout.addLayout(_make_ok_cancel_layout(self, self._on_ok), 7, 1)

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

    @staticmethod
    def _validate_formula(formula: str):
        """用虚拟变量（全部为 1.0，函数名除外）验证公式语法并试算。
        返回 (result, error_msg)：成功时 error_msg 为 None，失败时 result 为 None。"""
        try:
            var_names = CalcTask.extract_variables(formula)
            test_vars = {v: 1.0 for v in var_names
                         if v not in CalcTask._FUNC_MAP}
            return CalcTask.evaluate(formula, test_vars), None
        except Exception as e:
            return None, str(e)

    @_safe_event
    def _on_test_formula(self):
        """测试公式 (仅做语法检查, 不实际连接设备)"""
        formula = self.edit_formula.text().strip()
        if not formula:
            QMessageBox.warning(self, "警告", "请输入公式")
            return
        result, err = self._validate_formula(formula)
        if err is not None:
            QMessageBox.warning(self, "公式错误", err)
            return
        QMessageBox.information(self, "测试通过",
                                f"公式语法正确!\n测试结果 (变量=1时): {result}")

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
        _result, err = self._validate_formula(formula)
        if err is not None:
            QMessageBox.warning(self, "公式错误", err)
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
class CalcWriteTaskConfigDialog(_BaseWriteTaskDialog):
    """计算写入任务配置对话框 — 将指定任务的实时值写入设备"""

    _conn_label = "写入连接:"

    def __init__(self, connections: dict, calc_tasks: list, polling_tasks: list):
        # _build_ui 在基类 __init__ 中调用，需先备好源任务列表
        self._calc_tasks = calc_tasks
        self._polling_tasks = polling_tasks
        super().__init__(connections, "添加计算写入任务", min_width=520)

    def _build_ui(self):
        layout = QGridLayout(self)

        # 源任务选择（本对话框特有，置于最前）
        layout.addWidget(QLabel("数据源任务:"), 0, 0)
        self.cmb_source = QComboBox()
        self._build_source_combo()
        layout.addWidget(self.cmb_source, 0, 1)

        # 其余公共行从第 1 行开始
        self._add_common_write_rows(layout, 1)

    def _default_name(self) -> str:
        return "计算写入任务"

    def _task_id_prefix(self) -> str:
        return "calcwrite"

    def _validate(self):
        if not self.cmb_source.currentData():
            return "请选择数据源任务"
        return None

    def _extend_task_dict(self, task_dict):
        task_dict["source_task_id"] = self.cmb_source.currentData()

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


# ================================================================
#  第九部分: 主窗口
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

        # 右: 图表与磁贴（选项卡切换）
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(True)

        # 选项卡 1: 统计图
        chart_tab = QWidget()
        chart_tab_layout = QVBoxLayout(chart_tab)
        chart_tab_layout.setContentsMargins(0, 0, 0, 0)
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll_widget = QWidget()
        self.chart_container = QVBoxLayout(self.chart_scroll_widget)
        self.chart_container.setAlignment(Qt.AlignTop)
        self.chart_scroll.setWidget(self.chart_scroll_widget)
        chart_tab_layout.addWidget(self.chart_scroll)

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
        self.tab_widget.addTab(chart_tab, "📊 统计图")

        # 选项卡 2: 磁贴
        self.tile_display = TileDisplayWidget(columns=4)
        self.tab_widget.addTab(self.tile_display, "🔲 磁贴")

        right_layout.addWidget(self.tab_widget)
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
        if conn_type in _SERIAL_CONN_TYPES:
            new_port = str(params.get("port", "")).strip().lower()
            if new_port:
                for cid, info in self._connections.items():
                    if info["type"] in _SERIAL_CONN_TYPES and \
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
                with _block_signals(self.table_conn):
                    self._refresh_conn_table()
                return
            if new_value != conn_id and new_value in self._connections:
                QMessageBox.warning(self, "警告", f"连接ID '{new_value}' 已存在")
                with _block_signals(self.table_conn):
                    self._refresh_conn_table()
                return
            worker_conn = self.worker._connections.pop(conn_id, None)
            if worker_conn:
                self.worker._connections[new_value] = worker_conn
            self._connections[new_value] = self._connections.pop(conn_id)
            with _block_signals(self.table_conn):
                self._refresh_conn_table()
            self.status_bar.showMessage(f"连接ID已修改为: {new_value}", 3000)
        elif col == 1:
            conn_type = new_value.lower()
            if conn_type not in _CONNECTION_TYPES:
                QMessageBox.warning(self, "警告", f"不支持的连接类型: {new_value}")
                with _block_signals(self.table_conn):
                    self._refresh_conn_table()
                return
            self._connections[conn_id]["type"] = conn_type
            with _block_signals(self.table_conn):
                self._refresh_conn_table()
            self.status_bar.showMessage(f"连接类型已修改为: {new_value}", 3000)
        elif col == 2:
            info = self._connections[conn_id]
            worker_conn = self.worker._connections.get(conn_id)
            if info["type"] in _SERIAL_CONN_TYPES:
                info["params"]["port"] = new_value
                if worker_conn:
                    worker_conn["connector"].port = new_value
            else:
                info["params"]["host"] = new_value
                if worker_conn:
                    worker_conn["connector"].host = new_value
            with _block_signals(self.table_conn):
                self._refresh_conn_table()
        elif col == 3:
            info = self._connections[conn_id]
            try:
                val = int(new_value)
                if info["type"] in _SERIAL_CONN_TYPES:
                    info["params"]["baudrate"] = val
                    worker_conn = self.worker._connections.get(conn_id)
                    if worker_conn:
                        worker_conn["connector"].baudrate = val
                else:
                    info["params"]["port"] = val
                    worker_conn = self.worker._connections.get(conn_id)
                    if worker_conn:
                        worker_conn["connector"].port = val
                with _block_signals(self.table_conn):
                    self._refresh_conn_table()
            except ValueError:
                QMessageBox.warning(self, "警告", "端口/波特率必须为整数")
                with _block_signals(self.table_conn):
                    self._refresh_conn_table()

    # ---- 任务管理 ----
    @_safe_event
    def _on_add_task(self):
        if not self._require_connections():
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
        # 同步删除磁贴（计算任务的通道 id 即 channel_prefix）
        self.tile_display.remove_channel(task.channel_prefix)
        self.status_bar.showMessage(f"已删除计算任务: {task.channel_name}", 3000)

    @_safe_event
    def _refresh_calc_table(self):
        with _block_signals(self.table_calc):
            self.table_calc.setRowCount(len(self._calc_tasks))
            for i, task in enumerate(self._calc_tasks):
                self.table_calc.setItem(i, 0, QTableWidgetItem(task.channel_prefix))
                self.table_calc.setItem(i, 1, QTableWidgetItem(task.channel_name))
                self.table_calc.setItem(i, 2, QTableWidgetItem(task.formula))
                self.table_calc.setItem(i, 3, QTableWidgetItem(task.unit))
                self.table_calc.setItem(i, 4, QTableWidgetItem(str(task.scale)))

    @_safe_event
    def _ensure_chart(self, task, chart_id: str):
        """确保某任务的图表与磁贴已创建。采集任务与计算任务共用此逻辑。
        chart_id 区分两者：采集任务用 task_id，计算任务用 channel_prefix。"""
        self._placeholder_label.setVisible(False)
        if chart_id not in self._chart_widgets:
            chart = ChartWidget(title=task.channel_name)
            self.chart_container.addWidget(chart)
            self._chart_widgets[chart_id] = chart
        chart = self._chart_widgets[chart_id]
        for cid, cname in zip(task.get_channel_ids(), task.get_channel_names()):
            if cid not in self._channel_to_chart:
                chart.add_channel(cid, cname)
                self._channel_to_chart[cid] = chart_id
            # 同步创建磁贴（单位取自通道元数据，回退到任务配置的 unit）
            if not self.tile_display.has_channel(cid):
                meta = self.store.get_channel_meta(cid)
                self.tile_display.add_channel(
                    cid, cname, meta.get("unit", getattr(task, "unit", "")))

    def _ensure_chart_for_calc_task(self, task: CalcTask):
        self._ensure_chart(task, task.channel_prefix)

    # ---- 写入任务管理 ----
    def _require_connections(self) -> bool:
        """配置类对话框的前置校验：至少存在一个连接，否则弹窗提示。"""
        if not self._connections:
            QMessageBox.warning(self, "提示", "请先添加至少一个连接")
            return False
        return True

    def _register_write_task(self, task_dict, task_cls, task_list: list,
                             worker_add, refresh_table, label: str):
        """写入类任务（固定值/计算值）添加后处理共用流程：
        实例化→入列→注册到 worker→刷新表格→状态栏提示→确保写线程运行。"""
        task = task_cls.from_dict(task_dict)
        task_list.append(task)
        worker_add(task)
        refresh_table()
        self.status_bar.showMessage(f"已添加{label}: {task.name}", 3000)
        # 若采集已在运行，确保写入线程已启动
        self.worker.ensure_write_thread()

    def _delete_selected_write_task(self, table, task_list: list,
                                    worker_remove, refresh_table, label: str):
        """从指定写入任务表删除选中行对应的任务（固定值写入/计算值写入共用）。"""
        row = table.currentRow()
        if row < 0 or row >= len(task_list):
            QMessageBox.warning(self, "提示",
                                f"请先在{label}表中选择要删除的任务")
            return
        task = task_list.pop(row)
        worker_remove(task.task_id)
        refresh_table()
        self.status_bar.showMessage(f"已删除{label}: {task.name}", 3000)

    @_safe_event
    def _on_add_write_task(self):
        if not self._require_connections():
            return
        self._write_task_dialog = WriteTaskConfigDialog(self._connections)
        self._write_task_dialog.task_submitted.connect(self._add_write_task)
        self._write_task_dialog.show()

    @_safe_event
    def _add_write_task(self, task_dict):
        self._register_write_task(
            task_dict, WriteTask, self._write_tasks,
            self.worker.add_write_task, self._refresh_write_table, "写入任务")

    @_safe_event
    def _on_del_write_task(self):
        self._delete_selected_write_task(
            self.table_write, self._write_tasks,
            self.worker.remove_write_task, self._refresh_write_table, "写入任务")

    @staticmethod
    def _set_table_row_status(table, row: int, success: bool, status_text: str):
        """更新写入任务表第 7 列（状态）的文本与颜色（成功绿/失败红）"""
        item = table.item(row, 7)
        if item is None:
            item = QTableWidgetItem(status_text)
            table.setItem(row, 7, item)
        else:
            item.setText(status_text)
        item.setForeground(QColor("#a6e3a1") if success else QColor("#f38ba8"))

    @_safe_event
    def _on_write_status(self, task_id, success, message):
        # 计算写入任务：失败时状态列显示具体原因
        for row, task in enumerate(self._calc_write_tasks):
            if task.task_id == task_id:
                status_text = "✓ 成功" if success else f"✗ {message}"
                self._set_table_row_status(
                    self.table_calc_write, row, success, status_text)
                break
        else:
            # 固定值写入任务：失败时状态列仅显示"✗ 失败"
            for row, task in enumerate(self._write_tasks):
                if task.task_id == task_id:
                    status_text = "✓ 成功" if success else "✗ 失败"
                    self._set_table_row_status(
                        self.table_write, row, success, status_text)
                    break
        if not success:
            self.status_bar.showMessage(f"写入任务失败 [{task_id}]: {message}", 5000)

    @_safe_event
    def _refresh_write_table(self):
        with _block_signals(self.table_write):
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

    # ---- 计算写入任务管理 ----
    @_safe_event
    def _on_add_calc_write_task(self):
        if not self._require_connections():
            return
        if not self._calc_tasks and not self._tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务或计算任务 (计算写入任务需要引用数据源)")
            return
        self._calc_write_task_dialog = CalcWriteTaskConfigDialog(
            self._connections, self._calc_tasks, self._tasks)
        self._calc_write_task_dialog.task_submitted.connect(self._add_calc_write_task)
        self._calc_write_task_dialog.show()

    @_safe_event
    def _add_calc_write_task(self, task_dict):
        self._register_write_task(
            task_dict, CalcWriteTask, self._calc_write_tasks,
            self.worker.add_calc_write_task, self._refresh_calc_write_table,
            "计算写入任务")

    @_safe_event
    def _on_del_calc_write_task(self):
        self._delete_selected_write_task(
            self.table_calc_write, self._calc_write_tasks,
            self.worker.remove_calc_write_task,
            self._refresh_calc_write_table, "计算写入任务")

    @_safe_event
    def _refresh_calc_write_table(self):
        with _block_signals(self.table_calc_write):
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

    @_safe_event
    def _on_task_cell_changed(self, row, col):
        if row < 0 or row >= len(self._tasks):
            return
        task = self._tasks[row]
        new_value = self.table_task.item(row, col).text().strip()
        
        if col == 0:
            if new_value not in self._connections:
                QMessageBox.warning(self, "警告", f"连接ID '{new_value}' 不存在")
                with _block_signals(self.table_task):
                    self._refresh_task_table()
                return
            task.connection_id = new_value
            task.connection_type = self._connections[new_value]["type"]
            with _block_signals(self.table_task):
                self._refresh_task_table()
            self.status_bar.showMessage(f"任务连接已修改为: {new_value}", 3000)
        elif col == 1:
            conn_type = new_value.lower()
            if conn_type not in _CONNECTION_TYPES:
                QMessageBox.warning(self, "警告", f"不支持的连接类型: {new_value}")
                with _block_signals(self.table_task):
                    self._refresh_task_table()
                return
            task.connection_type = conn_type
            with _block_signals(self.table_task):
                self._refresh_task_table()
        elif col == 2:
            task.device_type = new_value
            with _block_signals(self.table_task):
                self._refresh_task_table()
        elif col == 3:
            try:
                task.start_addr = int(new_value)
                with _block_signals(self.table_task):
                    self._refresh_task_table()
            except ValueError:
                QMessageBox.warning(self, "警告", "起始地址必须为整数")
                with _block_signals(self.table_task):
                    self._refresh_task_table()
        elif col == 4:
            try:
                qty = int(new_value)
                if qty < 1 or qty > 125:
                    QMessageBox.warning(self, "警告", "读取数量必须在 1-125 之间")
                    with _block_signals(self.table_task):
                        self._refresh_task_table()
                    return
                task.quantity = qty
                with _block_signals(self.table_task):
                    self._refresh_task_table()
            except ValueError:
                QMessageBox.warning(self, "警告", "读取数量必须为整数")
                with _block_signals(self.table_task):
                    self._refresh_task_table()
        elif col == 5:
            QMessageBox.warning(self, "提示", "通道前缀不可编辑，请删除任务后重新添加")
            with _block_signals(self.table_task):
                self._refresh_task_table()
        elif col == 6:
            if not new_value:
                QMessageBox.warning(self, "警告", "通道名称不能为空")
                with _block_signals(self.table_task):
                    self._refresh_task_table()
                return
            old_name = task.channel_name
            task.channel_name = new_value
            with _block_signals(self.table_task):
                self._refresh_task_table()
            self._update_chart_for_task(task)
            self.status_bar.showMessage(f"通道名称已修改: {old_name} -> {new_value}", 3000)
        elif col == 7:
            task.unit = new_value
            with _block_signals(self.table_task):
                self._refresh_task_table()

    @_safe_event
    def _update_chart_for_task(self, task):
        chart_id = task.task_id
        if chart_id in self._chart_widgets:
            chart = self._chart_widgets[chart_id]
            chart.setTitle(task.channel_name)

    @_safe_event
    def _ensure_chart_for_task(self, task):
        self._ensure_chart(task, task.task_id)

    # ---- 采集控制 ----
    def _set_running_state(self, running: bool):
        """切换启动/停止状态下的按钮可用性：运行中禁用所有配置类按钮"""
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        for btn in (self.btn_add_conn, self.btn_add_task, self.btn_add_calc_task,
                    self.btn_add_write_task, self.btn_add_calc_write_task,
                    self.btn_del_write_task, self.btn_del_calc_write_task,
                    self.btn_del_calc_task):
            btn.setEnabled(not running)

    @_safe_event
    def _on_start(self):
        if not self._tasks and not self._write_tasks and not self._calc_tasks and not self._calc_write_tasks:
            QMessageBox.warning(self, "提示", "请先添加至少一个采集任务、写入任务、计算任务或计算写入任务")
            return
        self.worker.set_poll_interval(self.spin_interval.value())
        self.worker.start()
        self._set_running_state(True)
        self.status_bar.showMessage("采集已启动", 3000)

    @_safe_event
    def _on_stop(self):
        self.worker.stop()
        self._set_running_state(False)
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
            # 同步清空磁贴显示值（保留卡片本身，与图表清空曲线一致）
            self.tile_display.reset_values()
            self._refresh_status()
            self.status_bar.showMessage("数据已清空", 3000)

    # ---- 定时刷新 ----
    @_safe_event
    def _refresh_charts(self):
        # 刷新统计图折线
        for chart_id, chart in self._chart_widgets.items():
            for ch_id, cid in list(self._channel_to_chart.items()):
                if cid == chart_id:
                    ts, vals = self.store.get_channel_data(ch_id)
                    if ts:
                        chart.update_channel(ch_id, ts, vals)
        # 同步刷新磁贴最新值（遍历所有已注册通道，含计算通道）
        for ch_id in self.store.get_all_channel_ids():
            self.tile_display.update_channel(ch_id,
                                            self.store.get_latest_value(ch_id))

    @_safe_event
    def _refresh_status(self):
        self.lbl_record_count.setText(f"记录数: {self.store.get_record_count()}")
        active = sum(1 for c in self.worker._connections.values()
                     if c["connector"].is_connected())
        self.lbl_active_conn.setText(
            f"活跃连接: {active}/{len(self._connections)}")

    @_safe_event
    def _refresh_conn_table(self):
        with _block_signals(self.table_conn):
            self.table_conn.setRowCount(len(self._connections))
            for i, (cid, info) in enumerate(self._connections.items()):
                p = info["params"]
                self.table_conn.setItem(i, 0, QTableWidgetItem(cid))
                conn_type = info["type"]
                self.table_conn.setItem(i, 1, QTableWidgetItem(conn_type))

                if conn_type in _SERIAL_CONN_TYPES:
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

    @_safe_event
    def _refresh_task_table(self):
        with _block_signals(self.table_task):
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
                    yaml.dump(
                        config, f, Dumper=_IndentDumper,
                        allow_unicode=True, default_flow_style=False,
                        indent=2, sort_keys=False,
                    )
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
        # 同步清空磁贴，加载新配置后由 _add_task 重新创建
        self.tile_display.clear_all()
        self._placeholder_label.setVisible(True)

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
        self._at_line_start = True  # 跟踪日志文件当前是否处于行首，用于按行添加时间戳

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
                # 写入日志文件时为每行添加时间戳前缀（空行不加）
                for line in text.splitlines(keepends=True):
                    if self._at_line_start and line.strip():
                        self._log_file.write(
                            datetime.now().strftime("[%Y-%m-%d %H:%M:%S] "))
                    self._log_file.write(line)
                    self._at_line_start = line.endswith("\n")
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
