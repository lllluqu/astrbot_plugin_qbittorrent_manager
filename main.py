import asyncio
import base64
import hashlib
import html
import mimetypes
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - handled at runtime for friendly errors
    BeautifulSoup = None

try:
    from astrbot.api.message_components import At, Plain
except ImportError:  # pragma: no cover - 旧版 AstrBot 没有该导出
    At = None
    Plain = None

try:
    from astrbot.core.message.message_event_result import MessageChain
except ImportError:  # pragma: no cover - 旧版 AstrBot 没有该导出
    MessageChain = None


def _llm_tool(name: str):
    """Register an AstrBot LLM tool while keeping unit-test stubs compatible."""
    register_tool = getattr(filter, "llm_tool", None)
    if register_tool is None:
        return lambda handler: handler
    try:
        return register_tool(name=name)
    except TypeError:
        return register_tool(name)


PLUGIN_DIR = Path(__file__).resolve().parent
CARD_TEMPLATE = PLUGIN_DIR / "templates" / "torrent_results.html"
DEFAULT_RENDER_WIDTH = 900
MAX_RENDER_HEIGHT = 5000
RENDER_BASE_HEIGHT = 214
RENDER_ITEM_HEIGHT = 108
RENDER_STATUS_HEIGHT = 38
PLUGIN_VERSION = "1.12.0"

SORT_FIELD_ALIASES = {
    "time": "time",
    "date": "time",
    "时间": "time",
    "日期": "time",
    "seeders": "seeders",
    "seeder": "seeders",
    "做种": "seeders",
    "做种数": "seeders",
    "leechers": "leechers",
    "leecher": "leechers",
    "下载": "leechers",
    "下载数": "leechers",
    "completed": "completed",
    "completion": "completed",
    "完成": "completed",
    "完成数": "completed",
    "size": "size",
    "大小": "size",
    "title": "title",
    "标题": "title",
}
SORT_ORDER_ALIASES = {
    "desc": True,
    "descending": True,
    "降序": True,
    "倒序": True,
    "asc": False,
    "ascending": False,
    "升序": False,
    "正序": False,
}
NEXUSPHP_SORT_CODES = {
    "title": "1",
    "time": "4",
    "size": "5",
    "completed": "6",
    "seeders": "7",
    "leechers": "8",
}
MTEAM_SORT_FIELDS = {
    "title": "NAME",
    "time": "CREATED_DATE",
    "size": "SIZE",
    "completed": "TIMES_COMPLETED",
    "seeders": "SEEDERS",
    "leechers": "LEECHERS",
}
OPTION_PREFIX_PATTERN = r"(?:--|[－—–−]{1,2})"

# Gay-Torrents.net 是 vBulletin 4 + “Torrent Search v1.31 (SCS)” 模块的私有站，
# 搜索走 GT_torrents/GT_ajax_search.php，结果行是 <torrent> 自定义标签。
GAYTORRENTS_DEFAULT_BASE_URL = "https://www.gay-torrents.net/"
GAYTORRENTS_DEFAULT_SEARCH_PATH = "GT_torrents/GT_ajax_search.php?filename={keyword}"
GAYTORRENTS_LOGIN_PATH = "login.php"
GAYTORRENTS_LIST_PATH = "torrentslist.php"
GAYTORRENTS_MAX_PAGES = 5
GAYTORRENTS_SESSION_TTL_SECONDS = 6 * 3600
GAYTORRENTS_PROVIDER_TYPES = {"gaytorrents", "gaytorrentsnet", "gt", "gayt"}
GAYTORRENTS_DETAIL_HREF = re.compile(r"torrentdetails\.php\?torrentid=", re.IGNORECASE)
GAYTORRENTS_CATEGORY_HREF = re.compile(r"torrentslist\.php\?type=", re.IGNORECASE)
# 搜索接口返回 <searchresult><torrent><torrentdata><![CDATA[ 行 HTML ]]></torrentdata>。
GAYTORRENTS_TORRENTDATA_PATTERN = re.compile(
    r"<torrentdata\b[^>]*>(.*?)</torrentdata>", re.IGNORECASE | re.DOTALL
)
# 列表页的两种 class 命名（新版 Torrent-List-*，旧版 TorrentListN）。
GAYTORRENTS_CELL_CLASSES = {
    "size": ("Torrent-List-Size", "TorrentList3"),
    "seeders": ("Torrent-List-Seeds", "TorrentList6"),
    "leechers": ("Torrent-List-Peers", "TorrentList7"),
    "added_at": ("Torrent-List-Date", "TorrentList8"),
    "freeleech": ("Torrent-List-FL",),
}
# 列表页不提供完成次数，猜测未验证的 class 只会带来错误数据，因此固定为 “?”。
GAYTORRENTS_UNKNOWN_COMPLETED = "?"
# 登录失败时站点在 div.blockrow 里给出的原文（已对站点实测确认）。
GAYTORRENTS_CAPTCHA_ERROR = (
    "Gay-Torrents 自动登录失败：站点要求图片验证码，请改用登录后的 Cookie。"
)
GAYTORRENTS_LOCKED_ERROR = (
    "Gay-Torrents 登录尝试过多，账号或 IP 已被暂时锁定，请稍后再试。"
)
GAYTORRENTS_LOGIN_ERRORS = (
    ("invalid username or password", "Gay-Torrents 自动登录失败：账号或密码不正确。"),
    ("image verify", GAYTORRENTS_CAPTCHA_ERROR),
    ("image.php?do=verify", GAYTORRENTS_CAPTCHA_ERROR),
    ("too many login", GAYTORRENTS_LOCKED_ERROR),
    ("temporarily locked", GAYTORRENTS_LOCKED_ERROR),
)
GAYTORRENTS_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DEFAULT_PROVIDER_CONFIG = {
    "nexusphp": {
        "base_url": "https://www.tjupt.org/",
        "search_path": "torrents.php?search={keyword}&incldead=0",
    },
    "gaytorrents": {
        "base_url": GAYTORRENTS_DEFAULT_BASE_URL,
        "search_path": GAYTORRENTS_DEFAULT_SEARCH_PATH,
    },
    "mteam": {
        "base_url": "https://api.m-team.cc",
        "search_path": "/api/torrent/search",
        "download_path": "/api/torrent/genDlToken",
    },
}
BUILTIN_PROVIDER_BASE_URLS = {
    item["base_url"] for item in DEFAULT_PROVIDER_CONFIG.values()
}
BUILTIN_PROVIDER_SEARCH_PATHS = {
    item["search_path"] for item in DEFAULT_PROVIDER_CONFIG.values()
}


MONITOR_DEFAULT_INTERVAL_MINUTES = 5
MONITOR_DEFAULT_MAX_HOURS = 12
MONITOR_MAX_WATCHES = 50
MONITOR_MAX_REPORTS = 60
MONITOR_TITLE_LIMIT = 60
MONITOR_DEFAULT_COMPLETE_MESSAGE = (
    "种子已下载完成，请登录群网盘-桌面botdownload文件夹查看"
)
QB_COMPLETE_STATES = {
    "uploading",
    "stalledUP",
    "queuedUP",
    "forcedUP",
    "pausedUP",
    "stoppedUP",
}
QB_STATE_LABELS = {
    "error": "报错",
    "missingFiles": "文件缺失",
    "uploading": "做种中",
    "stalledUP": "做种中",
    "queuedUP": "排队做种",
    "forcedUP": "强制做种",
    "pausedUP": "下载完成（已暂停）",
    "stoppedUP": "下载完成（已停止）",
    "checkingUP": "校验中",
    "checkingDL": "校验中",
    "downloading": "下载中",
    "forcedDL": "强制下载",
    "forcedMetaDL": "获取元数据",
    "metaDL": "获取元数据",
    "stalledDL": "等待下载",
    "queuedDL": "排队中",
    "pausedDL": "已暂停",
    "stoppedDL": "已停止",
    "allocating": "分配空间",
    "unknown": "未知状态",
}

DOWNLOAD_KIND_QBITTORRENT = "qbittorrent"
DOWNLOAD_KIND_ARIA2 = "aria2"
DOWNLOAD_KIND_YTDLP = "ytdlp"
MONITORABLE_KINDS = (
    DOWNLOAD_KIND_QBITTORRENT,
    DOWNLOAD_KIND_ARIA2,
    DOWNLOAD_KIND_YTDLP,
)
DOWNLOAD_CLIENT_NAMES = {
    DOWNLOAD_KIND_QBITTORRENT: "qBittorrent",
    DOWNLOAD_KIND_ARIA2: "aria2",
    DOWNLOAD_KIND_YTDLP: "yt-dlp",
}
DOWNLOAD_BACKEND_ALIASES = {
    "aria2": DOWNLOAD_KIND_ARIA2,
    "qb": DOWNLOAD_KIND_QBITTORRENT,
    "qbt": DOWNLOAD_KIND_QBITTORRENT,
    "qbittorrent": DOWNLOAD_KIND_QBITTORRENT,
    "ytdlp": DOWNLOAD_KIND_YTDLP,
    "yt-dlp": DOWNLOAD_KIND_YTDLP,
}
ARIA2_DEFAULT_RPC_URL = "http://127.0.0.1:6800/jsonrpc"
ARIA2_TASK_FIELDS = (
    "status",
    "totalLength",
    "completedLength",
    "uploadLength",
    "downloadSpeed",
    "uploadSpeed",
    "errorCode",
    "errorMessage",
    "files",
)
ARIA2_STATUS_LABELS = {
    "active": "下载中",
    "waiting": "排队中",
    "paused": "已暂停",
    "removing": "正在移除",
    "error": "下载失败",
    "complete": "下载完成",
    "removed": "已移除",
}
YTDLP_STATUS_LABELS = {
    "waiting": "排队中",
    "processing": "解析下载中",
    "completed": "下载完成",
    "error": "下载失败",
}
MEDIA_URL_SUFFIXES = (
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".m2ts",
    ".ts",
    ".wmv",
    ".flv",
    ".mp3",
    ".m4a",
    ".flac",
    ".opus",
    ".wav",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".iso",
    ".apk",
    ".exe",
    ".pdf",
    ".epub",
    ".srt",
    ".ass",
    ".vtt",
)
VIDEO_HOST_DEFAULT = (
    "youtube.com,youtu.be,music.youtube.com,bilibili.com,b23.tv,twitter.com,x.com,"
    "vimeo.com,dailymotion.com,tiktok.com,douyin.com,instagram.com,facebook.com,"
    "fb.watch,twitch.tv,reddit.com,soundcloud.com,pornhub.com,xvideos.com,"
    "xhamster.com,onlyfans.com,boyfriendtv.com,eporner.com,spankbang.com,"
    "youporn.com,redtube.com,tube8.com,missav.com"
)


@dataclass(frozen=True)
class ConfigField:
    label: str
    value_type: str = "string"
    minimum: float | None = None
    maximum: float | None = None
    sensitive: bool = False


USER_CONFIG_FIELDS = {
    "provider_type": ConfigField("PT 站点类型"),
    "provider_base_url": ConfigField("PT 站点地址"),
    "provider_search_path": ConfigField("PT 搜索路径"),
    "provider_cookie": ConfigField("PT Cookie", sensitive=True),
    "provider_username": ConfigField("PT 账号"),
    "provider_password": ConfigField("PT 密码", sensitive=True),
    "mteam_api_token": ConfigField("M-Team API Token", sensitive=True),
    "mteam_search_path": ConfigField("M-Team API 搜索路径"),
    "mteam_download_path": ConfigField("M-Team API 下载路径"),
    "download_client": ConfigField("下载客户端"),
    "qb_url": ConfigField("qBittorrent WebUI 地址"),
    "qb_username": ConfigField("qBittorrent 用户名"),
    "qb_password": ConfigField("qBittorrent 密码", sensitive=True),
    "qb_save_path": ConfigField("qBittorrent 保存路径"),
    "qb_category": ConfigField("qBittorrent 分类"),
    "qb_paused": ConfigField("qBittorrent 添加后暂停", "bool"),
    "ut_url": ConfigField("uTorrent WebUI 地址"),
    "ut_username": ConfigField("uTorrent 用户名"),
    "ut_password": ConfigField("uTorrent 密码", sensitive=True),
    "aria2_rpc_url": ConfigField("aria2 RPC 地址"),
    "aria2_rpc_token": ConfigField("aria2 RPC 密钥", sensitive=True),
    "aria2_save_path": ConfigField("aria2 保存路径"),
    "ytdlp_api_url": ConfigField("yt-dlp 服务地址"),
    "ytdlp_api_key": ConfigField("yt-dlp API Key", sensitive=True),
    "ytdlp_format": ConfigField("yt-dlp 视频格式"),
    "video_hosts": ConfigField("视频站域名"),
    "direct_torrent_max_size_mb": ConfigField("直链种子大小上限（MB）", "int", 1, 500),
    "max_results": ConfigField("最大搜索结果数", "int", 1, 30),
    "cache_ttl_seconds": ConfigField("搜索缓存时间（秒）", "int", 60, 3600),
    "render_width": ConfigField("结果卡片宽度", "int", 640, 1400),
    "request_timeout": ConfigField("请求超时（秒）", "float", 5, 120),
    "monitor_enabled": ConfigField("下载状态监控", "bool"),
}

BUILTIN_USER_CONFIG = {
    "provider_type": "gaytorrents",
    "provider_base_url": GAYTORRENTS_DEFAULT_BASE_URL,
    "provider_search_path": GAYTORRENTS_DEFAULT_SEARCH_PATH,
    "provider_cookie": "",
    "provider_username": "",
    "provider_password": "",
    "mteam_api_token": "",
    "mteam_search_path": "/api/torrent/search",
    "mteam_download_path": "/api/torrent/genDlToken",
    "download_client": "qbittorrent",
    "qb_url": "",
    "qb_username": "",
    "qb_password": "",
    "qb_save_path": "",
    "qb_category": "",
    "qb_paused": False,
    "ut_url": "",
    "ut_username": "",
    "ut_password": "",
    "aria2_rpc_url": ARIA2_DEFAULT_RPC_URL,
    "aria2_rpc_token": "",
    "aria2_save_path": "",
    "ytdlp_api_url": "",
    "ytdlp_api_key": "",
    "ytdlp_format": "",
    "video_hosts": "",
    "direct_torrent_max_size_mb": 50,
    "max_results": 10,
    "cache_ttl_seconds": 600,
    "monitor_enabled": True,
    "render_width": 900,
    "request_timeout": 20.0,
}

CONFIG_KEY_ALIASES = {
    "站点类型": "provider_type",
    "provider": "provider_type",
    "mteam令牌": "mteam_api_token",
    "mteamtoken": "mteam_api_token",
    "mteam搜索路径": "mteam_search_path",
    "mteam下载路径": "mteam_download_path",
    "站点": "provider_base_url",
    "pt站点": "provider_base_url",
    "搜索路径": "provider_search_path",
    "cookie": "provider_cookie",
    "pt账号": "provider_username",
    "pt用户名": "provider_username",
    "pt密码": "provider_password",
    "站点账号": "provider_username",
    "站点密码": "provider_password",
    "客户端": "download_client",
    "qb地址": "qb_url",
    "qb用户名": "qb_username",
    "qb账号": "qb_username",
    "qb密码": "qb_password",
    "qb保存路径": "qb_save_path",
    "qb分类": "qb_category",
    "qb暂停": "qb_paused",
    "ut地址": "ut_url",
    "ut用户名": "ut_username",
    "ut账号": "ut_username",
    "ut密码": "ut_password",
    "aria2地址": "aria2_rpc_url",
    "aria2rpc": "aria2_rpc_url",
    "aria2密钥": "aria2_rpc_token",
    "aria2token": "aria2_rpc_token",
    "aria2保存路径": "aria2_save_path",
    "ytdlp地址": "ytdlp_api_url",
    "ytdlp服务地址": "ytdlp_api_url",
    "ytdlp密钥": "ytdlp_api_key",
    "ytdlp格式": "ytdlp_format",
    "视频站域名": "video_hosts",
    "视频域名": "video_hosts",
    "种子大小上限": "direct_torrent_max_size_mb",
    "结果数": "max_results",
    "缓存时间": "cache_ttl_seconds",
    "卡片宽度": "render_width",
    "超时": "request_timeout",
    "监控": "monitor_enabled",
    "监控下载": "monitor_enabled",
    "下载监控": "monitor_enabled",
}


@dataclass
class TorrentResult:
    index: int
    title: str
    subtitle: str
    size: str
    seeders: str
    leechers: str
    completed: str
    detail_url: str
    download_url: str
    added_at: str = "未知"
    status: str = ""
    progress: float | None = None
    status_kind: str = ""


