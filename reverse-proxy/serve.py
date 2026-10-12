#!/usr/bin/env python3
"""workbuddy-switch-plus 反代服务入口：codebuddy2api（协议转换 + 账号池）+ switch 账号库。

账号来源：`~/.wb-switch-plus/accounts.json`（可用 `WB_SWITCH_ACCOUNTS` / `--accounts` 覆盖路径）。
本服务只读该账号库；账号的增删仍由 workbuddy-switch 负责，服务侧做热加载。

运行：
  python serve.py                       # 默认 127.0.0.1:8787
  python serve.py --port 9000 --open
  uvicorn serve:app --host 127.0.0.1 --port 8787

打包（单文件 exe，无需 Python 环境）：
  python build-exe.py                   # 产出 dist/wb-reverse-proxy.exe
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import secrets
import sys
import webbrowser
from pathlib import Path

import uvicorn

from admin.server import create_app
from admin.switch_store import SwitchStore, switch_accounts_path

BANNER_WIDTH = 64


def frozen() -> bool:
    """是否运行在 PyInstaller 打包出的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """静态资源根：冻结时在 PyInstaller 的解包目录，否则是本文件所在目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def default_data_root() -> Path:
    """运行数据目录（state.json：密钥 / 用户 / 令牌 / IP 规则 / 池设置）。

    · 源码运行：`reverse-proxy/data`
    · exe 运行：**exe 同级目录**（绿色免安装，拷走整个文件夹就能带走配置）；
      该目录不可写时退回 `%APPDATA%\\workbuddy-switch-plus`。
    """
    if not frozen():
        return Path(__file__).resolve().parent / "data"
    beside = Path(sys.executable).resolve().parent / "data"
    try:
        beside.mkdir(parents=True, exist_ok=True)
        probe = beside / ".writable"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return beside
    except OSError:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        fallback = Path(base) / "workbuddy-switch-plus"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _setup_console() -> None:
    """让中文横幅在任何控制台代码页下都显示正确、且不会把服务打挂。

    Windows 上双击 exe 时控制台默认是本地代码页（中文系统是 cp936）。
    这里把控制台输出代码页切到 UTF-8 并把流设成 UTF-8；只有切换成功才改流编码，
    否则退回「保持控制台编码 + 不可表示字符替换」——两种情况下都不会抛异常。
    """
    switched = False
    if os.name == "nt":
        try:
            import ctypes

            switched = bool(ctypes.windll.kernel32.SetConsoleOutputCP(65001))
        except Exception:  # noqa: BLE001 - 无控制台 / 老系统
            switched = False
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8" if switched else None, errors="replace")
        except Exception:  # noqa: BLE001 - 流为 None 或不支持 reconfigure
            pass


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wb-reverse-proxy",
        description="把 WorkBuddy / CodeBuddy 登录态转成 OpenAI / Anthropic 兼容 API 的本地服务",
    )
    parser.add_argument("--host", default=os.environ.get("WB_SERVE_HOST", "127.0.0.1"),
                        help="监听地址（默认 127.0.0.1，只对本机开放）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WB_SERVE_PORT", "8787")),
                        help="监听端口（默认 8787）")
    parser.add_argument("--data", default=os.environ.get("WB_SWITCH_DATA", ""),
                        help="运行数据目录（默认：源码运行 ./data，exe 运行 exe 同级 data）")
    parser.add_argument("--accounts", default=os.environ.get("WB_SWITCH_ACCOUNTS", ""),
                        help="workbuddy-switch 账号库路径（默认 ~/.wb-switch-plus/accounts.json）")
    parser.add_argument("--open", action="store_true",
                        help="启动后打开内置管理后台（也可用 WB_SERVE_OPEN=1）")
    parser.add_argument("--no-banner", action="store_true", help="不打印启动信息")
    return parser.parse_args(argv)


def build_app(argv=None):
    args = parse_args(argv)
    root = args.data or str(default_data_root())
    accounts = Path(args.accounts) if args.accounts else switch_accounts_path()

    admin_key = (os.environ.get("ADMIN_KEY") or os.environ.get("WB_ADMIN_PASSWORD") or "").strip()
    # 没显式配就现场随机一个：Store 要求密钥至少 20 位，首启管理员口令也要靠它兜底。
    # 但要记住「这是随机来的，不是部署方配的」——这个区别决定登录页摆不摆
    # 「管理密钥」入口，见 Security.legacy_key_enabled()。桌面端托管从不传 ADMIN_KEY，
    # 所以那边每次启动都是随机值，用户无从得知，入口就必须藏起来。
    legacy_key_configured = len(admin_key) >= 20
    generated = not legacy_key_configured
    if generated:
        admin_key = secrets.token_urlsafe(32)

    store = SwitchStore(
        root=root,
        accounts_path=accounts,
        initial_key=os.environ.get("CODEBUDDY2OPENAI_KEY", ""),
        admin_key=admin_key,
    )
    # create_app → Security.bootstrap() 只在「还没有任何管理用户」时建 admin，
    # 所以「是否首次启动」要在建 app 之前判断。
    first_run = not store.data.get("users")
    app = create_app(store=store, legacy_key_configured=legacy_key_configured)

    base = f"http://{args.host}:{args.port}"
    app.state.banner = {
        "base": base,
        "accounts_path": str(store.accounts_path),
        "accounts": len(store.data["accounts"]),
        "data_root": str(Path(root).resolve()),
        "frozen": frozen(),
        "first_run_password": admin_key if (first_run and generated) else "",
    }
    if not args.no_banner:
        _setup_console()
        try:
            print_banner(app.state.banner)
        except Exception:  # noqa: BLE001 - 横幅只是提示，不能因为打印失败影响启动
            pass
    # 机器可读横幅：桌面端据此判断服务已就绪、并取走首次启动生成的管理员口令。
    # 与 --no-banner 解耦——那是给人看的开关，不影响这里的结构化输出。
    # 前缀 `WB_BANNER ` 是约定，改它要同步改 src-tauri/src/reverse_proxy.rs。
    try:
        print(
            f"WB_BANNER base={app.state.banner['base']} "
            f"port={args.port} frozen={int(frozen())}",
            file=sys.stderr,
            flush=True,
        )
        if app.state.banner["first_run_password"]:
            print(
                f"WB_BANNER first_run_password={app.state.banner['first_run_password']}",
                file=sys.stderr,
                flush=True,
            )
    except Exception:  # noqa: BLE001 - 同上
        pass
    return app


def _display_width(text: str) -> int:
    """终端显示宽度：中日韩全角字符占 2 列（用于横幅对齐）。"""
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(label: str, width: int) -> str:
    return label + " " * max(1, width - _display_width(label))


def print_banner(info: dict) -> None:
    line = "=" * BANNER_WIDTH
    rows = [
        ("内置管理后台", info["base"] + "/admin/"),
        ("SPA 管理入口", "workbuddy-switch 桌面端 → 侧边栏「反代服务」"),
        ("OpenAI 兼容", info["base"] + "/v1"),
        ("Anthropic 兼容", info["base"] + "/v1/messages"),
        ("账号库", f"{info['accounts_path']}（{info['accounts']} 个账号，热加载）"),
        ("运行数据", info["data_root"]),
    ]
    print(line, file=sys.stderr)
    print(" workbuddy-switch-plus 反代服务", file=sys.stderr)
    for label, value in rows:
        print(f"   {_pad(label, 18)}{value}", file=sys.stderr)
    if info["first_run_password"]:
        print(line, file=sys.stderr)
        print(" 首次启动，已生成管理员口令（用户名 admin，请立即保存）：", file=sys.stderr)
        print(f"   {info['first_run_password']}", file=sys.stderr)
        print("   之后启动沿用该口令；想用自己的口令：设 WB_ADMIN_PASSWORD，", file=sys.stderr)
        print(f"   删掉 {info['data_root']} 下的 state.json 再启动。", file=sys.stderr)
    print(line, file=sys.stderr)
    print(" 按 Ctrl+C 停止服务", file=sys.stderr)


def main() -> None:
    multiprocessing.freeze_support()  # PyInstaller onefile 下必须
    args = parse_args()
    # 【必须把 args 传进来】不能用模块级的 app —— 那个是在 import 时用空 argv
    # 建出来的，于是 --port / --data / --accounts 全被吞掉：uvicorn 听的是
    # args.port，而 store 里记的却是默认端口与真实账号库。
    # 表现是「命令行指定了隔离账号库，面板里却还是线上那批账号」——
    # 这不是理论问题：它让一次本该完全隔离的端到端验证读到了真实账号。
    app = build_app(sys.argv[1:])
    if args.open or os.environ.get("WB_SERVE_OPEN") == "1":
        info = getattr(app.state, "banner", None) or {}
        base = info.get("base") or f"http://{args.host}:{args.port}"
        try:
            webbrowser.open(base + "/admin/")
        except Exception:  # noqa: BLE001 - 打不开浏览器不该影响服务启动
            pass
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
