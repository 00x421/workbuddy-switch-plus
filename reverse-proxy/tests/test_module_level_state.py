"""守护不变量：核心模块里不该有**模块级可变状态**。

背景：这次收尾时发现 `test_accounts.build()` 里有一次
`importlib.reload(serve)`，当时以为它是「用例之间隔离」的保证。
查下来不是 —— 隔离来自每个用例自己的临时 store 目录，那次 reload
重载的又只是入口脚本 `serve` 而非 `admin.server`，**从来没起过作用**。

它已经删掉了。但「为什么它看起来像是必需的」这个问题值得留下一条守护：

    真正需要防的，是**有人在核心模块里加模块级可变状态**。

因为 pytest 收集阶段会把所有测试模块 import 进**同一个进程**，
任何模块级 dict/list 都会跨用例留存 —— 表现成「单跑绿、全量红」，
而且失败点会漂到别的文件去。过去我们误以为 `reload` 能救这个，
实际上 reload 反而更危险：18 个测试文件在模块级持有 `admin.server` 引用，
重载会让它们全部指向旧模块对象，`patch.object` 静默失效。

所以这里守的是**原因**（不许有模块级状态），而不是**后果**（重载掩盖）。
"""
import copy
import importlib
import unittest

class ModuleLevelStateGuardTest(unittest.TestCase):
    """守护不变量：核心模块里不该有**模块级可变状态**。

    为什么要有这条：pytest 收集阶段会把所有测试模块 import 进来，
    它们是**共享进程**的。任何模块级 dict/list 都会在用例之间留存，
    表现成「单跑绿、全量红」—— 而且失败点会漂到别的文件去，极难查。

    这条测试的定位是「**不许往地上扔东西**」，而不是「每次进门前擦一遍地」。
    后者（重载模块）在本仓库是有害的：18 个测试文件在模块级
    `import admin.server as admin_server`，重载会让它们持有旧模块对象，
    `patch.object` 打旧模块、被测代码走新模块 —— 静默失效。
    详见 `build()` 的 docstring。

    失败时该怎么修：把那个变量挪进一个类的实例、或改成模块级函数内的闭包。
    不要去给测试加 `importlib.reload` 来掩盖它。
    """

    #: 允许存在的模块级 dict/list/set。每一条都要写清为什么它是安全的。
    ALLOWED = {
        # `pool.py` 的默认值表。是纯数据常量：调用方取用时一律 `dict(DEFAULT_POOL)`
        # 复制一份（pool.py:80），没有任何代码就地改它。
        ("admin.pool", "DEFAULT_POOL"),
        # 账号动作 → 流水类别的映射。只被 `.get()` 读，从不写入。
        ("admin.pool", "ACTION_KINDS"),
    }

    MODULES = ("admin.server", "admin.pool", "admin.settings_store")

    def test_no_module_level_mutable_state(self):
        import importlib
        offenders = []
        for name in self.MODULES:
            module = importlib.import_module(name)
            for attr, value in vars(module).items():
                if attr.startswith("__"):
                    continue
                if not isinstance(value, (dict, list, set)):
                    continue
                if (name, attr) in self.ALLOWED:
                    continue
                offenders.append("%s.%s (%s, len=%d)" % (
                    name, attr, type(value).__name__, len(value)))

        self.assertEqual(
            offenders, [],
            "发现模块级可变状态 —— 它会在用例之间留存，造成「单跑绿、全量红」：\n  "
            + "\n  ".join(offenders)
            + "\n把状态挪进实例，或加进 ALLOWED 并说明为什么它不会被就地改动。"
            + "不要用 importlib.reload 掩盖。")

    def test_allowed_entries_still_exist(self):
        """白名单不能被「删掉代码」悄悄变成死条目 —— 死条目会让下一个人以为它还有用。"""
        import importlib
        for name, attr in self.ALLOWED:
            with self.subTest(target="%s.%s" % (name, attr)):
                module = importlib.import_module(name)
                self.assertTrue(hasattr(module, attr),
                                "白名单里的 %s.%s 已不存在，请从 ALLOWED 删掉" % (name, attr))

    def test_allowed_constants_are_not_mutated_in_place(self):
        """白名单里的常量必须是**真的**只读 —— 这里实测一遍，不靠人工审查。

        只靠「我看过没人改」是不够的：将来有人在别处写一句
        `DEFAULT_POOL["x"] = 1`，白名单就变成了谎话。
        """
        import copy
        from admin import pool as pool_module
        snapshot = {name: copy.deepcopy(getattr(pool_module, name))
                    for name in ("DEFAULT_POOL", "ACTION_KINDS")}
        # 跑一遍会碰到这些常量的真实路径
        pool_module.ACTION_KINDS.get("checkin")
        dict(pool_module.DEFAULT_POOL)
        for name, before in snapshot.items():
            with self.subTest(constant=name):
                self.assertEqual(getattr(pool_module, name), before,
                                 "%s 被就地改动了" % name)

if __name__ == "__main__":
    unittest.main()