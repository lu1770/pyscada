#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSV Pivot Table + Linear Interpolation + High-Resolution Line Charts

表头: timestamp, connection_id, channel_id, channel_name, raw_value, value, unit, scale, offset
- 行: timestamp
- 列: channel_name
- 值: value (取平均)
- 对结果做线性插值，填充所有 NaN / 空白单元格
- 为每个 channel_name 生成高分辨率折线图

C:/Users/zheng/AppData/Local/Python/pythoncore-3.14-64/python.exe c:/code/pyscada/pivot.py C:\Users\zheng\Downloads\下模上下层基本稳定daq_export_20260807_075837.csv --chart-dir=下模上下层基本稳定


"""

import pandas as pd
import numpy as np
import argparse
import sys
import os
import matplotlib
matplotlib.use("Agg")  # 无GUI后端
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib import font_manager
font_manager.fontManager.addfont("C:\\Windows\\Fonts\\STSONG.TTF")
plt.rcParams["font.sans-serif"] = ["STSONG"]

def load_data(csv_path: str) -> pd.DataFrame:
    """读取 CSV 并做基础类型转换。"""
    df = pd.read_csv(csv_path)

    if "timestamp" not in df.columns:
        raise ValueError("CSV 中缺少必需的 'timestamp' 列")

    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    if "value" in df.columns:
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
    else:
        raise ValueError("CSV 中缺少必需的 'value' 列")

    if "channel_name" in df.columns:
        df["channel_name"] = df["channel_name"].astype(str)
    else:
        raise ValueError("CSV 中缺少必需的 'channel_name' 列")

    return df


def build_pivot(df: pd.DataFrame) -> pd.DataFrame:
    """
    构建 Pivot Table:
      - 行: timestamp
      - 列: channel_name
      - 值: value (mean)
    """
    pivot = df.pivot_table(
        index="timestamp",
        columns="channel_name",
        values="value",
        aggfunc="mean"
    )
    pivot = pivot.sort_index()
    return pivot


def interpolate_pivot(pivot: pd.DataFrame, limit_direction: str = "both") -> pd.DataFrame:
    """
    对 Pivot Table 按行方向（时间轴）做线性插值。
    处理所有 NaN / 空白单元格。
    """
    interpolated = pivot.interpolate(
        method="linear",
        axis=0,
        limit_direction=limit_direction,
        inplace=False
    )

    # 兜底：整列全空时先用列均值，再用 0
    interpolated = interpolated.fillna(interpolated.mean())
    interpolated = interpolated.fillna(0)

    return interpolated


def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    invalid_chars = '\\/:*?"<>|'
    for ch in invalid_chars:
        name = name.replace(ch, "_")
    return name.strip()


def plot_all_channels(
    pivot: pd.DataFrame,
    output_dir: str,
    dpi: int = 300,
    figsize: tuple = (14, 6),
    line_color: str = "#2E86AB",
    grid_color: str = "#E0E0E0"
):
    """
    为 Pivot Table 的每一列生成高分辨率折线图。

    Parameters
    ----------
    pivot : pd.DataFrame
        插值后的 pivot table，index 为 timestamp，columns 为 channel_name
    output_dir : str
        图表输出文件夹路径
    dpi : int
        图像分辨率（默认 300 DPI）
    figsize : tuple
        图像尺寸（英寸）
    """
    os.makedirs(output_dir, exist_ok=True)
    print(f"图表输出目录: {output_dir}")

    timestamps = pivot.index
    total = len(pivot.columns)

    for i, channel in enumerate(pivot.columns, 1):
        values = pivot[channel].values
        safe_name = sanitize_filename(channel)
        filepath = os.path.join(output_dir, f"{safe_name}.png")

        fig, ax = plt.subplots(figsize=figsize, dpi=dpi)

        ax.plot(
            timestamps,
            values,
            color=line_color,
            linewidth=1.2,
            marker="",
            linestyle="-"
        )

        ax.set_title(f"Channel: {channel}", fontsize=14, fontweight="bold", pad=12)
        ax.set_xlabel("Timestamp", fontsize=11)
        ax.set_ylabel("Value", fontsize=11)

        # 时间轴格式化
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right")

        # 网格
        ax.grid(True, linestyle="--", linewidth=0.5, color=grid_color, alpha=0.8)

        # 自动调整边距
        fig.tight_layout()

        fig.savefig(filepath, dpi=dpi, bbox_inches="tight", facecolor="white")
        plt.close(fig)

        print(f"  [{i}/{total}] {channel} -> {filepath}")

    print(f"共生成 {total} 张图表。")


def main():
    parser = argparse.ArgumentParser(
        description="CSV Pivot Table + Linear Interpolation + Line Charts"
    )
    parser.add_argument("input_csv", help="输入 CSV 文件路径")
    parser.add_argument(
        "-o", "--output",
        default="pivot_interpolated.csv",
        help="输出 CSV 文件路径 (默认: pivot_interpolated.csv)"
    )
    parser.add_argument(
        "--chart-dir",
        default="charts",
        help="图表输出文件夹 (默认: charts)"
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="图表 DPI 分辨率 (默认: 300)"
    )
    args = parser.parse_args()

    # 1. 读取数据
    print(f"正在读取: {args.input_csv}")
    df = load_data(args.input_csv)
    print(f"原始数据行数: {len(df)}")

    # 2. 构建 Pivot Table
    print("构建 Pivot Table (timestamp x channel_name, value=mean)...")
    pivot = build_pivot(df)
    print(f"Pivot 形状: {pivot.shape}")
    print(f"Pivot 中 NaN 单元格数: {pivot.isna().sum().sum()}")

    # 3. 线性插值
    print("执行线性插值...")
    result = interpolate_pivot(pivot)
    print(f"插值后 NaN 单元格数: {result.isna().sum().sum()}")

    # 4. 保存 CSV 结果
    result.to_csv(args.output, encoding="utf-8-sig")
    print(f"Pivot CSV 已保存至: {args.output}")

    # 5. 生成高分辨率折线图
    print("\n开始生成折线图...")
    plot_all_channels(
        pivot=result,
        output_dir=args.chart_dir,
        dpi=args.dpi
    )

    print("\n全部完成！")


if __name__ == "__main__":
    main()