@dataclass(frozen=True)
class SearchRequest:
    keyword: str
    sort_by: str | None = None
    descending: bool = True


@dataclass
class TorrentFile:
    filename: str
    content: bytes
    content_type: str


@dataclass
class DirectDownload:
    uri: str
    title: str


@dataclass(frozen=True)
class MonitorTarget:
    """发起下载的会话信息，用于下载完成后主动回报。"""

    session: str
    user_id: str
    sender_id: str
    group: bool


@dataclass
class DownloadWatch:
    """已加入下载客户端、需要持续回报状态的任务。"""

    session: str
    user_id: str
    sender_id: str
    group: bool
    title: str
    task_ref: str
    kind: str = DOWNLOAD_KIND_QBITTORRENT
    endpoint: str = ""
    username: str = ""
    credential: str = ""
    registered_at: float = field(default_factory=time.time)
    report_count: int = 0

    @property
    def client_key(self) -> str:
        return f"{self.kind}|{self.endpoint}|{self.username}|{self.credential}"

    @property
    def watch_key(self) -> str:
        return f"{self.client_key}|{self.task_ref}|{self.user_id}"

    @property
    def client_name(self) -> str:
        return DOWNLOAD_CLIENT_NAMES.get(self.kind, self.kind)


@register(
    "astrbot_plugin_qbittorrent_manager",
    "Codex",
    "按 QQ 用户配置 PT 站并推送到 qBittorrent 或 uTorrent 下载。",
    PLUGIN_VERSION,
)
class QBittorrentManagerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._search_cache: dict[str, tuple[float, int, list[TorrentResult]]] = {}
        self._gt_sessions: dict[str, tuple[float, str]] = {}
        self._download_watches: dict[str, DownloadWatch] = {}
        self._monitor_task: asyncio.Task | None = None
        self._config_lock = asyncio.Lock()

    @filter.command("种子")
    async def search_torrents(self, event: AstrMessageEvent, query: GreedyStr):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        query = str(query).strip()
        if not query:
            yield event.plain_result(self._help_text())
            return

        if BeautifulSoup is None:
            yield event.plain_result(
                "缺少依赖 beautifulsoup4，请先安装插件依赖后重试。"
            )
            return

        try:
            request = self._parse_search_request(query)
            user_config = self._user_config(event)
            results = await self._search(
                request.keyword,
                user_config,
                request.sort_by,
                request.descending,
            )
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("搜索种子失败")
            yield event.plain_result(f"搜索失败：{error}")
            return

        if not results:
            yield event.plain_result(f"没有找到与「{request.keyword}」相关的种子。")
            return

        self._remember_results(event, results, user_config)
        image_url = await self._render_results(request.keyword, results, user_config)
        if image_url:
            yield event.image_result(image_url)
            return

        yield event.plain_result(self._format_plain_results(request.keyword, results))

    @filter.command("种子下载")
    async def download_torrent(self, event: AstrMessageEvent, selection: GreedyStr):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        selection = str(selection).strip()
        if not selection:
            yield event.plain_result("请发送 /种子下载 序号，例如：/种子下载 1 3 5")
            return

        try:
            user_config = self._user_config(event)
            results = self._get_cached_results(event)
            if not results:
                yield event.plain_result(
                    "没有可下载的搜索结果，请先使用 /种子 关键词 搜索。"
                )
                return

            torrents = self._pick_results(selection, results)
            client_name = self._download_client_name(user_config)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("添加下载任务失败")
            yield event.plain_result(f"添加下载失败：{error}")
            return

        succeeded: list[TorrentResult] = []
        failed: list[tuple[TorrentResult, str]] = []
        monitor = self._monitor_target(event, user_config)
        for torrent in torrents:
            try:
                await self._add_to_download_client(torrent, user_config, monitor)
                succeeded.append(torrent)
            except UserFacingError as error:
                failed.append((torrent, str(error)))
            except Exception as error:  # noqa: BLE001
                logger.exception("添加下载任务失败：%s", torrent.title)
                failed.append((torrent, f"添加下载失败：{error}"))

        yield event.plain_result(
            self._format_batch_download_result(
                client_name,
                succeeded,
                failed,
                self._monitor_note(user_config, monitor, len(succeeded)),
            )
        )

    @filter.command("直接下载")
    async def direct_download(self, event: AstrMessageEvent, uri: str = ""):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        uri = uri.strip()
        if not uri:
            yield event.plain_result(
                "请发送 /直接下载 磁链或种子链接，例如：/直接下载 magnet:?xt=..."
            )
            return
        if not self._is_supported_direct_uri(uri):
            yield event.plain_result(
                "链接格式不支持，请提供 magnet、http 或 https 链接。"
            )
            return

        try:
            user_config = self._user_config(event)
            client_name = self._download_client_name(user_config)
            monitor = self._monitor_target(event, user_config)
            direct_download = DirectDownload(uri=uri, title=self._direct_title(uri))
            await self._add_direct_download(direct_download, user_config, monitor)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("添加直接下载任务失败")
            yield event.plain_result(f"添加下载失败：{error}")
            return

        yield event.plain_result(
            "已添加到 "
            f"{client_name}：{direct_download.title}"
            f"{self._monitor_note(user_config, monitor, 1)}"
        )

    @filter.command("磁链下载")
    async def magnet_download(self, event: AstrMessageEvent, uri: str = ""):
        async for result in self.direct_download(event, uri):
            yield result

    @filter.command("链接下载")
    async def link_download(self, event: AstrMessageEvent, uri: str = ""):
        async for result in self.direct_download(event, uri):
            yield result

    @filter.command("下载")
    async def smart_download(self, event: AstrMessageEvent, uri: str = ""):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        backend, uri = self._split_download_backend(uri)
        if not uri:
            if backend:
                yield event.plain_result(
                    f"请在 /下载 {backend} 后面加上链接，"
                    f"例如：/下载 {backend} https://example.com/movie.mkv"
                )
                return
            yield event.plain_result(
                "请发送 /下载 链接，插件会自动判断下载方式：\n"
                "磁链和 .torrent 链接交给 qBittorrent，"
                "视频网页交给 yt-dlp，其他直链交给 aria2。\n"
                "也可以手动指定后端："
                "/下载 aria2 链接、/下载 qb 链接、/下载 ytdlp 链接。\n"
                "例如：/下载 https://example.com/movie.mkv"
            )
            return
        if not self._is_supported_direct_uri(uri):
            yield event.plain_result(
                "链接格式不支持，请提供 magnet、http 或 https 链接。"
            )
            return

        try:
            user_config = self._user_config(event)
            if backend:
                kind, reason = self._forced_download_kind(backend, user_config)
            else:
                kind, reason = self._resolve_download_kind(uri, user_config)
            title = self._download_title(uri, kind)
            monitor = self._monitor_target(event, user_config, kind)
            if kind == DOWNLOAD_KIND_YTDLP:
                task_ref = await self._add_to_ytdlp(uri, user_config)
            elif kind == DOWNLOAD_KIND_ARIA2:
                task_ref = await self._add_to_aria2(uri, user_config)
            else:
                task_ref = await self._add_uri_to_download_client(
                    uri, user_config, monitor, title
                )
            if kind in {DOWNLOAD_KIND_ARIA2, DOWNLOAD_KIND_YTDLP}:
                self._register_download_watch(
                    monitor, user_config, title, task_ref, kind
                )
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("提交下载任务失败")
            yield event.plain_result(f"提交下载失败：{error}")
            return

        if kind in {DOWNLOAD_KIND_ARIA2, DOWNLOAD_KIND_YTDLP}:
            client_name = DOWNLOAD_CLIENT_NAMES[kind]
        else:
            client_name = self._download_client_name(user_config)
        yield event.plain_result(
            f"已提交给 {client_name}（识别为{reason}）：{self._short_title(title)}"
            f"{self._monitor_note(user_config, monitor, 1, kind)}"
        )

    @filter.command("种子帮助")
    async def torrent_help(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        yield event.plain_result(self._help_text())

    @filter.command_group("种子监控")
    def torrent_monitor(self):
        pass

    @torrent_monitor.command("查看")
    async def show_monitor_status(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        watches = self._user_watches(event)
        if not watches:
            yield event.plain_result(
                "当前没有正在监控的下载任务。"
                "用 /种子下载、/直接下载 或 /下载 提交任务后会自动开始监控。"
            )
            return

        interval = self._monitor_interval_minutes()
        max_hours = self._monitor_max_hours()
        lines = [
            (
                f"正在监控 {len(watches)} 个下载任务，"
                f"每 {interval} 分钟汇报一次，完成后会通知你。"
            ),
            f"超过 {max_hours} 小时未完成的任务会自动停止监控。",
        ]
        for index, watch in enumerate(watches, start=1):
            minutes = max(0, int((time.time() - watch.registered_at) // 60))
            lines.append(
                f"{index}. [{watch.client_name}] "
                f"{self._short_title(watch.title)}｜已监控 {minutes} 分钟"
            )
        lines.append("停止汇报：/种子监控 停止 序号（或 全部），不会取消下载任务。")
        yield event.plain_result("\n".join(lines))

    @torrent_monitor.command("停止")
    async def stop_monitor(self, event: AstrMessageEvent, selection: GreedyStr):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        selection = str(selection).strip()
        if not selection:
            yield event.plain_result(
                "请发送 /种子监控 停止 序号，或 /种子监控 停止 全部。"
            )
            return

        watches = self._user_watches(event)
        if not watches:
            yield event.plain_result("当前没有正在监控的下载任务。")
            return

        if selection in {"全部", "all"}:
            removed = [watch.title for watch in watches]
            for watch in watches:
                self._download_watches.pop(watch.watch_key, None)
        else:
            try:
                picked = self._pick_watches(selection, watches)
            except UserFacingError as error:
                yield event.plain_result(str(error))
                return
            removed = [watch.title for watch in picked]
            for watch in picked:
                self._download_watches.pop(watch.watch_key, None)

        titles = "、".join(self._short_title(title) for title in removed)
        yield event.plain_result(
            f"已停止监控 {len(removed)} 个任务（不会取消下载任务）：{titles}"
        )

    @torrent_monitor.command("帮助")
    async def monitor_help(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        yield event.plain_result(self._monitor_help_text())

    @_llm_tool("search_torrents")
    async def llm_search_torrents(
        self,
        event: AstrMessageEvent,
        keyword: str,
        sort_by: str = "",
        descending: bool = True,
    ) -> str:
        """搜索 PT 种子。keyword 是搜索关键词；sort_by 可选时间、做种、下载、完成、大小或标题。"""
        self._ensure_user_access(event)
        keyword = str(keyword).strip()
        if not keyword:
            raise UserFacingError("搜索关键词不能为空。")
        user_config = self._user_config(event)
        normalized_sort = (
            self._normalize_sort_field(sort_by) if str(sort_by).strip() else None
        )
        results = await self._search(keyword, user_config, normalized_sort, bool(descending))
        if not results:
            return f"没有找到与“{keyword}”相关的种子。"
        self._remember_results(event, results, user_config)
        return self._format_plain_results(keyword, results)

    @_llm_tool("download_torrents")
    async def llm_download_torrents(
        self, event: AstrMessageEvent, indexes: str
    ) -> str:
        """把最近一次搜索结果中的指定序号添加到用户配置的下载客户端。序号可用空格或逗号分隔。"""
        self._ensure_user_access(event)
        results = self._get_cached_results(event)
        if not results:
            raise UserFacingError("没有可下载的搜索结果，请先搜索种子。")
        user_config = self._user_config(event)
        torrents = self._pick_results(str(indexes), results)
        client_name = self._download_client_name(user_config)
        succeeded: list[TorrentResult] = []
        failed: list[tuple[TorrentResult, str]] = []
        monitor = self._monitor_target(event, user_config)
        for torrent in torrents:
            try:
                await self._add_to_download_client(torrent, user_config, monitor)
                succeeded.append(torrent)
            except UserFacingError as error:
                failed.append((torrent, str(error)))
            except Exception as error:  # noqa: BLE001
                logger.exception("LLM 添加下载任务失败：%s", torrent.title)
                failed.append((torrent, f"添加下载失败：{error}"))
        return self._format_batch_download_result(
            client_name,
            succeeded,
            failed,
            self._monitor_note(user_config, monitor, len(succeeded)),
        )

    @_llm_tool("direct_download")
    async def llm_direct_download(self, event: AstrMessageEvent, uri: str) -> str:
        """直接添加 magnet 磁链或 HTTP(S) .torrent 链接到下载客户端。"""
        self._ensure_user_access(event)
        uri = str(uri).strip()
        if not self._is_supported_direct_uri(uri):
            raise UserFacingError("只支持 magnet、http 或 https 链接。")
        user_config = self._user_config(event)
        monitor = self._monitor_target(event, user_config)
        direct_download = DirectDownload(uri=uri, title=self._direct_title(uri))
        await self._add_direct_download(direct_download, user_config, monitor)
        return (
            f"已添加到 {self._download_client_name(user_config)}：{direct_download.title}"
            f"{self._monitor_note(user_config, monitor, 1)}"
        )

    @_llm_tool("show_torrent_config")
    async def llm_show_torrent_config(self, event: AstrMessageEvent) -> str:
        """查看当前用户生效的 PT 和下载客户端配置；敏感值只显示是否已设置。"""
        self._ensure_user_access(event)
        user_id = self._user_id(event)
        return self._format_user_config(
            user_id,
            self._user_config(event),
            self._user_overrides(user_id),
            self._can_use_global_config(event),
        )

    @staticmethod
    def _normalize_sort_field(value: str) -> str:
        normalized = str(value).strip().lower()
        sort_by = SORT_FIELD_ALIASES.get(normalized)
        if not sort_by:
            raise UserFacingError("不支持的排序字段，可选：时间、做种、下载、完成、大小、标题。")
        return sort_by

    @filter.command_group("种子配置")
    def torrent_config(self):
        pass

    @torrent_config.command("查看")
    async def show_user_config(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
            user_id = self._user_id(event)
            user_config = self._user_config(event)
            overrides = self._user_overrides(user_id)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        yield event.plain_result(
            self._format_user_config(
                user_id,
                user_config,
                overrides,
                self._can_use_global_config(event),
            )
        )

    @torrent_config.command("设置")
    async def set_user_config(
        self,
        event: AstrMessageEvent,
        key: str,
        value: GreedyStr,
    ):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            config_key = self._resolve_config_key(key)
            config_value = self._parse_user_config_value(config_key, str(value))
            await self._set_user_override(user_id, config_key, config_value)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        field = USER_CONFIG_FIELDS[config_key]
        message = f"已保存个人配置：{field.label}。"
        if field.sensitive and not event.is_private_chat():
            message += "\n你在群聊中发送了敏感信息，建议立即撤回并改用私聊配置。"
        yield event.plain_result(message)

    @torrent_config.command("删除")
    async def delete_user_config(
        self,
        event: AstrMessageEvent,
        key: str,
    ):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            config_key = self._resolve_config_key(key)
            removed = await self._delete_user_override(user_id, config_key)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        if not removed:
            yield event.plain_result("该配置项没有个人设置。")
            return
        yield event.plain_result(
            f"已删除个人配置：{USER_CONFIG_FIELDS[config_key].label}，恢复全局默认值。"
        )

    @torrent_config.command("重置")
    async def reset_user_config(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            removed = await self._reset_user_overrides(user_id)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        if not removed:
            yield event.plain_result("当前没有个人配置。")
            return
        yield event.plain_result("已清除全部个人配置，恢复使用全局默认值。")

    @torrent_config.command("帮助")
    async def user_config_help(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        yield event.plain_result(self._user_config_help_text())

    async def _search(
        self,
        keyword: str,
        user_config: dict[str, Any],
        sort_by: str | None = None,
        descending: bool = True,
    ) -> list[TorrentResult]:
        provider = self._provider_config(user_config)
        if provider["type"] == "mteam":
            return await self._search_mteam(keyword, user_config, sort_by, descending)
        if provider["type"] == "gaytorrents":
            return await self._search_gaytorrents(
                keyword, user_config, sort_by, descending
            )
        base_url = provider["base_url"].rstrip("/") + "/"
        search_path = provider["search_path"]
        limit = self._int_config("max_results", 10, 1, 30, user_config)
        search_url = urljoin(base_url, search_path.format(keyword=quote(keyword)))
        if sort_by:
            search_url = self._apply_search_sort(search_url, sort_by, descending)

        async with self._http_client(provider, user_config) as client:
            response = await client.get(search_url)
            self._raise_for_response(response, "搜索接口请求失败")

        results = self._parse_nexusphp_results(response.text, base_url, limit)
        return self._sort_results(results, sort_by, descending)

    async def _search_mteam(
        self,
        keyword: str,
        user_config: dict[str, Any],
        sort_by: str | None,
        descending: bool,
    ) -> list[TorrentResult]:
        provider = self._provider_config(user_config)
        limit = self._int_config("max_results", 10, 1, 30, user_config)
        form: dict[str, Any] = {
            "mode": "normal",
            "keyword": keyword,
            "pageNumber": 1,
            "pageSize": limit,
        }
        if sort_by:
            form["sortField"] = MTEAM_SORT_FIELDS.get(sort_by, "CREATED_DATE")
            form["sortDirection"] = "DESC" if descending else "ASC"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        token = provider.get("api_token", "")
        if token:
            headers["x-api-key"] = token
        async with httpx.AsyncClient(
            base_url=provider["base_url"].rstrip("/"),
            headers=headers,
            timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0, user_config),
            follow_redirects=True,
        ) as client:
            response = await client.post(provider["search_path"], json=form)
            self._raise_for_response(response, "M-Team API 搜索失败")
        try:
            payload = response.json()
        except ValueError as error:
            raise UserFacingError("M-Team API 返回不是有效 JSON。") from error
        self._check_mteam_api_result(payload, "M-Team API 搜索失败")
        items = self._mteam_items(payload)
        results: list[TorrentResult] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            torrent_id = self._first_value(item, "id", "torrentId", "torrent_id", "tid")
            title = str(self._first_value(item, "name", "title", "torrentName") or "").strip()
            if not title:
                continue
            status_data = item.get("status") if isinstance(item.get("status"), dict) else {}
            detail_url = self._absolute_url(provider["base_url"], self._first_value(item, "detailUrl", "detail_url", "detail", "url"))
            download_url = self._absolute_url(provider["base_url"], self._first_value(item, "downloadUrl", "download_url", "downloadLink"))
            if not download_url and torrent_id:
                download_url = f"mteam:{torrent_id}"
            results.append(TorrentResult(
                index=len(results) + 1,
                title=title,
                subtitle=str(self._first_value(item, "smallDescr", "subtitle", "description") or "").strip()[:120],
                size=self._format_mteam_size(self._first_value(item, "size", "sizeFormatted", "fileSize")),
                seeders=self._mteam_display_value(status_data, item, "seeders", "seeder", "seedCount"),
                leechers=self._mteam_display_value(status_data, item, "leechers", "leecher", "leechCount"),
                completed=self._mteam_display_value(status_data, item, "timesCompleted", "completed", "snatched", "completedCount"),
                detail_url=detail_url,
                download_url=download_url,
                added_at=str(self._first_value(item, "addedAt", "createdAt", "createdDate", "publishTime", "time") or "未知"),
                # M-Team status describes torrent metadata, not a user's client task.
                status="",
                progress=None,
                status_kind="",
            ))
        return self._sort_results(results, sort_by, descending)

    @staticmethod
    def _mteam_items(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []
        for key in ("data", "list", "results", "records", "torrents", "normalList", "adultList", "content"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = QBittorrentManagerPlugin._mteam_items(value)
                if nested:
                    return nested
        return []

    @staticmethod
    def _check_mteam_api_result(payload: Any, prefix: str):
        if not isinstance(payload, dict):
            return
        code = payload.get("code")
        if code in (None, 0, "0", 200, "200", "SUCCESS", "success"):
            return
        message = payload.get("message") or payload.get("msg") or "未知错误"
        raise UserFacingError(f"{prefix}：{message}")

    @staticmethod
    def _format_mteam_size(value: Any) -> str:
        if value in (None, ""):
            return "未知"
        text = str(value).strip()
        if re.search(r"[KMGT]i?B", text, re.I):
            return text
        try:
            size = float(text)
        except ValueError:
            return text
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        unit = 0
        while size >= 1024 and unit < len(units) - 1:
            size /= 1024
            unit += 1
        return f"{size:.2f}".rstrip("0").rstrip(".") + f" {units[unit]}"

    @staticmethod
    def _first_value(item: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if item.get(key) not in (None, ""):
                return item[key]
        return None

    @staticmethod
    def _mteam_display_value(primary: dict[str, Any], fallback: dict[str, Any], *keys: str) -> str:
        value = QBittorrentManagerPlugin._first_value(primary, *keys)
        if value is None:
            value = QBittorrentManagerPlugin._first_value(fallback, *keys)
        return "?" if value in (None, "") else str(value)

    @staticmethod
    def _absolute_url(base_url: str, value: Any) -> str:
        if not value:
            return ""
        return urljoin(base_url.rstrip("/") + "/", str(value))

    @staticmethod
    def _mteam_progress(item: dict[str, Any]) -> float | None:
        value = QBittorrentManagerPlugin._first_value(item, "progress", "percent", "completion")
        try:
            progress = float(value)
        except (TypeError, ValueError):
            return None
        if progress <= 1:
            progress *= 100
        return min(max(progress, 0.0), 100.0)

    def _parse_nexusphp_results(
        self,
        html_text: str,
        base_url: str,
        limit: int,
    ) -> list[TorrentResult]:
        soup = BeautifulSoup(html_text, "html.parser")
        results: list[TorrentResult] = []
        seen: set[str] = set()

        for row in soup.find_all("tr"):
            direct_cells = row.find_all("td", recursive=False)
            if len(direct_cells) < 6:
                continue

            download_link = row.find(
                "a",
                href=re.compile(r"(download|dl)\.php\?.*id=", re.IGNORECASE),
            )
            if not download_link or not download_link.get("href"):
                continue

            download_url = urljoin(base_url, download_link["href"])
            if download_url in seen:
                continue
            seen.add(download_url)

            detail_link = row.find(
                "a", href=re.compile(r"(details|detail)\.php\?.*id=", re.I)
            )
            title = self._extract_title(detail_link, row)
            if not title:
                continue

            cells = [cell.get_text(" ", strip=True) for cell in direct_cells]
            row_text = " ".join(cells)
            numbers = re.findall(r"\b\d+\b", row_text)
            size = self._extract_size(row_text)
            stats = self._extract_stats(row, numbers)
            subtitle = self._extract_subtitle(cells, title)
            added_at = self._extract_added_at(cells)
            status, progress, status_kind = self._extract_torrent_status(row)

            results.append(
                TorrentResult(
                    index=len(results) + 1,
                    title=title,
                    subtitle=subtitle,
                    size=size,
                    seeders=stats[0],
                    leechers=stats[1],
                    completed=stats[2],
                    detail_url=urljoin(base_url, detail_link["href"])
                    if detail_link
                    else "",
                    download_url=download_url,
                    added_at=added_at,
                    status=status,
                    progress=progress,
                    status_kind=status_kind,
                )
            )
            if len(results) >= limit:
                break

        return results

    async def _search_gaytorrents(
        self,
        keyword: str,
        user_config: dict[str, Any],
        sort_by: str | None,
        descending: bool,
    ) -> list[TorrentResult]:
        provider = self._provider_config(user_config)
        limit = self._int_config("max_results", 10, 1, 30, user_config)
        results: list[TorrentResult] = []
        login_required = False
        for attempt in range(2):
            cookie = await self._gaytorrents_cookie(provider, user_config)
            results, login_required = await self._fetch_gaytorrents_results(
                provider, user_config, keyword, cookie, limit
            )
            if not login_required:
                break
            self._gt_sessions.pop(self._gaytorrents_session_key(provider), None)
            if provider["cookie"]:
                if self._gaytorrents_cookie_is_guest(provider["cookie"]):
                    raise UserFacingError(
                        "Gay-Torrents 的 Cookie 缺少登录字段（ENvbb_userid、"
                        "ENvbb_password），站点仍按访客处理。请在浏览器登录并勾选"
                        " Remember Me，再从开发者工具的请求头复制完整 Cookie。"
                    )
                raise UserFacingError(
                    "Gay-Torrents 登录状态无效，请在浏览器登录站点后重新复制 Cookie。"
                )
            if attempt == 1:
                raise UserFacingError(
                    "Gay-Torrents 自动登录后仍无法访问搜索结果，请检查账号和密码。"
                )
        return self._sort_results(results, sort_by, descending)

    async def _fetch_gaytorrents_results(
        self,
        provider: dict[str, str],
        user_config: dict[str, Any],
        keyword: str,
        cookie: str,
        limit: int,
    ) -> tuple[list[TorrentResult], bool]:
        """分页调用 GT_ajax_search.php，返回（结果列表，是否需要重新登录）。"""
        base_url = provider["base_url"].rstrip("/") + "/"
        results: list[TorrentResult] = []
        seen: set[str] = set()
        # 站点搜索按 vBulletin 布尔模式工作，直接关键词为空时改用布尔写法重试。
        queries = [
            item
            for item in dict.fromkeys([keyword, self._gaytorrents_query(keyword)])
            if item
        ]
        async with self._http_client(
            provider,
            user_config,
            cookie=cookie,
            extra_headers={"Referer": base_url},
        ) as client:
            for query in queries:
                for page in range(GAYTORRENTS_MAX_PAGES):
                    response = await client.get(
                        self._gaytorrents_search_url(provider, query, page)
                    )
                    self._raise_for_response(response, "Gay-Torrents 搜索失败")
                    if self._gaytorrents_requires_login(response.text):
                        return [], True
                    rows = self._parse_gaytorrents_results(
                        response.text, base_url, max(limit - len(results), 1)
                    )
                    fresh = [row for row in rows if row.download_url not in seen]
                    seen.update(row.download_url for row in fresh)
                    results.extend(fresh)
                    if not rows or len(results) >= limit:
                        break
                if results:
                    break

        return results[:limit], False

    @staticmethod
    def _gaytorrents_search_url(
        provider: dict[str, str], keyword: str, page: int
    ) -> str:
        url = urljoin(
            provider["base_url"].rstrip("/") + "/",
            provider["search_path"].format(keyword=quote(keyword)),
        )
        return QBittorrentManagerPlugin._merge_query_params(url, {"page": str(page)})

    @staticmethod
    def _merge_query_params(url: str, params: dict[str, str]) -> str:
        parsed = urlparse(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in params
        ]
        query.extend(params.items())
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _gaytorrents_query(keyword: str) -> str:
        """按站点要求重写关键词：4 个字符以上的英文词加 +，with 是停用词。"""
        words: list[str] = []
        for word in keyword.split():
            if word.lower() == "with":
                continue
            words.append(f"+{word}" if re.fullmatch(r"[A-Za-z0-9]{4,}", word) else word)
        return " ".join(words)

    @staticmethod
    def _gaytorrents_requires_login(text: str) -> bool:
        lowered = text.lower()
        if "torrentdetails.php?torrentid=" in lowered:
            return False
        return (
            "you are not logged in" in lowered
            or 'securitytoken = "guest"' in lowered
            or "vb_login_username" in lowered
        )

    @staticmethod
    def _gaytorrents_cookie_is_guest(cookie: str) -> bool:
        """Cookie 缺少 vBulletin 登录字段（ENvbb_userid / ENvbb_password）时为访客。"""
        names = [part.split("=", 1)[0].strip().lower() for part in cookie.split(";")]
        has_user = any(name.endswith("userid") for name in names)
        has_password = any(
            name.endswith(("passwordhash", "password")) for name in names
        )
        return not (has_user and has_password)

    def _parse_gaytorrents_results(
        self,
        html_text: str,
        base_url: str,
        limit: int,
        allow_unescape: bool = True,
    ) -> list[TorrentResult]:
        fragments = self._gaytorrents_row_fragments(html_text)
        if fragments:
            return self._parse_gaytorrents_fragments(fragments, base_url, limit)

        soup = BeautifulSoup(html_text, "html.parser")
        rows = soup.find_all("torrent")
        if not rows:
            rows = [
                parent
                for parent in (
                    link.find_parent(["li", "tr", "div", "p"]) or link.parent
                    for link in soup.find_all("a", href=GAYTORRENTS_DETAIL_HREF)
                )
                if parent is not None
            ]
        if (
            allow_unescape
            and rows
            and not any(row.find("a", href=GAYTORRENTS_DETAIL_HREF) for row in rows)
        ):
            # 有的响应把条目 HTML 转义后放在标签文本里。
            inner = "\n".join(html.unescape(row.get_text()) for row in rows)
            return self._parse_gaytorrents_results(inner, base_url, limit, False)

        results: list[TorrentResult] = []
        for row in rows:
            result = self._parse_gaytorrents_row(row, base_url)
            if not result:
                continue
            results.append(replace(result, index=len(results) + 1))
            if len(results) >= limit:
                break

        return results

    @staticmethod
    def _gaytorrents_row_fragments(html_text: str) -> list[str]:
        """GT_ajax_search.php 返回 XML，行 HTML 包在 <torrentdata> 的 CDATA 里。"""
        fragments: list[str] = []
        for block in GAYTORRENTS_TORRENTDATA_PATTERN.findall(html_text):
            body = block.strip().removeprefix("<![CDATA[").removesuffix("]]>")
            if "torrentdetails.php" in body:
                fragments.append(body)
        return fragments

    def _parse_gaytorrents_fragments(
        self, fragments: list[str], base_url: str, limit: int
    ) -> list[TorrentResult]:
        results: list[TorrentResult] = []
        for fragment in fragments:
            result = self._parse_gaytorrents_row(
                BeautifulSoup(fragment, "html.parser"), base_url
            )
            if not result:
                continue
            results.append(replace(result, index=len(results) + 1))
            if len(results) >= limit:
                break

        return results

    def _parse_gaytorrents_row(self, row: Any, base_url: str) -> TorrentResult | None:
        link = row.find("a", href=GAYTORRENTS_DETAIL_HREF)
        if link is None or not link.get("href"):
            return None
        detail_url = urljoin(base_url, link["href"])
        separator = "&" if "?" in detail_url else "?"
        download_url = f"{detail_url}{separator}do=download"
        raw_title = (link.get_text(" ", strip=True) or link.get("title") or "").strip()
        if not raw_title:
            return None
        # 行内容以种子链接所在的 <ul> 为准，避免同一个块里的多条种子互相串数据。
        scope = link.find_parent("ul") or row
        freeleech = self._gaytorrents_freeleech(scope, raw_title)
        title = re.sub(r"^\[F?FL\]\s*", "", raw_title, flags=re.IGNORECASE).strip()
        row_text = scope.get_text(" ", strip=True)
        size = self._gaytorrents_cell(scope, "size") or self._extract_size(row_text)
        return TorrentResult(
            index=1,
            title=title,
            subtitle=self._gaytorrents_subtitle(scope, freeleech),
            size=size or "未知",
            seeders=self._gaytorrents_counter(scope, "seeders"),
            leechers=self._gaytorrents_counter(scope, "leechers"),
            # 站点列表不提供完成次数，保持占位符。
            completed=GAYTORRENTS_UNKNOWN_COMPLETED,
            detail_url=detail_url,
            download_url=download_url,
            added_at=self._normalize_gaytorrents_date(
                self._gaytorrents_cell(scope, "added_at")
            ),
        )

    def _gaytorrents_counter(self, row: Any, field: str) -> str:
        """做种/下载数列可能是 “11|” 或带空格，取第一段数字。"""
        match = re.search(r"\d[\d,.]*", self._gaytorrents_cell(row, field))
        return (
            match.group(0).replace(",", "") if match else GAYTORRENTS_UNKNOWN_COMPLETED
        )

    def _gaytorrents_freeleech(self, row: Any, title: str) -> bool:
        if re.match(r"^\[F?FL\]", title, flags=re.IGNORECASE):
            return True
        # 搜索接口的 Torrent-List-FL 单元格只在免费种子里有内容，普通种子只有 &nbsp;。
        if self._gaytorrents_cell(row, "freeleech"):
            return True
        marker = row.find(class_=re.compile("Torrent-List-FL", re.IGNORECASE))
        image = marker.find("img") if marker else None
        return bool(image and "free" in str(image.get("src", "")).lower())

    @staticmethod
    def _gaytorrents_cell(row: Any, field: str) -> str:
        for class_name in GAYTORRENTS_CELL_CLASSES[field]:
            cell = row.find(class_=re.compile(re.escape(class_name), re.IGNORECASE))
            if cell:
                text = cell.get_text(" ", strip=True)
                if text:
                    return text
        return ""

    def _gaytorrents_subtitle(self, row: Any, freeleech: bool) -> str:
        parts: list[str] = []
        category_link = row.find("a", href=GAYTORRENTS_CATEGORY_HREF)
        if category_link and category_link.get("href"):
            query = parse_qs(urlparse(category_link["href"]).query)
            category = unquote(query.get("type", [""])[0]).strip().strip("/")
            if category:
                parts.append(category)
        if freeleech:
            parts.append("免费")
        return " · ".join(parts)

    @staticmethod
    def _normalize_gaytorrents_date(value: str) -> str:
        """站点日期形如 HH:mm dd-MMM-yyyy，统一成 YYYY-MM-DD HH:mm 以支持按时间排序。"""
        text = value.strip()
        if not text:
            return "未知"
        match = re.search(r"(\d{1,2}):(\d{2})\s+(\d{1,2})-([A-Za-z]{3})-(\d{4})", text)
        if match:
            hour, minute, day, month_name, year = match.groups()
            month = GAYTORRENTS_MONTHS.get(month_name.lower())
            if month:
                return f"{int(year):04d}-{month:02d}-{int(day):02d} {hour.zfill(2)}:{minute}"
        match = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", text)
        if match:
            day, month_name, year = match.groups()
            month = GAYTORRENTS_MONTHS.get(month_name.lower())
            if month:
                return f"{int(year):04d}-{month:02d}-{int(day):02d}"
        return text

    async def _gaytorrents_cookie(
        self, provider: dict[str, str], user_config: dict[str, Any]
    ) -> str:
        if provider["cookie"]:
            return provider["cookie"]
        if not provider["username"] or not provider["password"]:
            raise UserFacingError(
                "Gay-Torrents 需要登录：请用 /种子配置 设置 cookie（登录后的 Cookie），"
                "或设置 pt账号 和 pt密码 让插件自动登录。"
            )
        cache_key = self._gaytorrents_session_key(provider)
        cached = self._gt_sessions.get(cache_key)
        if cached and time.time() - cached[0] < GAYTORRENTS_SESSION_TTL_SECONDS:
            return cached[1]
        cookie = await self._login_gaytorrents(provider, user_config)
        self._gt_sessions[cache_key] = (time.time(), cookie)
        return cookie

    @staticmethod
    def _gaytorrents_session_key(provider: dict[str, str]) -> str:
        return f"{provider['base_url']}|{provider['username']}"

    async def _login_gaytorrents(
        self, provider: dict[str, str], user_config: dict[str, Any]
    ) -> str:
        base_url = provider["base_url"].rstrip("/") + "/"
        async with self._http_client(
            provider,
            user_config,
            include_cookie=False,
            extra_headers={"Referer": base_url},
        ) as client:
            response = await client.post(
                urljoin(base_url, GAYTORRENTS_LOGIN_PATH),
                data={
                    "vb_login_username": provider["username"],
                    "vb_login_password": provider["password"],
                    "cookieuser": "1",
                    "do": "login",
                },
            )
            self._raise_for_response(response, "Gay-Torrents 登录失败")
            login_error = self._gaytorrents_login_error(response.text)
            if login_error:
                raise UserFacingError(login_error)
            check = await client.get(urljoin(base_url, GAYTORRENTS_LIST_PATH))
            self._raise_for_response(check, "Gay-Torrents 登录校验失败")
            cookie = "; ".join(
                f"{name}={value}" for name, value in client.cookies.items()
            )
        if not self._gaytorrents_is_logged_in(check.text):
            raise UserFacingError(
                "Gay-Torrents 自动登录失败：站点没有确认登录，"
                "请检查账号密码，或改用登录后的 Cookie。"
            )
        if not cookie:
            raise UserFacingError("Gay-Torrents 登录成功但没有返回 Cookie。")
        return cookie

    @staticmethod
    def _gaytorrents_login_error(text: str) -> str:
        lowered = text.lower()
        for marker, message in GAYTORRENTS_LOGIN_ERRORS:
            if marker in lowered:
                return message
        return ""

    @staticmethod
    def _gaytorrents_is_logged_in(text: str) -> bool:
        if BeautifulSoup is None:
            return "isuser" in text.lower()
        return (
            BeautifulSoup(text, "html.parser").find("ul", class_="isuser") is not None
        )

    async def _add_to_download_client(
        self,
        torrent: TorrentResult,
        user_config: dict[str, Any],
        monitor: MonitorTarget | None = None,
    ) -> str:
        torrent_file = await self._download_torrent_file(torrent, user_config)
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_to_utorrent(torrent_file, user_config)
            return ""
        torrent_hash = await self._add_to_qbittorrent(torrent_file, user_config)
        self._register_download_watch(monitor, user_config, torrent.title, torrent_hash)
        return torrent_hash

    async def _add_direct_download(
        self,
        direct_download: DirectDownload,
        user_config: dict[str, Any],
        monitor: MonitorTarget | None = None,
    ) -> str:
        if direct_download.uri.lower().startswith("magnet:"):
            return await self._add_uri_to_download_client(
                direct_download.uri,
                user_config,
                monitor,
                direct_download.title,
            )

        torrent_file = await self._download_torrent_from_url(
            direct_download.uri, user_config
        )
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_to_utorrent(torrent_file, user_config)
            return ""
        torrent_hash = await self._add_to_qbittorrent(torrent_file, user_config)
        self._register_download_watch(
            monitor, user_config, direct_download.title, torrent_hash
        )
        return torrent_hash

    async def _add_uri_to_download_client(
        self,
        uri: str,
        user_config: dict[str, Any],
        monitor: MonitorTarget | None = None,
        title: str = "",
    ) -> str:
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_uri_to_utorrent(uri, user_config)
            return ""
        torrent_hash = await self._add_uri_to_qbittorrent(uri, user_config)
        self._register_download_watch(
            monitor, user_config, title or self._direct_title(uri), torrent_hash
        )
        return torrent_hash

    def _split_download_backend(self, text: str) -> tuple[str, str]:
        """拆分 /下载 的手动指定写法，例如 /下载 aria2 https://…"""
        tokens = str(text or "").strip().split()
        if not tokens:
            return "", ""
        alias = tokens[0].strip("<>").lower().replace("_", "-")
        backend = DOWNLOAD_BACKEND_ALIASES.get(alias, "")
        if not backend:
            return "", self._clean_uri(" ".join(tokens))
        if len(tokens) < 2:
            return backend, ""
        return backend, self._clean_uri(tokens[1])

    def _forced_download_kind(
        self, backend: str, user_config: dict[str, Any]
    ) -> tuple[str, str]:
        kind = DOWNLOAD_BACKEND_ALIASES[backend]
        if kind == DOWNLOAD_KIND_ARIA2 and not self._aria2_config(user_config)["url"]:
            raise UserFacingError(
                "未配置 aria2 RPC 地址（aria2_rpc_url），"
                "请用 /种子配置 设置 aria2地址 后重试。"
            )
        if kind == DOWNLOAD_KIND_YTDLP and not self._ytdlp_config(user_config)["url"]:
            raise UserFacingError(
                "未配置 yt-dlp 服务地址（ytdlp_api_url），"
                "请用 /种子配置 设置 ytdlp地址 后重试。"
            )
        return kind, "手动指定"

    def _resolve_download_kind(
        self, uri: str, user_config: dict[str, Any]
    ) -> tuple[str, str]:
        kind = self._classify_download_target(uri, user_config)
        if kind == DOWNLOAD_KIND_QBITTORRENT:
            return DOWNLOAD_KIND_QBITTORRENT, "磁链或种子"
        if kind == DOWNLOAD_KIND_YTDLP:
            if not self._ytdlp_config(user_config)["url"]:
                raise UserFacingError(
                    "这是视频网页链接，但没有配置 yt-dlp 服务。"
                    "请在插件配置里填写 ytdlp_api_url，或用 /种子配置 设置 ytdlp地址。"
                )
            return DOWNLOAD_KIND_YTDLP, "视频网页"
        if self._aria2_config(user_config)["url"]:
            return DOWNLOAD_KIND_ARIA2, "普通直链"
        return (
            DOWNLOAD_KIND_QBITTORRENT,
            "普通直链（aria2 未配置，已改用 qBittorrent）",
        )

    def _classify_download_target(self, uri: str, user_config: dict[str, Any]) -> str:
        if uri.lower().startswith("magnet:"):
            return DOWNLOAD_KIND_QBITTORRENT
        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise UserFacingError("链接格式不支持，请提供 magnet、http 或 https 链接。")
        path = unquote(parsed.path).lower()
        name = path.rsplit("/", 1)[-1]
        torrent_marker = (
            path.endswith(".torrent")
            or "do=download" in parsed.query.lower()
            or (name.startswith(("download", "dl")) and name.endswith(".php"))
        )
        if torrent_marker:
            return DOWNLOAD_KIND_QBITTORRENT
        if path.endswith(MEDIA_URL_SUFFIXES):
            return DOWNLOAD_KIND_ARIA2
        host = self._url_host(uri)
        patterns = self._video_host_patterns(user_config)
        if any(self._host_matches(host, pattern) for pattern in patterns):
            return DOWNLOAD_KIND_YTDLP
        if self._looks_like_web_page(name):
            # 链接最后一段没有文件后缀，多半是视频站网页，交给 yt-dlp 解析
            return DOWNLOAD_KIND_YTDLP
        return DOWNLOAD_KIND_ARIA2

    @staticmethod
    def _url_host(uri: str) -> str:
        netloc = urlparse(uri).netloc
        return netloc.rsplit("@", 1)[-1].split(":")[0].strip().lower()

    @staticmethod
    def _video_host_patterns(user_config: dict[str, Any]) -> tuple[str, ...]:
        raw = f"{VIDEO_HOST_DEFAULT},{user_config.get('video_hosts') or ''}"
        hosts = {
            part.strip().lower().lstrip(".")
            for part in re.split(r"[,\s;，、]+", raw)
            if part.strip()
        }
        return tuple(sorted(hosts))

    @staticmethod
    def _host_matches(host: str, pattern: str) -> bool:
        if not pattern:
            return False
        return host == pattern or host.endswith(f".{pattern}")

    @staticmethod
    def _looks_like_web_page(name: str) -> bool:
        """链接最后一段没有文件后缀时，按网页链接处理。"""
        suffix = name.rsplit(".", 1)[-1] if "." in name else ""
        return not re.fullmatch(r"[0-9a-z]{1,5}", suffix)

    @staticmethod
    def _clean_uri(uri: str) -> str:
        tokens = str(uri or "").split()
        if not tokens:
            return ""
        return tokens[0].strip("<>")

    def _download_title(self, uri: str, kind: str) -> str:
        if kind == DOWNLOAD_KIND_YTDLP:
            host = self._url_host(uri)
            path = unquote(urlparse(uri).path).strip("/")
            title = f"视频｜{host}/{path}" if path else f"视频｜{host}"
            return self._short_title(title)
        return self._direct_title(uri)

    def _aria2_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        return {
            "url": str(user_config.get("aria2_rpc_url") or "").strip(),
            "token": str(user_config.get("aria2_rpc_token") or "").strip(),
            "save_path": str(user_config.get("aria2_save_path") or "").strip(),
        }

    def _ytdlp_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        return {
            "url": str(user_config.get("ytdlp_api_url") or "").strip().rstrip("/"),
            "api_key": str(user_config.get("ytdlp_api_key") or "").strip(),
            "format": str(user_config.get("ytdlp_format") or "").strip(),
        }

    @staticmethod
    def _ytdlp_headers(api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        return headers

    @staticmethod
    def _aria2_output_name(uri: str) -> str:
        name = unquote(urlparse(uri).path).rstrip("/").rsplit("/", 1)[-1]
        name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
        return name[:120]

    async def _add_to_aria2(self, uri: str, user_config: dict[str, Any]) -> str:
        aria2 = self._aria2_config(user_config)
        if not aria2["url"]:
            raise UserFacingError("未配置 aria2 RPC 地址（aria2_rpc_url）。")
        options: dict[str, str] = {}
        if aria2["save_path"]:
            options["dir"] = aria2["save_path"]
        filename = self._aria2_output_name(uri)
        if filename:
            options["out"] = filename
        try:
            gid = await self._aria2_call(
                aria2["url"],
                aria2["token"],
                "aria2.addUri",
                [[uri], options],
            )
        except httpx.TransportError as error:
            raise UserFacingError(
                f"无法连接 aria2 RPC（{aria2['url']}）。"
                "请确认 aria2 已用 --enable-rpc 启动，并检查 aria2_rpc_url 配置。"
            ) from error
        if not isinstance(gid, str) or not gid:
            raise UserFacingError("aria2 没有返回任务 ID。")
        return gid

    async def _aria2_call(
        self, rpc_url: str, token: str, method: str, params: list[Any]
    ) -> Any:
        # aria2 只在配置了 --rpc-secret 时要求 token: 前缀的鉴权参数
        args = [f"token:{token}"] if token else []
        args.extend(params)
        payload = {
            "jsonrpc": "2.0",
            "id": "astrbot_qbittorrent_manager",
            "method": method,
            "params": args,
        }
        async with httpx.AsyncClient(
            timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0)
        ) as client:
            response = await client.post(rpc_url, json=payload)
        self._raise_for_response(response, "aria2 RPC 请求失败")
        try:
            data = response.json()
        except ValueError as error:
            raise UserFacingError("aria2 RPC 返回了无法解析的内容。") from error
        if not isinstance(data, dict):
            raise UserFacingError("aria2 RPC 返回格式异常。")
        if data.get("error"):
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise UserFacingError(f"aria2 返回错误：{message}")
        return data.get("result")

    async def _add_to_ytdlp(self, uri: str, user_config: dict[str, Any]) -> str:
        ytdlp = self._ytdlp_config(user_config)
        if not ytdlp["url"]:
            raise UserFacingError("未配置 yt-dlp 服务地址（ytdlp_api_url）。")
        payload: dict[str, str] = {"url": uri}
        if ytdlp["format"]:
            payload["video_format"] = ytdlp["format"]
        endpoint = urljoin(f"{ytdlp['url']}/", "get_video")
        try:
            async with httpx.AsyncClient(
                headers=self._ytdlp_headers(ytdlp["api_key"]),
                timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0),
            ) as client:
                response = await client.post(endpoint, json=payload)
        except httpx.TransportError as error:
            raise UserFacingError(
                f"无法连接 yt-dlp 服务（{ytdlp['url']}），请检查 ytdlp_api_url 配置。"
            ) from error
        self._raise_for_response(response, "yt-dlp 服务请求失败")
        try:
            data = response.json()
        except ValueError as error:
            raise UserFacingError("yt-dlp 服务返回了无法解析的内容。") from error
        if not isinstance(data, dict):
            raise UserFacingError("yt-dlp 服务返回格式异常。")
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            reason = str(data.get("error") or "").strip()
            raise UserFacingError(f"yt-dlp 服务没有接受该链接。{reason}".strip())
        return task_id

    async def _download_torrent_file(
        self, torrent: TorrentResult, user_config: dict[str, Any]
    ) -> TorrentFile:
        provider = self._provider_config(user_config)
        if provider["type"] == "mteam" and torrent.download_url.startswith("mteam:"):
            return await self._download_mteam_torrent(
                torrent.download_url.removeprefix("mteam:"), torrent.title, user_config
            )
        cookie = (
            await self._gaytorrents_cookie(provider, user_config)
            if provider["type"] == "gaytorrents"
            else None
        )
        async with self._http_client(provider, user_config, cookie=cookie) as pt_client:
            torrent_response = await pt_client.get(
                torrent.download_url, headers={"Referer": torrent.detail_url}
            )
            self._raise_for_response(torrent_response, "下载种子文件失败")
            if provider["type"] == "gaytorrents":
                self._ensure_gaytorrents_torrent(torrent, torrent_response)

        return TorrentFile(
            filename=self._safe_filename(torrent.title),
            content=torrent_response.content,
            content_type=mimetypes.types_map.get(
                ".torrent", "application/x-bittorrent"
            ),
        )

    def _ensure_gaytorrents_torrent(
        self, torrent: TorrentResult, response: httpx.Response
    ):
        """登录失效时站点会返回登录页而不是 .torrent，这里给出可读错误。"""
        if self._looks_like_torrent_response(torrent.download_url, response):
            return
        raise UserFacingError(
            "Gay-Torrents 没有返回种子文件，登录状态可能已失效，"
            "请更新 Cookie 或账号配置。"
        )

    async def _download_mteam_torrent(
        self, torrent_id: str, title: str, user_config: dict[str, Any]
    ) -> TorrentFile:
        provider = self._provider_config(user_config)
        headers = {
            "Accept": "application/json",
            "x-api-key": provider["api_token"],
        }
        timeout = self._float_config("request_timeout", 20.0, 5.0, 120.0, user_config)
        async with httpx.AsyncClient(
            base_url=provider["base_url"].rstrip("/"),
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            token_response = await client.post(
                provider["download_path"], params={"id": torrent_id}
            )
            self._raise_for_response(token_response, "M-Team API 获取下载链接失败")
            try:
                payload = token_response.json()
            except ValueError as error:
                raise UserFacingError("M-Team API 下载链接响应不是有效 JSON。") from error
            self._check_mteam_api_result(payload, "M-Team API 获取下载链接失败")
            download_url = self._mteam_download_url(payload)
            if not download_url:
                raise UserFacingError("M-Team API 未返回种子下载链接。")
            torrent_response = await client.get(download_url, headers={"Accept": "application/x-bittorrent"})
            self._raise_for_response(torrent_response, "下载 M-Team 种子文件失败")
        if not torrent_response.content.lstrip().startswith(b"d"):
            raise UserFacingError("M-Team 返回的内容不是有效种子文件。")
        return TorrentFile(
            filename=self._safe_filename(title),
            content=torrent_response.content,
            content_type="application/x-bittorrent",
        )

    @classmethod
    def _mteam_download_url(cls, payload: Any) -> str:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, dict):
            return ""
        for key in ("data", "url", "downloadUrl", "download_url"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict):
                nested = cls._mteam_download_url(value)
                if nested:
                    return nested
        return ""

    async def _download_torrent_from_url(
        self, uri: str, user_config: dict[str, Any]
    ) -> TorrentFile:
        provider = self._provider_config(user_config)
        include_cookie = self._same_origin(uri, provider["base_url"])
        cookie = None
        if include_cookie and provider["type"] == "gaytorrents":
            cookie = await self._gaytorrents_cookie(provider, user_config)
        async with self._http_client(
            provider, user_config, include_cookie=include_cookie, cookie=cookie
        ) as client:
            response = await client.get(uri)
            self._raise_for_response(response, "下载种子链接失败")

        max_size = (
            self._int_config("direct_torrent_max_size_mb", 50, 1, 500, user_config)
            * 1024
            * 1024
        )
        if len(response.content) > max_size:
            raise UserFacingError(
                "种子链接响应过大，已拒绝上传。请确认链接会直接下载 .torrent 文件。"
            )

        if not self._looks_like_torrent_response(uri, response):
            raise UserFacingError(
                "该链接不像 .torrent 文件。如果这是磁链请以 magnet:? 开头；"
                "如果是普通下载链接，请确认浏览器打开会直接下载种子文件。"
            )

        filename = self._filename_from_response(uri, response)
        return TorrentFile(
            filename=filename,
            content=response.content,
            content_type=response.headers.get(
                "content-type", "application/x-bittorrent"
            ).split(";")[0],
        )

    async def _add_to_qbittorrent(
        self, torrent_file: TorrentFile, user_config: dict[str, Any]
    ) -> str:
        qb_config = self._qbittorrent_config(user_config)
        save_path = (user_config.get("qb_save_path") or "").strip()
        category = (user_config.get("qb_category") or "").strip()
        paused = bool(user_config.get("qb_paused", False))

        async with httpx.AsyncClient(
            base_url=qb_config["url"],
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
        ) as qb_client:
            await self._login_qbittorrent(qb_client, qb_config)
            known = await self._qbittorrent_hashes(qb_client)

            data: dict[str, Any] = {"paused": "true" if paused else "false"}
            if save_path:
                data["savepath"] = save_path
            if category:
                data["category"] = category

            files = {
                "torrents": (
                    torrent_file.filename,
                    torrent_file.content,
                    torrent_file.content_type,
                )
            }
            add_response = await qb_client.post(
                "/api/v2/torrents/add", data=data, files=files
            )
            self._check_qbittorrent_add_response(
                add_response, "qBittorrent 添加任务失败"
            )
            return await self._resolve_qbittorrent_hash(
                qb_client,
                known,
                self._torrent_btih(torrent_file.content),
                torrent_file.filename,
            )

    async def _add_uri_to_qbittorrent(
        self, uri: str, user_config: dict[str, Any]
    ) -> str:
        qb_config = self._qbittorrent_config(user_config)
        save_path = (user_config.get("qb_save_path") or "").strip()
        category = (user_config.get("qb_category") or "").strip()
        paused = bool(user_config.get("qb_paused", False))

        async with httpx.AsyncClient(
            base_url=qb_config["url"],
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
        ) as qb_client:
            await self._login_qbittorrent(qb_client, qb_config)
            known = await self._qbittorrent_hashes(qb_client)

            data: dict[str, Any] = {
                "urls": uri,
                "paused": "true" if paused else "false",
            }
            if save_path:
                data["savepath"] = save_path
            if category:
                data["category"] = category

            add_response = await qb_client.post("/api/v2/torrents/add", data=data)
            self._check_qbittorrent_add_response(
                add_response, "qBittorrent 添加链接失败"
            )
            magnet_hash = self._magnet_btih(uri)
            return await self._resolve_qbittorrent_hash(
                qb_client, known, magnet_hash, self._direct_title(uri)
            )

    async def _qbittorrent_hashes(self, qb_client: httpx.AsyncClient) -> set[str]:
        """添加任务前的任务快照，用于识别新加的任务；列表接口不可用时返回空集合。"""
        try:
            tasks = await self._qbittorrent_tasks(qb_client)
        except Exception as error:  # noqa: BLE001
            logger.warning("读取 qBittorrent 任务列表失败：%s", error)
            return set()
        return {
            str(task.get("hash") or "").strip().lower()
            for task in tasks
            if task.get("hash")
        }

    async def _resolve_qbittorrent_hash(
        self,
        qb_client: httpx.AsyncClient,
        known: set[str],
        expected_hash: str,
        expected_name: str,
    ) -> str:
        """优先用种子自身的 info hash，其次用添加前后的任务差集，拿不到返回空串。"""
        try:
            tasks = await self._qbittorrent_tasks(qb_client)
        except Exception as error:  # noqa: BLE001
            logger.warning("查询 qBittorrent 新任务失败：%s", error)
            return expected_hash
        hashes = [str(task.get("hash") or "").strip().lower() for task in tasks]
        if expected_hash and expected_hash in hashes:
            return expected_hash
        fresh = [
            task
            for task, digest in zip(tasks, hashes)
            if digest and digest not in known
        ]
        if not fresh:
            return expected_hash
        if len(fresh) > 1:
            target = self._normalize_title(expected_name)
            matched = [
                task
                for task in fresh
                if target
                and target in self._normalize_title(str(task.get("name") or ""))
            ]
            fresh = matched or fresh
        fresh.sort(key=lambda task: self._int_value(task.get("added_on")), reverse=True)
        return str(fresh[0].get("hash") or "").strip().lower()

    @staticmethod
    async def _qbittorrent_tasks(
        qb_client: httpx.AsyncClient, hashes: list[str] | None = None
    ) -> list[dict[str, Any]]:
        params = {"filter": "all"} if hashes is None else {"hashes": ",".join(hashes)}
        response = await qb_client.get("/api/v2/torrents/info", params=params)
        QBittorrentManagerPlugin._raise_for_response(
            response, "查询 qBittorrent 任务列表失败"
        )
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _bdecode_skip(data: bytes, index: int) -> int:
        """跳过（不解析）一个 bencode 值，返回它的结束位置。"""
        marker = data[index : index + 1]
        if marker == b"i":
            return data.index(b"e", index) + 1
        if marker in (b"d", b"l"):
            cursor = index + 1
            while data[cursor : cursor + 1] not in (b"e", b""):
                cursor = QBittorrentManagerPlugin._bdecode_skip(data, cursor)
                if marker == b"d":
                    cursor = QBittorrentManagerPlugin._bdecode_skip(data, cursor)
            return cursor + 1
        if marker.isdigit():
            colon = data.index(b":", index)
            length = int(data[index:colon])
            return colon + 1 + max(length, 0)
        raise ValueError("不是有效的 bencode 数据")

    @staticmethod
    def _bdecode_key(data: bytes, index: int) -> tuple[bytes, int]:
        colon = data.index(b":", index)
        length = int(data[index:colon])
        start = colon + 1
        return data[start : start + max(length, 0)], start + max(length, 0)

    @classmethod
    def _torrent_btih(cls, content: bytes) -> str:
        """计算 .torrent 的 v1 info hash，也就是 qBittorrent 里的任务 hash。"""
        if not content.startswith(b"d"):
            return ""
        try:
            cursor = 1
            while content[cursor : cursor + 1] not in (b"e", b""):
                key, cursor = cls._bdecode_key(content, cursor)
                value_start = cursor
                cursor = cls._bdecode_skip(content, cursor)
                if key == b"info":
                    return hashlib.sha1(content[value_start:cursor]).hexdigest()
        except (ValueError, IndexError):
            return ""
        return ""

    @staticmethod
    def _magnet_btih(uri: str) -> str:
        if not uri.lower().startswith("magnet:"):
            return ""
        query = urlparse(uri).query
        for target in parse_qs(query).get("xt", []):
            if not target.lower().startswith("urn:btih:"):
                continue
            digest = target.split(":", 2)[2].strip()
            if re.fullmatch(r"[0-9a-fA-F]{40}", digest):
                return digest.lower()
            if re.fullmatch(r"[A-Za-z2-7]{32}", digest):
                try:
                    return base64.b32decode(digest.upper()).hex()
                except ValueError:
                    return ""
        return ""

    def _monitor_kind(self, kind: str | None, user_config: dict[str, Any]) -> str:
        """qb 这一路按配置里的真实客户端判定，uTorrent 不支持下载状态监控。"""
        if kind in (None, "", DOWNLOAD_KIND_QBITTORRENT):
            return self._download_client_type(user_config)
        return kind

    def _monitor_target(
        self,
        event: AstrMessageEvent,
        user_config: dict[str, Any],
        kind: str | None = None,
    ) -> MonitorTarget | None:
        """决定要不要为这次添加建立监控，不需要时返回 None。"""
        if not bool(user_config.get("monitor_enabled", True)):
            return None
        if self._monitor_kind(kind, user_config) not in MONITORABLE_KINDS:
            return None
        if not self._monitor_push_available():
            return None
        session = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not session:
            return None
        user_id = self._user_id(event)
        return MonitorTarget(
            session=session,
            user_id=user_id,
            sender_id=str(
                getattr(event, "get_sender_id", lambda: user_id)() or user_id
            ),
            group=self._event_is_group(event),
        )

    @staticmethod
    def _event_is_group(event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_private_chat", None)
        if not callable(checker):
            return False
        try:
            return not bool(checker())
        except Exception:  # noqa: BLE001
            return False

    def _monitor_push_available(self) -> bool:
        if MessageChain is None or Plain is None:
            return False
        return hasattr(getattr(self, "context", None), "send_message")

    def _monitor_note(
        self,
        user_config: dict[str, Any],
        monitor: Any,
        added: int,
        kind: str | None = None,
    ) -> str:
        if added <= 0:
            return ""
        if monitor is not None:
            return (
                f"已加入下载状态监控，每 {self._monitor_interval_minutes()} 分钟汇报一次，"
                "下载完成后会通知你。"
            )
        kind = self._monitor_kind(kind, user_config)
        if not bool(user_config.get("monitor_enabled", True)):
            return "提示：你已关闭下载状态监控。"
        if kind not in MONITORABLE_KINDS:
            client_name = self._download_client_name(user_config)
            return f"提示：{client_name} 暂不支持下载状态监控。"
        return "提示：当前 AstrBot 版本不支持主动推送，已跳过下载状态监控。"

    def _register_download_watch(
        self,
        monitor: MonitorTarget | None,
        user_config: dict[str, Any],
        title: str,
        task_ref: str,
        kind: str = DOWNLOAD_KIND_QBITTORRENT,
    ) -> None:
        if monitor is None or not task_ref:
            return
        endpoint, username, credential = self._watch_credentials(kind, user_config)
        if not endpoint:
            return
        watch = DownloadWatch(
            session=monitor.session,
            user_id=monitor.user_id,
            sender_id=monitor.sender_id,
            group=monitor.group,
            title=title,
            task_ref=task_ref,
            kind=kind,
            endpoint=endpoint,
            username=username,
            credential=credential,
        )
        self._download_watches[watch.watch_key] = watch
        self._prune_watches()
        self._ensure_monitor_task()

    def _watch_credentials(
        self, kind: str, user_config: dict[str, Any]
    ) -> tuple[str, str, str]:
        if kind == DOWNLOAD_KIND_ARIA2:
            aria2 = self._aria2_config(user_config)
            return aria2["url"], "", aria2["token"]
        if kind == DOWNLOAD_KIND_YTDLP:
            ytdlp = self._ytdlp_config(user_config)
            return ytdlp["url"], "", ytdlp["api_key"]
        qb_config = self._qbittorrent_config(user_config)
        return qb_config["url"], qb_config["username"], qb_config["password"]

    def _prune_watches(self) -> None:
        extra = len(self._download_watches) - MONITOR_MAX_WATCHES
        if extra <= 0:
            return
        oldest = sorted(
            self._download_watches.values(), key=lambda watch: watch.registered_at
        )[:extra]
        for watch in oldest:
            self._download_watches.pop(watch.watch_key, None)
        logger.warning(
            "下载状态监控数量已达上限 %d，停止监控最早的 %d 个任务。",
            MONITOR_MAX_WATCHES,
            extra,
        )

    def _user_watches(self, event: AstrMessageEvent) -> list[DownloadWatch]:
        user_id = self._user_id(event)
        session = str(getattr(event, "unified_msg_origin", "") or "").strip()
        watches = [
            watch
            for watch in self._download_watches.values()
            if watch.user_id == user_id and (not session or watch.session == session)
        ]
        return sorted(watches, key=lambda watch: watch.registered_at)

    @staticmethod
    def _pick_watches(
        selection: str, watches: list[DownloadWatch]
    ) -> list[DownloadWatch]:
        picked: list[DownloadWatch] = []
        for token in re.split(r"[\s,，]+", selection.strip()):
            if not token:
                continue
            if not token.isdigit():
                raise UserFacingError("序号只能是数字，例如 /种子监控 停止 1 2。")
            index = int(token)
            if not 1 <= index <= len(watches):
                raise UserFacingError(
                    f"序号 {index} 超出范围，当前监控 {len(watches)} 个任务。"
                )
            watch = watches[index - 1]
            if watch not in picked:
                picked.append(watch)
        if not picked:
            raise UserFacingError("请选择要停止监控的序号。")
        return picked

    def _monitor_interval_minutes(self) -> int:
        return self._int_config(
            "monitor_interval_minutes",
            MONITOR_DEFAULT_INTERVAL_MINUTES,
            1,
            120,
        )

    def _monitor_max_hours(self) -> int:
        return self._int_config("monitor_max_hours", MONITOR_DEFAULT_MAX_HOURS, 1, 168)

    def _monitor_complete_message(self) -> str:
        message = str(self.config.get("monitor_complete_message") or "").strip()
        return message or MONITOR_DEFAULT_COMPLETE_MESSAGE

    def _ensure_monitor_task(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("当前没有运行中的事件循环，未启动下载状态监控。")
            return
        self._monitor_task = asyncio.create_task(self._monitor_downloads())

    async def _monitor_downloads(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._monitor_interval_minutes() * 60)
                await self._monitor_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("下载状态监控任务出错")

    async def _monitor_once(self) -> None:
        watches = list(self._download_watches.values())
        if not watches:
            return
        report_progress = bool(self.config.get("monitor_report_progress", True))
        max_age = self._monitor_max_hours() * 3600
        groups: dict[str, list[DownloadWatch]] = {}
        for watch in watches:
            groups.setdefault(watch.client_key, []).append(watch)
        for group in groups.values():
            entries = await self._fetch_task_map(group)
            if entries is None:
                continue
            for watch in group:
                await self._report_download_watch(
                    watch,
                    entries.get(watch.task_ref),
                    max_age,
                    report_progress,
                )

    async def _fetch_task_map(
        self, watches: list[DownloadWatch]
    ) -> dict[str, dict[str, Any]] | None:
        kind = watches[0].kind
        if kind == DOWNLOAD_KIND_ARIA2:
            return await self._fetch_aria2_task_map(watches)
        if kind == DOWNLOAD_KIND_YTDLP:
            return await self._fetch_ytdlp_task_map(watches)
        return await self._fetch_qbittorrent_task_map(watches)

    async def _fetch_qbittorrent_task_map(
        self, watches: list[DownloadWatch]
    ) -> dict[str, dict[str, Any]] | None:
        first = watches[0]
        qb_config = {
            "url": first.endpoint,
            "username": first.username,
            "password": first.credential,
        }
        hashes = [watch.task_ref for watch in watches]
        try:
            async with httpx.AsyncClient(
                base_url=qb_config["url"],
                timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0),
            ) as qb_client:
                await self._login_qbittorrent(qb_client, qb_config)
                tasks = await self._qbittorrent_tasks(qb_client, hashes)
        except Exception as error:  # noqa: BLE001
            logger.warning(
                "查询 qBittorrent 下载状态失败（%s）：%s", qb_config["url"], error
            )
            return None
        return {
            str(task.get("hash") or "").strip().lower(): task
            for task in tasks
            if task.get("hash")
        }

    async def _fetch_aria2_task_map(
        self, watches: list[DownloadWatch]
    ) -> dict[str, dict[str, Any]] | None:
        entries: dict[str, dict[str, Any]] = {}
        for watch in watches:
            try:
                raw = await self._aria2_call(
                    watch.endpoint,
                    watch.credential,
                    "aria2.tellStatus",
                    # gid 必须是字符串参数，写成 [gid] 会被 aria2 判成
                    # “The parameter at 0 has wrong type.”
                    [watch.task_ref, list(ARIA2_TASK_FIELDS)],
                )
            except UserFacingError as error:
                logger.warning("查询 aria2 任务状态失败：%s", error)
                continue
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "查询 aria2 下载状态失败（%s）：%s", watch.endpoint, error
                )
                return None
            if isinstance(raw, dict):
                entries[watch.task_ref] = self._aria2_entry(raw)
        return entries

    async def _fetch_ytdlp_task_map(
        self, watches: list[DownloadWatch]
    ) -> dict[str, dict[str, Any]] | None:
        first = watches[0]
        entries: dict[str, dict[str, Any]] = {}
        status_base = urljoin(f"{first.endpoint.rstrip('/')}/", "status/")
        try:
            async with httpx.AsyncClient(
                headers=self._ytdlp_headers(first.credential),
                timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0),
            ) as client:
                for watch in watches:
                    response = await client.get(f"{status_base}{watch.task_ref}")
                    if response.status_code == 404:
                        continue
                    self._raise_for_response(response, "yt-dlp 状态查询失败")
                    payload = response.json()
                    if isinstance(payload, dict):
                        entries[watch.task_ref] = self._ytdlp_entry(payload, watch)
        except Exception as error:  # noqa: BLE001
            logger.warning("查询 yt-dlp 下载状态失败（%s）：%s", first.endpoint, error)
            return None
        return entries

    @staticmethod
    def _aria2_entry(raw: dict[str, Any]) -> dict[str, Any]:
        total = QBittorrentManagerPlugin._int_value(raw.get("totalLength"))
        completed = QBittorrentManagerPlugin._int_value(raw.get("completedLength"))
        status = str(raw.get("status") or "")
        progress: float | None = None
        if status == "complete":
            progress = 1.0
        elif total > 0 and completed >= 0:
            progress = completed / total
        speed = QBittorrentManagerPlugin._int_value(raw.get("downloadSpeed"))
        eta = -1
        if 0 < speed and total > 0 and completed >= 0:
            eta = max(0, (total - completed) // speed)
        name = ""
        files = raw.get("files")
        if isinstance(files, list) and files and isinstance(files[0], dict):
            name = str(files[0].get("path") or "").replace("\\", "/").rsplit("/", 1)[-1]
        error = str(raw.get("errorMessage") or "").strip()
        if not error:
            # aria2 的正常任务也带 errorCode=0，不能当成错误
            code = str(raw.get("errorCode") or "").strip()
            error = "" if code in ("", "0") else code
        return {
            "name": name,
            "state": status,
            "status": status,
            "progress": progress,
            "size": total,
            "received": completed,
            "dlspeed": speed,
            "upspeed": QBittorrentManagerPlugin._int_value(raw.get("uploadSpeed")),
            "eta": eta,
            "error": error,
        }

    @staticmethod
    def _ytdlp_entry(raw: dict[str, Any], watch: DownloadWatch) -> dict[str, Any]:
        status = str(raw.get("status") or "")
        error = str(raw.get("error") or "").strip()
        name = str(raw.get("output_filename") or "").strip()
        if not name:
            file_ref = str(raw.get("file") or "").strip()
            if file_ref:
                name = file_ref.replace("\\", "/").rsplit("/", 1)[-1]
        return {
            "name": name or watch.title,
            "state": status,
            "status": status,
            "progress": 1.0 if status == "completed" else None,
            "size": -1,
            "received": -1,
            "dlspeed": -1,
            "upspeed": -1,
            "eta": -1,
            "error": error,
        }

    async def _report_download_watch(
        self,
        watch: DownloadWatch,
        entry: dict[str, Any] | None,
        max_age: float,
        report_progress: bool,
    ) -> None:
        title = self._short_title(watch.title)
        if entry is None or str(entry.get("status") or "") == "removed":
            self._download_watches.pop(watch.watch_key, None)
            await self._send_monitor_message(
                watch,
                f"{watch.client_name} 中已找不到「{title}」，已停止汇报下载状态。",
            )
            return
        if self._entry_completed(watch.kind, entry):
            self._download_watches.pop(watch.watch_key, None)
            await self._send_monitor_message(
                watch, self._monitor_complete_message(), mention=True
            )
            return
        failure = self._entry_failure(watch.kind, entry)
        if failure:
            self._download_watches.pop(watch.watch_key, None)
            text = f"「{title}」下载失败：{failure}，已停止汇报下载状态。"
            hint = self._download_failure_hint(watch.kind, failure)
            if hint:
                text = f"{text}\n提示：{hint}"
            await self._send_monitor_message(watch, text)
            return
        if time.time() - watch.registered_at > max_age:
            self._download_watches.pop(watch.watch_key, None)
            await self._send_monitor_message(
                watch,
                f"「{title}」下载超过 {self._monitor_max_hours()} 小时仍未完成，"
                "已停止汇报下载状态。",
            )
            return
        watch.report_count += 1
        if watch.report_count > MONITOR_MAX_REPORTS:
            self._download_watches.pop(watch.watch_key, None)
            await self._send_monitor_message(
                watch,
                f"「{title}」已汇报 {MONITOR_MAX_REPORTS} 次仍未完成，已停止汇报。",
            )
            return
        if not report_progress:
            return
        await self._send_monitor_message(
            watch, self._format_progress_text(watch, entry)
        )

    @staticmethod
    def _entry_completed(kind: str, entry: dict[str, Any]) -> bool:
        if kind == DOWNLOAD_KIND_ARIA2:
            return str(entry.get("status") or "") == "complete"
        if kind == DOWNLOAD_KIND_YTDLP:
            return str(entry.get("status") or "") == "completed"
        return QBittorrentManagerPlugin._qbittorrent_entry_completed(entry)

    @staticmethod
    def _download_failure_hint(kind: str, failure: str) -> str:
        """把后端的下载错误翻译成可操作的提示。"""
        text = failure.lower()
        if kind == DOWNLOAD_KIND_YTDLP:
            if failure in ("", "未知错误"):
                return (
                    "yt-dlp 服务没有返回错误详情，"
                    "请在服务机器上执行 docker compose logs worker 查看原因。"
                )
            if "unsupported url" in text:
                return "yt-dlp 不支持该站点，可以改用 /下载 aria2 链接 直接下载文件。"
            if any(
                marker in text
                for marker in ("cloudflare", "403", "impersonate", "anti-bot")
            ):
                return (
                    "站点有反爬或 Cloudflare 拦截，"
                    "给 yt-dlp 服务安装 curl_cffi 并开启 impersonate"
                    "（YTDLP_IMPERSONATE=chrome），或在浏览器登录后导出 Cookie。"
                )
            if any(
                marker in text
                for marker in (
                    "unable to download webpage",
                    "connection reset",
                    "network is unreachable",
                    "timed out",
                )
            ):
                return (
                    "yt-dlp 服务访问不了这个站点，请检查服务的 YTDLP_PROXY 代理设置。"
                )
        return ""

    @staticmethod
    def _entry_failure(kind: str, entry: dict[str, Any]) -> str:
        if kind == DOWNLOAD_KIND_QBITTORRENT:
            state = str(entry.get("state") or "")
            if state in {"error", "missingFiles"}:
                return QB_STATE_LABELS.get(state, state)
            return ""
        if str(entry.get("status") or "") == "error":
            return str(entry.get("error") or "").strip() or "未知错误"
        return ""

    @staticmethod
    def _qbittorrent_entry_completed(entry: dict[str, Any]) -> bool:
        state = str(entry.get("state") or "")
        status = str(entry.get("status") or "")
        if status == "uploaded" or state in QB_COMPLETE_STATES:
            return True
        if state.endswith("UP"):
            return True
        return QBittorrentManagerPlugin._float_value(entry.get("progress")) >= 1

    def _format_progress_text(self, watch: DownloadWatch, entry: dict[str, Any]) -> str:
        state = str(entry.get("state") or "")
        labels = QB_STATE_LABELS
        if watch.kind == DOWNLOAD_KIND_ARIA2:
            labels = ARIA2_STATUS_LABELS
        elif watch.kind == DOWNLOAD_KIND_YTDLP:
            labels = YTDLP_STATUS_LABELS
        state_label = labels.get(state) or (state or "未知状态")
        name = str(entry.get("name") or "").strip() or watch.title
        lines = [f"下载进度｜{self._short_title(name)}"]
        ratio = self._float_value(entry.get("progress"))
        if ratio >= 0:
            percent = ratio * 100 if ratio <= 1 else ratio
            lines.append(f"状态：{state_label} ｜ 进度：{percent:.1f}%")
        else:
            lines.append(f"状态：{state_label}")
        if watch.kind in (DOWNLOAD_KIND_ARIA2, DOWNLOAD_KIND_YTDLP):
            lines.append(f"来源：{DOWNLOAD_CLIENT_NAMES[watch.kind]}")
        size = self._int_value(entry.get("size"))
        received = self._int_value(entry.get("received"))
        if size > 0 and received >= 0:
            lines.append(
                f"已下载：{self._format_bytes(received)} / {self._format_bytes(size)}"
            )
        error = str(entry.get("error") or "").strip()
        if error:
            lines.append(f"错误：{error}")
        if watch.kind != DOWNLOAD_KIND_YTDLP and not state.endswith("UP"):
            lines.append(
                f"速度：↓{self._format_speed(entry.get('dlspeed'))} "
                f"↑{self._format_speed(entry.get('upspeed'))}"
            )
            lines.append(f"预计剩余：{self._format_eta(entry.get('eta'))}")
        lines.append("完成下载后会通知你，随时可用 /种子监控 查看。")
        return "\n".join(lines)

    async def _send_monitor_message(
        self, watch: DownloadWatch, text: str, *, mention: bool = False
    ) -> None:
        chain = self._monitor_message(watch, text, mention)
        if chain is None:
            return
        try:
            await self.context.send_message(watch.session, chain)
        except Exception:  # noqa: BLE001
            logger.exception("推送下载状态失败：%s", watch.title)

    @staticmethod
    def _monitor_message(watch: DownloadWatch, text: str, mention: bool) -> Any:
        if MessageChain is None or Plain is None:
            return None
        components: list[Any] = []
        if mention and watch.group and At is not None and watch.sender_id.isdigit():
            components.append(At(qq=int(watch.sender_id)))
            components.append(Plain(" "))
        components.append(Plain(text))
        return MessageChain(chain=components)

    @staticmethod
    def _short_title(title: str) -> str:
        clean = " ".join(str(title or "").split())
        if len(clean) <= MONITOR_TITLE_LIMIT:
            return clean
        return f"{clean[: MONITOR_TITLE_LIMIT - 1]}…"

    @staticmethod
    def _normalize_title(title: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", str(title or "").lower())

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _float_value(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return -1.0

    @staticmethod
    def _format_bytes(value: Any) -> str:
        size = QBittorrentManagerPlugin._int_value(value)
        if size < 0:
            return "未知"
        units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
        scaled = float(size)
        for unit in units:
            if scaled < 1024 or unit == units[-1]:
                return f"{scaled:.0f} {unit}" if unit == "B" else f"{scaled:.2f} {unit}"
            scaled /= 1024
        return "未知"

    @classmethod
    def _format_speed(cls, value: Any) -> str:
        speed = cls._int_value(value)
        if speed <= 0:
            return "0 B/s"
        return f"{cls._format_bytes(speed)}/s"

    @staticmethod
    def _format_eta(value: Any) -> str:
        seconds = QBittorrentManagerPlugin._int_value(value)
        if seconds <= 0:
            return "未知"
        hours, remainder = divmod(seconds, 3600)
        minutes, rest = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {rest} 秒"
        return f"{rest} 秒"

    @staticmethod
    def _monitor_help_text() -> str:
        return (
            "下载状态监控（支持 qBittorrent、aria2 和 yt-dlp 任务）：\n"
            "/种子下载、/直接下载 或 /下载 提交任务后自动开始监控。\n"
            "/种子监控 查看：查看当前正在监控的任务。\n"
            "/种子监控 停止 序号：停止某个任务的汇报（不会取消下载）。\n"
            "/种子监控 停止 全部：停止所有汇报。\n"
            "qBittorrent 和 aria2 会汇报百分比进度，yt-dlp 服务只汇报任务状态。\n"
            "下载完成会按固定提示词提醒添加者；监控记录只保存在内存，重启后需重新添加。"
        )

    async def _add_to_utorrent(
        self, torrent_file: TorrentFile, user_config: dict[str, Any]
    ):
        ut_config = self._utorrent_config(user_config)
        async with httpx.AsyncClient(
            base_url=ut_config["url"],
            auth=self._utorrent_auth(ut_config),
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        ) as ut_client:
            token = await self._get_utorrent_token(ut_client)

            files = {
                "torrent_file": (
                    torrent_file.filename,
                    torrent_file.content,
                    torrent_file.content_type,
                )
            }
            add_response = await ut_client.post(
                "/gui/",
                params={"action": "add-file", "token": token},
                files=files,
            )
            self._raise_for_response(add_response, "uTorrent 添加任务失败")
            body = add_response.text.strip().lower()
            if "invalid request" in body or "invalid token" in body:
                raise UserFacingError(f"uTorrent 返回异常：{add_response.text[:120]}")

    async def _add_uri_to_utorrent(self, uri: str, user_config: dict[str, Any]):
        ut_config = self._utorrent_config(user_config)
        async with httpx.AsyncClient(
            base_url=ut_config["url"],
            auth=self._utorrent_auth(ut_config),
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        ) as ut_client:
            token = await self._get_utorrent_token(ut_client)
            add_response = await ut_client.get(
                "/gui/",
                params={"action": "add-url", "token": token, "s": uri},
            )
            self._raise_for_response(add_response, "uTorrent 添加链接失败")
            body = add_response.text.strip().lower()
            if "invalid request" in body or "invalid token" in body:
                raise UserFacingError(f"uTorrent 返回异常：{add_response.text[:120]}")

    async def _login_qbittorrent(
        self, qb_client: httpx.AsyncClient, qb_config: dict[str, str]
    ):
        if not (qb_config["username"] or qb_config["password"]):
            return

        login_response = await qb_client.post(
            "/api/v2/auth/login",
            data={
                "username": qb_config["username"],
                "password": qb_config["password"],
            },
        )

        if login_response.status_code == 401:
            raise UserFacingError("qBittorrent 登录失败，请检查用户名和密码。")
        if login_response.status_code == 403:
            raise UserFacingError(
                "qBittorrent 登录被拒绝，可能因失败次数过多导致 IP 被临时封禁。"
            )
        self._raise_for_response(login_response, "qBittorrent 登录失败")

        response_body = login_response.text.strip().lower()
        if login_response.status_code == 204 or response_body in {"ok", "ok."}:
            return
        if response_body in {"fails", "fails."}:
            raise UserFacingError("qBittorrent 登录失败，请检查用户名和密码。")
        raise UserFacingError(
            f"qBittorrent 返回无法识别的登录响应（HTTP {login_response.status_code}）。"
        )

    def _check_qbittorrent_add_response(self, response: httpx.Response, prefix: str):
        if response.status_code == 409:
            raise UserFacingError(
                f"{prefix}：任务未被接受，种子可能已存在或被客户端拒绝。"
            )
        self._raise_for_response(response, prefix)

        response_body = response.text.strip()
        if response.status_code == 204 or not response_body:
            return
        if response_body.lower() in {"ok", "ok."}:
            return

        try:
            result = response.json()
        except ValueError:
            result = None
        if isinstance(result, dict) and any(
            key in result for key in ("success_count", "pending_count", "failure_count")
        ):
            success_count = self._response_count(result, "success_count")
            pending_count = self._response_count(result, "pending_count")
            if success_count + pending_count > 0:
                return
            raise UserFacingError(f"{prefix}：qBittorrent 未接受任务。")

        raise UserFacingError(f"qBittorrent 返回异常：{response_body[:120]}")

    @staticmethod
    def _response_count(result: dict[str, Any], key: str) -> int:
        try:
            return max(0, int(result.get(key, 0)))
        except (TypeError, ValueError):
            return 0

    async def _get_utorrent_token(self, ut_client: httpx.AsyncClient) -> str:
        token_response = await ut_client.get("/gui/token.html")
        self._raise_for_response(token_response, "uTorrent 获取 Token 失败")
        token = self._extract_utorrent_token(token_response.text)
        if not token:
            raise UserFacingError(
                "uTorrent 获取 Token 失败，请检查 WebUI 地址、账号密码和 WebUI 是否启用。"
            )
        return token

    async def _render_results(
        self,
        keyword: str,
        results: list[TorrentResult],
        user_config: dict[str, Any],
    ) -> str | None:
        if not CARD_TEMPLATE.exists():
            return None

        render_width, render_height = self._render_dimensions(results, user_config)
        rows = "\n".join(
            f"""
            <article class="item{" has-status" if result.status else ""}">
              <div class="rank">{result.index}</div>
              <div class="content">
                <h2>{html.escape(result.title)}</h2>
                <p>{html.escape(result.subtitle or "暂无简介")}</p>
                {self._status_html(result)}
                <div class="meta">
                  <span>大小 {html.escape(result.size)}</span>
                  <span>时间 {html.escape(result.added_at)}</span>
                  <span>做种 {html.escape(result.seeders)}</span>
                  <span>下载 {html.escape(result.leechers)}</span>
                  <span>完成 {html.escape(result.completed)}</span>
                </div>
              </div>
            </article>
            """
            for result in results
        )
        rendered_html = (
            CARD_TEMPLATE.read_text(encoding="utf-8")
            .replace(
                "{{ keyword }}",
                html.escape(keyword),
            )
            .replace("{{ results }}", rows)
            .replace("{{ render_width }}", str(render_width))
            .replace("{{ render_height }}", str(render_height))
            .replace(
                "{{ client_name }}",
                html.escape(self._download_client_name(user_config)),
            )
        )

        try:
            return await self.html_render(
                rendered_html,
                {},
                options={
                    "viewport_width": render_width,
                    "viewport_height": render_height,
                    "full_page": False,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": render_width,
                        "height": render_height,
                    },
                    "type": "png",
                    "scale": "css",
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("t2i 渲染失败，回退纯文本结果")
            return None

    def _provider_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        provider_type = (
            self._normalize_provider_type(user_config.get("provider_type"))
            or "nexusphp"
        )
        defaults = DEFAULT_PROVIDER_CONFIG[provider_type]
        search_key = (
            "mteam_search_path" if provider_type == "mteam" else "provider_search_path"
        )
        base_url = (user_config.get("provider_base_url") or "").strip()
        search_path = (user_config.get(search_key) or "").strip()
        # 用户只切换站点类型时，内置默认地址/路径要跟着切换，否则会串站。
        if base_url in BUILTIN_PROVIDER_BASE_URLS and base_url != defaults["base_url"]:
            base_url = ""
        if (
            search_path in BUILTIN_PROVIDER_SEARCH_PATHS
            and search_path != defaults["search_path"]
        ):
            search_path = ""
        base_url = base_url or defaults["base_url"]
        search_path = search_path or defaults["search_path"]
        cookie = (user_config.get("provider_cookie") or "").strip()
        api_token = (user_config.get("mteam_api_token") or "").strip()
        download_path = (
            user_config.get("mteam_download_path") or defaults.get("download_path", "")
        ).strip()
        username = (user_config.get("provider_username") or "").strip()
        password = (user_config.get("provider_password") or "").strip()
        if not base_url:
            raise UserFacingError("未配置 PT 站点地址。")
        if provider_type != "mteam" and "{keyword}" not in search_path:
            raise UserFacingError("搜索路径必须包含 {keyword} 占位符。")
        if provider_type == "mteam" and not api_token:
            raise UserFacingError("未配置 M-Team API Token。")
        return {
            "type": provider_type,
            "base_url": base_url,
            "search_path": search_path,
            "cookie": cookie,
            "api_token": api_token,
            "download_path": download_path,
            "username": username,
            "password": password,
        }

    @staticmethod
    def _normalize_provider_type(value: Any) -> str:
        normalized = re.sub(r"[\s\-_]", "", str(value or "").strip().lower())
        if normalized in {"", "nexusphp", "nexus"}:
            return "nexusphp"
        if normalized in {"mteam", "mteamcc", "api"}:
            return "mteam"
        if normalized in GAYTORRENTS_PROVIDER_TYPES:
            return "gaytorrents"
        return ""

    def _qbittorrent_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        qb_url = (user_config.get("qb_url") or "").strip().rstrip("/")
        if not qb_url:
            raise UserFacingError("未配置 qBittorrent WebUI 地址。")
        return {
            "url": qb_url,
            "username": (user_config.get("qb_username") or "").strip(),
            "password": (user_config.get("qb_password") or "").strip(),
        }

    def _utorrent_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        ut_url = (user_config.get("ut_url") or "").strip().rstrip("/")
        if ut_url.endswith("/gui"):
            ut_url = ut_url.removesuffix("/gui")
        if not ut_url:
            raise UserFacingError("未配置 uTorrent WebUI 地址。")
        return {
            "url": ut_url,
            "username": (user_config.get("ut_username") or "").strip(),
            "password": (user_config.get("ut_password") or "").strip(),
        }

    def _download_client_type(self, user_config: dict[str, Any]) -> str:
        client_type = (user_config.get("download_client") or "qbittorrent").strip()
        client_type = client_type.lower().replace("-", "").replace("_", "")
        if client_type in {"utorrent", "μtorrent"}:
            return "utorrent"
        return "qbittorrent"

    def _download_client_name(self, user_config: dict[str, Any]) -> str:
        if self._download_client_type(user_config) == "utorrent":
            return "uTorrent"
        return "qBittorrent"

    @staticmethod
    def _utorrent_auth(ut_config: dict[str, str]) -> tuple[str, str] | None:
        if ut_config["username"] or ut_config["password"]:
            return ut_config["username"], ut_config["password"]
        return None

    def _http_client(
        self,
        provider: dict[str, str],
        user_config: dict[str, Any],
        *,
        include_cookie: bool = True,
        cookie: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.AsyncClient:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        }
        if extra_headers:
            headers.update(extra_headers)
        effective_cookie = cookie if cookie is not None else provider.get("cookie", "")
        if include_cookie and effective_cookie:
            headers["Cookie"] = effective_cookie
        return httpx.AsyncClient(
            headers=headers,
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        )

    def _render_dimensions(
        self, results: list[TorrentResult], user_config: dict[str, Any]
    ) -> tuple[int, int]:
        width = self._int_config(
            "render_width", DEFAULT_RENDER_WIDTH, 640, 1400, user_config
        )
        result_count = len(results)
        status_count = sum(bool(result.status) for result in results)
        height = RENDER_BASE_HEIGHT + max(result_count, 1) * RENDER_ITEM_HEIGHT
        height += status_count * RENDER_STATUS_HEIGHT
        height = min(max(height, 360), MAX_RENDER_HEIGHT)
        return width, height

    def _remember_results(
        self,
        event: AstrMessageEvent,
        results: list[TorrentResult],
        user_config: dict[str, Any],
    ):
        self._cleanup_cache()
        ttl = self._int_config("cache_ttl_seconds", 600, 60, 3600, user_config)
        self._search_cache[self._cache_key(event)] = (time.time(), ttl, results)

    def _get_cached_results(self, event: AstrMessageEvent) -> list[TorrentResult]:
        cached = self._search_cache.get(self._cache_key(event))
        if not cached:
            return []
        created_at, ttl, results = cached
        if time.time() - created_at > ttl:
            self._search_cache.pop(self._cache_key(event), None)
            return []
        return results

    def _cache_key(self, event: AstrMessageEvent) -> str:
        return self._user_id(event)

    @staticmethod
    def _same_origin(first_url: str, second_url: str) -> bool:
        first = urlparse(first_url)
        second = urlparse(second_url)

        def origin(parsed):
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None
            default_port = 443 if parsed.scheme == "https" else 80
            return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port

        return origin(first) is not None and origin(first) == origin(second)

    def _cleanup_cache(self):
        now = time.time()
        expired = [
            key
            for key, (created_at, ttl, _) in self._search_cache.items()
            if now - created_at > ttl
        ]
        for key in expired:
            self._search_cache.pop(key, None)

    def _user_id(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            raise UserFacingError("无法识别你的 QQ 号，暂时不能使用该功能。")
        return user_id

    def _ensure_user_access(self, event: AstrMessageEvent):
        user_id = self._user_id(event)
        if self._admins_bypass_access_control() and self._event_is_admin(event):
            return

        blacklist = self._configured_user_ids("access_blacklist")
        if user_id in blacklist:
            raise UserFacingError("你已被加入插件黑名单，无法使用该功能。")

        whitelist = self._configured_user_ids("access_whitelist")
        if whitelist and user_id not in whitelist:
            raise UserFacingError("你不在插件白名单中，无法使用该功能。")

    def _configured_user_ids(self, key: str) -> set[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            candidates = re.split(r"[\s,，;；]+", value)
        elif isinstance(value, list | tuple | set):
            candidates = value
        else:
            return set()
        return {str(item).strip() for item in candidates if str(item).strip()}

    def _admins_bypass_access_control(self) -> bool:
        return bool(self.config.get("access_admin_bypass", True))

    def _can_use_global_config(self, event: AstrMessageEvent) -> bool:
        if not bool(self.config.get("global_config_admin_only", False)):
            return True
        return self._event_is_admin(event)

    @staticmethod
    def _event_is_admin(event: AstrMessageEvent) -> bool:
        admin_value = getattr(event, "is_admin", None)
        if callable(admin_value):
            try:
                return bool(admin_value())
            except Exception:  # noqa: BLE001
                logger.exception("读取 AstrBot 管理员状态失败")
        elif admin_value is not None:
            return bool(admin_value)
        return getattr(event, "role", "member") == "admin"

    def _user_config(self, event: AstrMessageEvent) -> dict[str, Any]:
        user_id = self._user_id(event)
        merged: dict[str, Any] = dict(BUILTIN_USER_CONFIG)
        if self._can_use_global_config(event):
            merged.update(
                {
                    key: self.config.get(key)
                    for key in USER_CONFIG_FIELDS
                    if key in self.config
                }
            )
        merged.update(self._user_overrides(user_id))
        return merged

    def _user_overrides(self, user_id: str) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        for profile in self._user_profiles():
            if str(profile.get("qq") or "").strip() != user_id:
                continue
            for key in USER_CONFIG_FIELDS:
                if key in profile:
                    overrides[key] = profile[key]
        return overrides

    def _user_profiles(self) -> list[dict[str, Any]]:
        profiles = self.config.get("user_profiles", [])
        if not isinstance(profiles, list):
            return []
        return [profile for profile in profiles if isinstance(profile, dict)]

    def _user_config_commands_enabled(self) -> bool:
        return bool(self.config.get("enable_user_config_commands", True))

    def _resolve_config_key(self, key: str) -> str:
        normalized = key.strip().lower().replace("-", "_")
        config_key = CONFIG_KEY_ALIASES.get(normalized, normalized)
        if config_key not in USER_CONFIG_FIELDS:
            raise UserFacingError(
                f"未知配置项：{key}。发送 /种子配置 帮助 查看可用配置项。"
            )
        return config_key

    def _parse_user_config_value(self, key: str, value: str) -> Any:
        field = USER_CONFIG_FIELDS[key]
        value = value.strip()
        if not value:
            raise UserFacingError(
                "配置值不能为空；如需恢复默认值请使用 /种子配置 删除。"
            )

        if field.value_type == "bool":
            normalized = value.lower()
            if normalized in {"true", "yes", "1", "on", "是", "开启"}:
                parsed: Any = True
            elif normalized in {"false", "no", "0", "off", "否", "关闭"}:
                parsed = False
            else:
                raise UserFacingError("该配置项只接受 true/false、是/否或开启/关闭。")
        elif field.value_type == "int":
            try:
                parsed = int(value)
            except ValueError as error:
                raise UserFacingError("该配置项必须是整数。") from error
        elif field.value_type == "float":
            try:
                parsed = float(value)
            except ValueError as error:
                raise UserFacingError("该配置项必须是数字。") from error
        else:
            parsed = value

        if isinstance(parsed, int | float):
            if field.minimum is not None and parsed < field.minimum:
                raise UserFacingError(f"该配置项不能小于 {field.minimum:g}。")
            if field.maximum is not None and parsed > field.maximum:
                raise UserFacingError(f"该配置项不能大于 {field.maximum:g}。")

        if key == "download_client":
            normalized_client = value.lower().replace("-", "").replace("_", "")
            if normalized_client in {"utorrent", "μtorrent"}:
                return "utorrent"
            if normalized_client in {"qbittorrent", "qb"}:
                return "qbittorrent"
            raise UserFacingError("下载客户端只支持 qbittorrent 或 utorrent。")

        if key == "provider_search_path" and "{keyword}" not in value:
            raise UserFacingError("PT 搜索路径必须包含 {keyword} 占位符。")

        if key == "provider_type":
            normalized_provider = self._normalize_provider_type(value)
            if not normalized_provider:
                raise UserFacingError("站点类型只支持 gaytorrents、nexusphp 或 mteam。")
            return normalized_provider

        if key in {"provider_base_url", "qb_url", "ut_url"}:
            parsed_url = urlparse(value)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise UserFacingError("地址必须是有效的 http 或 https URL。")

        return parsed

    async def _set_user_override(self, user_id: str, key: str, value: Any):
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            profile = self._merged_profile(user_id, profiles)
            profile[key] = value
            self._replace_user_profile(user_id, profiles, profile)
            await self._save_plugin_config(profiles)

    async def _delete_user_override(self, user_id: str, key: str) -> bool:
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            profile = self._merged_profile(user_id, profiles)
            if key not in profile:
                return False
            profile.pop(key)
            self._replace_user_profile(user_id, profiles, profile)
            await self._save_plugin_config(profiles)
            return True

    async def _reset_user_overrides(self, user_id: str) -> bool:
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            remaining = [
                profile
                for profile in profiles
                if str(profile.get("qq") or "").strip() != user_id
            ]
            if len(remaining) == len(profiles):
                return False
            await self._save_plugin_config(remaining)
            return True

    @staticmethod
    def _merged_profile(user_id: str, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {"__template_key": "user", "qq": user_id}
        for profile in profiles:
            if str(profile.get("qq") or "").strip() == user_id:
                merged.update(profile)
        merged["__template_key"] = "user"
        merged["qq"] = user_id
        return merged

    @staticmethod
    def _replace_user_profile(
        user_id: str,
        profiles: list[dict[str, Any]],
        replacement: dict[str, Any],
    ):
        profiles[:] = [
            profile
            for profile in profiles
            if str(profile.get("qq") or "").strip() != user_id
        ]
        if any(key in replacement for key in USER_CONFIG_FIELDS):
            profiles.append(replacement)

    async def _save_plugin_config(self, profiles: list[dict[str, Any]]):
        self.config["user_profiles"] = profiles
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                await asyncio.to_thread(save_config)
            except Exception as error:
                logger.exception("保存用户配置失败")
                raise UserFacingError("保存配置失败，请稍后重试。") from error

    def _format_user_config(
        self,
        user_id: str,
        user_config: dict[str, Any],
        overrides: dict[str, Any],
        global_config_allowed: bool,
    ) -> str:
        lines = [f"QQ {user_id} 的种子配置："]
        if global_config_allowed:
            lines.append("全局配置：允许继承")
        else:
            lines.append("全局配置：仅管理员可用，当前仅使用个人或内置默认配置")
        for key, spec in USER_CONFIG_FIELDS.items():
            value = user_config.get(key)
            if spec.sensitive:
                display = "已设置" if value else "未设置"
            elif isinstance(value, bool):
                display = "是" if value else "否"
            elif value in {None, ""}:
                display = "未设置"
            else:
                display = str(value)
            if key in overrides:
                source = "个人"
            elif global_config_allowed and key in self.config:
                source = "全局"
            else:
                source = "内置"
            lines.append(f"{spec.label}：{display}（{source}）")
        lines.append("发送 /种子配置 帮助 查看设置方法。")
        return "\n".join(lines)

    @staticmethod
    def _pick_results(
        selection: str, results: list[TorrentResult]
    ) -> list[TorrentResult]:
        tokens = [token for token in re.split(r"[\s,，]+", selection.strip()) if token]
        if not tokens:
            raise UserFacingError("请至少提供一个种子序号。")

        invalid_tokens = [token for token in tokens if not token.isdigit()]
        if invalid_tokens:
            values = "、".join(invalid_tokens)
            raise UserFacingError(f"序号格式错误：{values}，请只输入数字。")

        indexes = list(dict.fromkeys(int(token) for token in tokens))
        invalid_indexes = [
            index for index in indexes if index < 1 or index > len(results)
        ]
        if invalid_indexes:
            values = "、".join(str(index) for index in invalid_indexes)
            raise UserFacingError(
                f"无效序号：{values}，当前只有 {len(results)} 条结果，"
                f"请在 1-{len(results)} 之间选择。"
            )

        return [results[index - 1] for index in indexes]

    @staticmethod
    def _format_batch_download_result(
        client_name: str,
        succeeded: list[TorrentResult],
        failed: list[tuple[TorrentResult, str]],
        note: str = "",
    ) -> str:
        lines: list[str] = []
        if succeeded:
            lines.append(f"已添加到 {client_name}：{len(succeeded)} 个任务")
            lines.extend(f"{torrent.index}. {torrent.title}" for torrent in succeeded)
        if failed:
            if lines:
                lines.append("")
            lines.append(f"添加失败：{len(failed)} 个任务")
            lines.extend(
                f"{torrent.index}. {torrent.title}：{reason}"
                for torrent, reason in failed
            )
        if lines and note:
            lines.append("")
            lines.append(note)
        return "\n".join(lines) or "没有可添加的下载任务。"

    @staticmethod
    def _extract_title(detail_link: Any, row: Any) -> str:
        if detail_link:
            return (
                detail_link.get("title")
                or detail_link.get_text(" ", strip=True)
                or detail_link.find_parent().get_text(" ", strip=True)
            ).strip()
        return row.get_text(" ", strip=True)[:120].strip()

    @staticmethod
    def _extract_subtitle(cells: list[str], title: str) -> str:
        for cell in cells:
            subtitle = cell.replace(title, "", 1).strip()
            if len(subtitle) > 4:
                return subtitle[:120]
        return ""

    @staticmethod
    def _extract_size(text: str) -> str:
        match = re.search(
            r"(\d+(?:\.\d+)?\s*(?:TiB|GiB|MiB|KiB|TB|GB|MB|KB))", text, re.I
        )
        return match.group(1) if match else "未知"

    @staticmethod
    def _extract_added_at(cells: list[str]) -> str:
        pattern = re.compile(
            r"^(\d{4}[-/]\d{1,2}[-/]\d{1,2})"
            r"(?:\s+(\d{1,2}:\d{2}(?::\d{2})?))?$"
        )
        for cell in cells:
            match = pattern.fullmatch(cell.strip())
            if match:
                date = match.group(1).replace("/", "-")
                return f"{date} {match.group(2)}" if match.group(2) else date
        return "未知"

    @classmethod
    def _extract_torrent_status(cls, row: Any) -> tuple[str, float | None, str]:
        torrent_table = row.find("table", class_=re.compile(r"\btorrentname\b", re.I))
        if not torrent_table:
            return "", None, ""

        progress_bar = torrent_table.find(
            "div", class_=re.compile(r"\bprobar_a[123]\b", re.I)
        )
        if not progress_bar:
            return "", None, ""

        classes = [str(value).lower() for value in progress_bar.get("class", [])]
        progress_class = next(
            (value for value in classes if re.fullmatch(r"probar_a[123]", value)),
            "",
        )
        detail = str(progress_bar.get("title") or "").strip()
        status, status_kind = cls._normalize_torrent_status(detail, progress_class)
        progress = cls._extract_progress_percent(progress_bar)
        return status, progress, status_kind

    @staticmethod
    def _normalize_torrent_status(detail: str, progress_class: str) -> tuple[str, str]:
        if any(word in detail for word in ("暂停", "停止", "未活动", "未做种")):
            return "暂停", "inactive"
        if any(word in detail for word in ("正在做种", "做种中")):
            return "做种", "seeding"
        if any(word in detail for word in ("正在下载", "下载中")):
            return "下载", "downloading"
        if any(word in detail for word in ("完成", "已下载", "下载完")):
            return "完成", "completed"
        if "做种" in detail:
            return "做种", "seeding"

        defaults = {
            "probar_a1": ("下载", "downloading"),
            "probar_a2": ("做种", "seeding"),
            "probar_a3": ("完成", "completed"),
        }
        if progress_class in defaults:
            return defaults[progress_class]
        return (detail[:20], "inactive") if detail else ("", "")

    @staticmethod
    def _extract_progress_percent(progress_bar: Any) -> float | None:
        inner_bar = progress_bar.find(
            "div", class_=re.compile(r"\bprobar_b[123]\b", re.I)
        )
        style_candidates = [
            inner_bar.get("style") if inner_bar else "",
            progress_bar.get("style") or "",
        ]
        for candidate in style_candidates:
            match = re.search(r"width\s*:\s*(\d+(?:\.\d+)?)\s*%", str(candidate))
            if match:
                return min(max(float(match.group(1)), 0.0), 100.0)

        value_candidates = [
            progress_bar.get("aria-valuenow") or "",
            progress_bar.get("data-progress") or "",
        ]
        for candidate in value_candidates:
            match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", str(candidate))
            if match:
                return min(max(float(match.group(1)), 0.0), 100.0)

        title_match = re.search(
            r"(\d+(?:\.\d+)?)\s*%", str(progress_bar.get("title") or "")
        )
        if title_match:
            return min(max(float(title_match.group(1)), 0.0), 100.0)
        return None

    @staticmethod
    def _status_html(result: TorrentResult) -> str:
        if not result.status:
            return ""

        progress = 0.0 if result.progress is None else result.progress
        progress_text = (
            "—"
            if result.progress is None
            else f"{progress:.1f}".rstrip("0").rstrip(".") + "%"
        )
        kind = (
            result.status_kind
            if result.status_kind
            in {
                "downloading",
                "seeding",
                "completed",
                "inactive",
            }
            else "inactive"
        )
        return (
            f'<div class="torrent-status {kind}">'
            f'<span class="status-label">{html.escape(result.status)}</span>'
            '<div class="progress-track">'
            f'<div class="progress-fill" style="width:{progress}%"></div>'
            "</div>"
            f'<span class="progress-value">{progress_text}</span>'
            "</div>"
        )

    @staticmethod
    def _parse_search_request(query: str) -> SearchRequest:
        escaped_keyword = re.match(rf"^{OPTION_PREFIX_PATTERN}(?:\s+|$)", query)
        if escaped_keyword:
            keyword = query[escaped_keyword.end() :].strip()
            if not keyword:
                raise UserFacingError("缺少搜索关键词。")
            return SearchRequest(keyword=keyword)

        delimiter = re.search(rf"(?:^|\s){OPTION_PREFIX_PATTERN}(?:\s|$)", query)
        starts_with_option = re.match(
            rf"^{OPTION_PREFIX_PATTERN}(?:sort|order|排序|顺序)[=＝]",
            query,
            re.IGNORECASE,
        )
        if not delimiter or not starts_with_option:
            return SearchRequest(keyword=query.strip())

        options_text = query[: delimiter.start()].strip()
        keyword = query[delimiter.end() :].strip()
        if not keyword:
            raise UserFacingError("排序参数后缺少搜索关键词。")

        sort_by: str | None = None
        descending = True
        order_was_set = False
        for token in options_text.split():
            normalized = re.sub(
                rf"^{OPTION_PREFIX_PATTERN}", "--", token, count=1
            ).replace("＝", "=", 1)
            if "=" not in normalized:
                raise UserFacingError(
                    f"无法识别排序参数：{token}，请使用 --sort=字段。"
                )
            option, value = normalized.split("=", 1)
            option = option.lower()
            value = value.strip().lower()
            if option in {"--sort", "--排序"}:
                if sort_by is not None:
                    raise UserFacingError("请勿重复设置排序字段。")
                sort_by = SORT_FIELD_ALIASES.get(value)
                if not sort_by:
                    raise UserFacingError(
                        "不支持的排序字段，可选：时间、做种、下载、完成、大小、标题。"
                    )
            elif option in {"--order", "--顺序"}:
                if order_was_set:
                    raise UserFacingError("请勿重复设置排序顺序。")
                order_was_set = True
                if value not in SORT_ORDER_ALIASES:
                    raise UserFacingError("不支持的排序顺序，请使用升序或降序。")
                descending = SORT_ORDER_ALIASES[value]
            else:
                raise UserFacingError(f"无法识别排序参数：{option}。")

        if order_was_set and sort_by is None:
            raise UserFacingError("设置排序顺序时必须同时设置排序字段。")
        return SearchRequest(keyword, sort_by, descending)

    @staticmethod
    def _apply_search_sort(url: str, sort_by: str, descending: bool) -> str:
        parsed = urlparse(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"sort", "type"}
        ]
        query.extend(
            (
                ("sort", NEXUSPHP_SORT_CODES[sort_by]),
                ("type", "desc" if descending else "asc"),
            )
        )
        return urlunparse(parsed._replace(query=urlencode(query)))

    @classmethod
    def _sort_results(
        cls,
        results: list[TorrentResult],
        sort_by: str | None,
        descending: bool,
    ) -> list[TorrentResult]:
        if not sort_by:
            return results

        known: list[tuple[Any, TorrentResult]] = []
        unknown: list[TorrentResult] = []
        for result in results:
            value = cls._sort_value(result, sort_by)
            if value is None:
                unknown.append(result)
            else:
                known.append((value, result))
        known.sort(key=lambda item: item[0], reverse=descending)
        ordered = [result for _, result in known] + unknown
        return [replace(result, index=index) for index, result in enumerate(ordered, 1)]

    @staticmethod
    def _sort_value(result: TorrentResult, sort_by: str) -> Any | None:
        if sort_by == "title":
            return result.title.casefold()
        if sort_by == "time":
            match = re.search(
                r"(\d{4})-(\d{1,2})-(\d{1,2})"
                r"(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
                result.added_at,
            )
            if not match:
                return None
            parts = [int(value or 0) for value in match.groups()]
            return tuple(parts)
        if sort_by == "size":
            match = re.search(
                r"(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB)",
                result.size,
                re.I,
            )
            if not match:
                return None
            powers = {
                "KIB": 1,
                "KB": 1,
                "MIB": 2,
                "MB": 2,
                "GIB": 3,
                "GB": 3,
                "TIB": 4,
                "TB": 4,
            }
            return float(match.group(1)) * 1024 ** powers[match.group(2).upper()]

        value = getattr(result, sort_by, "")
        digits = re.sub(r"[^\d]", "", value)
        return int(digits) if digits else None

    @staticmethod
    def _extract_stats(row: Any, numbers: list[str]) -> tuple[str, str, str]:
        classes = ("seeders", "leechers", "snatched", "completed")
        by_class: list[str] = []
        for class_name in classes:
            cell = row.find(class_=re.compile(class_name, re.I))
            if cell:
                by_class.append(cell.get_text(" ", strip=True) or "0")
        if len(by_class) >= 3:
            return by_class[0], by_class[1], by_class[2]
        if len(numbers) >= 3:
            return numbers[-3], numbers[-2], numbers[-1]
        return "?", "?", "?"

    @staticmethod
    def _raise_for_response(response: httpx.Response, prefix: str):
        if response.status_code in {401, 403}:
            raise UserFacingError(f"{prefix}：认证失败，请检查 Cookie 或账号权限。")
        if response.status_code >= 400:
            raise UserFacingError(f"{prefix}：HTTP {response.status_code}")

    @staticmethod
    def _safe_filename(title: str) -> str:
        filename = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()[:120]
        return f"{filename or 'torrent'}.torrent"

    @staticmethod
    def _is_supported_direct_uri(uri: str) -> bool:
        lowered = uri.lower()
        if lowered.startswith("magnet:?"):
            return True
        parsed = urlparse(uri)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _direct_title(uri: str) -> str:
        if uri.lower().startswith("magnet:?"):
            display = uri[:80] + "..." if len(uri) > 80 else uri
            return f"磁链任务 {display}"
        parsed = urlparse(uri)
        filename = Path(unquote(parsed.path)).name
        return filename or parsed.netloc or "直接下载任务"

    @staticmethod
    def _looks_like_torrent_response(uri: str, response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        content_disposition = response.headers.get("content-disposition", "").lower()
        lowered_uri = uri.lower()
        content = response.content.lstrip()

        return (
            "application/x-bittorrent" in content_type
            or "application/octet-stream" in content_type
            or ".torrent" in lowered_uri
            or ".torrent" in content_disposition
            or content.startswith(b"d")
        )

    @staticmethod
    def _filename_from_response(uri: str, response: httpx.Response) -> str:
        content_disposition = response.headers.get("content-disposition", "")
        filename_match = re.search(
            r'filename\*?=(?:UTF-8\'\')?["]?([^";]+)["]?',
            content_disposition,
            re.IGNORECASE,
        )
        if filename_match:
            filename = unquote(filename_match.group(1)).strip()
        else:
            filename = Path(unquote(urlparse(uri).path)).name

        filename = re.sub(r'[\\/:*?"<>|]+', "_", filename).strip()[:120]
        if not filename:
            filename = "direct-download.torrent"
        if not filename.lower().endswith(".torrent"):
            filename = f"{filename}.torrent"
        return filename

    @staticmethod
    def _extract_utorrent_token(response_text: str) -> str:
        match = re.search(
            r"<div[^>]+id=[\"']token[\"'][^>]*>([^<]+)</div>",
            response_text,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    def _format_plain_results(self, keyword: str, results: list[TorrentResult]) -> str:
        lines = [f"「{keyword}」搜索结果："]
        for result in results:
            item = (
                f"{result.index}. {result.title}\n"
                f"   时间：{result.added_at} 大小：{result.size} "
                f"做种：{result.seeders} 下载：{result.leechers} "
                f"完成：{result.completed}"
            )
            if result.status:
                progress = (
                    "未知"
                    if result.progress is None
                    else (f"{result.progress:.1f}".rstrip("0").rstrip(".") + "%")
                )
                item += f"\n   当前状态：{result.status}，进度：{progress}"
            lines.append(item)
        lines.append("发送 /种子下载 序号... 批量添加到下载客户端。")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "种子下载助手（默认搜索 Gay-Torrents）：\n"
            "1. /种子 关键词 - 搜索 PT 站种子\n"
            "2. /种子 --sort=做种 --order=降序 -- 关键词 - 排序搜索\n"
            "3. /种子下载 序号... - 批量添加所选种子到下载客户端\n"
            "4. /直接下载 磁链或种子链接 - 直接添加下载任务\n"
            "5. /下载 链接 - 自动识别：磁链/.torrent 给 qBittorrent，"
            "视频网页给 yt-dlp，其他直链给 aria2\n"
            "   也可以手动指定后端：/下载 aria2 链接、/下载 qb 链接、/下载 ytdlp 链接\n"
            "6. /种子监控 查看 - 查看下载进度监控（完成会提醒你）\n"
            "7. /种子配置 帮助 - 配置自己的 PT 与下载客户端\n"
            "Gay-Torrents 需要登录：/种子配置 设置 pt账号 你的账号，"
            "再 /种子配置 设置 pt密码 你的密码。\n"
            "每位用户按 QQ 号使用独立配置。"
        )

    @staticmethod
    def _user_config_help_text() -> str:
        aliases = "、".join(CONFIG_KEY_ALIASES)
        return (
            "个人种子配置：\n"
            "/种子配置 查看\n"
            "/种子配置 设置 配置项 值\n"
            "/种子配置 删除 配置项\n"
            "/种子配置 重置\n"
            "示例：/种子配置 设置 客户端 qbittorrent\n"
            "示例：/种子配置 设置 qb地址 http://127.0.0.1:8080\n"
            "示例：/种子配置 设置 pt账号 你的账号\n"
            "示例：/种子配置 设置 pt密码 你的密码\n"
            "示例：/种子配置 设置 cookie 登录后复制的 Cookie\n"
            "示例：/种子配置 设置 aria2地址 http://127.0.0.1:6800/jsonrpc\n"
            "示例：/种子配置 设置 ytdlp地址 http://127.0.0.1:5000\n"
            f"常用配置项：{aliases}\n"
            "也可直接使用 AstrBot 面板中的英文配置键。\n"
            "站点类型可选 gaytorrents、nexusphp、mteam。\n"
            "/下载 会自动分流：磁链和 .torrent 给 qBittorrent，"
            "视频网页给 yt-dlp，其他直链给 aria2。\n"
            "Cookie 和密码建议仅在私聊中设置。"
        )

    def _int_config(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int,
        user_config: dict[str, Any] | None = None,
    ) -> int:
        config = user_config if user_config is not None else self.config
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    def _float_config(
        self,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
        user_config: dict[str, Any] | None = None,
    ) -> float:
        config = user_config if user_config is not None else self.config
        try:
            value = float(config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    async def terminate(self):
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._download_watches.clear()
        await asyncio.sleep(0)


class UserFacingError(Exception):
    pass
