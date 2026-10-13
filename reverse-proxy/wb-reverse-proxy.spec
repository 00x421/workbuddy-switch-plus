# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件 exe（workbuddy-switch-plus 反代服务）。

用法：
  python build-exe.py            # 产出 dist/wb-reverse-proxy.exe（默认单文件）
  WB_EXE_ONEDIR=1 python build-exe.py   # 产出 dist/wb-reverse-proxy/ 目录形态

产物特性：
  · 双击即启动，控制台打印管理地址与（首次的）管理员口令；
  · 运行数据落在 **exe 同级 data/**（绿色免安装；不可写时退回 %APPDATA%）；
  · 内置静态后台（admin/static）随包带走，`/admin/` 直接可用。
"""
import os
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve()        # reverse-proxy/
REPO = ROOT.parent                     # workbuddy-switch-plus/
ICON = REPO / "src-tauri" / "icons" / "icon.ico"

NAME = "wb-reverse-proxy"
ONEDIR = os.environ.get("WB_EXE_ONEDIR") == "1"

# uvicorn 的这些模块是运行时按字符串动态选择的，静态分析看不到，必须显式声明。
# 参考 uvicorn 的 auto 模块：loops / protocols.http / protocols.websockets / lifespan。
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    # 本项目的包：serve.py 里有 import，但显式列出可避免被误裁
    "admin",
    "admin.assets",
    "admin.browser_login",
    "admin.iputil",
    "admin.keysvc",
    "admin.metrics",
    "admin.pool",
    "admin.security",
    "admin.server",
    "admin.switch_store",
    "core",
    "core.anthropic_adapter",
    "core.converter",
    "core.desensitize",
    "core.responses_adapter",
    "core.responses_projection",
    "core.system_identity",
    "core.workbuddy_atrest_crypto",
]

datas = [
    # 内置静态管理后台（admin/server.py 里的 ASSETS 按 admin/static 定位）
    (str(ROOT / "admin" / "static"), "admin/static"),
]

excludes = [
    "tkinter", "matplotlib", "numpy", "pandas", "PIL",
    "pytest", "_pytest", "unittest", "pydoc", "doctest",
    "IPython", "notebook",
]

a = Analysis(
    [str(ROOT / "serve.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # 控制台形态：双击后有窗口显示地址与口令
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], **common)
