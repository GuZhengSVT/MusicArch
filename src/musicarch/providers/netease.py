"""Read data through the local NeteaseCloudMusicApiEnhanced 4.41.1 adapter."""

import ipaddress
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ..store import MusicArchError
from . import Collection, Member, Page, ProviderError, Track, incomplete


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def platform_id(value):
    if isinstance(value, (str, int)) and not isinstance(value, bool) and re.fullmatch(r"[1-9][0-9]*", str(value)):
        return str(value)
    return None


def count(value):
    return value if type(value) is int and value >= 0 else None


def clean_text(value):
    return value if isinstance(value, str) and value.strip() else None


def timestamp(value):
    if type(value) is int and value > 0:
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return None


def release_date(value):
    if type(value) is int and 1000 <= value <= 2999:
        return str(value)
    date = timestamp(value)
    if date:
        return date.date().isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        try:
            datetime.fromisoformat((value + {4: "-01-01", 7: "-01", 10: ""}[len(value)]))
        except ValueError:
            return None
        return value
    return None


def album_fields(raw):
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in {
        "album": clean_text(raw.get("name")),
        "album_id": platform_id(raw.get("id")),
        "artwork_url": clean_text(raw.get("picUrl")),
        "release_date": release_date(raw.get("publishTime")),
    }.items() if value is not None}


def original_url(track_id):
    return f"https://music.163.com/song?id={track_id}"


def map_track(track_id, raw, *, detail=False, unavailable=False):
    artists = raw.get("ar")
    artists = [{"name": person["name"], "id": platform_id(person.get("id"))}
               for person in artists if isinstance(person, dict) and clean_text(person.get("name"))] if isinstance(artists, list) else []
    aliases = raw.get("alia")
    isrc = clean_text(raw.get("isrc"))
    if isrc and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", isrc.replace("-", "").upper(), re.ASCII):
        isrc = None
    release = album_fields(raw.get("al"))
    fields = {
        **release,
        "title": clean_text(raw.get("name")),
        "artists": artists,
        "aliases": [alias for alias in aliases if clean_text(alias)] if isinstance(aliases, list) else [],
        "duration_ms": count(raw.get("dt")) or None,
        "version": clean_text(raw.get("version")) or ("翻唱" if raw.get("originCoverType") == 2 else None),
        "isrc": isrc,
        "language": clean_text(raw.get("language")),
        "genre": clean_text(raw.get("genre")),
    }
    fields["release_date"] = release_date(raw.get("publishTime")) or release.get("release_date")
    fields = {key: value for key, value in fields.items() if value is not None and value != []}
    status = "unavailable" if unavailable else "available" if detail and "title" in fields else "unresolved"
    source = "platform_detail" if detail else "collection"
    # Cloud uploads have no public page for their private source ID.
    url = None if raw.get("t") in (1, 2) else original_url(track_id)
    return Track(track_id, url, status, fields, dict.fromkeys(fields, source))


def retry_delay(value, attempt):
    if value:
        try:
            return max(0, int(value))
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2 ** attempt


def cookie_header(raw):
    if not raw or any(not 32 <= ord(character) < 127 for character in raw):
        raise MusicArchError("授权 Cookie 为空或格式错误。请按 README 重新授权。")
    attributes = {"domain", "expires", "max-age", "path", "priority", "samesite"}
    pairs = []
    for item in raw.split(";"):
        key, _, value = item.strip().partition("=")
        if value and key.casefold() not in attributes and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            pairs.append(f"{key}={value}")
    if not pairs:
        raise MusicArchError("授权 Cookie 缺少有效的键值。请按 README 重新授权。")
    # The QR page stores Set-Cookie lines joined by ';'. The service expects '; ' in a Cookie header.
    return "; ".join(pairs)


