"""Command line entry point and private configuration."""

import argparse
import os
import re
import sqlite3
import sys
import tomllib
from pathlib import Path

from . import catalog
from .providers.netease import Netease, platform_id, release_date
from .store import MusicArchError, open_db, transaction
from .sync import sync


def parser():
    app = argparse.ArgumentParser(prog="musicarch", description="同步、整理和迁移本地音乐收藏")
    app.add_argument("--db", type=Path, default=Path(".musicarch/library.sqlite3"))
    app.add_argument("--config", type=Path, default=Path(".musicarch.toml"))
    commands = app.add_subparsers(dest="command", required=True)
    sync_args = commands.add_parser("sync", help="读取平台收藏")
    sync_args.add_argument("platform", choices=["netease"])
    sync_args.add_argument("--account", required=True)
    scope = sync_args.add_mutually_exclusive_group()
    scope.add_argument("--include", help="以逗号分隔的 liked,owned,subscribed")
    scope.add_argument("--playlist-id")
    account = commands.add_parser("account", help="管理本地账号别名")
    rename = account.add_subparsers(dest="action", required=True).add_parser("rename")
    rename.add_argument("platform")
    rename.add_argument("alias")
    rename.add_argument("new_alias")
    commands.add_parser("export", help="导出完整档案").add_argument("--out", required=True, type=Path)
    commands.add_parser("import", help="导入完整档案").add_argument("path", type=Path)
    review = commands.add_parser("review", help="查看、合并或排除候选歌曲")
    decisions = review.add_subparsers(dest="decision")
    for name in ("merge", "separate"):
        args = decisions.add_parser(name)
        args.add_argument("first", help="合并时保留的歌曲 ID")
        args.add_argument("second")
    edit = commands.add_parser("edit", help="修改歌曲展示字段")
    edit.add_argument("recording_id")
    for name in catalog.EDITABLE:
        if name != "artists":
            edit.add_argument(f"--{name.replace('_', '-')}")
    edit.add_argument("--artist", dest="artists", action="append", help="按署名顺序重复指定")
    edit.add_argument("--reset", choices=catalog.EDITABLE, action="append", default=[], help="恢复自动取值")
    tag = commands.add_parser("tag", help="添加或移除标签")
    tag.add_argument("recording_id")
    tag.add_argument("--dimension", choices=catalog.EDITABLE_TAGS, default="custom")
    tag.add_argument("--add", action="append", default=[])
    tag.add_argument("--remove", action="append", default=[])
    site = commands.add_parser("site", help="构建静态收藏页")
    build = site.add_subparsers(dest="site_command", required=True).add_parser("build")
    build.add_argument("--collection", action="append")
    build.add_argument("--base-url")
    return app


def config_data(path):
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError:
        raise MusicArchError(f"TOML 配置格式错误。请检查 {path}。") from None


def config_table(data, *keys):
    for key in keys:
        data = data.get(key, {})
        if not isinstance(data, dict):
            raise MusicArchError(f"配置 {'.'.join(keys)} 必须是 TOML 表。")
    return data


