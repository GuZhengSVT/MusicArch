import io
import json
import os
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs
from unittest.mock import patch

from musicarch.cli import main
from musicarch.providers import Page, ProviderError
from musicarch.providers.netease import Netease
from musicarch.store import open_db
from musicarch.sync import sync


@contextmanager
def fake_service():
    fixture = json.loads((Path(__file__).parent / "fixtures/netease.json").read_text())
    state = {"data": fixture, "requests": [], "failures": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            query = {key: values[0] for key, values in parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
            state["requests"].append((self.path, query))
            fixture = state["data"]
            if self.headers.get("Cookie") != "MUSIC_U=synthetic" or not self.headers.get("X-Apicache-Bypass"):
                body = {"code": 301}
            elif self.path in state["failures"]:
                body = {"code": state["failures"][self.path]}
            else:
                match self.path:
                    case "/user/account":
                        body = fixture["account"]
                    case "/likelist":
                        ids = state.get("liked_ids", [row["id"] for row in fixture["playlists"]["998"]["trackIds"]])
                        body = {"code": 200, "ids": ids, "count": len(ids)}
                    case "/user/playlist":
                        offset = int(query["offset"])
                        end = offset + state.get("page_size", 2)
                        body = {"code": 200, "playlist": fixture["directory"][offset:end],
                                "more": end < len(fixture["directory"]), "count": len(fixture["directory"])}
                    case "/playlist/detail":
                        body = {"code": 200, "playlist": fixture["playlists"].get(query["id"])}
                    case "/song/detail":
                        songs = fixture["songs"]
                        ids = query["ids"].split(",")
                        body = {"code": 200, "songs": [songs[item] for item in reversed(ids) if item in songs],
                                "privileges": state.get("privileges", [])}
                    case "/album":
                        body = {"code": 200, "album": fixture["albums"].get(query["id"])}
                    case "/song/wiki/summary":
                        body = {"code": 200, "data": {"blocks": state.get("wiki", {}).get(query["id"], [])}}
                    case _:
                        body = {"code": 404}
            self.send_response(301 if body["code"] == 301 else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", state
        finally:
            server.shutdown()
            worker.join()


def run_cli(root, *args, db="library.sqlite3"):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(["--db", str(root / db), "--config", str(root / "config.toml"), *map(str, args)])
    return code, stdout.getvalue(), stderr.getvalue()


def member_ids(db, key):
    return [row[0] for row in db.execute("SELECT track_id FROM memberships WHERE collection_key = ? ORDER BY position", (key,))]


class PagedNetease(Netease):
    """Expose the same adapter response over two pages to exercise the sync contract."""

    fail_second = False

    def read_members(self, account_id, collection, offset):
        if offset == 2 and self.fail_second and collection.key == "liked":
            raise ProviderError("network", "模拟第二页失败")
        page = super().read_members(account_id, collection, offset)
        return Page(page.items[offset:offset + 2], page.total,
                    offset + 2 if offset + 2 < len(page.items) else None)


class SyncTest(unittest.TestCase):
    def test_default_owned_sync_counts_cloud_members_and_enriches_unique_tracks(self):
        with TemporaryDirectory() as folder, fake_service() as (endpoint, state):
            root = Path(folder)
            root.joinpath("config.toml").write_text(f'[accounts.netease.main]\nendpoint = "{endpoint}"\n')
            playlist = state["data"]["playlists"]["801"]
            playlist["trackCount"] = 2
            playlist["cloudTrackCount"] = 1
            playlist["trackIds"][1] = {"id": 104, "t": 1}
            state["data"]["songs"]["104"] = {"id": 104, "name": "本地上传歌曲", "t": 1}
            state["wiki"] = {"102": [{"code": "SONG_PLAY_ABOUT_SONG_BASIC", "creatives": [
                {"creativeType": "language", "uiElement": {"textLinks": [{"text": "日语"}]}},
                {"creativeType": "songTag", "resources": [
                    {"resourceType": "melody_style", "uiElement": {"mainTitle": {"title": "流行"}}},
                    {"resourceType": "melody_style", "uiElement": {"mainTitle": {"title": "摇滚"}}}]},
                {"creativeType": "songBizTag", "resources": [
                    {"resourceType": "melody_style", "uiElement": {"mainTitle": {"title": "欢快"}}}]},
            ]}]}
            with patch.dict(os.environ, {"MUSICARCH_NETEASE_COOKIE": "MUSIC_U=synthetic"}):
                first = run_cli(root, "sync", "netease", "--account", "main")
                self.assertEqual(first[0], 0, first)
                second = run_cli(root, "sync", "netease", "--account", "main")
                self.assertEqual(second[0], 0, second)
            wiki_calls = [query["id"] for route, query in state["requests"] if route == "/song/wiki/summary"]
            self.assertEqual(wiki_calls, ["102"])
            with open_db(root / "library.sqlite3") as db:
                self.assertEqual(member_ids(db, "801"), ["102", "104", "102"])
                self.assertEqual(db.execute("SELECT count(*) FROM collections").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 4)
                self.assertIsNone(db.execute("SELECT url FROM sources WHERE track_id = '104'").fetchone()[0])
                tags = json.loads(db.execute("SELECT metadata FROM sources WHERE track_id = '102'").fetchone()[0])
                self.assertEqual((tags["language"], tags["genre"]), ("日语", "流行 / 摇滚"))

    def test_complete_directory_can_exceed_requested_limit(self):
        with fake_service() as (endpoint, state):
            directory = state["data"]["directory"]
            directory.extend({"id": 1000 + index, "name": "合成歌单", "userId": 901} for index in range(53))
            state["page_size"] = len(directory)
            page = Netease(endpoint, "MUSIC_U=synthetic").list_collections("901", 0)
            self.assertEqual((len(page.items), page.total, page.next_offset), (56, 56, None))

    def test_liked_prefers_playlist_order_when_membership_lists_agree(self):
        with TemporaryDirectory() as folder, fake_service() as (endpoint, state):
            provider = Netease(endpoint, "MUSIC_U=synthetic")
            state["data"]["playlists"]["998"]["trackCount"] = 2
            state["liked_ids"] = [103, 102, 101]
            with open_db(Path(folder) / "library.sqlite3") as db:
                self.assertEqual(sync(db, provider, "main", {"liked"}).memberships, 3)
                self.assertEqual(member_ids(db, "liked"), ["101", "102", "103"])
                self.assertEqual(db.execute("SELECT count(*) FROM memberships WHERE added_at IS NOT NULL").fetchone()[0], 1)
                state["liked_ids"] = [103, 102]
                report = sync(db, provider, "main", {"liked"})
                self.assertEqual(report.failures[0][1], "incomplete")
                self.assertEqual(member_ids(db, "liked"), ["101", "102", "103"])

    def test_snapshots_updates_and_failures(self):
        with TemporaryDirectory() as folder, fake_service() as (endpoint, service):
            root = Path(folder)
            root.joinpath("config.toml").write_text(f'[accounts.netease.main]\nendpoint = "{endpoint}"\n')
            args = ("sync", "netease", "--account", "main", "--include", "liked,owned")
            with patch.dict(os.environ, {"MUSICARCH_NETEASE_COOKIE": "Path=/; HttpOnly;MUSIC_U=synthetic;Path=/"}):
                code, output, error = run_cli(root, *args)
                self.assertEqual((code, error), (0, ""))
                self.assertIn("2 个集合、6 个位置、3 条来源、1 条待补详情", output)
                self.assertIn(("/user/playlist", {"uid": "901", "offset": "2", "limit": "50", "noCookie": "true"}), service["requests"])
                self.assertEqual(sum(route == "/album" for route, _ in service["requests"]), 1)
                self.assertEqual(sum(route == "/playlist/detail" and query.get("id") == "998"
                                     for route, query in service["requests"]), 1)
                service["requests"].clear()
                self.assertEqual(run_cli(root, *args)[0], 0)
                self.assertFalse(any(route == "/album" for route, _ in service["requests"]))
                with open_db(root / "library.sqlite3") as db:
                    self.assertEqual(member_ids(db, "liked"), ["101", "102", "103"])
                    self.assertEqual(member_ids(db, "801"), ["102", "101", "102"])
                    ids = {row["track_id"]: row["recording_id"] for row in db.execute("SELECT * FROM sources")}
                    self.assertEqual(db.execute("SELECT added_at FROM memberships WHERE track_id = '102' AND collection_key = 'liked'").fetchone()[0], "2024-01-01T00:00:00Z")

                    provider = PagedNetease(endpoint, "MUSIC_U=synthetic")
                    self.assertEqual(sync(db, provider, "main", {"liked", "owned"}).memberships, 6)
                    provider.fail_second = True
                    service["data"]["playlists"]["998"]["trackIds"][0] = {"id": 199}
                    report = sync(db, provider, "main", {"liked", "owned"})
                    self.assertEqual((report.collections, len(report.failures)), (1, 1))
                    self.assertEqual(member_ids(db, "liked"), ["101", "102", "103"])
                    provider.fail_second = False

                    liked = service["data"]["playlists"]["998"]
                    liked["trackIds"].pop(0)
                    liked["trackCount"] = 2
                    self.assertEqual(sync(db, provider, "main", {"liked", "owned"}).memberships, 5)
                    self.assertEqual(member_ids(db, "801"), ["102", "101", "102"])
                    self.assertEqual(member_ids(db, "liked"), ["102", "103"])
                    self.assertEqual(ids, {row["track_id"]: row["recording_id"] for row in db.execute("SELECT * FROM sources")})
                    self.assertEqual(db.execute("SELECT status FROM sources WHERE track_id = '103'").fetchone()[0], "unresolved")

                    service["privileges"] = [{"id": 102, "st": -200}]
                    del service["data"]["songs"]["102"]
                    self.assertEqual(run_cli(root, "sync", "netease", "--account", "main", "--playlist-id", "801")[0], 0)
                    song = db.execute("SELECT * FROM sources WHERE track_id = '102'").fetchone()
                    self.assertEqual((song["status"], json.loads(song["metadata"])["title"]), ("unavailable", "晴窗"))
                    self.assertEqual(member_ids(db, "liked"), ["102", "103"])
                    self.assertEqual(run_cli(root, "sync", "netease", "--account", "main", "--playlist-id", "802")[0], 0)
                    self.assertEqual(db.execute("SELECT type FROM collections WHERE collection_key = '802'").fetchone()[0], "specified")

                    directory = deepcopy(service["data"]["directory"])
                    service["data"]["directory"] = [item for item in directory if item["id"] != 801]
                    self.assertEqual(run_cli(root, *args)[0], 0)
                    self.assertEqual(db.execute("SELECT status FROM collections WHERE collection_key = '801'").fetchone()[0], "missing")
                    self.assertEqual(member_ids(db, "801"), ["102", "101", "102"])
                    service["failures"]["/user/account"] = 301
                    code, _, error = run_cli(root, *args)
                    self.assertEqual(code, 1)
                    self.assertIn("重新授权", error)
                    self.assertNotIn("MUSIC_U", error)
                    del service["failures"]["/user/account"]
                    service["data"]["account"] = {"code": 200, "account": {"id": 999}, "profile": {"userId": 999}}
                    self.assertIn("身份冲突", run_cli(root, *args)[2])
                    service["data"]["account"] = {"code": 200, "account": {"id": 901}, "profile": {"userId": 901}}
                    liked["trackIds"].clear()
                    liked["trackCount"] = 0
                    self.assertEqual(run_cli(root, "sync", "netease", "--account", "main")[0], 0)
                    self.assertEqual(member_ids(db, "liked"), [])
