# Multi-Channel Industrial Data Acquisition System

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![PySide6](https://img.shields.io/badge/PySide6-6.11.1-green.svg)](https://pypi.org/project/PySide6/)
[![Build](https://img.shields.io/badge/build-passing-brightgreen.svg)](.github/workflows/build.yml)
[![License](https://img.shields.io/badge/license-GPL%20v3-blue.svg)](LICENSE)

## 项目简介

多通道工业数据采集系统是一个基于Python的跨平台工业数据采集软件，支持多种工业通信协议，提供实时数据可视化和数据导出功能。除数据采集外，还支持周期性写入、公式计算通道、磁贴显示以及离线数据分析等能力。

## 功能特性

### 通信协议支持
- **Modbus TCP**：原生socket实现，无需pymodbus依赖
- **Modbus RTU**：基于pyserial的串口通信，支持多种串口参数配置
- **Modbus ASCII**：基于pyserial的串口通信，ASCII编码模式（数据位通常为7）
- **Keyence PLC**：支持基恩士PLC上位链接协议（KV-5500/7500/8000/Nano等型号）

### 寄存器/设备类型
- **Modbus**：`holding`（保持寄存器）、`input`（输入寄存器）、`coil`（线圈）、`discrete_input`（离散输入）
- **Keyence**：`DM`（数据寄存器）、`MR`（内部继电器）、`LR`（链接寄存器）、`TIM`（定时器）、`CNT`（计数器）、`VR`（变址寄存器）

### 数据类型与字节序
- 支持8种数据类型：`uint16`, `int16`, `uint32`, `int32`, `float32`, `uint64`, `int64`, `float64`
- 支持4种字节序：
  - `abcd` - 大端序（Motorola）
  - `dcba` - 小端序（Intel）
  - `badc` - 双字节交换
  - `cdab` - 四字交换

### 任务类型
系统支持四类任务，均在 YAML 配置文件中声明：

- **采集任务 (`tasks`)**：周期性轮询读取寄存器值，支持多通道并发、自定义轮询间隔、数据缩放与偏移。
- **写入任务 (`write_tasks`)**：按 `write_interval` 周期向设备写入固定值，用于设定温度、控制寄存器等场景。
- **计算任务 (`calc_tasks`)**：基于公式实时计算通道值，结果作为新通道显示并存储。公式可引用采集任务的 `task_id`/`channel_prefix`，支持运算符 `+ - * / % ** ()` 与函数 `abs, min, max, sqrt, sin, cos, tan, log, exp, pow`。
- **计算写入任务 (`calc_write_tasks`)**：将采集/计算任务的实时值周期性写入设备（写入值动态来自 `source_task_id`）。

### 数据采集
- 多通道并发采集，支持自定义轮询间隔
- 线程安全数据存储，支持实时折线图显示
- 数据缩放与偏移处理，支持工程单位转换
- 全量数据CSV导出

### 配置管理
- YAML格式配置文件，支持配置持久化
- 可视化连接配置对话框
- 可视化采集任务配置对话框
- 可视化写入任务 / 计算任务 / 计算写入任务配置对话框
- 配置文件导入/导出
- 配置文件结构校验脚本 `_validate.py`，可快速打印连接/任务清单并核对数量

### 可视化
- 基于pyqtgraph的实时折线图（统计图选项卡）
- 磁贴显示选项卡：按网格布局展示各通道实时数值，多通道颜色区分
- 深色主题，多通道颜色区分
- 实时状态监控
- 连接状态自动重连

### 离线数据分析
- `analysis.py`：CSV 读取、透视表构建、按时间重采样、温度走势图绘制（依赖 pandas / matplotlib / scipy / numpy）。
- `pivot.py`：命令行工具，将导出的 CSV 构建为透视表并执行线性插值，批量生成高分辨率折线图。

### 自动更新
- `updater.py`：自动更新启动器。通过 HEAD 请求比对远端 `Last-Modified` / `Etag`，与本地 `.updater_state.json` 记录比较，若有变化则下载替换后启动本地 `DAQ_System.exe`；服务器不可达时回退到本地版本。仅依赖 Python 标准库。

## 系统要求

| 组件 | 版本要求 | 备注 |
|------|----------|------|
| Python | 3.8+ | CI 构建使用 3.12 |
| PySide6 | 6.11.1 | GUI 框架 |
| pyqtgraph | 0.14.0 | 实时折线图 |
| PyYAML | 6.0.3 | 配置文件解析 |
| pyserial | 3.5 | Modbus RTU / ASCII 串口通信 |
| numpy | 2.5.1 | 数据处理 |
| openpyxl | 3.1.5 | Excel 读写（notebooks 分析） |
| psutil | 7.2.2 | 进程/系统监控 |
| matplotlib | （经 ipython 栈） | 离线分析绘图 |
| scipy | （分析脚本） | `analysis.py` 统计计算 |

> 主程序运行仅需 PySide6 / pyqtgraph / PyYAML / pyserial（及 numpy）。matplotlib / scipy / openpyxl / psutil 主要用于离线分析与 notebooks。

## 安装步骤

```bash
# 克隆仓库
git clone https://github.com/lu1770/pyscada.git
cd pyscada

# 安装依赖
pip install -r requirements.txt

# 运行程序
python main.py
```

## 配置说明

系统使用YAML格式的配置文件 `daq_config.yml`，包含连接配置和采集任务配置两部分。完整模板见 `daq_config.yml.template`。

### 配置文件结构

```yaml
connections:
  connection_id:
    type: modbus_tcp | modbus_rtu | modbus_ascii | keyence
    params:
      # Modbus TCP
      host: 192.168.1.100
      port: 502
      slave_id: 1
      timeout: 1.0
      
      # Modbus RTU
      port: COM1
      baudrate: 9600
      parity: N
      stopbits: 1
      bytesize: 8
      slave_id: 1
      timeout: 1.0
      
      # Modbus ASCII（bytesize 通常为 7）
      port: COM2
      baudrate: 38400
      parity: E
      stopbits: 1
      bytesize: 7
      slave_id: 1
      timeout: 1.0
      
      # Keyence PLC
      host: 192.168.1.101
      port: 3000
      unit: 0
      timeout: 3.0

poll_interval: 0.5

tasks:
  - task_id: task_1
    connection_id: connection_id
    connection_type: modbus_tcp | modbus_rtu | modbus_ascii | keyence
    device_type: holding | input | coil | discrete_input | DM | MR | LR | TIM | CNT | VR
    start_addr: 0
    quantity: 1
    channel_prefix: temp
    channel_name: 温度
    unit: °C
    scale: 1.0
    offset: 0.0
    data_type: uint16
    byte_order: abcd

# 写入任务（可选）：周期性向设备写入固定值
write_tasks:
  - task_id: wtask_1
    connection_id: connection_id
    connection_type: modbus_tcp
    device_type: holding
    start_addr: 4108
    value: 25.0
    write_interval: 1.0
    data_type: uint16
    byte_order: abcd
    name: 写入设定温度

# 计算任务（可选）：基于公式生成新通道
calc_tasks:
  - task_id: calc_avg
    channel_prefix: calc_avg
    channel_name: 四通道平均值
    formula: "(task_ch1 + task_ch2 + task_ch3 + task_ch4) / 4"
    unit: '%'
    scale: 1.0
    offset: 0.0

# 计算写入任务（可选）：将采集/计算任务的实时值周期性写入设备
calc_write_tasks:
  - task_id: calcwrite_1
    source_task_id: calc_avg
    connection_id: connection_id
    connection_type: modbus_tcp
    device_type: holding
    start_addr: 100
    write_interval: 1.0
    data_type: uint16
    byte_order: abcd
    name: 写入计算结果
```

### 示例配置

- 根目录 `daq_config.yml`：连接 Delta DTE-10T 温度控制器并采集 32 个通道的完整示例。
- `configurations/` 目录：包含多套针对不同设备的实战配置，覆盖 DTE / DTM 温控、Keyence PLC、PT100 测试、PWM、PLC、多设备合并等场景。
- `daq_config.yml.template`：包含全部字段说明与各任务类型示例的完整模板。

## 使用说明

1. **添加连接**：点击"添加连接"按钮，选择连接类型并配置参数
2. **添加采集任务**：点击"添加采集任务"按钮，选择所属连接并配置采集参数
3. **添加写入/计算任务**：通过对应配置对话框添加 `write_tasks` / `calc_tasks` / `calc_write_tasks`
4. **开始采集**：点击"开始采集"按钮，系统将开始轮询所有配置的通道
5. **停止采集**：点击"停止采集"按钮，系统将停止轮询并断开所有连接
6. **切换视图**：在"统计图"与"磁贴显示"选项卡之间切换查看折线图或实时数值
7. **导出数据**：点击"导出CSV"按钮，将当前所有采集数据导出为CSV文件
8. **保存配置**：点击"保存配置"按钮，将当前配置保存到`daq_config.yml`
9. **加载配置**：点击"加载配置"按钮，从`daq_config.yml`加载配置
10. **校验配置**：`python _validate.py <config.yml>` 打印连接与各任务清单及数量

## 构建打包

项目提供了 `build.py` 脚本用于打包为独立可执行文件：

```bash
python build.py
```

打包完成后，可执行文件位于 `dist/DAQ_System.exe`（Windows）。

### CI/CD

仓库集成 GitHub Actions（`.github/workflows/build.yml`）：在 push/PR 到 `main` 时于 `windows-latest` 上自动安装依赖、构建可执行文件、上传构建产物，并在 main 分支创建版本标签 `v1.0.<run_number>` 与 Release。

### 自动更新分发

`updater.py` 可作为终端侧启动器：比对远端可执行文件修改时间，按需下载替换后启动本地程序，便于在现场设备上分发新版本。

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
├── main.py                  # 主程序入口（连接器/任务/采集worker/图表/磁贴/主窗口）
├── build.py                 # PyInstaller 打包脚本
├── updater.py               # 自动更新启动器（标准库实现）
├── analysis.py              # CSV 离线分析与走势图脚本
├── pivot.py                 # CSV 透视表 + 线性插值 + 批量绘图 CLI
├── _validate.py             # 配置文件结构校验脚本
├── daq_config.yml           # 默认配置文件
├── daq_config.yml.template  # 完整配置模板（含全部字段说明）
├── DAQ_System.spec          # PyInstaller spec 文件
├── requirements.txt         # 依赖列表（含版本锁定）
├── configurations/          # 多设备实战示例配置集合
├── docs/                    # 设备通讯地址表（DTE.md / DTM.md）
├── notebooks/               # 数据分析与运行示例 Jupyter notebooks
├── .github/workflows/       # CI/CD 自动构建工作流
├── .gitignore               # Git忽略文件
└── README.md                # 项目说明文档
```

## 设备文档

`docs/` 目录提供现场常用设备的通讯地址表，便于配置 `device_type` / `start_addr`：

- `docs/DTE.md`：台达 DTE 系列温度控制器通讯地址表（H10xx 基本控制、H19xx/H48xx 特殊功能、H20xx 可程式控制区）
- `docs/DTM.md`：台达 DTM 系列温度控制器通讯地址表

## 许可证

GNU General Public License v3 (GPL-3.0) - 详见 [LICENSE](LICENSE) 文件

---

# Multi-Channel Industrial Data Acquisition System

## Introduction

Multi-Channel Industrial Data Acquisition System is a Python-based cross-platform industrial data acquisition software that supports multiple industrial communication protocols and provides real-time data visualization, export, periodic writing, formula-based calculated channels, and offline data analysis.

## Features

### Communication Protocols
- **Modbus TCP**: Native socket implementation, no pymodbus dependency
- **Modbus RTU**: Serial communication based on pyserial
- **Modbus ASCII**: Serial communication in ASCII encoding mode (bytesize usually 7)
- **Keyence PLC**: Supports Keyence PLC upper-link protocol (KV-5500/7500/8000/Nano series)

### Register / Device Types
- **Modbus**: `holding`, `input`, `coil`, `discrete_input`
- **Keyence**: `DM`, `MR`, `LR`, `TIM`, `CNT`, `VR`

### Data Types & Byte Orders
- 8 data types: `uint16`, `int16`, `uint32`, `int32`, `float32`, `uint64`, `int64`, `float64`
- 4 byte orders: `abcd`, `dcba`, `badc`, `cdab`

### Task Types
- **`tasks`**: Periodic polling/read tasks with scaling & offset
- **`write_tasks`**: Periodically write a fixed value to a device (e.g. setpoint)
- **`calc_tasks`**: Real-time formula-based calculated channels (operators `+ - * / % ** ()`; functions `abs, min, max, sqrt, sin, cos, tan, log, exp, pow`)
- **`calc_write_tasks`**: Periodically write a live value from an acquisition/calc task to a device

### Data Acquisition
- Multi-channel concurrent acquisition with configurable polling interval
- Thread-safe data storage with real-time line chart display
- Data scaling and offset processing for engineering unit conversion
- Full data CSV export

### Configuration Management
- YAML configuration with persistence
- Visual dialogs for connections and all task types
- Config import/export and a `_validate.py` structural checker

### Visualization
- Real-time line charts based on pyqtgraph (chart tab)
- Tile display tab: grid layout showing live channel values with multi-channel colors
- Dark theme, real-time status monitoring, automatic reconnection

### Offline Analysis & Tooling
- `analysis.py` / `pivot.py`: CSV pivot tables, resampling, linear interpolation, high-resolution charts (pandas / matplotlib / scipy / numpy)
- `updater.py`: standard-library auto-updater launcher (HEAD-based `Last-Modified` / `Etag` comparison)
- `_validate.py`: print connection/task inventory and counts for a config file

## System Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.8+ | CI uses 3.12 |
| PySide6 | 6.11.1 | GUI framework |
| pyqtgraph | 0.14.0 | Real-time charts |
| PyYAML | 6.0.3 | Config parsing |
| pyserial | 3.5 | Modbus RTU / ASCII serial |
| numpy | 2.5.1 | Data processing |
| openpyxl | 3.1.5 | Excel I/O (notebooks) |
| psutil | 7.2.2 | Process/system monitoring |
| matplotlib / scipy | via analysis scripts | Offline plotting & stats |

> Running the app only requires PySide6 / pyqtgraph / PyYAML / pyserial (+ numpy). matplotlib / scipy / openpyxl / psutil are mainly for offline analysis and notebooks.

## Installation

```bash
git clone https://github.com/lu1770/pyscada.git
cd pyscada
pip install -r requirements.txt
python main.py
```

## Configuration

The system uses a YAML configuration file `daq_config.yml` containing `connections`, `tasks`, and optional `write_tasks` / `calc_tasks` / `calc_write_tasks`. See `daq_config.yml.template` for the full schema with all fields documented, and the `configurations/` directory for real-world examples per device (DTE / DTM / Keyence / PT100 / PWM / PLC / merged).

## Usage

1. Add Connection → Configure parameters
2. Add Acquisition Task → Select connection and configure parameters
3. Add Write / Calc / Calc-Write Tasks via their dialogs
4. Start Acquisition → Begin polling all configured channels
5. Stop Acquisition → Stop polling and disconnect
6. Switch view between the Chart tab and Tile display tab
7. Export CSV → Export all acquired data
8. Save/Load Configuration → `daq_config.yml`
9. Validate config → `python _validate.py <config.yml>`

## Building

```bash
python build.py
```

Output: `dist/DAQ_System.exe`. CI (`.github/workflows/build.yml`) builds on `windows-latest` and publishes a Release tagged `v1.0.<run_number>`. Use `updater.py` as a client-side launcher to auto-download and start the latest build.

## License

GNU General Public License v3 (GPL-3.0)
