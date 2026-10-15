"""目录浏览后端（web_dir_browser）单元测试：真实绝对路径、隐藏目录、中文空格、错误映射。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pycut import web_dir_browser


class ListDirTests(unittest.TestCase):
    def _make_tree(self, root: Path) -> dict[str, Path]:
        (root / "中文 空格").mkdir()
        (root / "Alpha").mkdir()
        (root / "beta").mkdir()
        (root / ".hidden").mkdir()
        (root / "file.txt").write_text("x", encoding="utf-8")
        return {
            "cn": root / "中文 空格",
            "alpha": root / "Alpha",
            "beta": root / "beta",
        }

    def test_lists_only_subdirs_hides_dot_and_returns_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            made = self._make_tree(root)
            result = web_dir_browser.list_dir(str(root), project_root=root)
            self.assertIsNone(result["error"])
            names = [d["name"] for d in result["dirs"]]
            self.assertEqual(names, ["Alpha", "beta", "中文 空格"])
            for d in result["dirs"]:
                self.assertTrue(Path(d["path"]).is_absolute())
            self.assertNotIn(".hidden", names)
            self.assertNotIn("file.txt", names)
            self.assertIn(str((root / "中文 空格").resolve()), [d["path"] for d in result["dirs"]])
            self.assertEqual(result["path"], str(root.resolve()))
            self.assertEqual(result["parent"], str(root.resolve().parent))

    def test_empty_path_starts_at_home(self):
        result = web_dir_browser.list_dir(None, project_root=Path.home())
        self.assertIsNone(result["error"])
        self.assertEqual(result["path"], str(Path.home()))

    def test_missing_path_returns_error_not_exception(self):
        with tempfile.TemporaryDirectory() as td:
            result = web_dir_browser.list_dir(str(Path(td) / "nope"), project_root=Path(td))
            self.assertEqual(result["error"], "目录不存在")
            self.assertEqual(result["dirs"], [])

    def test_file_path_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f = root / "file.txt"
            f.write_text("x", encoding="utf-8")
            result = web_dir_browser.list_dir(str(f), project_root=root)
            self.assertEqual(result["error"], "目录不存在")

    def test_permission_error_maps_to_access_message(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(web_dir_browser.os, "scandir", side_effect=PermissionError("denied")):
                result = web_dir_browser.list_dir(td, project_root=Path(td))
            self.assertEqual(result["error"], "无权访问该目录")

    def test_relative_path_resolves_against_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "sub").mkdir()
            result = web_dir_browser.list_dir("sub", project_root=root)
            self.assertEqual(result["path"], str((root / "sub").resolve()))

    def test_parent_is_none_at_filesystem_root(self):
        result = web_dir_browser.list_dir("/", project_root=Path.home())
        self.assertIsNone(result["error"])
        self.assertIsNone(result["parent"])


class ResolvePathTests(unittest.TestCase):
    def test_absolute_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(web_dir_browser.resolve_path("/tmp", root), Path("/tmp").resolve())

    def test_relative_against_project_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(web_dir_browser.resolve_path("a/b", root), (root / "a/b").resolve())

    def test_expands_user_and_vars(self):
        with mock.patch.dict(os.environ, {"FOO": "/tmp/foo"}, clear=False):
            resolved = web_dir_browser.resolve_path("$FOO", Path.home())
            self.assertEqual(resolved, Path("/tmp/foo").resolve())


class RootsTests(unittest.TestCase):
    def test_roots_include_home_and_existing_only(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            (home / "Desktop").mkdir()
            got = web_dir_browser.roots(home=home)
            names = [r["name"] for r in got]
            self.assertIn("主目录", names)
            self.assertIn("桌面", names)
            self.assertNotIn("文档", names)  # 不存在则不出现
            self.assertTrue(all(Path(r["path"]).is_dir() for r in got))


if __name__ == "__main__":
    unittest.main()