def setup_netease(path, alias):
    account = config_table(config_data(path), "accounts", "netease", alias)
    endpoint = account.get("endpoint", "http://127.0.0.1:3000")
    cookie_env, cookie_file = account.get("cookie_env"), account.get("cookie_file")
    if not isinstance(endpoint, str) or any(value is not None and not isinstance(value, str)
                                          for value in (cookie_env, cookie_file)):
        raise MusicArchError("endpoint、cookie_env、cookie_file 应为字符串。")
    if cookie_env and cookie_file:
        raise MusicArchError("每个账号请选择 cookie_env 或 cookie_file 中的一项。")
    if cookie_file:
        try:
            cookie = (path.parent / cookie_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise MusicArchError("无法读取账号 Cookie 文件。请检查私有配置和授权。") from None
    else:
        cookie = os.environ.get(cookie_env or "MUSICARCH_NETEASE_COOKIE", "").strip()
    return Netease(endpoint, cookie)


def validate_alias(alias):
    if not re.fullmatch(r"[^:\s]+", alias):
        raise MusicArchError("账号别名不能为空或包含冒号和空白。")


def run_sync(db, args):
    validate_alias(args.account)
    scopes = set((args.include if args.include is not None else "liked,owned").split(","))
    if not scopes <= {"liked", "owned", "subscribed"}:
        raise MusicArchError("--include 仅接受 liked、owned、subscribed，用逗号分隔。")
    if args.playlist_id is not None and not platform_id(args.playlist_id):
        raise MusicArchError("--playlist-id 必须为有效的网易云歌单 ID。")
    report = sync(db, setup_netease(args.config, args.account), args.account, scopes, args.playlist_id)
    print(f"同步成功 {report.collections} 个集合、{report.memberships} 个位置、{len(report.sources)} 条来源、"
          f"{len(report.unresolved)} 条待补详情。失败 {len(report.failures)} 项。")
    if report.enriched:
        print(f"已补充 {report.enriched} 条来源的语种或流派。")
    for key, kind, message in report.failures:
        print(f"{key} [{kind}] {message}", file=sys.stderr)
    return int(bool(report.failures))


def run_edit(db, args, recording_id):
    values = {key: getattr(args, key) for key in catalog.EDITABLE if getattr(args, key) is not None}
    if not values and not args.reset:
        raise MusicArchError("请指定要修改或恢复的字段。")
    if values.keys() & set(args.reset):
        raise MusicArchError("同一字段不能同时修改和恢复。")
    if "artists" in values:
        if any(not name.strip() for name in values["artists"]):
            raise MusicArchError("艺人名不能为空。")
        values["artists"] = [{"name": name.strip(), "id": None} for name in values["artists"]]
    for key, value in values.items():
        if isinstance(value, str):
            values[key] = value.strip() or None
    if "title" in values and values["title"] is None:
        raise MusicArchError("展示标题不能为空。")
    if values.get("release_date") and not release_date(values["release_date"]):
        raise MusicArchError("发行日期应为 YYYY、YYYY-MM 或 YYYY-MM-DD。")
    db.executemany("DELETE FROM overrides WHERE recording_id = ? AND field = ?",
                   ((recording_id, key) for key in args.reset))
    catalog.edit(db, recording_id, values)


def dispatch(db, args):
    match args.command:
        case "sync":
            return run_sync(db, args)
        case "account":
            validate_alias(args.new_alias)
            with transaction(db):
                taken = db.execute("SELECT 1 FROM accounts WHERE (platform, alias) = (?, ?)",
                                   (args.platform, args.new_alias)).fetchone()
                if taken and args.alias != args.new_alias:
                    raise MusicArchError(f"账号别名已使用 {args.platform}:{args.new_alias}。")
                updated = db.execute("UPDATE accounts SET alias = ? WHERE (platform, alias) = (?, ?)",
                                     (args.new_alias, args.platform, args.alias))
                if not updated.rowcount:
                    raise MusicArchError(f"找不到账号 {args.platform}:{args.alias}。")
            print(f"已更新账号别名 {args.platform}:{args.new_alias}。")
        case "import" | "export":
            from .archive import export_file, import_file

            if args.command == "export":
                stats = export_file(db, args.out)
                action = "已导出"
            else:
                stats = import_file(db, args.path)
                action = f"已导入（新增 {stats.pop('new')} 条记录）"
            print(f"{action} {stats['recordings']} 首歌曲、{stats['sources']} 条来源、{stats['collections']} 个集合、"
                  f"{stats['memberships']} 个位置、{stats['manual']} 条人工记录。")
        case "review":
            if args.decision:
                with transaction(db):
                    if args.decision == "merge":
                        print(f"保留歌曲 {catalog.merge(db, args.first, args.second)}。")
                    else:
                        catalog.separate(db, args.first, args.second)
                        print("已记录分开决定。")
            else:
                with transaction(db, read=True):
                    for ids, titles, reason in catalog.candidates(db):
                        print(f"{ids[0]} {titles[0] or '详情未知'} | {ids[1]} {titles[1] or '详情未知'} | {reason}")
        case "edit" | "tag":
            with transaction(db):
                recording_id = catalog.resolve_id(db, args.recording_id)
                if args.command == "edit":
                    run_edit(db, args, recording_id)
                else:
                    if not args.add and not args.remove:
                        raise MusicArchError("请指定 --add 或 --remove。")
                    catalog.update_tags(db, recording_id, args.dimension, args.add, "add")
                    catalog.update_tags(db, recording_id, args.dimension, args.remove, "remove")
            print(f"已更新歌曲 {recording_id}。")
        case "site":
            from .site import build_site

            configured = args.collection
            if configured is None:
                configured = config_table(config_data(args.config), "site").get("collections", [])
            if not isinstance(configured, list) or any(not isinstance(key, str) for key in configured):
                raise MusicArchError("site.collections 应为集合键字符串数组。")
            destination = build_site(db, configured, args.base_url)
            print(f"站点已构建至 {destination}。")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with open_db(args.db) as db:
            return dispatch(db, args)
    except (MusicArchError, OSError, sqlite3.Error) as error:
        print(f"musicarch: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("同步或操作已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
