# MusicArch

[中文](#中文) · [English](#english)

## 中文

MusicArch 是一个本地音乐收藏归档工具。它读取网易云音乐收藏，用 SQLite 整理歌曲、来源与歌单，再用 Hugo 生成可搜索的收藏曲库和独立的偏好分析页。

- 默认同步红心和自建歌单。歌单保留顺序、重复位置和可获得的收藏时间，曲库总览按歌曲版本去重。
- 可按歌名、艺人、歌单、年代、版本、语种和流派筛选。偏好分析包含七张图表，使用构建时选定的歌单。
- `review`、`edit` 和 `tag` 用于复核重复歌曲和修订元数据。可用带版本的 JSON 档案离线迁移曲库。

### 本地预览

需要 Python 3.11 或以上及 Hugo 0.156 或以上。仓库的示例使用虚构歌曲，可以离线导入。

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
musicarch import examples/musicarch-demo.json
musicarch site build --collection netease:main:liked --collection netease:main:owned
python -m http.server 8000 --bind 127.0.0.1 --directory .musicarch/public
```

在浏览器打开[收藏曲库](http://127.0.0.1:8000/)和[偏好分析](http://127.0.0.1:8000/analysis/)。将 `site build` 的 `--base-url` 设为目标地址可构建到部署子路径。

### 同步自己的账号

在本机通过 Node.js 启动固定版本的 [NeteaseCloudMusicApiEnhanced](https://neteasecloudmusicapienhanced.js.org/)。

```sh
HOST=127.0.0.1 PORT=3000 npx --yes --package=@neteasecloudmusicapienhanced/api@4.41.1 -- api
```

复制 `config.example.toml` 为 `.musicarch.toml`。打开[本机扫码页](http://127.0.0.1:3000/qrlogin.html)并扫码授权，在浏览器本地存储中读取 `cookie`。按配置样例使用本机私有文件或环境变量提供 Cookie。真实账号使用独立的库，避免与演示账号的别名冲突。

```sh
musicarch --db .musicarch/personal.sqlite3 sync netease --account main
musicarch --db .musicarch/personal.sqlite3 site build
musicarch --db .musicarch/personal.sqlite3 export --out .musicarch/collection.json
```

### 开发

`providers` 负责读取平台数据，`sync.py` 管理歌单快照，`catalog.py` 处理去重和人工修订，`archive.py` 实现[交换格式](schemas/musicarch-v1.schema.json)。`site.py` 生成展示数据，`analysis.py` 按唯一歌曲统计，Hugo 分别渲染两个页面。测试使用虚构账号与歌曲。

```sh
python -m unittest discover -s tests -v
```

## English

MusicArch is a local archive for NetEase Cloud Music collections. It organizes songs, sources, and playlists in SQLite, then uses Hugo to build a searchable catalog and a separate preference dashboard.

- Sync liked songs and playlists you created. Playlists retain their order, repeated positions, and available add dates. The catalog groups matching versions of the same song.
- Filter by title, artist, playlist, decade, version, language, or genre. Seven charts describe the playlists selected for the build.
- Review potential duplicates and edit metadata with `review`, `edit`, and `tag`. Export or restore a library offline with a versioned JSON archive.

### Preview locally

Install Python 3.11 or later and Hugo 0.156 or later. The bundled demo uses fictional song data and can be imported offline.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
musicarch import examples/musicarch-demo.json
musicarch site build --collection netease:main:liked --collection netease:main:owned
python -m http.server 8000 --bind 127.0.0.1 --directory .musicarch/public
```

Open the [catalog](http://127.0.0.1:8000/) and the [preference dashboard](http://127.0.0.1:8000/analysis/). Pass `--base-url` to `site build` to deploy under a subpath.

### Sync your account

Run the pinned [NeteaseCloudMusicApiEnhanced](https://neteasecloudmusicapienhanced.js.org/) adapter locally with Node.js.

```sh
HOST=127.0.0.1 PORT=3000 npx --yes --package=@neteasecloudmusicapienhanced/api@4.41.1 -- api
```

Copy `config.example.toml` to `.musicarch.toml`. Scan the code on the [local sign-in page](http://127.0.0.1:3000/qrlogin.html) and retrieve the `cookie` value from the browser's local storage for that page. Use a private local file or an environment variable as shown in the example configuration. Use a separate database for the real account so its alias does not conflict with the demo.

```sh
musicarch --db .musicarch/personal.sqlite3 sync netease --account main
musicarch --db .musicarch/personal.sqlite3 site build
musicarch --db .musicarch/personal.sqlite3 export --out .musicarch/collection.json
```

### Development

`providers` reads platform data, `sync.py` manages playlist snapshots, and `catalog.py` handles song identity and edits. `archive.py` implements the [exchange format](schemas/musicarch-v1.schema.json). `site.py` prepares data for Hugo, and `analysis.py` aggregates statistics across unique songs. The test suite uses fictional accounts and songs.

```sh
python -m unittest discover -s tests -v
```
