#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DAQ_System 自动更新启动器
============================================================
流程:
  1. HEAD 请求获取远端 DAQ_System.exe 的 Last-Modified / Etag
  2. 与本地 .updater_state.json 中记录的上次值比较
  3. 若修改日期变化(或本地无 exe),则下载替换
  4. 启动本地 DAQ_System.exe

用法:  python updater.py
依赖:  仅 Python 标准库
============================================================
"""

import os
import sys
import json
import shutil
import urllib.request
import urllib.error

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# ---------------- 配置 ----------------
REMOTE_URL = "http://192.168.188.1:5244/d/dist/DAQ_System.exe?sign=0IO1c8pzrMamr9tPYI2wtFuAKrkvXa4kmwPREhDgsY8=:0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_EXE = os.path.join(BASE_DIR, "DAQ_System.exe")
STATE_FILE = os.path.join(BASE_DIR, ".updater_state.json")
TIMEOUT = 15  # 网络 HEAD 超时(秒)
DOWNLOAD_TIMEOUT = 300  # 下载超时(秒)


def fetch_remote_info():
    """通过 HEAD 请求获取远端文件的修改信息。"""
    req = urllib.request.Request(REMOTE_URL, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return {
                "last_modified": resp.headers.get("Last-Modified"),
                "etag": resp.headers.get("Etag"),
                "content_length": resp.headers.get("Content-Length"),
                "status": resp.status,
            }
    except urllib.error.URLError as e:
        print(f"[更新] 无法连接服务器: {e}")
        return None
    except Exception as e:
        print(f"[更新] 获取远端信息失败: {e}")
        return None


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(info):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[更新] 保存状态失败: {e}")


def is_changed(remote, state):
    """判断远端是否相对本地记录有更新。"""
    if not remote.get("last_modified"):
        return False  # 无修改日期信息，无法判断
    if not state.get("last_modified"):
        return True  # 首次运行
    if remote["last_modified"] != state.get("last_modified"):
        return True
    # 修改日期相同，再用 Etag 兜底
    if remote.get("etag") and remote["etag"] != state.get("etag"):
        return True
    return False


def download(remote):
    """下载远端文件到本地，先写临时文件再原子替换。"""
    tmp = LOCAL_EXE + ".tmp"
    try:
        print(f"[更新] 开始下载 ({remote.get('content_length', '?')} bytes)...")
        req = urllib.request.Request(REMOTE_URL, method="GET")
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
        # 校验下载大小
        actual = os.path.getsize(tmp)
        expected = remote.get("content_length")
        if expected and str(actual) != str(expected):
            print(f"[更新] 警告: 大小不匹配 (期望 {expected}, 实际 {actual})")
        # 原子替换
        os.replace(tmp, LOCAL_EXE)
        print(f"[更新] 下载完成: {LOCAL_EXE}")
        return True
    except PermissionError:
        print("[更新] 本地 exe 正在运行，无法替换。跳过更新。")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False
    except Exception as e:
        print(f"[更新] 下载失败: {e}")
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        return False


def launch():
    """启动本地 exe。"""
    if not os.path.exists(LOCAL_EXE):
        print(f"[更新] 本地程序不存在: {LOCAL_EXE}")
        return False
    try:
        os.startfile(LOCAL_EXE)  # Windows 专用
        print(f"[更新] 已启动: {LOCAL_EXE}")
        return True
    except Exception as e:
        print(f"[更新] 启动失败: {e}")
        return False


def main():
    print("=" * 50)
    print("DAQ_System 自动更新启动器")
    print("=" * 50)

    remote = fetch_remote_info()
    state = load_state()

    if remote is None:
        # 服务器不可达，直接尝试启动本地版本
        print("[更新] 服务器不可达，使用本地版本")
        return 0 if launch() else 1

    print(f"[更新] 远端 Last-Modified: {remote.get('last_modified')}")
    print(f"[更新] 远端 Etag:          {remote.get('etag')}")

    if is_changed(remote, state) or not os.path.exists(LOCAL_EXE):
        if not os.path.exists(LOCAL_EXE):
            print("[更新] 本地无 exe，需要下载")
        else:
            print("[更新] 检测到修改日期变化，开始更新")
        if download(remote):
            save_state(remote)
        elif not os.path.exists(LOCAL_EXE):
            print("[更新] 下载失败且本地无可用程序")
            return 1
        else:
            print("[更新] 下载失败，使用本地版本")
    else:
        print("[更新] 无变化，直接启动")

    return 0 if launch() else 1


if __name__ == "__main__":
    sys.exit(main())
