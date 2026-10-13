"""静态资源的定位与固化。**唯一入口**。

## 为什么要单独一个模块

原先 `ASSETS` 在 `server.py` 顶部算一次，指向 `sys._MEIPASS/admin/static`。
`_MEIPASS` 是 PyInstaller 单文件模式**运行时解包到 `%TEMP%\\_MEIxxxxx`** 的目录。

这在服务器上是个真实故障（已复现）：Windows 的磁盘清理 / 存储感知 /
第三方清理工具会删掉 `%TEMP%` 里的旧目录。目录一没，所有页面立刻 404 ——
服务进程还活着、API 还通、日志也不报错，只有网页全打不开。
实测：删掉解包目录后 `/login` 与 `/me/assets/me.css` 双双 404。

## 做法

**首次启动时把 `admin/static` 从解包目录复制到 exe 同级的 `static/`**，
之后一律从那里读。那个位置是持久的，清理工具不会碰。

三条性质要同时成立：
  1. **持久**：不依赖 `%TEMP%`，清完临时目录照常工作；
  2. **自愈**：如果 `static/` 被删或残缺（用户误删、拷贝中断），
     下次启动自动重新复制一份，而不是一直 404；
  3. **可更新**：升级 exe 后资源要跟着更新，不能永远用第一次那份 ——
     靠版本戳（`.version`）比对，内容版本变了就重新复制。

## 为什么读文件而不是直接读 `_MEIPASS`

也可以在请求时判断「`_MEIPASS` 还在不在，不在就回退」。
放弃它的原因：单文件模式下解包目录**可能被删到一半**——
（清理工具删目录时文件是逐个删的，此刻 `index.html` 还在、`app.js` 没了）。
那种「半死不活」状态按目录存在性判断是检测不出来的，
而请求级逐个文件去猜来源，会让每个静态请求都变慢且逻辑分散。
一次性固化到位，判据只有「目录在 + 版本对」，简单且可测。
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("wb.assets")

#: 固化后的目录名（相对 exe 同级 / 数据根）。
FIXED_DIRNAME = "static"

#: 版本戳文件名。存在固化目录里，用来判断是否需要重新复制。
STAMP_NAME = ".version"

#: 固化目录的定位优先用这个环境变量（测试与特殊部署用）。
ENV_OVERRIDE = "WB_STATIC_DIR"


def frozen() -> bool:
    """是否运行在 PyInstaller 打包的可执行文件里。"""
    return bool(getattr(sys, "frozen", False))


def source_dir() -> Path:
    """**源**静态资源目录：打包后在解包目录，源码运行时在本文件同级。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "admin" / "static"
    return Path(__file__).resolve().parent / "static"


def _writable_dir() -> Path | None:
    """固化目录该放哪：优先 exe（或数据目录）同级，不可写则返回 None。

    与 `serve.default_data_root()` 同一套取舍：
    绿色免安装 —— 拷走整个文件夹就带走了配置与页面。
    """
    if frozen():
        candidates = [Path(sys.executable).resolve().parent / FIXED_DIRNAME]
    else:
        # 源码运行时**不固化**：直接用仓库里的 admin/static，
        # 免得改完前端还要多想一层"为什么改了不生效"。
        return None
    env = os.environ.get(ENV_OVERRIDE)
    if env:
        candidates.insert(0, Path(env))
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".writable"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return cand
        except OSError:
            continue
    return None


def _content_stamp() -> str:
    """固化内容的版本戳：目录里所有文件的 (相对路径, 大小, mtime) 摘要。

    为什么不用固定字符串（如版本号）：升级 exe 时如果版本号没改
    （开发中很常见），页面就永远停在旧的那份。用文件系统指纹则
    「内容一变就重新复制」，不需要任何人记得改版本号。
    """
    root = source_dir()
    if not root.is_dir():
        return ""
    parts = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.relative_to(root)}:{stat.st_size}:{int(stat.st_mtime)}")
    return "|".join(parts)


def _read_stamp(dest: Path) -> str:
    try:
        return (dest / STAMP_NAME).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _copy_tree(src: Path, dest: Path) -> None:
    """把 src 的内容同步到 dest：先清空再复制，保证不留旧文件。

    为什么先清空：只做增量复制的话，前端删掉的文件会**留在** dest 里，
    表现为"本地明明删了那个 JS，线上却还在加载"，极难排查。
    """
    for child in dest.iterdir():
        if child.name == STAMP_NAME:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def resolve() -> Path:
    """返回**可用的**静态资源目录（必要时先固化）。**唯一对外入口**。

    返回优先级：
      1. 固化目录（存在且版本戳一致）—— 常态，服务器上走这条；
      2. 重新固化后的目录 —— `static/` 被删或 exe 升级过；
      3. 源目录 —— 固化失败（磁盘只读等）时的兜底，
         此时至少和旧行为一样（能跑就跑，跑不了也不是这次改动引入的）。
    """
    src = source_dir()
    dest = _writable_dir()

    if dest is None:
        return src                                   # 源码运行：直接用源目录

    want = _content_stamp()
    if not want:
        # 源目录都读不到，固化无从谈起 —— 保留旧行为并留一条日志，
        # 而不是返回一个空目录让所有请求 404 且没有任何线索。
        logger.error("静态资源源目录不可读：%s", src)
        return src

    have = _read_stamp(dest)
    marker = dest / "index.html"
    if have == want and marker.exists():
        return dest

    reason = ("版本戳不一致" if marker.exists() else "固化目录不完整")
    try:
        _copy_tree(src, dest)
        (dest / STAMP_NAME).write_text(want, encoding="utf-8")
        logger.info("静态资源已固化到 %s（原因：%s）", dest, reason)
    except OSError as exc:
        logger.warning("静态资源固化失败（将退回解包目录）：%s", exc)
        return src if src.is_dir() else dest
    return dest


#: 进程内缓存。`server.py` 在导入时取一次即可 —— 请求路径上不该碰文件系统。
#:
#: 为什么不每次请求都调 `resolve()`：那是每个静态请求一次目录扫描 + 一次
#: 版本戳比对，纯浪费；而固化目录在进程生命周期内不会变
#: （变了也说明有人手工动了它，重启即可）。
CACHE: Path | None = None


def get() -> Path:
    """带缓存的 `resolve()`。`server.py` 用它取 `ASSETS`。"""
    global CACHE
    if CACHE is None:
        CACHE = resolve()
    return CACHE
