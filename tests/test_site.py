import json
import os
import shutil
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_archive import prepare_library
from test_sync import fake_service, run_cli


def public_files(path):
    return {file.relative_to(path): file.read_bytes() for file in path.rglob("*") if file.is_file()}


@unittest.skipUnless(shutil.which("hugo"), "需要 Hugo 0.156+")
class SiteTest(unittest.TestCase):
    def test_restored_site_selection_and_empty_collection(self):
        with TemporaryDirectory() as folder, fake_service() as (endpoint, state), chdir(folder):
            root = Path(folder)
            _, ids = prepare_library(root, endpoint, state)
            self.assertEqual(run_cli(root, "edit", ids["101"], "--title", "纸风车手记 <旧稿>")[0], 0)
            self.assertEqual(run_cli(root, "tag", ids["101"], "--add", "通勤")[0], 0)
            archive = root / "archive.json"
            self.assertEqual(run_cli(root, "export", "--out", archive)[0], 0)
            root.joinpath("config.toml").unlink()
            env = {key: value for key, value in os.environ.items() if key != "MUSICARCH_NETEASE_COOKIE"}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(run_cli(root, "import", archive, db="restored.sqlite3")[0], 0)
                self.assertIn("netease:main:liked", run_cli(root, "site", "build", db="restored.sqlite3")[2])
                args = ("site", "build", "--collection", "netease:main:liked", "--collection", "netease:main:801",
                        "--base-url", "https://example.org/music/")
                first = run_cli(root, *args)
                self.assertEqual(first[0], 0, first)
                output = root / ".musicarch/public"
                original = public_files(output)
                catalog = root / "site/data/musicarch/catalog.json"
                first_data = catalog.read_bytes()
                second = run_cli(root, *args, db="restored.sqlite3")
                self.assertEqual(second[0], 0, second)
                self.assertEqual(original, public_files(output))
                self.assertEqual(first_data, catalog.read_bytes())
                page = (output / "index.html").read_text()
                self.assertIn('/music/catalog.css', page)
                self.assertIn('/music/catalog.js', page)
                self.assertIn('/music/analysis/', page)
                self.assertNotIn('analysis.js', page)
                self.assertNotIn('常见流派', page)
                analysis_page = (output / "analysis/index.html").read_text()
                self.assertIn('/music/analysis.js', analysis_page)
                self.assertIn('收藏轨迹', analysis_page)
                self.assertNotIn('catalog.js', analysis_page)
                self.assertIn('纸风车手记 &lt;旧稿&gt;', page)
                self.assertIn('https://music.163.com/song?id=101', page)
                for dimension in ("decade", "version", "language", "genre", "other"):
                    self.assertIn(f'id="tag-{dimension}" data-tag-filter', page)
                for marker in ("藏于远岸·未选歌名", "隐藏收藏", "私人账号歌单", "private-account-marker", "12345678901011121314", "netease:main:"):
                    self.assertNotIn(marker.encode(), first_data)
                    self.assertTrue(all(marker.encode() not in value for value in original.values()), marker)
                data = json.loads(first_data)
                recordings = data["recordings"]
                self.assertEqual(len(recordings), 9)
                analysis = json.loads((root / "site/data/musicarch/analysis.json").read_text())
                self.assertEqual((analysis["recordings"], analysis["collections"]), (9, 2))
                self.assertEqual(sum(segment["count"] for segment in analysis["languages"]), 9)
                self.assertEqual(analysis["timeline"], [{"year": 2024, "count": 1}])
                self.assertTrue(analysis["heatmap"]["rows"])
                main = next(index for index, row in enumerate(recordings) if row["title"].startswith("纸风车手记"))
                self.assertEqual(recordings[main]["artists"], ["林屿", "夏砚"])
                self.assertEqual([recordings[member["recording"]]["title"] for member in data["collections"][1]["members"]],
                                 ["晴窗", "纸风车手记 <旧稿>", "晴窗"])
                self.assertEqual([sum(member["recording"] == main for member in row["members"]) for row in data["collections"]], [2, 1])

                root.joinpath("config.toml").write_text('[site]\ncollections = ["netease:main:owned", "netease:main:801"]\n')
                (output / "old-generated.json").write_text("{}")
                self.assertEqual(run_cli(root, "site", "build", db="restored.sqlite3")[0], 0)
                self.assertFalse((output / "old-generated.json").exists())
                narrowed = json.loads(catalog.read_text())
                self.assertEqual(len(narrowed["recordings"]), 2)
                self.assertEqual([collection["name"] for collection in narrowed["collections"]], ["日常收藏"])
                self.assertNotIn('song?id=201', (output / "index.html").read_text())

            root.joinpath("config.toml").write_text(f'[accounts.netease.main]\nendpoint = "{endpoint}"\n')
            state["data"]["playlists"]["998"]["trackIds"].clear()
            with patch.dict(os.environ, {"MUSICARCH_NETEASE_COOKIE": "MUSIC_U=synthetic"}):
                self.assertEqual(run_cli(root, "sync", "netease", "--account", "main")[0], 0)
            self.assertEqual(run_cli(root, "site", "build", "--collection", "netease:main:liked")[0], 0)
            self.assertIn("所选歌单暂无歌曲", (output / "index.html").read_text())
            self.assertIn("所选歌单暂无歌曲", (output / "analysis/index.html").read_text())
