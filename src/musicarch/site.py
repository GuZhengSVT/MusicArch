"""Project explicitly selected memberships into a portable static Hugo site."""

import json
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from contextlib import closing
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, urlunsplit

from .analysis import analyze
from .archive import atomic_json, group
from .catalog import display_fields, normalized, recording_tags, source_keys
from .providers.netease import original_url, platform_id
from .store import MusicArchError, collection_selector, transaction


MIN_HUGO = (0, 156, 0)
SONGS_PER_PAGE = 50
PLATFORMS = {"netease": "网易云音乐", "qq": "QQ 音乐", "kugou": "酷狗音乐", "spotify": "Spotify", "youtube_music": "YouTube Music"}
DIMENSIONS = {"custom": "自定义", "decade": "年代", "version": "版本", "language": "语种", "genre": "流派", "platform": "平台"}
PRIMARY_TAGS = ("decade", "version", "language", "genre")


def display_link(row):
    platform, track_id = source_keys(row)
    url = original_url(track_id) if platform == "netease" and platform_id(track_id) and row["url"] else None
    return {"platform": PLATFORMS.get(platform, platform), "url": url, "status": row["status"]}


def collection_rows(db, keys):
    available = {collection_selector(row["platform"], row["alias"], row["collection_key"]): row for row in db.execute("""
        SELECT collections.*, accounts.alias FROM collections JOIN accounts USING (platform, account_id)
        ORDER BY collections.platform, accounts.alias, collections.collection_key
    """)}
    owned_groups = {collection_selector(row["platform"], row["alias"], "owned"): []
                    for row in db.execute("SELECT platform, alias FROM accounts")}
    for selector, row in available.items():
        if row["type"] == "owned" and row["status"] == "active":
            group_key = collection_selector(row["platform"], row["alias"], "owned")
            owned_groups[group_key].append(selector)
    missing = [key for key in keys if key not in available and key not in owned_groups]
    if missing or not keys:
        listing = "\n".join(f"  {selector}  {row['name']}" for selector, row in available.items()) or "  暂无集合，请先同步或导入。"
        reason = f"找不到集合 {'、'.join(missing)}。" if missing else "请选择要展示的集合。"
        raise MusicArchError(f"{reason}\n用法 musicarch site build --collection KEY\n{listing}")
    expanded = [selector for key in keys for selector in ([key] if key in available else owned_groups[key])]
    return [available[key] for key in dict.fromkeys(expanded)]


def selected_recordings(db, collections):
    sources = defaultdict(dict)
    positions = []
    for collection in collections:
        key = collection["platform"], collection["account_id"], collection["collection_key"]
        rows = db.execute("""
            SELECT sources.*, memberships.added_at FROM memberships JOIN sources USING (platform, track_id)
            WHERE (memberships.platform, account_id, collection_key) = (?, ?, ?) ORDER BY position
        """, key).fetchall()
        for row in rows:
            sources[row["recording_id"]][source_keys(row)] = row
        positions.append(rows)
    return sources, positions


def catalog_data(db, keys):
    collections = collection_rows(db, keys)
    sources, positions = selected_recordings(db, collections)
    overrides = group(db.execute("SELECT * FROM overrides"), lambda row: row["recording_id"])
    annotations = group(db.execute("SELECT * FROM tags"), lambda row: row["recording_id"])
    recordings = {}
    labels = defaultdict(dict)
    for rid, group_rows in sources.items():
        rows = [group_rows[key] for key in sorted(group_rows)]
        meta, origins = display_fields(rows, {row["field"]: json.loads(row["value"]) for row in overrides[rid]})
        tags = recording_tags(meta, origins, annotations[rid])
        for platform in {row["platform"] for row in rows}:
            label = PLATFORMS.get(platform, platform)
            tags.append({"dimension": "platform", "value": normalized(label), "label": label})
        tag_keys = set()
        artist_keys = set()
        for tag in tags:
            dimension, value, label = tag["dimension"], tag["value"], tag["label"]
            if dimension in DIMENSIONS:
                labels[dimension].setdefault(value, set()).add(label)
                tag_keys.add((dimension, value))
            elif dimension == "artist":
                labels[dimension].setdefault(value, set()).add(label)
                artist_keys.add((dimension, value))
        artists = [artist["name"] for artist in meta["artists"]]
        status = "available" if any(row["status"] == "available" for row in rows) else (
            "unresolved" if any(row["status"] == "unresolved" for row in rows) else "unavailable")
        title = meta["title"] or "未知歌曲"
        recordings[rid] = {"title": title, "artists": artists, "album": meta["album"], "version": meta["version"],
                           "release_date": meta["release_date"], "status": status, "tag_keys": sorted(tag_keys),
                           "artist_tag_keys": sorted(artist_keys),
                           "aliases": meta["aliases"], "links": [display_link(row) for row in rows]}
    facets = {}
    for dimension, values in labels.items():
        for index, (value, variants) in enumerate(sorted(values.items()), 1):
            facets[(dimension, value)] = {"id": f"{dimension}-{index}", "label": min(variants), "count": 0}
    order = sorted(recordings, key=lambda rid: (normalized(recordings[rid]["title"]),
                   [normalized(artist) for artist in recordings[rid]["artists"]], rid))
    indexed = {rid: index for index, rid in enumerate(order)}
    output = []
    for rid in order:
        row = recordings[rid]
        tag_keys = row.pop("tag_keys")
        tags = [facets[key] for key in tag_keys]
        artists = [facets[key] for key in row.pop("artist_tag_keys")]
        for facet in tags + artists:
            facet["count"] += 1
        row["tags"] = [{"id": tag["id"], "label": DIMENSIONS[key[0]] + tag["label"] if tag["label"] == "未标注" else tag["label"]}
                       for key, tag in zip(tag_keys, tags)]
        row["artist_keys"] = [artist["id"] for artist in artists]
        output.append(row)
    public_collections = [{"id": f"collection-{index}", "name": row["name"], "status": row["status"],
                           "members": [{"recording": indexed[member["recording_id"]], "link": display_link(member)} for member in members]}
                          for index, (row, members) in enumerate(zip(collections, positions), 1)]
    first_added = {}
    for members in positions:
        for member in members:
            date = member["added_at"]
            if date:
                rid = member["recording_id"]
                first_added[rid] = min(first_added.get(rid, date), date)
    catalog = {"recordings": output, "collections": public_collections, "page_size": SONGS_PER_PAGE,
               "artists": [facet for (dimension, _), facet in facets.items() if dimension == "artist"],
               "tag_groups": [{"dimension": dimension, "label": label,
                               "tags": [facet for (kind, _), facet in facets.items() if kind == dimension]}
                              for dimension, label in DIMENSIONS.items() if dimension in labels or dimension in PRIMARY_TAGS]}
    return catalog, first_added


