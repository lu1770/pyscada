import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# 设置中文字体支持
plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# ========================================
# 方案1：所有通道在同一张图上
# ========================================

def plot_all_channels_combined(csv_file):
    """绘制所有温度通道在一张图上"""
    
    # 读取CSV文件
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # 筛选温度测量通道（排除设定温度和输出功率）
    temp_channels = df[df['channel_name'].str.contains('当前温度|PV值')].copy()
    
    # 创建图表
    fig, ax = plt.subplots(figsize=(16, 10))
    
    # 定义颜色方案
    colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', 
              '#FFEAA7', '#DDA0DD', '#98D8C8', '#F7DC6F']
    
    # 绘制各通道曲线
    for idx, channel_name in enumerate(temp_channels['channel_name'].unique()):
        channel_data = temp_channels[temp_channels['channel_name'] == channel_name].sort_values('timestamp')
        
        ax.plot(channel_data['timestamp'], channel_data['value'], 
                marker='o', markersize=4, linewidth=2.5, 
                color=colors[idx % len(colors)], 
                label=channel_name, alpha=0.85)
    
    # 设置图表属性
    ax.set_title('各通道温度变化曲线 (降温测试 200°C → 195°C)', 
                 fontsize=18, fontweight='bold', pad=20)
    ax.set_xlabel('时间', fontsize=14)
    ax.set_ylabel('温度 (°C)', fontsize=14)
    
    # 格式化x轴时间显示
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=1))
    plt.xticks(rotation=45)
    
    # 添加网格和图例
    ax.grid(True, linestyle='--', alpha=0.6)
    ax.legend(loc='best', fontsize=11, framealpha=0.95, ncol=2)
    
    # 添加目标温度参考线
    ax.axhline(y=195, color='red', linestyle='--', linewidth=2, 
               alpha=0.7, label='目标温度 195°C')
    
    plt.tight_layout()
    plt.savefig('temperature_curves_combined.png', dpi=300, bbox_inches='tight')
    plt.show()
    
    return fig


# ========================================
# 方案2：分组子图（DTE和DTM分开）
# ========================================

def plot_channels_grouped(csv_file):
    """按DTE和DTM分组绘制温度曲线"""
    
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    temp_channels = df[df['channel_name'].str.contains('当前温度|PV值')].copy()
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 12))
    colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4']
    
    # DTE通道
    dte_channels = temp_channels[temp_channels['channel_name'].str.contains('DTE')]
    for idx, channel_name in enumerate(dte_channels['channel_name'].unique()):
        channel_data = dte_channels[dte_channels['channel_name'] == channel_name].sort_values('timestamp')
        ax1.plot(channel_data['timestamp'], channel_data['value'], 
                marker='o', markersize=5, linewidth=2.5,
                color=colors[idx % 4], label=channel_name, alpha=0.85)
    
    ax1.set_title('DTE通道温度变化曲线', fontsize=16, fontweight='bold')
    ax1.set_xlabel('时间', fontsize=12)
    ax1.set_ylabel('温度 (°C)', fontsize=12)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    ax1.grid(True, linestyle='--', alpha=0.6)
    ax1.legend(loc='best', fontsize=11)
    ax1.axhline(y=195, color='red', linestyle='--', linewidth=1.5, alpha=0.7)
    
    # DTM通道
    dtm_channels = temp_channels[temp_channels['channel_name'].str.contains('DTM')]
    for idx, channel_name in enumerate(dtm_channels['channel_name'].unique()):
        channel_data = dtm_channels[dtm_channels['channel_name'] == channel_name].sort_values('timestamp')
        ax2.plot(channel_data['timestamp'], channel_data['value'], 
                marker='s', markersize=5, linewidth=2.5,
                color=colors[idx % 4], label=channel_name, alpha=0.85)
    
    ax2.set_title('DTM通道温度变化曲线', fontsize=16, fontweight='bold')
    ax2.set_xlabel('时间', fontsize=12)
    ax2.set_ylabel('温度 (°C)', fontsize=12)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
    ax2.grid(True, linestyle='--', alpha=0.6)
    ax2.legend(loc='best', fontsize=11)
    
    plt.tight_layout()
    plt.savefig('temperature_curves_grouped.png', dpi=300, bbox_inches='tight')
    plt.show()
    
    return fig


# ========================================
# 方案3：每个通道独立子图
# ========================================

def plot_channels_individual(csv_file):
    """每个通道绘制独立子图"""
    
    df = pd.read_csv(csv_file)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    temp_channels = df[df['channel_name'].str.contains('当前温度|PV值')].copy()
    
    channel_names = temp_channels['channel_name'].unique()
    n_channels = len(channel_names)
    
    fig, axes = plt.subplots(n_channels, 1, figsize=(16, 4*n_channels))
    if n_channels == 1:
        axes = [axes]
    
    colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', 
              '#FFEAA7', '#DDA0DD', '#98D8C8', '#F7DC6F']
    
    for idx, (channel_name, ax) in enumerate(zip(channel_names, axes)):
        channel_data = temp_channels[temp_channels['channel_name'] == channel_name].sort_values('timestamp')
        
        ax.plot(channel_data['timestamp'], channel_data['value'], 
               marker='o', markersize=6, linewidth=2.5,
               color=colors[idx % len(colors)], alpha=0.85)
        
        ax.set_title(f'{channel_name}', fontsize=14, fontweight='bold')
        ax.set_xlabel('时间', fontsize=11)
        ax.set_ylabel('温度 (°C)', fontsize=11)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))
        ax.grid(True, linestyle='--', alpha=0.6)
        
        # 标注目标温度和最高最低温度
        ax.axhline(y=195, color='red', linestyle='--', linewidth=1.5, alpha=0.5, label='目标 195°C')
        max_temp = channel_data['value'].max()
        min_temp = channel_data['value'].min()
        ax.axhline(y=max_temp, color='green', linestyle=':', linewidth=1, alpha=0.6)
        ax.axhline(y=min_temp, color='blue', linestyle=':', linewidth=1, alpha=0.6)
        
        # 设置y轴范围
        ax.set_ylim([min_temp - 0.5, max_temp + 0.5])
        ax.legend(loc='best', fontsize=9)
    
    plt.tight_layout()
    plt.savefig('temperature_curves_individual.png', dpi=300, bbox_inches='tight')
    plt.show()
    
    return fig


# ========================================
# 使用方法
# ========================================

if __name__ == '__main__':
    csv_file = '升温测试195到200.csv'
    
    # 生成图表
    print("生成图表1：所有通道合并图...")
    fig1 = plot_all_channels_combined(csv_file)
    
    print("\n生成图表2：DTE/DTM分组图...")
    fig2 = plot_channels_grouped(csv_file)
    
    print("\n生成图表3：各通道独立图...")
    fig3 = plot_channels_individual(csv_file)
    
    print("\n所有图表已生成完成！")
