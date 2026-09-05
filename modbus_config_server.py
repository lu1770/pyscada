#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ModbusTCP 服务端示例
- 监听端口 502
- 从站(Slave) ID = 1
- 客户端读取 Holding Register 时，返回随机数
依赖: pip install pymodbus
"""

import logging
import random

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# 寄存器数量与随机数范围，可按需修改
REGISTER_COUNT = 100
RANDOM_MIN = 0
RANDOM_MAX = 65535  # 16位寄存器最大值


class RandomHoldingRegisterBlock(ModbusSequentialDataBlock):
    """
    自定义数据块：
    每次被读取(getValues)时，先用随机数填充所有寄存器，
    再返回请求的那部分数据，从而实现“读取即随机”的效果。
    """

    def getValues(self, address, count=1):
        # 每次读取前刷新为随机值
        random_values = [
            random.randint(RANDOM_MIN, RANDOM_MAX) for _ in range(count)
        ]
        # 同步写回内部存储（可选，保证 setValues/日志一致）
        super().setValues(address, random_values)
        return random_values


def run_server():
    # 初始化持有寄存器数据块，起始地址0，初始值全为0
    hr_block = RandomHoldingRegisterBlock(0, [0] * REGISTER_COUNT)

    # 从站上下文：这里只演示 Holding Register (hr)
    # di=离散输入, co=线圈, ir=输入寄存器 也可按需添加
    slave_context = ModbusSlaveContext(
        hr=hr_block,
    )

    # 从站 ID = 1
    context = ModbusServerContext(slaves={1: slave_context}, single=False)

    log.info("Modbus TCP 服务启动，监听端口 502，从站ID=1 ...")
    StartTcpServer(
        context=context,
        address=("0.0.0.0", 502),
    )


if __name__ == "__main__":
    run_server()