class Netease:
    platform = "netease"
    batch_size = 100

    def __init__(self, endpoint, cookie, *, timeout=15):
        try:
            url = urlsplit(endpoint)
            local = url.hostname == "localhost" or ipaddress.ip_address(url.hostname or "").is_loopback
            valid = (local and url.scheme in {"http", "https"} and not url.username and not url.password
                     and not url.query and not url.fragment and (url.port is None or url.port > 0))
        except ValueError:
            valid = False
        if not valid:
            raise MusicArchError("网易云适配服务地址必须使用本机回环地址。")
        self.endpoint = endpoint.rstrip("/")
        self.cookie = cookie_header(cookie)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _request(self, route, **params):
        body = urlencode({**params, "noCookie": "true"}).encode()
        headers = {"Cookie": self.cookie, "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded", "X-Apicache-Bypass": "1"}
        for attempt in range(3):
            retry_after = None
            try:
                with self.opener.open(Request(self.endpoint + route, data=body, headers=headers), timeout=self.timeout) as response:
                    data = json.load(response)
                    retry_after = response.headers.get("Retry-After")
            except HTTPError as error:
                # The adapter forwards upstream errors as HTTP status codes.
                retry_after = error.headers.get("Retry-After")
                data = {"code": error.code}
                if error.code == 400:
                    try:
                        payload = json.load(error)
                        if isinstance(payload, dict) and type(payload.get("code")) is int and payload["code"] != 200:
                            data = payload
                    except (UnicodeError, ValueError, OSError):
                        pass
                error.close()
            except (OSError, HTTPException):
                data = {"code": 503}
            except (UnicodeError, ValueError):
                raise incomplete(f"{route} 返回了无效数据。") from None
            if not isinstance(data, dict) or type(data.get("code")) is not int:
                raise incomplete(f"{route} 缺少有效响应状态。")
            code = data["code"]
            if code == 200:
                return data
            if code in {301, 401, -101}:
                raise ProviderError("auth", "网易云登录已失效。请按 README 重新授权。")
            if code in {403, 404, 405, 406, -460}:
                kind = "private" if code == 405 else "permission"
                raise ProviderError(kind, f"{route} 无法读取，服务状态 {code}。")
            if code in {408, 429, 500, 502, 503, 504}:
                delay = retry_delay(retry_after, attempt)
                if attempt < 2 and delay <= 30:
                    time.sleep(delay)
                    continue
                kind = "rate_limit" if code == 429 else "network"
                raise ProviderError(kind, f"{route} 暂时不可用，服务状态 {code}，请稍后重试。")
            raise ProviderError("response", f"{route} 请求失败，服务状态 {code}。")

    def account_id(self):
        data = self._request("/user/account")
        account = data.get("account")
        profile = data.get("profile")
        account_id = platform_id(account.get("id")) if isinstance(account, dict) else None
        profile_id = platform_id(profile.get("userId")) if isinstance(profile, dict) else None
        if not account_id or not profile_id or account_id != profile_id:
            raise ProviderError("auth", "未能确认网易云登录账号。请按 README 重新授权。")
        return account_id

    def _playlist(self, raw, account_id):
        if not isinstance(raw, dict):
            raise incomplete("歌单缺少有效标识。")
        playlist_id = platform_id(raw.get("id"))
        owner = platform_id(raw.get("userId"))
        if not owner and isinstance(raw.get("creator"), dict):
            owner = platform_id(raw["creator"].get("userId"))
        if not playlist_id or not owner:
            raise incomplete("歌单缺少有效标识或创建者。")
        kind = "owned" if owner == account_id else "subscribed"
        if kind == "owned" and raw.get("specialType") == 5:
            kind = "liked"
        visibility = {0: "public", 10: "private"}.get(raw.get("privacy"), "unknown") if type(raw.get("privacy")) is int else "unknown"
        key = "liked" if kind == "liked" else playlist_id
        return Collection(key, kind, clean_text(raw.get("name")) or "歌单", visibility, playlist_id)

    def list_collections(self, account_id, offset):
        limit = 50
        data = self._request("/user/playlist", uid=account_id, offset=offset, limit=limit)
        playlists = data.get("playlist")
        more = data.get("more")
        # Authenticated requests can return a complete directory larger than the requested limit.
        if not isinstance(playlists, list) or type(more) is not bool:
            raise incomplete("歌单目录缺少有效的分页结束标记。")
        return Page([self._playlist(raw, account_id) for raw in playlists], count(data.get("count")),
                    offset + len(playlists) if more else None)

    def _playlist_data(self, playlist_id):
        data = self._request("/playlist/detail", id=playlist_id)
        playlist = data.get("playlist")
        if not isinstance(playlist, dict) or platform_id(playlist.get("id")) != playlist_id:
            raise incomplete("歌单详情与请求标识不符。")
        return playlist

    def resolve_collection(self, account_id, playlist_id):
        return self._playlist(self._playlist_data(playlist_id), account_id)

    def _liked_members(self, account_id):
        data = self._request("/likelist", uid=account_id)
        ids = data.get("ids")
        if not isinstance(ids, list) or data.get("more") is True:
            raise incomplete("红心列表缺少完整成员标识。")
        return Page([Member(self._member_track(item)) for item in ids], count(data.get("count")))

    def read_members(self, account_id, collection, offset):
        if collection.playlist_id is None:
            return self._liked_members(account_id)
        playlist = self._playlist_data(collection.playlist_id)
        if self._playlist(playlist, account_id).key != collection.key:
            raise incomplete("歌单详情与目录的集合类型不符。")
        ids = playlist.get("trackIds")
        total = count(playlist.get("trackCount"))
        if not isinstance(ids, list) or total is None or playlist.get("more") is True:
            raise incomplete("歌单缺少完整成员标识或总数。")
        tracks = playlist.get("tracks")
        slim = {platform_id(item.get("id")): item for item in tracks if isinstance(item, dict)} if isinstance(tracks, list) else {}
        members = []
        for item in ids:
            if not isinstance(item, dict):
                raise incomplete("歌单成员格式无效。")
            track_id = platform_id(item.get("id"))
            track = self._member_track(track_id, slim.get(track_id))
            added = timestamp(item.get("at"))
            members.append(Member(track, added.isoformat().replace("+00:00", "Z") if added else None))
        if collection.type == "liked" and len(members) != total:
            # The playlist counter can exclude entries present in both independent membership lists.
            liked = self._liked_members(account_id)
            if (liked.total in (None, len(liked.items))
                    and Counter(member.track.id for member in liked.items) == Counter(member.track.id for member in members)):
                total = len(members)
        cloud_count = count(playlist.get("cloudTrackCount"))
        if cloud_count and len(members) == total + cloud_count:
            if sum(item.get("t") in (1, 2) for item in ids) == cloud_count:
                total += cloud_count
        return Page(members, total)

    def _member_track(self, track_id, slim=None):
        track_id = platform_id(track_id)
        if not track_id:
            raise incomplete("集合成员缺少有效歌曲 ID。")
        return map_track(track_id, slim or {})

    def fetch_tracks(self, track_ids):
        data = self._request("/song/detail", ids=",".join(track_ids))
        tracks = data.get("songs")
        if not isinstance(tracks, list):
            raise incomplete("歌曲详情缺少有效列表。")
        privileges = data.get("privileges")
        denied = {platform_id(item.get("id")) for item in privileges
                  if isinstance(item, dict) and type(item.get("st")) is int and item["st"] < 0} if isinstance(privileges, list) else set()
        output = {}
        expected = set(track_ids)
        for raw in tracks:
            track_id = platform_id(raw.get("id")) if isinstance(raw, dict) else None
            if track_id not in expected or track_id in output:
                raise incomplete("歌曲详情包含未知或重复 ID。")
            output[track_id] = map_track(track_id, raw, detail=True, unavailable=track_id in denied)
        for track_id in (expected & denied) - output.keys():
            output[track_id] = map_track(track_id, {}, unavailable=True)
        return output

    def fetch_album(self, album_id):
        raw = self._request("/album", id=album_id).get("album")
        if not isinstance(raw, dict) or platform_id(raw.get("id")) != album_id:
            raise incomplete("专辑详情与请求标识不符。")
        return album_fields(raw)

    def fetch_track_tags(self, track_id):
        data = self._request("/song/wiki/summary", id=track_id).get("data")
        if data is None:
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
            raise incomplete("歌曲百科缺少有效的数据块。")
        labels = {"language": [], "genre": []}
        for block in data["blocks"]:
            if block.get("code") != "SONG_PLAY_ABOUT_SONG_BASIC":
                continue
            for creative in block.get("creatives") or ():
                if creative.get("creativeType") == "language":
                    links = (creative.get("uiElement") or {}).get("textLinks") or ()
                    labels["language"].extend(link.get("text") for link in links)
                elif creative.get("creativeType") == "songTag":
                    for resource in creative.get("resources") or ():
                        if resource.get("resourceType") == "melody_style":
                            title = (resource.get("uiElement") or {}).get("mainTitle") or {}
                            labels["genre"].append(title.get("title"))
        result = {}
        for field, values in labels.items():
            unique = dict.fromkeys(text.strip() for text in values if clean_text(text))
            if unique:
                result[field] = " / ".join(unique)
        return result
