"""HyactCrack 技术支持 — 验证静态资源固化（不依赖真 exe，纯逻辑测）

要证明的三条性质（对应 `admin/assets.py` 的设计目标）：
  1. **持久**：源目录（解包目录）消失后，仍能返回可用目录
  2. **自愈**：固化目录被删 / 被打断（残缺）后，下次解析会重新复制
  3. **可更新**：源内容变了，会重新固化，而不是永远用第一份

另外验证两条容易写错的地方：
  · 源目录里**删掉**的文件，固化目录里也必须消失（不能只做增量复制）
  · 源码运行（非 frozen）时**不**固化，直接用仓库里的 admin/static，
    否则改前端会"改了不生效"

这些是纯函数，不启动服务、不占端口。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin import assets  # noqa: E402


def make_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


class AssetsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.src = self.base / "src"
        self.dest = self.base / "static"
        self.env = mock.patch.dict(os.environ, {assets.ENV_OVERRIDE: str(self.dest)})
        self.env.start()
        self.addCleanup(self.env.stop)
        assets.CACHE = None
        self.addCleanup(lambda: setattr(assets, "CACHE", None))

    def patch_source(self, files: dict[str, str]):
        return mock.patch.object(assets, "source_dir", lambda: (
            make_tree(self.src, files), self.src)[1])

    def patch_frozen(self, on: bool = True):
        return mock.patch.object(assets, "frozen", lambda: on)


class PersistenceTest(AssetsFixture):
    def test_source_vanishing_does_not_break_resolution(self):
        """⭐ 核心：源目录（=解包目录）整个消失后，仍返回可用目录。

        这正是用户报的故障 —— 磁盘清理删了 %TEMP%\\_MEIxxxxx。
        """
        with self.patch_frozen(), self.patch_source(
                {"index.html": "<html>A</html>", "app.js": "console.log(1)"}):
            first = assets.resolve()
            self.assertEqual(first, self.dest)
            self.assertTrue((first / "index.html").exists())

            # 模拟磁盘清理：源目录整个删掉
            import shutil
            shutil.rmtree(self.src)
            self.assertFalse(self.src.exists())

            assets.CACHE = None
            second = assets.resolve()
        self.assertEqual(second, self.dest,
                         "源目录消失后没能继续用固化目录 —— 页面会 404")
        self.assertTrue((second / "index.html").exists())
        self.assertEqual((second / "index.html").read_text(encoding="utf-8"),
                         "<html>A</html>")

    def test_served_files_match_source(self):
        files = {"index.html": "<html>X</html>", "app.js": "let a=1",
                 "sub/deep.txt": "deep"}
        with self.patch_frozen(), self.patch_source(files):
            got = assets.resolve()
        for rel, text in files.items():
            self.assertEqual((got / rel).read_text(encoding="utf-8"), text, rel)


class SelfHealTest(AssetsFixture):
    def test_deleted_dest_is_rebuilt(self):
        """固化目录被用户误删 -> 下次解析自动重建。"""
        import shutil
        with self.patch_frozen(), self.patch_source({"index.html": "<html>B</html>"}):
            self.assertTrue((assets.resolve() / "index.html").exists())
            shutil.rmtree(self.dest)
            assets.CACHE = None
            got = assets.resolve()
        self.assertTrue((got / "index.html").exists(), "固化目录被删后没自愈")

    def test_partial_dest_is_rebuilt(self):
        """⭐ 只有版本戳、index.html 没了（复制被中断 / 被删一半）。

        只看版本戳的实现会在这里翻车：它认为"已经固化过了"，
        于是返回一个缺 index.html 的目录 —— 首页 404。
        """
        with self.patch_frozen(), self.patch_source({"index.html": "<html>C</html>"}):
            got = assets.resolve()
            (got / "index.html").unlink()          # 模拟删到一半
            self.assertTrue((got / assets.STAMP_NAME).exists(),
                            "前置条件：版本戳还在")
            assets.CACHE = None
            again = assets.resolve()
        self.assertTrue((again / "index.html").exists(),
                        "残缺的固化目录没有被重建 —— 首页会 404")

    def test_stale_extra_files_are_removed(self):
        """固化目录里的**多余**文件要被清掉。

        增量复制会让"本地已删除的 JS"永远留在固化目录里，
        表现为线上还在加载一个不存在的文件，极难排查。
        """
        with self.patch_frozen(), self.patch_source(
                {"index.html": "<html>D</html>", "old.js": "OLD"}):
            got = assets.resolve()
            self.assertTrue((got / "old.js").exists())

        # 源里删掉 old.js
        (self.src / "old.js").unlink()
        assets.CACHE = None
        with self.patch_frozen(), self.patch_source({"index.html": "<html>D</html>"}):
            again = assets.resolve()
        self.assertFalse((again / "old.js").exists(),
                         "源里已删除的文件仍留在固化目录里")


class UpdateTest(AssetsFixture):
    def test_changed_content_triggers_recopy(self):
        """⭐ 源内容变了要重新固化 —— 否则升级 exe 后页面还是旧的。"""
        with self.patch_frozen(), self.patch_source({"index.html": "<html>v1</html>"}):
            got = assets.resolve()
            self.assertIn("v1", (got / "index.html").read_text(encoding="utf-8"))

        # 改内容（注意 mtime 精度：写完确保时间戳变化）
        import time
        time.sleep(1.1)
        with self.patch_frozen(), self.patch_source({"index.html": "<html>v2</html>"}):
            assets.CACHE = None
            again = assets.resolve()
        self.assertIn("v2", (again / "index.html").read_text(encoding="utf-8"),
                      "内容变了但没重新固化 —— 升级后页面停在旧版")


class SourceRunTest(AssetsFixture):
    def test_source_mode_does_not_freeze(self):
        """源码运行**不**固化：直接用仓库里的 admin/static。

        固化会让"改完前端不生效"，开发时极其困扰。
        """
        with self.patch_frozen(False):
            got = assets.resolve()
        self.assertNotEqual(got, self.dest,
                            "源码运行时不该固化到 exe 同级")
        self.assertTrue(got.is_dir(), f"源码模式返回的目录不存在：{got}")
        self.assertTrue((got / "index.html").exists(),
                        "源码模式的静态目录里没有 index.html")

    def test_source_dir_points_at_repo_static(self):
        with self.patch_frozen(False):
            self.assertEqual(assets.source_dir().name, "static")
            self.assertTrue(assets.source_dir().is_dir())


class FallbackTest(AssetsFixture):
    def test_unreadable_source_falls_back_without_crashing(self):
        """源目录读不到时**不抛异常**，退回旧行为。

        服务起不来是比"页面 404"严重得多的故障，
        所以这里的取舍是：宁可返回一个可能不对的路径，也不能让进程崩。
        """
        with self.patch_frozen(), self.patch_source({}):
            import shutil
            shutil.rmtree(self.src, ignore_errors=True)
            got = assets.resolve()
        self.assertIsInstance(got, Path)          # 不抛就是通过

    def test_readonly_dest_falls_back_to_source(self):
        """固化目录不可写时退回源目录，而不是返回一个空目录。"""
        with self.patch_frozen(), self.patch_source({"index.html": "<html>E</html>"}):
            with mock.patch.object(assets, "_writable_dir", lambda: None):
                got = assets.resolve()
        self.assertEqual(got, self.src)


class CacheTest(AssetsFixture):
    def test_get_caches_result(self):
        """`get()` 要缓存 —— 请求路径上不该反复扫目录。"""
        with self.patch_frozen(), self.patch_source({"index.html": "<html>F</html>"}):
            a = assets.get()
            b = assets.get()
        self.assertIs(a, b)

    def test_assets_dir_has_what_server_needs(self):
        """server.py 用到的文件都得在解析出的目录里。

        这条是防"改了固化逻辑、忘了某个文件"——静态资源缺一个
        就是某个页面白屏或控制台一条 404。
        """
        with self.patch_frozen(False):
            got = assets.resolve()
        for name in ("index.html", "app.js", "style.css"):
            self.assertTrue((got / name).exists(), f"静态资源缺文件：{name}")


if __name__ == "__main__":
    unittest.main()
