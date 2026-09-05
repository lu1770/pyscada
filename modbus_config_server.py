#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ModbusTCP 服务端（配置文件驱动版）
- 启动时从 config.yml 加载配置：IP、端口、从站ID
- 每个 Holding Register 区段可配置为三种模式之一：
    random    随机数（在 min~max 范围内每次读取都随机）
    fixed     固定值（每次读取都返回同一个值）
    increment 自增长（每次读取值+step，到达 max 后按 wrap 配置回绕或停止）
依赖:
    pip install pymodbus==3.8.6 ruamel.yaml
""" 

import argparse
import logging
import random
import threading

from ruamel.yaml import YAML

_yaml = YAML(typ="safe", pure=True)
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 各种寄存器取值策略
# ---------------------------------------------------------------------------

class BaseRegister:
    """单个寄存器地址的取值策略基类。"""

    def read(self) -> int:
        raise NotImplementedError


class FixedRegister(BaseRegister):
    """固定值：每次读取都返回同一个数。"""

    def __init__(self, value: int):
        self.value = int(value)

    def read(self) -> int:
        return self.value


class RandomRegister(BaseRegister):
    """随机数：在 [min, max] 范围内均匀随机。"""

    def __init__(self, min_value: int, max_value: int):
        self.min_value = int(min_value)
        self.max_value = int(max_value)

    def read(self) -> int:
        return random.randint(self.min_value, self.max_value)


class IncrementRegister(BaseRegister):
    """自增长：每次读取后按 step 递增，到达边界后按 wrap 配置回绕或停止在边界。"""

    def __init__(self, start: int, step: int, min_value: int, max_value: int, wrap: bool = True):
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


REGISTER_TYPE_MAP = {
    "random": lambda cfg: RandomRegister(
        min_value=cfg.get("min", 0),
        max_value=cfg.get("max", 65535),
    ),
    "fixed": lambda cfg: FixedRegister(
        value=cfg.get("value", 0),
    ),
    "increment": lambda cfg: IncrementRegister(
        start=cfg.get("start", 0),
        step=cfg.get("step", 1),
        min_value=cfg.get("min", 0),
        max_value=cfg.get("max", 65535),
        wrap=cfg.get("wrap", True),
    ),
}


# ---------------------------------------------------------------------------
# 数据块：按地址分派给对应的取值策略
# ---------------------------------------------------------------------------

class ConfigurableHoldingRegisterBlock(ModbusSequentialDataBlock):
    """
    每次被读取(getValues)时，按地址查找预先配置好的取值策略并生成数据。
    未在配置中出现的地址，默认返回 0。
    """

    def __init__(self, size: int, handlers: dict):
        # 起始地址设为1，是为了抵消 pymodbus ModbusSlaveContext 内部固定的
        # "address += 1" 偏移（这样配置文件里 address: 0 对应主站看到的地址0）
        super().__init__(1, [0] * size)
        self.handlers = handlers

    def getValues(self, address, count=1):
        values = []
        for offset in range(count):
            # address 是经过 ModbusSlaveContext 加过1的内部地址，这里换算回
            # 配置文件/主站视角的 0基地址
            client_addr = address - 1 + offset
            handler = self.handlers.get(client_addr)
            values.append(handler.read() if handler else 0)
        # 同步写回内部列表，便于其他工具查看/日志一致（不影响下次读取时重新生成）
        super().setValues(address, values)
        return values


# ---------------------------------------------------------------------------
# 配置加载 与 服务启动
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return _yaml.load(f)


def build_handlers(config: dict):
    """根据配置构建 {地址: 取值策略实例} 字典，并返回所需的最小寄存器数量。"""
    handlers = {}
    max_addr = -1

    for reg in config.get("registers", []):
        addr = reg["address"]
        count = reg.get("count", 1)
        rtype = reg.get("type")

        factory = REGISTER_TYPE_MAP.get(rtype)
        if factory is None:
            raise ValueError(f"未知的寄存器类型: {rtype!r}（可选: random / fixed / increment）")

        for i in range(count):
            a = addr + i
            handlers[a] = factory(reg)
            max_addr = max(max_addr, a)

    configured_size = config.get("datastore", {}).get("size", 0)
    size = max(max_addr + 1, configured_size, 1)
    return handlers, size


def run_server(config_path: str = "config.yml"):
    config = load_config(config_path)

    server_cfg = config.get("server", {})
    ip = server_cfg.get("ip", "0.0.0.0")
    port = server_cfg.get("port", 502)
    slave_id = server_cfg.get("slave_id", 1)

    handlers, size = build_handlers(config)

    hr_block = ConfigurableHoldingRegisterBlock(size, handlers)
    slave_context = ModbusSlaveContext(hr=hr_block)
    context = ModbusServerContext(slaves={slave_id: slave_context}, single=False)

    log.info(
        "ModbusTCP 服务启动: %s:%s, 从站ID=%s, Holding Register 数量=%s",
        ip, port, slave_id, size,
    )
    StartTcpServer(context=context, address=(ip, port))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ModbusTCP 服务端（配置文件驱动）")
    parser.add_argument(
        "-c", "--config",
        default="config.yml",
        help="YAML 配置文件路径（默认: config.yml）",
    )
    args = parser.parse_args()
    run_server(args.config)