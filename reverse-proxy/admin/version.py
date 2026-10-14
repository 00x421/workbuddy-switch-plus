"""反代服务的版本号。

**为什么需要一个专门的模块，而不是散在 server.py 里写个常量：**

这个服务是**独立部署**的 —— 它跟着桌面端走同一个版本号，但可以单独更新
（见 `_hyact_work/服务器更新步骤.md`：只换一个 exe）。所以"服务端版本"和
"桌面端版本"是两个必须能分别确认的事实：用户报障时第一句话要问的就是
"你服务器上是哪个版本"，而这个答案不能靠"我传的是哪个包"来猜 ——
必须是运行中的服务自己说得出来。

## 版本是怎么进到 exe 里的（这里踩过一次坑）

**打包时的环境变量不会留在 exe 里。** 第一版把版本通过 `env[VERSION_ENV]`
传给 PyInstaller，源码模式读得到、打包后却永远是 `dev` —— 因为
`os.environ` 是**运行期**读的，构建时设的值只影响构建进程，不会写进产物。

实测确认过这个边界：
  · 源码直跑 + 环境变量          -> 读得到
  · exe + **运行期**环境变量     -> 读得到
  · exe（构建时设过环境变量）    -> 读不到，回落 dev

所以正确做法是**随包带一个 `version.json`**（在 spec 的 datas 里），
运行期从冻结目录里读。环境变量保留为**覆盖手段**（部署时想临时标一个
"测试构建"可以直接设），但不再是唯一来源。

读取顺序：
  1. 环境变量 `WB_REVERSE_PROXY_VERSION`（运行期覆盖，最高优先级）
  2. 随包的 `version.json`（打包时写入）
  3. `"dev"`（源码直跑）

读不到时返回 `"dev"` 而不是空字符串或猜测值 —— "dev" 是一个诚实的答案，
空值会被前端渲染成"版本："后面什么都没有，看起来像 bug。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

#: 运行期覆盖用的环境变量名。build-exe.py 与这里必须一致。
VERSION_ENV = "WB_REVERSE_PROXY_VERSION"

#: 随包携带的版本文件（spec 的 datas 里打进冻结目录）。
VERSION_FILE = "version.json"

#: 读不到任何来源时的兜底。故意用 "dev" 而不是 "0.0.0"：
#: 后者看起来像一个真实但很旧的版本，会让人去查根本不存在的升级。
FALLBACK = "dev"


def _bundle_root() -> Path:
    """冻结后的资源根目录。

    PyInstaller onefile 会把 datas 解到 `sys._MEIPASS`（临时目录）；
    源码模式下没有这个属性，就在本文件旁边找。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


def _from_env() -> str:
    return (os.environ.get(VERSION_ENV) or "").strip()


def _from_file() -> str:
    """读随包的 version.json。任何异常都当作"没有"，不向上抛。

    这里刻意吞掉异常：版本读不到是**降级**，不是故障。为了显示版本号
    而让整个服务起不来，代价远大于收益。
    """
    for candidate in (_bundle_root() / VERSION_FILE,
                      Path(__file__).resolve().parent / VERSION_FILE):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = str(data.get("version") or "").strip()
        if value:
            return value
    return ""


def _resolve() -> str:
    # 环境变量优先：部署时可以临时标一个测试构建，不用重新打包。
    return _from_env() or _from_file() or FALLBACK


#: 进程生命周期内不会变，模块加载时算一次即可。
__version__ = _resolve()


def version() -> str:
    """当前反代服务版本。"""
    return __version__


def is_dev() -> bool:
    """是否跑在未注入版本的源码模式（供界面上做区分显示）。"""
    return __version__ == FALLBACK


def source() -> str:
    """版本号是从哪来的（"env" / "file" / "fallback"）。

    排查"为什么显示 dev"时，这是第一个要看的东西 ——
    没有它就只能猜是注入失败还是读取失败。
    """
    if _from_env():
        return "env"
    if _from_file():
        return "file"
    return "fallback"
