#!/usr/bin/env python3
"""把反代服务打成单文件 exe（Windows）。

    python build-exe.py                 # 单文件：dist/wb-reverse-proxy.exe
    python build-exe.py --onedir        # 目录形态：dist/wb-reverse-proxy/
    python build-exe.py --clean         # 先清 build/ 与 dist/

产物运行：
    双击 dist/wb-reverse-proxy.exe，控制台会打印管理地址与（首次的）管理员口令；
    运行数据落在 exe 同级的 data/ 目录，拷走整个文件夹即可换机使用。

依赖：pip install -r requirements.txt pyinstaller
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "wb-reverse-proxy.spec"
NAME = "wb-reverse-proxy"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包反代服务为 exe")
    parser.add_argument("--onedir", action="store_true", help="产出目录形态（启动更快、体积更分散）")
    parser.add_argument("--clean", action="store_true", help="先删除 build/ 与 dist/")
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller。请先运行：", file=sys.stderr)
        print(f"  {sys.executable} -m pip install pyinstaller", file=sys.stderr)
        return 2

    if args.clean:
        for folder in ("build", "dist"):
            path = ROOT / folder
            if path.exists():
                shutil.rmtree(path)
                print(f"[build-exe] 已清理 {path}")

    env = dict(os.environ)
    if args.onedir:
        env["WB_EXE_ONEDIR"] = "1"

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", str(SPEC)]
    print("[build-exe] " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    if result.returncode != 0:
        print("[build-exe] 打包失败", file=sys.stderr)
        return result.returncode

    target = ROOT / "dist" / (NAME + ("" if args.onedir else ".exe"))
    print()
    if target.exists():
        size = target.stat().st_size if target.is_file() else sum(
            f.stat().st_size for f in target.rglob("*") if f.is_file()
        )
        print(f"[build-exe] 完成：{target}（{size / 1024 / 1024:.1f} MB）")
        print(f"[build-exe] 直接运行：{target}")
    else:
        print(f"[build-exe] 打包结束，但没找到 {target}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
