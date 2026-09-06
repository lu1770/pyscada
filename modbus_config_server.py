#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ModbusTCP 服务端（全功能码 + 配置文件驱动版）

支持的功能码：
    01 Read Coils                      读线圈
    02 Read Discrete Inputs            读离散输入
    03 Read Holding Registers          读保持寄存器
    04 Read Input Registers            读输入寄存器
    05 Write Single Coil               写单个线圈
    06 Write Single Register           写单个保持寄存器
    0F(15) Write Multiple Coils        写多个线圈
    10(16) Write Multiple Registers    写多个保持寄存器
    16(22) Mask Write Register         掩码写寄存器
    17(23) Read/Write Multiple Regs    读写多个寄存器
    2B/0E(43/14) Read Device Identification  读设备标识

    07 Read Exception Status / 08 Diagnostics / 0B/0C Comm Event /
    11 Report Slave ID / 20/21 File Record / 24 FIFO Queue
    这些由 pymodbus 协议栈自动处理（无需数据存储支持），无需额外开发。

每个数据区（coils / discrete_inputs / input_registers / holding_registers）
里的每一段地址都可以配置为以下模式之一：
    random    随机数 / 随机bool
    fixed     固定值
    increment 自增长（仅寄存器，到达max后按wrap回绕或停止）
    toggle    翻转（仅线圈/离散输入，每次读取 True/False 交替）
    static    可写（行为等同真实寄存器/线圈：写入什么，读出什么，重启后恢复初始值）

未在配置中出现的地址，自动表现为普通可读写寄存器/线圈（初始值0/False）。

依赖:
    pip install pymodbus==3.8.6 pyyaml
