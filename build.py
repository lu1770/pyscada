import subprocess
import sys
import os
import shutil


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    print("=" * 60)
    print("多通道工业数据采集系统 - 打包脚本")
    print("=" * 60)

    if not os.path.exists("requirements.txt"):
        print("[错误] 未找到 requirements.txt")
        return 1

    print("\n[1/4] 安装依赖...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    print("      ✓ 依赖安装完成")

    print("\n[2/4] 清理旧构建...")
    for dir_name in ["dist", "build", "__pycache__"]:
        if os.path.exists(dir_name):
            shutil.rmtree(dir_name)
            print(f"      ✓ 已删除 {dir_name}")
    print("      ✓ 清理完成")

    print("\n[3/4] 执行 PyInstaller 打包...")
    pyinstaller_path = shutil.which("pyinstaller")
    if not pyinstaller_path:
        pyinstaller_path = os.path.join(os.path.dirname(sys.executable), "Scripts", "pyinstaller.exe")
        if not os.path.exists(pyinstaller_path):
            print("[错误] 无法找到 pyinstaller 可执行文件")
            return 1
    
    cmd = [
        pyinstaller_path,
        "--name", "DAQ_System",
        "--onefile",
        "--windowed",
        "--add-data", "daq_config.yml;.",
        "--add-data", "daq_config.yml.template;.",
        "--hidden-import", "yaml",
        "--hidden-import", "serial",
        "--hidden-import", "pyqtgraph",
        "--hidden-import", "PySide6.QtSvg",
        "--hidden-import", "PySide6.QtNetwork",
        "--hidden-import", "PySide6.QtPrintSupport",
        "--clean",
        "main.py"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[错误] PyInstaller 执行失败:\n{result.stderr}")
        return 1
    print("      ✓ PyInstaller 打包完成")

    print("\n[4/4] 复制配置文件到输出目录...")
    dist_dir = os.path.join("dist")
    for file_name in ["daq_config.yml", "daq_config.yml.template"]:
        src = file_name
        dst = os.path.join(dist_dir, file_name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            print(f"      ✓ 已复制 {file_name}")

    print("\n" + "=" * 60)
    print("打包完成!")
    print(f"输出目录: {os.path.abspath(dist_dir)}")
    print("运行命令: dist\\DAQ_System.exe")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())