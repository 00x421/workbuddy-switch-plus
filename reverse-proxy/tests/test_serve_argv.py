"""回归：serve.py 的命令行参数必须真的生效。

原来的写法是模块级 `app = build_app()`（空 argv），于是 `--data` / `--accounts`
被静默吞掉——store 记的是默认端口与**真实账号库**，uvicorn 却听 args.port。
表现是「命令行指定了隔离账号库，面板里还是线上那批账号」。
这个 bug 是端到端验证发现的：一次本该完全隔离的验证读到了真实账号。
留在测试里，因为它的症状（读错账号库）比原因（漏传 argv）危险得多。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN = "wb-verify-admin-key-long-enough-1234"


class ServeArgvTest(unittest.TestCase):
    def test_data_and_accounts_flags_reach_the_store(self):
        tmp = Path(tempfile.mkdtemp(prefix="wb-argv-"))
        accounts = tmp / "accounts.json"
        record = [{"id": "x1", "name": "ISOLATED", "uid": "ISO-1", "enabled": True,
                   "status": "ok", "refresh_token": "r", "access_token": "a"}]
        accounts.write_text(json_dumps(record), encoding="utf-8")
        code = SERVECODE % (tmp / "data", accounts)
        # encoding 必须显式给 utf-8：Windows 默认是 GBK，而子进程会输出中文
        # （启动横幅、报错文案），用 text=True 直接解码会在读线程里抛
        # UnicodeDecodeError，症状是 stderr/stdout 变成 None、断言报
        # "'NoneType' object is not subscriptable" —— 完全看不出真正的原因。
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "ADMIN_KEY": ADMIN})
        self.assertEqual(proc.returncode, 0, (proc.stderr or "")[-800:])
        out = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        self.assertEqual(out[0], str(accounts), "store 没读到我们的隔离账号库")
        self.assertEqual(out[1], "1", "账号数不对：说明读的不是隔离库")
        self.assertIn("8899", out[2], "端口没生效")

    def test_app_is_not_built_at_import_time_with_empty_argv(self):
        """守住根因：模块顶层不能再出现 `app = build_app()`。

        那行会在**任何** import 时用空 argv 建一个 app，把真实账号库读进来。
        它是纯粹的多余物——main() 自己会建。这里直接断言它没回来。
        """
        source = (ROOT / "serve.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotEqual(
                stripped, "app = build_app()",
                "模块顶层又出现了 app = build_app()（空 argv，会读真实账号库）")


def json_dumps(value):
    import json
    return json.dumps(value)


SERVECODE = (
    "import sys, serve\n"
    "app = serve.build_app(['--port', '8899', '--data', r'%s', '--accounts', r'%s'])\n"
    "b = app.state.banner\n"
    "print(b['accounts_path'])\n"
    "print(b['accounts'])\n"
    "print(b['base'])\n"
)


if __name__ == "__main__":
    unittest.main()