"""

import argparse
import logging
import random
import threading

import yaml
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DEFAULT_AREA_SIZE = 100  # 未指定 size 时，每个数据区默认分配的地址空间大小


# ---------------------------------------------------------------------------
# 寄存器 (16位字) 取值策略 —— 用于 holding_registers / input_registers
# ---------------------------------------------------------------------------

class BaseRegister:
    def read(self) -> int:
        raise NotImplementedError

    def write(self, value: int) -> None:
        """默认忽略写入（random/fixed/increment 由内部逻辑驱动，不接受外部写）。"""


class FixedRegister(BaseRegister):
    def __init__(self, value: int = 0):
        self.value = int(value)

    def read(self) -> int:
        return self.value


class RandomRegister(BaseRegister):
    def __init__(self, min_value: int = 0, max_value: int = 65535):
        self.min_value = int(min_value)
        self.max_value = int(max_value)

    def read(self) -> int:
        return random.randint(self.min_value, self.max_value)


class IncrementRegister(BaseRegister):
    def __init__(self, start: int = 0, step: int = 1, min_value: int = 0,
                 max_value: int = 65535, wrap: bool = True):
        self.step = int(step)
        self.min_value = int(min_value)
        self.max_value = int(max_value)
        self.wrap = bool(wrap)
        self._current = int(start)
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            value = self._current
            nxt = self._current + self.step
            if nxt > self.max_value:
                nxt = self.min_value if self.wrap else self.max_value
            elif nxt < self.min_value:
                nxt = self.max_value if self.wrap else self.min_value
            self._current = nxt
            return value


class StaticRegister(BaseRegister):
    """可写寄存器：行为等同真实硬件寄存器，写入什么下次就读到什么。"""

    def __init__(self, value: int = 0):
        self.value = int(value)
        self._lock = threading.Lock()

    def read(self) -> int:
        with self._lock:
            return self.value

    def write(self, value: int) -> None:
        with self._lock:
            self.value = int(value)


REGISTER_TYPE_MAP = {
    "random": lambda cfg: RandomRegister(cfg.get("min", 0), cfg.get("max", 65535)),
    "fixed": lambda cfg: FixedRegister(cfg.get("value", 0)),
    "increment": lambda cfg: IncrementRegister(
        start=cfg.get("start", 0), step=cfg.get("step", 1),
        min_value=cfg.get("min", 0), max_value=cfg.get("max", 65535),
        wrap=cfg.get("wrap", True),
    ),
    "static": lambda cfg: StaticRegister(cfg.get("value", 0)),
}


# ---------------------------------------------------------------------------
# 线圈/离散输入 (1位bool) 取值策略 —— 用于 coils / discrete_inputs
# ---------------------------------------------------------------------------

class BaseBit:
    def read(self) -> bool:
        raise NotImplementedError

    def write(self, value: bool) -> None:
        """默认忽略写入。"""


class FixedBit(BaseBit):
    def __init__(self, value: bool = False):
        self.value = bool(value)

    def read(self) -> bool:
        return self.value


class RandomBit(BaseBit):
    def read(self) -> bool:
        return random.choice([True, False])


class ToggleBit(BaseBit):
    def __init__(self, start: bool = False):
        self.value = bool(start)
        self._lock = threading.Lock()

    def read(self) -> bool:
        with self._lock:
            value = self.value
            self.value = not self.value
            return value


class StaticBit(BaseBit):
    """可写线圈：行为等同真实线圈。"""

    def __init__(self, value: bool = False):
        self.value = bool(value)
        self._lock = threading.Lock()

    def read(self) -> bool:
        with self._lock:
            return self.value

    def write(self, value: bool) -> None:
        with self._lock:
            self.value = bool(value)


BIT_TYPE_MAP = {
    "random": lambda cfg: RandomBit(),
    "fixed": lambda cfg: FixedBit(cfg.get("value", False)),
    "toggle": lambda cfg: ToggleBit(cfg.get("start", False)),
    "static": lambda cfg: StaticBit(cfg.get("value", False)),
}


# ---------------------------------------------------------------------------
# 数据块：按地址分派给对应的取值策略；未配置的地址走普通读写兜底逻辑
# ---------------------------------------------------------------------------
#
# 说明：pymodbus 的 ModbusSlaveContext 在调用 datablock 之前会固定把地址 +1
# （对应经典 Modbus 协议里 40001 这类地址与协议报文里 0 基地址的转换关系）。
# 这里把数据块自身的基地址设为 1，再在读写时换算回 0 基地址，
# 这样配置文件里写 address: 0 就正好对应主站请求的地址 0。

class ConfigurableRegisterBlock(ModbusSequentialDataBlock):
    """通用 16位寄存器数据块，供 holding_registers / input_registers 复用。"""

    def __init__(self, size: int, handlers: dict):
        super().__init__(1, [0] * size)
        self.handlers = handlers

    def getValues(self, address, count=1):
        values = []
        for offset in range(count):
            client_addr = address - 1 + offset
            handler = self.handlers.get(client_addr)
            if handler:
                values.append(handler.read())
            else:
                idx = client_addr
                values.append(self.values[idx] if 0 <= idx < len(self.values) else 0)
        super().setValues(address, values)
        return values

    def setValues(self, address, values):
        if not isinstance(values, list):
            values = [values]
        for offset, v in enumerate(values):
            client_addr = address - 1 + offset
            handler = self.handlers.get(client_addr)
            if handler:
                handler.write(v)
        super().setValues(address, values)


class ConfigurableBitBlock(ModbusSequentialDataBlock):
    """通用 1位(bool)数据块，供 coils / discrete_inputs 复用。"""

    def __init__(self, size: int, handlers: dict):
        super().__init__(1, [False] * size)
        self.handlers = handlers

    def getValues(self, address, count=1):
        values = []
        for offset in range(count):
            client_addr = address - 1 + offset
            handler = self.handlers.get(client_addr)
            if handler:
                values.append(handler.read())
            else:
                idx = client_addr
                values.append(bool(self.values[idx]) if 0 <= idx < len(self.values) else False)
        super().setValues(address, values)
        return values

    def setValues(self, address, values):
        if not isinstance(values, list):
            values = [values]
        for offset, v in enumerate(values):
            client_addr = address - 1 + offset
            handler = self.handlers.get(client_addr)
            if handler:
                handler.write(bool(v))
        super().setValues(address, values)


# ---------------------------------------------------------------------------
# 配置加载与解析
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_word_handlers(entries: list):
    handlers, max_addr = {}, -1
    for reg in entries:
        addr, count, rtype = reg["address"], reg.get("count", 1), reg.get("type")
        factory = REGISTER_TYPE_MAP.get(rtype)
        if factory is None:
            raise ValueError(f"未知的寄存器类型: {rtype!r}（可选: random/fixed/increment/static）")
        for i in range(count):
            a = addr + i
            handlers[a] = factory(reg)
            max_addr = max(max_addr, a)
    return handlers, max_addr


def build_bit_handlers(entries: list):
    handlers, max_addr = {}, -1
    for reg in entries:
        addr, count, rtype = reg["address"], reg.get("count", 1), reg.get("type")
        factory = BIT_TYPE_MAP.get(rtype)
        if factory is None:
            raise ValueError(f"未知的线圈/离散输入类型: {rtype!r}（可选: random/fixed/toggle/static）")
        for i in range(count):
            a = addr + i
            handlers[a] = factory(reg)
            max_addr = max(max_addr, a)
    return handlers, max_addr


def build_context(config: dict) -> ModbusServerContext:
    areas = config.get("areas", {})
    ds_cfg = config.get("datastore", {})

    co_handlers, co_max = build_bit_handlers(areas.get("coils", []))
    di_handlers, di_max = build_bit_handlers(areas.get("discrete_inputs", []))
    ir_handlers, ir_max = build_word_handlers(areas.get("input_registers", []))
    hr_handlers, hr_max = build_word_handlers(areas.get("holding_registers", []))

    co_size = max(co_max + 1, ds_cfg.get("coils_size", 0), DEFAULT_AREA_SIZE)
    di_size = max(di_max + 1, ds_cfg.get("discrete_inputs_size", 0), DEFAULT_AREA_SIZE)
    ir_size = max(ir_max + 1, ds_cfg.get("input_registers_size", 0), DEFAULT_AREA_SIZE)
    hr_size = max(hr_max + 1, ds_cfg.get("holding_registers_size", 0), DEFAULT_AREA_SIZE)

    slave_context = ModbusSlaveContext(
        co=ConfigurableBitBlock(co_size, co_handlers),
        di=ConfigurableBitBlock(di_size, di_handlers),
        ir=ConfigurableRegisterBlock(ir_size, ir_handlers),
        hr=ConfigurableRegisterBlock(hr_size, hr_handlers),
    )

    slave_id = config.get("server", {}).get("slave_id", 1)
    return ModbusServerContext(slaves={slave_id: slave_context}, single=False)


def build_identity(config: dict) -> ModbusDeviceIdentification:
    id_cfg = config.get("server", {}).get("identity", {})
    return ModbusDeviceIdentification(info_name={
        "VendorName": id_cfg.get("vendor_name", "PyModbusSim"),
        "ProductCode": id_cfg.get("product_code", "PMS"),
        "VendorUrl": id_cfg.get("vendor_url", "https://github.com/pymodbus-dev/pymodbus"),
        "ProductName": id_cfg.get("product_name", "Modbus TCP Simulator"),
        "ModelName": id_cfg.get("model_name", "Simulator"),
        "MajorMinorRevision": id_cfg.get("major_minor_revision", "1.0"),
    })


def run_server(config_path: str = "config.yml"):
    config = load_config(config_path)
    server_cfg = config.get("server", {})
    ip = server_cfg.get("ip", "0.0.0.0")
    port = server_cfg.get("port", 502)
    slave_id = server_cfg.get("slave_id", 1)

    context = build_context(config)
    identity = build_identity(config)

    log.info("ModbusTCP 服务启动: %s:%s, 从站ID=%s", ip, port, slave_id)
    StartTcpServer(context=context, identity=identity, address=(ip, port))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ModbusTCP 服务端（全功能码 + 配置文件驱动）")
    parser.add_argument("-c", "--config", default="config.yml", help="YAML 配置文件路径")
    args = parser.parse_args()
    run_server(args.config)