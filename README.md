# Multi-Channel Industrial Data Acquisition System

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![PySide6](https://img.shields.io/badge/PySide6-6.11.1-green.svg)](https://pypi.org/project/PySide6/)
[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)

## 项目简介

多通道工业数据采集系统是一个基于Python的跨平台工业数据采集软件，支持多种工业通信协议，提供实时数据可视化和数据导出功能。

## 功能特性

### 通信协议支持
- **Modbus TCP**：原生socket实现，无需pymodbus依赖
- **Modbus RTU**：基于pyserial的串口通信，支持多种串口参数配置
- **Keyence PLC**：支持基恩士PLC上位链接协议（KV-5500/7500/8000/Nano等型号）

### 数据类型与字节序
- 支持8种数据类型：`uint16`, `int16`, `uint32`, `int32`, `float32`, `uint64`, `int64`, `float64`
- 支持4种字节序：
  - `abcd` - 大端序（Motorola）
  - `dcba` - 小端序（Intel）
  - `badc` - 双字节交换
  - `cdab` - 四字交换

### 数据采集
- 多通道并发采集，支持自定义轮询间隔
- 线程安全数据存储，支持实时折线图显示
- 数据缩放与偏移处理，支持工程单位转换
- 全量数据CSV导出

### 配置管理
- YAML格式配置文件，支持配置持久化
- 可视化连接配置对话框
- 可视化采集任务配置对话框
- 配置文件导入/导出

### 可视化
- 基于pyqtgraph的实时折线图
- 深色主题，多通道颜色区分
- 实时状态监控
- 连接状态自动重连

## 系统要求

| 组件 | 版本要求 |
|------|----------|
| Python | 3.8+ |
| PySide6 | 6.11.1 |
| pyqtgraph | 0.14.0 |
| PyYAML | 6.0.3 |
| pyserial | 3.5 (仅Modbus RTU需要) |

## 安装步骤

```bash
# 克隆仓库
git clone https://github.com/yourusername/pyscada.git
cd pyscada

# 安装依赖
pip install -r requirements.txt

# 运行程序
python main.py
```

## 配置说明

系统使用YAML格式的配置文件 `daq_config.yml`，包含连接配置和采集任务配置两部分。

### 配置文件结构

```yaml
connections:
  connection_id:
    type: modbus_tcp | modbus_rtu | keyence
    params:
      # Modbus TCP
      host: 192.168.1.100
      port: 502
      slave_id: 1
      
      # Modbus RTU
      port: COM1
      baudrate: 9600
      parity: N
      stopbits: 1
      bytesize: 8
      slave_id: 1
      
      # Keyence PLC
      host: 192.168.1.101
      port: 3000
      unit: 0

poll_interval: 0.5

tasks:
  - task_id: task_1
    connection_id: connection_id
    connection_type: modbus_tcp | modbus_rtu | keyence
    device_type: holding | input | coil | DM | MR | LR
    start_addr: 0
    quantity: 1
    channel_prefix: temp
    channel_name: 温度
    unit: °C
    scale: 1.0
    offset: 0.0
    data_type: uint16
    byte_order: abcd
```

### 示例配置

项目包含一个完整的示例配置 `daq_config.yml`，展示了如何连接Delta DTE-10T温度控制器并采集32个通道的数据。

## 使用说明

1. **添加连接**：点击"添加连接"按钮，选择连接类型并配置参数
2. **添加采集任务**：点击"添加采集任务"按钮，选择所属连接并配置采集参数
3. **开始采集**：点击"开始采集"按钮，系统将开始轮询所有配置的通道
4. **停止采集**：点击"停止采集"按钮，系统将停止轮询并断开所有连接
5. **导出数据**：点击"导出CSV"按钮，将当前所有采集数据导出为CSV文件
6. **保存配置**：点击"保存配置"按钮，将当前配置保存到`daq_config.yml`
7. **加载配置**：点击"加载配置"按钮，从`daq_config.yml`加载配置

## 构建打包

项目提供了 `build.py` 脚本用于打包为独立可执行文件：

```bash
python build.py
```

打包完成后，可执行文件位于 `dist/DAQ_System.exe`（Windows）。

## 支持的数据类型

| 数据类型 | 寄存器数量 | 说明 |
|----------|------------|------|
| uint16 | 1 | 无符号16位整数 |
| int16 | 1 | 有符号16位整数 |
| uint32 | 2 | 无符号32位整数 |
| int32 | 2 | 有符号32位整数 |
| float32 | 2 | 32位浮点数 |
| uint64 | 4 | 无符号64位整数 |
| int64 | 4 | 有符号64位整数 |
| float64 | 4 | 64位浮点数 |

## 项目结构

```
pyscada/
├── main.py              # 主程序入口
├── build.py             # PyInstaller打包脚本
├── daq_config.yml       # 配置文件
├── daq_config.yml.template # 配置文件模板
├── requirements.txt     # 依赖列表
├── .gitignore           # Git忽略文件
└── README.md            # 项目说明文档
```

## 许可证

GNU General Public License v3 (GPL-3.0) - 详见 [LICENSE](LICENSE) 文件

---

# Multi-Channel Industrial Data Acquisition System

## Introduction

Multi-Channel Industrial Data Acquisition System is a Python-based cross-platform industrial data acquisition software that supports multiple industrial communication protocols and provides real-time data visualization and export functionality.

## Features

### Communication Protocols
- **Modbus TCP**: Native socket implementation, no pymodbus dependency
- **Modbus RTU**: Serial communication based on pyserial, supports multiple serial parameters
- **Keyence PLC**: Supports Keyence PLC upper-link protocol (KV-5500/7500/8000/Nano series)

### Data Types & Byte Orders
- 8 data types: `uint16`, `int16`, `uint32`, `int32`, `float32`, `uint64`, `int64`, `float64`
- 4 byte orders: `abcd`, `dcba`, `badc`, `cdab`

### Data Acquisition
- Multi-channel concurrent acquisition with configurable polling interval
- Thread-safe data storage with real-time line chart display
- Data scaling and offset processing for engineering unit conversion
- Full data CSV export

### Configuration Management
- YAML format configuration file with persistence support
- Visual connection configuration dialog
- Visual acquisition task configuration dialog
- Configuration import/export

### Visualization
- Real-time line charts based on pyqtgraph
- Dark theme with multi-channel color differentiation
- Real-time status monitoring
- Automatic connection reconnection

## System Requirements

| Component | Version |
|-----------|---------|
| Python | 3.8+ |
| PySide6 | 6.11.1 |
| pyqtgraph | 0.14.0 |
| PyYAML | 6.0.3 |
| pyserial | 3.5 (for Modbus RTU only) |

## Installation

```bash
git clone https://github.com/yourusername/pyscada.git
cd pyscada
pip install -r requirements.txt
python main.py
```

## Configuration

The system uses a YAML configuration file `daq_config.yml` containing connection and task configurations. See the example configuration for details.

## Usage

1. Add Connection → Configure parameters
2. Add Acquisition Task → Select connection and configure parameters
3. Start Acquisition → Begin polling all configured channels
4. Stop Acquisition → Stop polling and disconnect
5. Export CSV → Export all acquired data
6. Save Configuration → Save to `daq_config.yml`
7. Load Configuration → Load from `daq_config.yml`

## Building

```bash
python build.py
```

Output: `dist/DAQ_System.exe`

## License

GNU General Public License v3 (GPL-3.0)