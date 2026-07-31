import pandas as pd
import os
import re
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

# 设置绘图风格（必须放在字体配置之前，否则会覆盖 rcParams）
plt.style.use('seaborn-v0_8-darkgrid')

# 设置中文字体（style.use 会重置 rcParams，所以必须在其之后设置）
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False
# 读取CSV文件
file_path = r'C:\code\pyscada\dist\DTM自整定完成200-195稳定过程.csv'
df = pd.read_csv(file_path, encoding='utf-8').head(10**5)

# 转换为透视表
pivot_df = df.pivot_table(
    index='timestamp',
    columns='channel_name',
    values='value',
    aggfunc='mean'
).sort_index()

# 确保timestamp是datetime类型
pivot_df.index = pd.to_datetime(pivot_df.index)

print("\n处理前的数据（含NaN）:")
print(pivot_df.head(10))

# 按指定时间间隔重采样（例如每100ms、1秒等）
# 可选: '100ms', '500ms', '1s', '5s' 等
pivot_df_resampled = pivot_df.resample('1s').mean()  # 1秒间隔
pivot_df_clean = pivot_df_resampled.ffill().bfill()

print(f"\n重采样后数据形状: {pivot_df_clean.shape}")
print("重采样处理后的数据:")
print(pivot_df_clean.head(10))

# 保存
output_dir = r'C:\code\pyscada\dist'
os.makedirs(output_dir, exist_ok=True)
output_path = os.path.join(output_dir, 'DTM_通道透视表_重采样.csv')
pivot_df_clean.to_csv(output_path, encoding='utf-8-sig')

print(f"\n透视表已保存至: {output_path}")


df = pivot_df_clean
# df['timestamp'] = pd.to_datetime(df['timestamp'])
# df.set_index('timestamp', inplace=True)

temp_columns = [col for col in df.columns if '当前温度' in str(col) and 'DTE' not in str(col)]

if not temp_columns:
    print("\n警告: 没有找到包含'当前温度'的列")
    print("可用列名:", list(df.columns))
else:
    print(f"\n找到温度列: {temp_columns}")
    print(f"数据范围: {df.index.min()} 至 {df.index.max()}")
    
    # 为每个温度列单独绘制走势图
    for col in temp_columns:
        fig = plt.figure(figsize=(16, 10))
        
        # 创建网格布局
        gs = fig.add_gridspec(3, 1, height_ratios=[3, 1, 1], hspace=0.3)
        
        # ==================== 子图1: 温度走势曲线 ====================
        ax1 = fig.add_subplot(gs[0])
        
        y_data = df[col].values
        x_data = np.arange(len(y_data))
        
        # 绘制温度曲线
        line = ax1.plot(df.index, y_data, 
                       color='#FF6B6B', 
                       linewidth=2.5,
                       label='温度值',
                       alpha=0.9)
                       
        ax1.set_title(f'{col} 走势图 (前10000行)', fontsize=18, fontweight='bold', pad=15)
        ax1.set_ylabel('温度 (°C)', fontsize=14, fontweight='bold')
        ax1.legend(loc='best', fontsize=11)
        ax1.grid(True, alpha=0.3)
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', col)
    output_image = os.path.join(output_dir, f'{safe_name}_走势图_前1万行.png')
    plt.savefig(output_image, dpi=300, bbox_inches='tight')
    print(f"\n图像已保存至: {output_image}")
 
