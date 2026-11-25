import json
import os
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from musicarch.archive import validator
from musicarch.catalog import display_fields, recording_sources, recording_tags, read_overrides
from musicarch.store import bind_account, open_db, transaction

from test_sync import fake_service, run_cli


def prepare_library(root, endpoint, state):
    root.joinpath("config.toml").write_text(f'[accounts.netease.main]\nendpoint = "{endpoint}"\n')
    tracks = state["data"]["songs"]
    duet = tracks["101"]
    tracks.update({
        "201": {**duet, "id": 201, "ar": duet["ar"][::-1], "dt": 210900,
                "al": {"id": 12, "name": "再发行", "picUrl": "https://p1.music.126.net/reissue.jpg"}, "publishTime": "2023-02"},
        "202": {"id": 202, "name": "纸风车", "ar": [{"name": "不同艺人"}], "dt": 211200},
        "203": {**duet, "id": 203, "name": "纸风车（Live）", "isrc": "XXABC2400002"},
        "204": {**tracks["102"], "id": 204, "name": "　晴 窗　", "dt": 203600},
        "205": {**tracks["102"], "id": 205, "name": " 晴　窗", "dt": 204300},
        "206": {"id": 206, "name": "桥上", "ar": [{"name": "许枫"}], "dt": 225100},
        "207": {"id": 207, "name": "桥上", "ar": [{"name": "许枫"}], "dt": 225300},
    })
    liked = state["data"]["playlists"]["998"]
    liked["trackIds"].extend({"id": number} for number in range(201, 208))
    liked["trackCount"] = len(liked["trackIds"])
    args = ("sync", "netease", "--account", "main", "--include", "liked,owned,subscribed")
    with patch.dict(os.environ, {"MUSICARCH_NETEASE_COOKIE": "MUSIC_U=synthetic"}):
        result = run_cli(root, *args)
        if result[0]:
            raise AssertionError(result)
    with open_db(root / "library.sqlite3") as db:
        with transaction(db):
            bind_account(db, "netease", "12345678901011121314", "private-account-marker")
            db.execute("INSERT INTO collections VALUES ('netease', '12345678901011121314', '803', 'owned', '私人账号歌单', 'private', 'active', '803')")
            db.execute("INSERT INTO memberships VALUES ('netease', '12345678901011121314', '803', 0, '199', NULL)")
        ids = {row["track_id"]: row["recording_id"] for row in db.execute("SELECT * FROM sources")}
    return args, ids


class ArchiveTest(unittest.TestCase):
    def assert_ok(self, result):
        self.assertEqual(result[0], 0, result)
        return result[1]

    def test_catalog_review_annotations_and_roundtrip(self):
        with TemporaryDirectory() as folder, fake_service() as (endpoint, state):
            root = Path(folder)
            args, ids = prepare_library(root, endpoint, state)
            self.assertEqual(ids["101"], ids["201"])
            self.assertEqual(len({ids[key] for key in ("101", "202", "203", "204", "205")}), 5)
            candidates = self.assert_ok(run_cli(root, "review"))
            self.assertIn(ids["204"], candidates)
            self.assertNotIn(ids["203"], candidates)
            self.assert_ok(run_cli(root, "review", "separate", ids["204"], ids["205"]))
            self.assertNotIn(ids["204"], self.assert_ok(run_cli(root, "review")))
            self.assert_ok(run_cli(root, "edit", ids["206"], "--title", "桥上甲"))
            self.assert_ok(run_cli(root, "edit", ids["207"], "--title", "桥上乙"))
            self.assertIn("title", run_cli(root, "review", "merge", ids["206"], ids["207"])[2])
            self.assert_ok(run_cli(root, "edit", ids["207"], "--reset", "title"))
            self.assert_ok(run_cli(root, "tag", ids["207"], "--add", "迁移标签"))
            self.assert_ok(run_cli(root, "review", "merge", ids["206"], ids["207"]))
            self.assert_ok(run_cli(root, "tag", ids["207"], "--add", "老 ID 也可用"))
            rid = ids["101"]
            self.assert_ok(run_cli(root, "edit", rid, "--title", "纸风车手记", "--language", "中文"))
            self.assert_ok(run_cli(root, "tag", rid, "--add", "通勤", "--add", " Quiet  Walk ", "--remove", "quiet walk"))
            self.assert_ok(run_cli(root, "tag", rid, "--dimension", "genre", "--remove", "民谣"))
            with patch.dict(os.environ, {"MUSICARCH_NETEASE_COOKIE": "MUSIC_U=synthetic"}):
                self.assert_ok(run_cli(root, *args))
            with open_db(root / "library.sqlite3") as db:
                sources = recording_sources(db, rid)
                metadata, origins = display_fields(sources, read_overrides(db, rid))
                tags = recording_tags(metadata, origins, list(db.execute("SELECT * FROM tags WHERE recording_id = ?", (rid,))))
                self.assertEqual([artist["name"] for artist in metadata["artists"]], ["林屿", "夏砚"])
                self.assertEqual((metadata["title"], metadata["release_date"], metadata["language"]), ("纸风车手记", "1997", "中文"))
                self.assertEqual((origins["title"]["kind"], origins["release_date"]["kind"]), ("manual", "album_detail"))
                self.assertEqual({json.loads(row["metadata"])["release_date"] for row in sources}, {"1997", "2023-02"})
                self.assertIn("通勤", {row["label"] for row in tags})
                self.assertNotIn("民谣", {row["label"] for row in tags})
                self.assertTrue(db.execute("SELECT 1 FROM tags WHERE recording_id = ? AND norm = 'quiet walk' AND action = 'remove'", (rid,)).fetchone())
            original = root / "original.json"
            restored = root / "restored.json"
            self.assert_ok(run_cli(root, "export", "--out", original))
            offline_env = {key: value for key, value in os.environ.items() if key != "MUSICARCH_NETEASE_COOKIE"}
            root.joinpath("config.toml").unlink()
            with patch.dict(os.environ, offline_env, clear=True):
                self.assert_ok(run_cli(root, "import", original, db="restored.sqlite3"))
                self.assert_ok(run_cli(root, "export", "--out", restored, db="restored.sqlite3"))
                self.assertEqual(original.read_bytes(), restored.read_bytes())
                self.assertIn("新增 0 条记录", self.assert_ok(run_cli(root, "import", original, db="restored.sqlite3")))

            broken = root / "broken.json"
            data = json.loads(original.read_bytes())
            validator().check_schema(validator().schema)
            validator().validate(data)
            dangling = deepcopy(data)
            dangling["memberships"][0]["source"][-1] = "9999"
            broken.write_text(json.dumps(dangling))
            self.assertIn("$.memberships[0].source", run_cli(root, "import", broken, db="restored.sqlite3")[2])
            version = data | {"schema_version": 2}
            broken.write_text(json.dumps(version))
            self.assertIn("$.schema_version", run_cli(root, "import", broken, db="restored.sqlite3")[2])
            broken.write_text('{"format":"musicarch","format":"musicarch"}')
            self.assertIn("对象键重复", run_cli(root, "import", broken, db="restored.sqlite3")[2])

            conflict = deepcopy(data)
            conflict["accounts"].append({"platform": "netease", "account_id": "9999", "alias": "new-planned"})
            conflict["memberships"][0]["source"][-1] = "103"
            broken.write_text(json.dumps(conflict))
            self.assertIn("集合成员", run_cli(root, "import", broken, db="restored.sqlite3")[2])
            self.assert_ok(run_cli(root, "export", "--out", restored, db="restored.sqlite3"))
            self.assertEqual(original.read_bytes(), restored.read_bytes())
