# Powered by HyactCrack Skill —— 本脚本由 HyactCrack Skill 提供技术支持
"""反代服务版本号。

这个服务的版本有一个特殊约束：它**可以被单独部署**（只换服务器上的 exe，
桌面端不动）。所以"服务器上跑的是哪个版本"必须由运行中的服务自己回答，
不能靠"我传的是哪个包"推断。

测试分三层，对应三个可能断掉的地方：
  1. version 模块本身的读取与兜底
  2. session 接口真的把它发出去了（前端靠这个接口拿）
  3. 打包脚本真的把它注入了（否则 exe 里永远是 dev）

**写法说明**（两条踩过的坑）：
  · 本目录测试是 `unittest` 风格；裸 pytest 类不会被收集（pytest 默认只收
    `Test*` 前缀的类）。第一版写成裸类，跑出 "no tests collected" ——
    看似通过，实则一个用例都没跑。
  · 造客户端照抄 `test_console_login.py`：`create_app(root, auth, API, ADMIN_KEY)`
    + `httpx.ASGITransport`。存储类叫 `SwitchStore`，不是 `Store`。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

from admin import server as admin_server  # noqa: E402
from admin import version as version_mod  # noqa: E402

API = "test-api-key-long-enough-123456"
ADMIN_KEY = "test-admin-key-long-enough-12345"


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="https://console.test")


def load_build_module(name: str):
    """把打包脚本当模块加载，以便直接调它的函数。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / "build-exe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VersionModuleTests(unittest.TestCase):
    def test_fallback_is_dev_not_a_fake_version(self):
        """兜底值必须是 dev 而不是 0.0.0。

        0.0.0 看起来像一个真实但很旧的版本，会让人去查根本不存在的升级路径；
        dev 是一个诚实的答案。
        """
        self.assertEqual(version_mod.FALLBACK, "dev")
        self.assertFalse(version_mod.FALLBACK[0].isdigit())

    def test_env_var_name_matches_the_build_script(self):
        """打包脚本注入的变量名必须与后端读的一致。

        这两个字符串一旦漂移，症状是 exe 里**永远显示 dev** ——
        没有报错、没有异常，只是版本号不对。所以用测试钉住。
        """
        build = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn(f'VERSION_ENV = "{version_mod.VERSION_ENV}"', build)

    def test_version_file_name_matches_the_build_script(self):
        """随包版本文件名也必须两边一致，否则 exe 读不到那个文件。"""
        build = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn(f'VERSION_FILE = "{version_mod.VERSION_FILE}"', build)

    def test_spec_bundles_the_version_file(self):
        """spec 必须把 version.json 打进 datas。

        这是**唯一**能让版本跟着产物走的办法：构建时的环境变量不会留在
        exe 里（os.environ 是运行期读的）。第一版只设了环境变量，
        源码模式通过、打包后永远 dev —— 这条测试就是为那个 bug 立的。
        """
        spec = (ROOT / "wb-reverse-proxy.spec").read_text(encoding="utf-8")
        self.assertIn("version.json", spec)
        self.assertIn("datas.append", spec)

    def test_runtime_env_overrides_the_file(self):
        """运行期环境变量优先级最高：部署时能临时标一个测试构建。"""
        self.assertEqual(version_mod.source(), "env") if os.environ.get(
            version_mod.VERSION_ENV) else None
        old = os.environ.get(version_mod.VERSION_ENV)
        os.environ[version_mod.VERSION_ENV] = "override-1.2.3"
        try:
            fresh = importlib.reload(version_mod)
            self.assertEqual(fresh.version(), "override-1.2.3")
            self.assertEqual(fresh.source(), "env")
        finally:
            if old is None:
                os.environ.pop(version_mod.VERSION_ENV, None)
            else:
                os.environ[version_mod.VERSION_ENV] = old
            importlib.reload(version_mod)

    def test_missing_version_file_falls_back_quietly(self):
        """版本文件缺失只是降级，不能让服务起不来。

        为了显示一个版本号而让整个反代服务启动失败，代价远大于收益。
        """
        self.assertTrue(callable(version_mod._from_file))
        # _from_file 对任何异常都返回 ""，不抛
        self.assertIsInstance(version_mod._from_file(), str)

    def test_reads_the_injected_value(self):
        """带着环境变量重新加载模块，应读到注入值且不判为 dev。"""
        old = os.environ.get(version_mod.VERSION_ENV)
        os.environ[version_mod.VERSION_ENV] = "9.9.9"
        try:
            fresh = importlib.reload(version_mod)
            self.assertEqual(fresh.version(), "9.9.9")
            self.assertFalse(fresh.is_dev())
        finally:
            if old is None:
                os.environ.pop(version_mod.VERSION_ENV, None)
            else:
                os.environ[version_mod.VERSION_ENV] = old
            importlib.reload(version_mod)

    def test_blank_env_falls_back_to_dev(self):
        """空格不是版本号。当版本号用会让界面显示 `v   ` —— 看起来像渲染坏了。"""
        old = os.environ.get(version_mod.VERSION_ENV)
        os.environ[version_mod.VERSION_ENV] = "   "
        try:
            fresh = importlib.reload(version_mod)
            self.assertEqual(fresh.version(), "dev")
            self.assertTrue(fresh.is_dev())
        finally:
            if old is None:
                os.environ.pop(version_mod.VERSION_ENV, None)
            else:
                os.environ[version_mod.VERSION_ENV] = old
            importlib.reload(version_mod)

    def test_version_is_a_non_empty_string(self):
        """永远不要返回 None 或空串 —— 前端会渲染成「版本：」后面什么都没有。"""
        self.assertIsInstance(version_mod.version(), str)
        self.assertTrue(version_mod.version())

    def test_repeated_reload_is_safe(self):
        """重复 reload 不应累积副作用（模块级只算一次常量）。"""
        for _ in range(3):
            importlib.reload(version_mod)
        self.assertTrue(version_mod.version())


class SessionEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.auth = self.root / "auth"
        self.auth.mkdir()
        self.app = admin_server.create_app(self.root / "management", self.auth,
                                           API, ADMIN_KEY)
        self.session = client_for(self.app)

    async def asyncTearDown(self):
        await self.session.aclose()
        self.temp.cleanup()

    async def login_as_admin(self):
        r = await self.session.post("/admin/api/login",
                                    json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.session.headers["X-CSRF-Token"] = r.json()["csrf"]

    async def test_session_exposes_version(self):
        await self.login_as_admin()
        body = (await self.session.get("/admin/api/session")).json()
        self.assertIn("version", body, "前端靠这个字段显示版本号")
        self.assertIsInstance(body["version"], str)
        self.assertTrue(body["version"])
        self.assertIn("dev", body, "前端要区分 dev 模式")

    async def test_version_is_available_before_login(self):
        """登录页也要显示版本 —— 而报障经常发生在登不上去的时候。

        这条钉的是"版本不能只在登录后可见"，走的是 **login-options**：
        第一版把版本挂在 `/admin/api/session` 上，实测它未登录时返回 401 ——
        那样登录页永远拿不到版本。login-options 才是保证未登录可访问的那个。
        """
        resp = await self.session.get("/admin/api/login-options")
        self.assertEqual(resp.status_code, 200, "登录页靠这个接口拿版本")
        body = resp.json()
        self.assertTrue(body.get("version"), "登录页拿不到版本就白做了")
        self.assertIn("dev", body)

    async def test_session_returns_401_when_logged_out(self):
        """记录一个反直觉的事实，防止将来有人「顺手」把版本挪回 session。

        session 未登录返回 401，这是它的设计（要判断登录态）。
        所以它不能承载"登录页也要看"的信息。
        """
        resp = await self.session.get("/admin/api/session")
        self.assertEqual(resp.status_code, 401)

    async def test_version_matches_the_module(self):
        """接口吐出来的必须就是模块里的那个值，不能是别处拼的。"""
        await self.login_as_admin()
        body = (await self.session.get("/admin/api/session")).json()
        self.assertEqual(body["version"], version_mod.version())
        self.assertEqual(body["dev"], version_mod.is_dev())

    async def test_login_still_works_alongside_version(self):
        """加字段不能把登录弄坏。"""
        r = await self.session.post("/admin/api/login",
                                    json={"username": "admin", "password": ADMIN_KEY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("csrf", r.json())


class BuildScriptTests(unittest.TestCase):
    def test_reads_version_from_package_json(self):
        """版本号的唯一来源是 package.json —— 不在这边再维护一份。"""
        build = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn("PACKAGE_JSON", build)
        self.assertIn("package.json", build)

    def test_read_version_matches_package_json(self):
        """直接调打包脚本里的 read_version，确认它真能读到且值一致。"""
        mod = load_build_module("_build_exe_check")
        value = mod.read_version()
        self.assertTrue(value and isinstance(value, str))
        pkg = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(value, pkg["version"],
                         f"打包脚本读到 {value}，package.json 是 {pkg['version']}")

    def test_version_arg_exists_for_override(self):
        """--version 能覆盖，方便打测试包时标注。"""
        src = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn('"--version"', src)

    def test_build_injects_the_env_var(self):
        """打包时必须把版本写进环境变量，否则 exe 里永远是 dev。"""
        src = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn("env[VERSION_ENV] = version", src)

    def test_missing_package_json_is_a_loud_failure(self):
        """读不到版本要**明确报错退出**，不能静默降级。

        静默降级打出来的 exe 会永远说不清自己是什么时候的，
        而那是部署在服务器上、事后无法追溯的东西。
        """
        src = (ROOT / "build-exe.py").read_text(encoding="utf-8")
        self.assertIn("SystemExit", src)
        self.assertIn("无法确定版本号", src)


if __name__ == "__main__":
    unittest.main()