def hugo_executable():
    binary = shutil.which("hugo")
    version = subprocess.run([binary, "version"], capture_output=True, text=True).stdout if binary else ""
    match = re.search(r"\bv(\d+)\.(\d+)\.(\d+)", version)
    if not match or tuple(map(int, match.groups())) < MIN_HUGO:
        raise MusicArchError("构建需要 Hugo 0.156.0 或以上版本。安装说明 https://gohugo.io/installation/")
    return binary


def normalize_base_url(base_url):
    if base_url is None:
        return None
    try:
        url = urlsplit(base_url)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password
                or url.query or url.fragment or re.search(r"[\s\\]", base_url)):
            raise ValueError
    except ValueError:
        raise MusicArchError("--base-url 应为带域名的 HTTP(S) URL，可包含部署子路径。") from None
    return urlunsplit(url._replace(path=url.path.rstrip("/") + "/"))


def stage_site(source):
    template = files("musicarch_site")
    for name in ("hugo.toml", "content/analysis/_index.md", "layouts/home.html", "layouts/catalog/single.html",
                 "layouts/analysis/section.html", "layouts/_partials/catalog.html", "layouts/_partials/navigation.html",
                 "layouts/_partials/tracks.html", "layouts/_partials/ranked-chart.html",
                 "static/catalog.css", "static/catalog.js", "static/analysis.css", "static/analysis.js"):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(template.joinpath(*name.split("/")).read_bytes())


def stage_catalog_pages(source, catalog):
    search = [[" ".join((row["title"], " ".join(row["artists"]), " ".join(row["aliases"]))),
               row["artist_keys"], [tag["id"] for tag in row["tags"]]] for row in catalog["recordings"]]
    views = [("all", search)] + [
        (collection["id"], [search[member["recording"]] for member in collection["members"]])
        for collection in catalog["collections"]
    ]
    for view, rows in views:
        index = source / "static/catalog-data" / view / "search.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        path = "" if view == "all" else f"playlists/{view}/"
        pages = max(1, (len(rows) + SONGS_PER_PAGE - 1) // SONGS_PER_PAGE)
        for page in range(2 if view == "all" else 1, pages + 1):
            url = path if page == 1 else f"{path}page/{page}/"
            content = source / "content" / f"{view}-{page}.md"
            content.write_text(json.dumps({"type": "catalog", "url": f"/{url}", "view": view, "page_number": page}) + "\n",
                               encoding="utf-8")


def publish(workspace, output, data):
    destination = workspace / "public"
    previous = output.with_name("previous")
    # Separate databases can build to the same workspace. Publish one selection at a time.
    with closing(sqlite3.connect(workspace / "site-build.lock", timeout=15)) as lock, transaction(lock):
        try:
            if destination.exists():
                destination.rename(previous)
            output.rename(destination)
            for name, payload in data.items():
                atomic_json(Path("site/data/musicarch") / name, payload)
        except BaseException:
            if not output.exists():
                destination.rename(output)
            if previous.exists():
                previous.rename(destination)
            raise
    return destination


def build_site(db, keys, base_url=None):
    base_url = normalize_base_url(base_url)
    with transaction(db, read=True):
        catalog, first_added = catalog_data(db, keys)
    data = {"catalog.json": catalog, "analysis.json": analyze(catalog, first_added)}
    hugo = hugo_executable()
    workspace = Path(".musicarch").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="site-", dir=workspace) as directory:
        staging = Path(directory)
        source, output = staging / "source", staging / "public"
        stage_site(source)
        stage_catalog_pages(source, catalog)
        for name, payload in data.items():
            atomic_json(source / "data/musicarch" / name, payload)
        args = [hugo, "--source", str(source), "--destination", str(output), "--quiet"]
        if base_url:
            args.extend(("--baseURL", base_url))
        built = subprocess.run(args, capture_output=True, text=True)
        if built.returncode:
            raise MusicArchError("Hugo 构建失败。\n" + (built.stderr or built.stdout).strip())
        return publish(workspace, output, data)
