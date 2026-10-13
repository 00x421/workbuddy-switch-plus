"""全局测试装置。**目前只做一件事：防止 `wb-runtime-writer` 线程跨用例泄漏。**

## 为什么必须有这个文件

`SwitchStore.__init__` 默认会起一个 **daemon** 线程（`start_runtime_writer()`），
每 60 秒把运行时长写进 `runtime.json`。设计上它必须一直活着（服务由 Tauri 拉起，
闲时也要记录运行时长），所以它**不会自己退出**。

问题出在测试：`setUp` 里 `SwitchStore(...)` 一建，线程就起来了，
而绝大多数用例从没调用 `stop_runtime_writer()`。于是线程活到**后续用例**里，
继续调 `os.replace`。

实测（`_hyact_work/prove_thread_leak.py`，三条独立证据）：
  · 连建 10 只 store        -> 10 个 `wb-runtime-writer` 存活
  · 1 秒内 `os.replace`     被这些线程调 4 次
  · 于是 `test_repeated_appends_do_not_rewrite_the_file_every_time`
    那条用「replace 被调了几次」当判据的测试，**单跑绿、全量红**

这是**测试隔离缺陷，不是产品缺陷**：产品里那一个线程本来就该长活。
所以修在测试侧，不动 `switch_store` 的默认行为。

## 为什么不逐个用例补 stop

那样是「靠记性」——下一个人再写用例照样会漏。
改成 autouse 装置后，**任何**用例（unittest 或裸函数）跑完都会自动清理，
不需要任何人记得。这才是结构性的修法。

## 顺带说明

只处理 `wb-runtime-writer` 这一个已知泄漏源。若将来还有别的后台线程泄漏，
`tests/test_module_level_state.py` 那类「守卫型测试」与本文件同样的思路
（默认值收窄 + 白名单写明理由）可以继续扩展。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_runtime_writer_default(monkeypatch):
    """**默认关掉**定期写入线程，除非用例显式要求。

    这是根治手段：既然线程是问题之源，就让测试默认不起它。

    · 绝大多数用例根本不关心 `runtime.json` 的定时刷新；
    · 少数关心的用例（`test_runtime_and_logs.py`）会自己设
      `WB_RUNTIME_WRITER=1` —— 用例内的设置在本装置之后执行，因而**优先**。
    · `SwitchStore.__init__` 本来就读这个开关（见 `switch_store.py` 里
      `os.environ.get("WB_RUNTIME_WRITER", "1")`），所以这里只是改测试环境的
      默认值，**没有改产品行为**。

    注意 `WB_RUNTIME_WRITER=0` 只关「定期线程」；`__init__` 末尾那次
    `write_runtime_file()` 仍会执行，所以「启动瞬间就该有 runtime.json」
    这个行为不受影响。
    """
    monkeypatch.setenv("WB_RUNTIME_WRITER", "0")
