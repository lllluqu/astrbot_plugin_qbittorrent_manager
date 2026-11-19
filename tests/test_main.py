import asyncio
import base64
import hashlib
import importlib
import inspect
import sys
import time
import types
import unittest


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_filter = types.ModuleType("astrbot.core.star.filter")
    command = types.ModuleType("astrbot.core.star.filter.command")

    class Logger:
        def exception(self, *_args, **_kwargs):
            pass

        def warning(self, *_args, **_kwargs):
            pass

        def info(self, *_args, **_kwargs):
            pass

        def debug(self, *_args, **_kwargs):
            pass

        def error(self, *_args, **_kwargs):
            pass

    class CommandGroup:
        def command(self, *_args, **_kwargs):
            return lambda handler: handler

    class Filter:
        def command(self, *_args, **_kwargs):
            return lambda handler: handler

        def command_group(self, *_args, **_kwargs):
            return lambda _handler: CommandGroup()

    class Star:
        def __init__(self, context):
            self.context = context

    class GreedyStr(str):
        pass

    class Plain:
        def __init__(self, text, convert=True):
            self.text = text
            self.convert = convert

    class At:
        def __init__(self, qq, name=""):
            self.qq = qq
            self.name = name

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = list(chain or [])

    api.logger = Logger()
    event_api.AstrMessageEvent = object
    event_api.filter = Filter()
    star_api.Context = object
    star_api.Star = Star
    star_api.register = lambda *_args, **_kwargs: lambda cls: cls
    command.GreedyStr = GreedyStr

    message_components = types.ModuleType("astrbot.api.message_components")
    message_components.At = At
    message_components.Plain = Plain
    core_message = types.ModuleType("astrbot.core.message")
    message_event_result = types.ModuleType("astrbot.core.message.message_event_result")
    message_event_result.MessageChain = MessageChain

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
        "astrbot.api.message_components": message_components,
        "astrbot.core": core,
        "astrbot.core.star": core_star,
        "astrbot.core.star.filter": core_filter,
        "astrbot.core.star.filter.command": command,
        "astrbot.core.message": core_message,
        "astrbot.core.message.message_event_result": message_event_result,
    }
    sys.modules.update(modules)


_install_astrbot_stubs()
plugin_module = importlib.import_module("main")


class SavingConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeEvent:
    def __init__(self, user_id, *, admin=False, session="", private=False):
        self.user_id = user_id
        self.admin = admin
        self.unified_msg_origin = session
        self.private = private

    def get_sender_id(self):
        return self.user_id

    def is_admin(self):
        return self.admin

    def is_private_chat(self):
        return self.private

    def plain_result(self, message):
        return message


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


def chain_text(chain):
    return "".join(
        component.text for component in chain.chain if hasattr(component, "text")
    )


def make_plugin(config):
    instance = plugin_module.QBittorrentManagerPlugin.__new__(
        plugin_module.QBittorrentManagerPlugin
    )
    instance.config = config
    instance._config_lock = asyncio.Lock()
    instance._search_cache = {}
    instance._gt_sessions = {}
    instance._download_watches = {}
    instance._monitor_task = None
    instance.context = FakeContext()
    return instance


def make_results(count):
    return [
        plugin_module.TorrentResult(
            index=index,
            title=f"种子 {index}",
            subtitle="",
            size="1 GiB",
            seeders="1",
            leechers="0",
            completed="1",
            detail_url=f"https://example.com/details.php?id={index}",
            download_url=f"https://example.com/download.php?id={index}",
            added_at=f"2026-01-{index:02d} 12:00:00",
        )
        for index in range(1, count + 1)
    ]


class ParserTests(unittest.TestCase):
    def test_skips_outer_layout_row(self):
        html_text = """
        <table><tr><td>搜索箱（点击收起/展开）
          <table><tr>
            <td>动漫</td>
            <td><table class="torrentname"><tr><td>
              <a title="测试种子 2030-12-31" href="details.php?id=1">测试种子 2030-12-31</a><br>正确简介
              <a href="download.php?id=1">下载</a>
            </td></tr></table></td>
            <td>0</td><td>2026-01-01</td><td>1.5 GiB</td>
            <td>4</td><td>0</td><td>10</td><td>tester</td>
          </tr></table>
        </td></tr></table>
        """
        instance = make_plugin({})

        results = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "测试种子 2030-12-31")
        self.assertEqual(results[0].subtitle, "正确简介 下载")
        self.assertEqual(
            (results[0].seeders, results[0].leechers, results[0].completed),
            ("4", "0", "10"),
        )
        self.assertEqual(results[0].added_at, "2026-01-01")
        self.assertEqual(results[0].status, "")
        self.assertIsNone(results[0].progress)

    def test_extracts_seeding_status_and_progress(self):
        html_text = """
        <table><tr>
          <td>动漫</td>
          <td><table class="torrentname">
            <tr><td><a title="测试种子" href="details.php?id=2">测试种子</a>
              <a href="download.php?id=2">下载</a></td></tr>
            <tr><td><img src="s_up.gif"></td><td>
              <div class="probar_a2" title="已下载，正在做种">
                <div class="probar_b2" style="width:100%"></div>
              </div>
            </td></tr>
          </table></td>
          <td>0</td><td>2026-07-25 12:00:00</td><td>2 GiB</td>
          <td>8</td><td>0</td><td>20</td><td>tester</td>
        </tr></table>
        """
        instance = make_plugin({})

        results = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "做种")
        self.assertEqual(results[0].progress, 100.0)
        self.assertEqual(results[0].status_kind, "seeding")

    def test_extracts_downloading_status_and_decimal_progress(self):
        html_text = """
        <table><tr>
          <td>电影</td>
          <td><table class="torrentname">
            <tr><td><a title="下载中的种子" href="details.php?id=3">下载中的种子</a>
              <a href="download.php?id=3">下载</a></td></tr>
            <tr><td></td><td><div class="probar_a1" title="正在下载">
              <div class="probar_b1" style="width:42.5%"></div>
            </div></td></tr>
          </table></td>
          <td>0</td><td>2026-07-25</td><td>3 GiB</td>
          <td>5</td><td>1</td><td>9</td><td>tester</td>
        </tr></table>
        """
        instance = make_plugin({})

        result = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )[0]

        self.assertEqual(result.status, "下载")
        self.assertEqual(result.progress, 42.5)
        self.assertEqual(result.status_kind, "downloading")

    def test_inactive_seeding_text_is_not_treated_as_active(self):
        instance = make_plugin({})
        status, kind = instance._normalize_torrent_status(
            "已下载，停止做种", "probar_a3"
        )

        self.assertEqual(status, "暂停")
        self.assertEqual(kind, "inactive")

    def test_missing_progress_is_not_rendered_as_zero_percent(self):
        instance = make_plugin({})
        result = make_results(1)[0]
        result.status = "暂停"
        result.status_kind = "inactive"

        rendered = instance._status_html(result)

        self.assertIn('class="status-label">暂停</span>', rendered)
        self.assertIn('class="progress-value">—</span>', rendered)
        self.assertNotIn("0%</span>", rendered)

    def test_unrelated_number_in_status_title_is_not_progress(self):
        soup = plugin_module.BeautifulSoup(
            '<div class="probar_a3" title="已下载 2 天，停止做种"></div>',
            "html.parser",
        )

        progress = make_plugin({})._extract_progress_percent(soup.div)

        self.assertIsNone(progress)

    def test_render_height_includes_each_status_row_without_clipping(self):
        instance = make_plugin({})
        results = make_results(30)
        for result in results:
            result.status = "做种"
            result.progress = 100.0

        width, height = instance._render_dimensions(results, {"render_width": 900})

        self.assertEqual(width, 900)
        self.assertEqual(height, 4594)


class SearchSortTests(unittest.TestCase):
    def setUp(self):
        self.instance = make_plugin({})

    def test_legacy_search_treats_entire_query_as_keyword(self):
        request = self.instance._parse_search_request(
            "[VCB-Studio] title --sort=seeders"
        )

        self.assertEqual(request.keyword, "[VCB-Studio] title --sort=seeders")
        self.assertIsNone(request.sort_by)

    def test_standalone_dashes_inside_legacy_keyword_are_not_options(self):
        request = self.instance._parse_search_request("Movie -- Director's Cut")

        self.assertEqual(request.keyword, "Movie -- Director's Cut")
        self.assertIsNone(request.sort_by)

    def test_option_like_keyword_can_be_escaped(self):
        request = self.instance._parse_search_request("-- --sort=seeders special title")

        self.assertEqual(request.keyword, "--sort=seeders special title")
        self.assertIsNone(request.sort_by)

    def test_options_are_separated_from_keyword(self):
        request = self.instance._parse_search_request(
            "--排序=做种 --顺序=升序 -- [VCB-Studio] 漆黑 = 子弹 -- final"
        )

        self.assertEqual(request.keyword, "[VCB-Studio] 漆黑 = 子弹 -- final")
        self.assertEqual(request.sort_by, "seeders")
        self.assertFalse(request.descending)

    def test_search_handler_uses_required_greedy_string(self):
        parameter = inspect.signature(self.instance.search_torrents).parameters["query"]

        self.assertIs(parameter.annotation, plugin_module.GreedyStr)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_fullwidth_chinese_options_are_supported(self):
        request = self.instance._parse_search_request(
            "－－排序＝做种 －－顺序＝降序 －－ 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "seeders")
        self.assertTrue(request.descending)

    def test_chinese_em_dash_options_are_supported(self):
        request = self.instance._parse_search_request(
            "——排序＝完成 ——顺序＝升序 —— 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "completed")
        self.assertFalse(request.descending)

    def test_single_unicode_dash_options_are_supported(self):
        request = self.instance._parse_search_request(
            "—排序＝时间 —顺序＝降序 — 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "time")
        self.assertTrue(request.descending)

    def test_unknown_sort_field_is_rejected(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "不支持的排序字段"):
            self.instance._parse_search_request("--sort=热度 -- 关键词")

    def test_sort_query_replaces_existing_provider_order(self):
        url = self.instance._apply_search_sort(
            "https://pt.example/torrents.php?search=test&sort=4&type=asc",
            "seeders",
            True,
        )

        self.assertIn("search=test", url)
        self.assertIn("sort=7", url)
        self.assertIn("type=desc", url)
        self.assertEqual(url.count("sort="), 1)

    def test_numeric_results_are_sorted_and_reindexed(self):
        results = make_results(3)
        results[0].seeders = "2"
        results[1].seeders = "10"
        results[2].seeders = "?"

        sorted_results = self.instance._sort_results(results, "seeders", True)

        self.assertEqual(
            [result.title for result in sorted_results], ["种子 2", "种子 1", "种子 3"]
        )
        self.assertEqual([result.index for result in sorted_results], [1, 2, 3])


class MTeamTests(unittest.TestCase):
    def setUp(self):
        self.instance = make_plugin({})

    def test_mteam_provider_requires_token(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "API Token"):
            self.instance._provider_config({"provider_type": "mteam"})

    def test_mteam_provider_uses_api_defaults(self):
        provider = self.instance._provider_config(
            {"provider_type": "mteam", "mteam_api_token": "token"}
        )

        self.assertEqual(provider["type"], "mteam")
        self.assertEqual(provider["base_url"], "https://api.m-team.cc")
        self.assertEqual(provider["search_path"], "/api/torrent/search")
        self.assertEqual(provider["download_path"], "/api/torrent/genDlToken")

    def test_mteam_result_payload_is_parsed(self):
        payload = {
            "code": 0,
            "data": {
                "data": [
                    {
                        "id": 42,
                        "name": "M-Team 测试种子",
                        "size": 1073741824,
                        "seeders": 8,
                        "leechers": 2,
                        "timesCompleted": 10,
                        "createdDate": "2026-08-27 12:00:00",
                    }
                ]
            },
        }

        items = self.instance._mteam_items(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(self.instance._format_mteam_size(items[0]["size"]), "1 GiB")

    def test_mteam_download_url_is_extracted_from_wrapped_payload(self):
        self.assertEqual(
            self.instance._mteam_download_url(
                {"code": 0, "data": {"url": "https://dl.example/test.torrent"}}
            ),
            "https://dl.example/test.torrent",
        )

    def test_mteam_provider_alias_is_accepted(self):
        self.assertEqual(
            self.instance._parse_user_config_value("provider_type", "m-team"),
            "mteam",
        )

    def test_mteam_sort_field_uses_official_enum(self):
        self.assertEqual(plugin_module.MTEAM_SORT_FIELDS["completed"], "TIMES_COMPLETED")

    def test_mteam_stats_are_read_from_nested_status_without_leaking_metadata(self):
        item = {
            "name": "真实标题",
            "status": {
                "seeders": 12,
                "leechers": 3,
                "timesCompleted": 8,
                "toppingLevel": 1,
                "status": "NORMAL",
                "modifiedDate": "2026-08-28T00:00:00",
            },
        }
        status = item["status"]

        self.assertEqual(
            self.instance._mteam_display_value(status, item, "seeders"), "12"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "leechers"), "3"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "timesCompleted"), "8"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "unknown"), "?"
        )

    def test_mteam_zero_stats_are_preserved(self):
        status = {"seeders": 0, "leechers": 0, "timesCompleted": 0}
        item = {}

        self.assertEqual(
            self.instance._mteam_display_value(status, item, "seeders"), "0"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "leechers"), "0"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "timesCompleted"), "0"
        )


class UserConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SavingConfig(
            {
                "download_client": "qbittorrent",
                "qb_url": "http://global:8080",
                "provider_cookie": "global-cookie",
                "user_profiles": [
                    {
                        "__template_key": "user",
                        "qq": "10001",
                        "download_client": "utorrent",
                        "ut_url": "http://user:8080",
                    }
                ],
            }
        )
        self.instance = make_plugin(self.config)

    def test_configuration_is_selected_by_qq(self):
        first = self.instance._user_config(FakeEvent("10001"))
        second = self.instance._user_config(FakeEvent("10002"))

        self.assertEqual(first["download_client"], "utorrent")
        self.assertEqual(first["ut_url"], "http://user:8080")
        self.assertEqual(second["download_client"], "qbittorrent")
        self.assertEqual(second["qb_url"], "http://global:8080")

    async def test_user_override_is_saved_and_can_be_deleted(self):
        await self.instance._set_user_override("10002", "qb_password", "secret-value")

        overrides = self.instance._user_overrides("10002")
        rendered = self.instance._format_user_config(
            "10002",
            self.instance._user_config(FakeEvent("10002")),
            overrides,
            True,
        )
        self.assertEqual(overrides["qb_password"], "secret-value")
        self.assertNotIn("secret-value", rendered)
        self.assertIn("qBittorrent 密码：已设置（个人）", rendered)
        self.assertEqual(self.config.save_count, 1)

        removed = await self.instance._delete_user_override("10002", "qb_password")
        self.assertTrue(removed)
        self.assertNotIn("qb_password", self.instance._user_overrides("10002"))
        self.assertEqual(self.config.save_count, 2)

    def test_configuration_value_validation(self):
        self.assertEqual(
            self.instance._parse_user_config_value("download_client", "qb"),
            "qbittorrent",
        )
        self.assertTrue(self.instance._parse_user_config_value("qb_paused", "开启"))
        with self.assertRaises(plugin_module.UserFacingError):
            self.instance._parse_user_config_value("max_results", "31")

    def test_cookie_origin_check(self):
        self.assertTrue(
            self.instance._same_origin(
                "https://pt.example/download.php?id=1", "https://pt.example/"
            )
        )
        self.assertFalse(
            self.instance._same_origin(
                "https://files.example/item.torrent", "https://pt.example/"
            )
        )


class AccessControlTests(unittest.TestCase):
    def test_blacklist_takes_priority_for_regular_users(self):
        instance = make_plugin(
            {
                "access_whitelist": ["10001"],
                "access_blacklist": ["10001"],
                "access_admin_bypass": False,
            }
        )

        with self.assertRaisesRegex(plugin_module.UserFacingError, "黑名单"):
            instance._ensure_user_access(FakeEvent("10001"))

    def test_nonempty_whitelist_rejects_other_users(self):
        instance = make_plugin({"access_whitelist": ["10001"]})

        instance._ensure_user_access(FakeEvent("10001"))
        with self.assertRaisesRegex(plugin_module.UserFacingError, "白名单"):
            instance._ensure_user_access(FakeEvent("10002"))

    def test_admin_can_bypass_access_lists(self):
        instance = make_plugin(
            {
                "access_whitelist": ["10001"],
                "access_blacklist": ["90001"],
                "access_admin_bypass": True,
            }
        )

        instance._ensure_user_access(FakeEvent("90001", admin=True))

    def test_global_config_can_be_limited_to_admins(self):
        instance = make_plugin(
            {
                "global_config_admin_only": True,
                "qb_url": "http://global:8080",
                "provider_cookie": "global-cookie",
                "user_profiles": [
                    {
                        "__template_key": "user",
                        "qq": "10001",
                        "qb_username": "personal-user",
                    }
                ],
            }
        )

        regular_config = instance._user_config(FakeEvent("10001"))
        admin_config = instance._user_config(FakeEvent("90001", admin=True))

        self.assertEqual(regular_config["qb_username"], "personal-user")
        self.assertEqual(regular_config["qb_url"], "")
        self.assertEqual(regular_config["provider_cookie"], "")
        self.assertEqual(
            regular_config["provider_base_url"], "https://www.gay-torrents.net/"
        )
        self.assertEqual(admin_config["qb_url"], "http://global:8080")
        self.assertEqual(admin_config["provider_cookie"], "global-cookie")

    def test_legacy_event_admin_role_is_supported(self):
        class LegacyEvent:
            role = "admin"

            def get_sender_id(self):
                return "90001"

        instance = make_plugin(
            {
                "access_blacklist": ["90001"],
                "access_admin_bypass": True,
            }
        )

        instance._ensure_user_access(LegacyEvent())


class BatchDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = make_plugin({})
        self.results = make_results(5)

    def test_multiple_indexes_are_selected_in_order(self):
        selected = self.instance._pick_results("1 3 5", self.results)

        self.assertEqual([torrent.index for torrent in selected], [1, 3, 5])

    def test_commas_and_duplicate_indexes_are_supported(self):
        selected = self.instance._pick_results("1,3，3 5", self.results)

        self.assertEqual([torrent.index for torrent in selected], [1, 3, 5])

    def test_any_out_of_range_index_rejects_the_whole_selection(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "无效序号：4、6"):
            self.instance._pick_results("1 4 6", make_results(3))

    def test_non_numeric_index_is_rejected(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "序号格式错误：abc"):
            self.instance._pick_results("1 abc 3", self.results)

    async def test_handler_reports_partial_client_failures(self):
        event = FakeEvent("10001")
        self.instance._search_cache["10001"] = (
            time.time(),
            600,
            make_results(3),
        )

        async def fake_add(torrent, _user_config, _monitor=None):
            if torrent.index == 2:
                raise plugin_module.UserFacingError("客户端拒绝任务")

        self.instance._add_to_download_client = fake_add

        response = await anext(self.instance.download_torrent(event, "1 2 3"))

        self.assertIn("已添加到 qBittorrent：2 个任务", response)
        self.assertIn("添加失败：1 个任务", response)
        self.assertIn("2. 种子 2：客户端拒绝任务", response)


class QBittorrentWebAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = make_plugin({})
        self.config = {"username": "tester", "password": "secret"}

    @staticmethod
    def make_client(status_code, body=""):
        class FakeClient:
            async def post(self, _url, data):
                return plugin_module.httpx.Response(status_code, text=body)

        return FakeClient()

    async def test_accepts_qbittorrent_52_no_content_response(self):
        client = self.make_client(204)

        await self.instance._login_qbittorrent(client, self.config)

    async def test_accepts_legacy_ok_response(self):
        client = self.make_client(200, "Ok.")

        await self.instance._login_qbittorrent(client, self.config)

    async def test_legacy_failed_response_reports_invalid_credentials(self):
        client = self.make_client(200, "Fails.")

        with self.assertRaisesRegex(plugin_module.UserFacingError, "用户名和密码"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_unauthorized_response_reports_invalid_credentials(self):
        client = self.make_client(401)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "用户名和密码"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_forbidden_response_mentions_temporary_ban(self):
        client = self.make_client(403)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "临时封禁"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_unrecognized_success_response_is_not_reported_as_bad_password(self):
        client = self.make_client(200, "Unexpected")

        with self.assertRaisesRegex(plugin_module.UserFacingError, "无法识别"):
            await self.instance._login_qbittorrent(client, self.config)

    def test_accepts_qbittorrent_52_structured_add_response(self):
        response = plugin_module.httpx.Response(
            200,
            json={
                "success_count": 1,
                "pending_count": 0,
                "failure_count": 0,
                "added_torrent_ids": ["hash"],
            },
        )

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_accepts_qbittorrent_52_pending_add_response(self):
        response = plugin_module.httpx.Response(
            202,
            json={"success_count": 0, "pending_count": 1, "failure_count": 0},
        )

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_accepts_no_content_add_response(self):
        response = plugin_module.httpx.Response(204)

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_conflict_add_response_reports_rejected_task(self):
        response = plugin_module.httpx.Response(409)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "可能已存在"):
            self.instance._check_qbittorrent_add_response(response, "添加失败")


GT_ROWS_HTML = """
<div class="torrents">
  <torrent>
    <li class="Torrent-List-Size">1.2 GB</li>
    <li class="Torrent-List-Seeds">42</li>
    <li class="Torrent-List-Peers">7</li>
    <li class="Torrent-List-Date">21:05 03-Nov-2026</li>
    <a href="torrentslist.php?type=Video%2FHDTV">Video/HDTV</a>
    <a href="torrentdetails.php?torrentid=12345">[FL] Some Movie 1080p</a>
  </torrent>
  <torrent>
    <li class="TorrentList3">700 MB</li>
    <li class="TorrentList6">3</li>
    <li class="TorrentList7">1</li>
    <li class="TorrentList8">09:00 01-Jan-2025</li>
    <a href="torrentslist.php?type=Games">Games</a>
    <a href="/torrentdetails.php?torrentid=999">Some Game</a>
  </torrent>
</div>
"""

GT_LOGIN_PAGE_HTML = """
<html><head><script>var SECURITYTOKEN = "guest";</script></head>
<body><div class="standard_error">You are not logged in or you do not have
permission to access this page.</div>
<form><input type="text" name="vb_login_username"></form></body></html>
"""

# GT_ajax_search.php 的真实响应形态：XML 外壳 + CDATA 包裹的行 HTML。
GT_SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<searchresult>
\t<torrent>
\t\t<torrentdata><![CDATA[<div id="display"><ul class="Torrent-List  TListSnatched">
\t<li class="Torrent-List-Image"><a href="torrentslist.php?type=nonporn/Drama"><img src="Drama.gif"></a></li>
\t<li class="Torrent-List-FL">&nbsp;</li>
\t<li class="Torrent-List-Name-ns"><a class="ajax_popup_class" data-querystring="id=abc123" href="torrentdetails.php?torrentid=abc123">Jonathan Glazer - Sexy Beast (2000) UK</a></li>
\t<li class="Torrent-List-Size">2.92 GiB</li>
\t<li class="Torrent-List-Mod-ns">&nbsp;</li>
\t<li class="Torrent-List-UL-ns"><a href="torrents_user_list.php?user=fusain">fusain</a></li>
\t<li class="Torrent-List-Seeds">11|</li>
\t<li class="Torrent-List-Peers">   0</li>
\t<li class="Torrent-List-Date">04:38 02-Oct-2026</li>
</ul></div>]]></torrentdata>
\t</torrent>
\t<torrent>
\t\t<torrentdata><![CDATA[<ul class="Torrent-List ">
\t<li class="Torrent-List-FL">Free</li>
\t<li class="Torrent-List-Name-ns"><a href="torrentdetails.php?torrentid=def456">Some Free Torrent</a></li>
\t<li class="Torrent-List-Size">749.46 MiB</li>
\t<li class="Torrent-List-Seeds">3|</li>
\t<li class="Torrent-List-Peers">1</li>
\t<li class="Torrent-List-Date">19:54 03-Sep-2026</li>
</ul>]]></torrentdata>
\t</torrent>
\t<torrentcount><count>533</count></torrentcount>
\t<maxtorrents><count>20</count></maxtorrents>
</searchresult>
"""


class GayTorrentsProviderTests(unittest.TestCase):
    def test_default_provider_is_gaytorrents(self):
        instance = make_plugin({})

        provider = instance._provider_config(instance._user_config(FakeEvent("1")))

        self.assertEqual(provider["type"], "gaytorrents")
        self.assertEqual(provider["base_url"], "https://www.gay-torrents.net/")
        self.assertEqual(
            provider["search_path"],
            "GT_torrents/GT_ajax_search.php?filename={keyword}",
        )

    def test_legacy_provider_types_keep_their_defaults(self):
        instance = make_plugin({})

        nexusphp = instance._provider_config({"provider_type": "nexusphp"})
        mteam = instance._provider_config(
            {"provider_type": "M-Team", "mteam_api_token": "token"}
        )

        self.assertEqual(nexusphp["base_url"], "https://www.tjupt.org/")
        self.assertIn("torrents.php?search={keyword}", nexusphp["search_path"])
        self.assertEqual(mteam["type"], "mteam")
        self.assertEqual(mteam["base_url"], "https://api.m-team.cc")
        self.assertEqual(mteam["search_path"], "/api/torrent/search")

    def test_provider_type_aliases_map_to_gaytorrents(self):
        instance = make_plugin({})

        for value in ("gaytorrents", "gay-torrents-net", "GT", "gay_t"):
            self.assertEqual(
                instance._provider_config({"provider_type": value})["type"],
                "gaytorrents",
                value,
            )

    def test_switching_provider_type_resets_builtin_defaults(self):
        instance = make_plugin({})

        provider = instance._provider_config(
            {
                "provider_type": "nexusphp",
                "provider_base_url": "https://www.gay-torrents.net/",
                "provider_search_path": "GT_torrents/GT_ajax_search.php?filename={keyword}",
            }
        )

        self.assertEqual(provider["type"], "nexusphp")
        self.assertEqual(provider["base_url"], "https://www.tjupt.org/")
        self.assertIn("torrents.php?search={keyword}", provider["search_path"])

    def test_custom_site_url_is_preserved(self):
        instance = make_plugin({})

        provider = instance._provider_config(
            {
                "provider_type": "gaytorrents",
                "provider_base_url": "https://mirror.example.com/",
            }
        )

        self.assertEqual(provider["base_url"], "https://mirror.example.com/")


class GayTorrentsParserTests(unittest.TestCase):
    def setUp(self):
        self.instance = make_plugin({})
        self.provider = self.instance._provider_config(
            dict(plugin_module.BUILTIN_USER_CONFIG)
        )

    def test_parses_torrent_rows(self):
        results = self.instance._parse_gaytorrents_results(
            GT_ROWS_HTML, "https://www.gay-torrents.net/", 10
        )

        self.assertEqual(len(results), 2)
        first, second = results
        self.assertEqual((first.index, second.index), (1, 2))
        self.assertEqual(first.title, "Some Movie 1080p")
        self.assertEqual(first.subtitle, "Video/HDTV · 免费")
        self.assertEqual(first.size, "1.2 GB")
        self.assertEqual(first.seeders, "42")
        self.assertEqual(first.leechers, "7")
        self.assertEqual(first.completed, "?")
        self.assertEqual(
            first.detail_url,
            "https://www.gay-torrents.net/torrentdetails.php?torrentid=12345",
        )
        self.assertEqual(
            first.download_url,
            "https://www.gay-torrents.net/torrentdetails.php?torrentid=12345&do=download",
        )
        self.assertEqual(first.added_at, "2026-11-03 21:05")
        self.assertEqual(second.title, "Some Game")
        self.assertEqual(second.subtitle, "Games")
        self.assertEqual(second.added_at, "2025-01-01 09:00")

    def test_parses_ajax_search_xml_payload(self):
        results = self.instance._parse_gaytorrents_results(
            GT_SEARCH_XML, "https://www.gay-torrents.net/", 10
        )

        self.assertEqual(len(results), 2)
        first, second = results
        self.assertEqual(first.title, "Jonathan Glazer - Sexy Beast (2000) UK")
        self.assertEqual(first.subtitle, "nonporn/Drama")
        self.assertEqual(first.size, "2.92 GiB")
        self.assertEqual(first.seeders, "11")
        self.assertEqual(first.leechers, "0")
        self.assertEqual(first.completed, "?")
        self.assertEqual(first.added_at, "2026-10-02 04:38")
        self.assertEqual(
            first.detail_url,
            "https://www.gay-torrents.net/torrentdetails.php?torrentid=abc123",
        )
        self.assertEqual(
            first.download_url,
            "https://www.gay-torrents.net/torrentdetails.php?torrentid=abc123&do=download",
        )
        self.assertEqual(second.subtitle, "免费")

    def test_falls_back_to_anchor_parent(self):
        html_text = """
        <ul><li class="Torrent-Item">
          <a href="torrentdetails.php?torrentid=8">Plain Title</a>
          <span class="Torrent-List-Size">5 GB</span>
        </li></ul>
        """

        results = self.instance._parse_gaytorrents_results(
            html_text, "https://www.gay-torrents.net/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Plain Title")
        self.assertEqual(results[0].size, "5 GB")

    def test_parses_escaped_row_markup(self):
        html_text = (
            '<torrent>&lt;a href="torrentdetails.php?torrentid=7"&gt;'
            "Escaped Title&lt;/a&gt;</torrent>"
        )

        results = self.instance._parse_gaytorrents_results(
            html_text, "https://www.gay-torrents.net/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Escaped Title")

    def test_respects_result_limit(self):
        results = self.instance._parse_gaytorrents_results(
            GT_ROWS_HTML, "https://www.gay-torrents.net/", 1
        )

        self.assertEqual(len(results), 1)

    def test_cookie_guest_detection_accepts_both_password_fields(self):
        self.assertTrue(
            self.instance._gaytorrents_cookie_is_guest("ENvbb_sessionhash=x")
        )
        self.assertFalse(
            self.instance._gaytorrents_cookie_is_guest(
                "ENvbb_userid=17; ENvbb_password=hash"
            )
        )
        self.assertFalse(
            self.instance._gaytorrents_cookie_is_guest(
                "ENvbb_userid=17; ENvbb_passwordhash=hash"
            )
        )

    def test_detects_login_required_pages(self):
        self.assertTrue(self.instance._gaytorrents_requires_login(GT_LOGIN_PAGE_HTML))
        self.assertFalse(self.instance._gaytorrents_requires_login(GT_ROWS_HTML))

    def test_boolean_keyword_rewriting(self):
        self.assertEqual(
            self.instance._gaytorrents_query("Big Blue Movie with Friends"),
            "Big +Blue +Movie +Friends",
        )
        self.assertEqual(self.instance._gaytorrents_query("TIM abc"), "TIM abc")

    def test_search_url_quotes_keyword_and_appends_page(self):
        url = self.instance._gaytorrents_search_url(self.provider, "Big Blue", 2)

        self.assertEqual(
            url,
            "https://www.gay-torrents.net/GT_torrents/GT_ajax_search.php"
            "?filename=Big+Blue&page=2",
        )

    def test_search_url_replaces_existing_page_parameter(self):
        provider = self.instance._provider_config(
            {
                "provider_type": "gaytorrents",
                "provider_search_path": "GT_torrents/GT_ajax_search.php"
                "?filename={keyword}&page=0",
            }
        )

        url = self.instance._gaytorrents_search_url(provider, "tim", 3)

        self.assertEqual(
            url,
            "https://www.gay-torrents.net/GT_torrents/GT_ajax_search.php"
            "?filename=tim&page=3",
        )

    def test_normalizes_site_dates(self):
        self.assertEqual(
            self.instance._normalize_gaytorrents_date("21:05 03-Nov-2026"),
            "2026-11-03 21:05",
        )
        self.assertEqual(
            self.instance._normalize_gaytorrents_date("03-Nov-2026"), "2026-11-03"
        )
        self.assertEqual(self.instance._normalize_gaytorrents_date(""), "未知")
        self.assertEqual(self.instance._normalize_gaytorrents_date("今天"), "今天")


class GayTorrentsSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = make_plugin({})

    def install_client(self, client_factory):
        calls = []

        def factory(provider, user_config, **kwargs):
            calls.append(kwargs)
            return client_factory()

        self.instance._http_client = factory
        return calls

    async def test_search_logs_in_and_uses_session_cookie(self):
        posted = []

        class FakeClient:
            def __init__(self):
                self.cookies = plugin_module.httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, data=None, **kwargs):
                posted.append((url, data))
                self.cookies.set("ENvbb_userid", "17")
                self.cookies.set("ENvbb_password", "hash")
                return plugin_module.httpx.Response(200, text="Logged in")

            async def get(self, url, headers=None, **kwargs):
                if "torrentslist.php" in url:
                    return plugin_module.httpx.Response(
                        200, text='<ul class="isuser">tester</ul>'
                    )
                return plugin_module.httpx.Response(200, text=GT_ROWS_HTML)

        calls = self.install_client(FakeClient)
        user_config = {
            "provider_type": "gaytorrents",
            "provider_username": "tester",
            "provider_password": "pw",
        }

        results = await self.instance._search_gaytorrents(
            "tim", user_config, None, True
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].title, "Some Movie 1080p")
        self.assertEqual(posted[0][1]["vb_login_username"], "tester")
        self.assertEqual(posted[0][1]["do"], "login")
        self.assertEqual(
            set(calls[-1]["cookie"].split("; ")),
            {"ENvbb_userid=17", "ENvbb_password=hash"},
        )
        cached_cookie = self.instance._gt_sessions[
            "https://www.gay-torrents.net/|tester"
        ][1]
        self.assertEqual(
            set(cached_cookie.split("; ")),
            {"ENvbb_userid=17", "ENvbb_password=hash"},
        )

    async def test_missing_credentials_ask_for_cookie_or_account(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "pt账号 和 pt密码"):
            await self.instance._search_gaytorrents("tim", {}, None, True)

    async def test_expired_cookie_reports_invalid_login(self):
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get(self, url, headers=None, **kwargs):
                return plugin_module.httpx.Response(200, text=GT_LOGIN_PAGE_HTML)

        self.install_client(FakeClient)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "登录状态无效"):
            await self.instance._search_gaytorrents(
                "tim",
                {
                    "provider_type": "gaytorrents",
                    "provider_cookie": (
                        "ENvbb_userid=17; ENvbb_passwordhash=old; ENvbb_sessionhash=x"
                    ),
                },
                None,
                True,
            )

    async def test_guest_cookie_explains_missing_login_fields(self):
        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get(self, url, headers=None, **kwargs):
                return plugin_module.httpx.Response(200, text=GT_LOGIN_PAGE_HTML)

        self.install_client(FakeClient)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "缺少登录字段"):
            await self.instance._search_gaytorrents(
                "tim",
                {
                    "provider_type": "gaytorrents",
                    "provider_cookie": (
                        "ENvbb_lastvisit=1795011326; ENvbb_sessionhash=abc"
                    ),
                },
                None,
                True,
            )

    async def test_auto_login_retries_once_then_reports_failure(self):
        class FakeClient:
            def __init__(self):
                self.cookies = plugin_module.httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, data=None, **kwargs):
                self.cookies.set("ENvbb_userid", "17")
                return plugin_module.httpx.Response(200, text="Logged in")

            async def get(self, url, headers=None, **kwargs):
                if "torrentslist.php" in url:
                    return plugin_module.httpx.Response(
                        200, text='<ul class="isuser">tester</ul>'
                    )
                return plugin_module.httpx.Response(200, text=GT_LOGIN_PAGE_HTML)

        self.install_client(FakeClient)
        user_config = {
            "provider_type": "gaytorrents",
            "provider_username": "tester",
            "provider_password": "pw",
        }

        with self.assertRaisesRegex(
            plugin_module.UserFacingError, "自动登录后仍无法访问"
        ):
            await self.instance._search_gaytorrents("tim", user_config, None, True)

    async def test_login_without_isuser_marks_credentials_invalid(self):
        class FakeClient:
            def __init__(self):
                self.cookies = plugin_module.httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, data=None, **kwargs):
                self.cookies.set("ENvbb_userid", "17")
                return plugin_module.httpx.Response(
                    200, text='<div class="blockrow">Invalid username or password</div>'
                )

            async def get(self, url, headers=None, **kwargs):
                return plugin_module.httpx.Response(200, text=GT_LOGIN_PAGE_HTML)

        self.install_client(FakeClient)
        provider = self.instance._provider_config(
            {"provider_username": "tester", "provider_password": "pw"}
        )

        with self.assertRaisesRegex(plugin_module.UserFacingError, "自动登录失败"):
            await self.instance._login_gaytorrents(provider, {})

    def test_login_error_messages(self):
        cases = {
            '<div class="blockrow">You have entered an invalid username or '
            "password. Please try again.</div>": "账号或密码不正确",
            '<img src="image.php?do=verify&amp;t=1" />': "图片验证码",
            "<div>You have exceeded too many login attempts</div>": "暂时锁定",
        }
        for html, expected in cases.items():
            with self.subTest(html=html):
                self.assertIn(
                    expected,
                    plugin_module.QBittorrentManagerPlugin._gaytorrents_login_error(
                        html
                    ),
                )
        self.assertEqual(
            plugin_module.QBittorrentManagerPlugin._gaytorrents_login_error(
                GT_ROWS_HTML
            ),
            "",
        )

    async def test_invalid_credentials_report_specific_message(self):
        class FakeClient:
            def __init__(self):
                self.cookies = plugin_module.httpx.Cookies()
                self.gets = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, data=None, **kwargs):
                return plugin_module.httpx.Response(
                    200,
                    text=(
                        '<div class="blockrow">You have entered an invalid username '
                        "or password. Please enter the correct details.</div>"
                    ),
                )

            async def get(self, url, headers=None, **kwargs):
                self.gets.append(url)
                return plugin_module.httpx.Response(200, text=GT_LOGIN_PAGE_HTML)

        self.install_client(FakeClient)
        provider = self.instance._provider_config(
            {
                "provider_type": "gaytorrents",
                "provider_username": "tester",
                "provider_password": "pw",
            }
        )

        with self.assertRaisesRegex(plugin_module.UserFacingError, "账号或密码不正确"):
            await self.instance._login_gaytorrents(provider, {})


class DownloadMonitorTests(unittest.IsolatedAsyncioTestCase):
    COMPLETE_MESSAGE = "种子已下载完成，请登录群网盘-桌面botdownload文件夹查看"

    def setUp(self):
        self.instance = make_plugin({"monitor_interval_minutes": 5})
        self.user_config = {
            "download_client": "qbittorrent",
            "qb_url": "http://127.0.0.1:8080/",
            "qb_username": "admin",
            "qb_password": "adminadmin",
        }
        self.event = FakeEvent("10001", session="QQ|GroupMessage|group1|10001")

    def register_watch(self, title="种子 A", torrent_hash="ab12" * 10):
        self.instance._register_download_watch(
            self.instance._monitor_target(self.event, self.user_config),
            self.user_config,
            title,
            torrent_hash,
        )

    def test_download_client_config_is_the_default_qbittorrent(self):
        self.assertEqual(
            self.instance._download_client_type({}),
            "qbittorrent",
        )

    def test_torrent_btih_is_sha1_of_info_section(self):
        info = b"d4:name4:test12:piece lengthi65536e6:pieces0:e"
        content = b"d4:info" + info + b"e"
        expected = hashlib.sha1(info).hexdigest()

        self.assertEqual(self.instance._torrent_btih(content), expected)

    def test_torrent_btih_skips_keys_before_info(self):
        info = b"d4:name4:teste"
        announce = b"http://tracker.example/announce"
        content = (
            b"d8:announce"
            + str(len(announce)).encode()
            + b":"
            + announce
            + b"13:creation datei1700000000e4:info"
            + info
            + b"e"
        )
        expected = hashlib.sha1(info).hexdigest()

        self.assertEqual(self.instance._torrent_btih(content), expected)

    def test_torrent_btih_rejects_invalid_data(self):
        self.assertEqual(self.instance._torrent_btih(b"<html>nope</html>"), "")

    def test_magnet_btih_supports_hex_and_base32(self):
        digest = bytes(range(20))
        hex_uri = f"magnet:?xt=urn:btih:{digest.hex()}&dn=test"
        base32 = base64.b32encode(digest).decode().rstrip("=")
        base32_uri = f"magnet:?xt=urn:btih:{base32}&dn=test"

        self.assertEqual(self.instance._magnet_btih(hex_uri), digest.hex())
        self.assertEqual(self.instance._magnet_btih(base32_uri), digest.hex())
        self.assertEqual(self.instance._magnet_btih("magnet:?dn=test"), "")

    def test_completed_detection_uses_qbittorrent_states(self):
        for state in ("uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP"):
            self.assertTrue(
                self.instance._qbittorrent_entry_completed({"state": state}),
                state,
            )
        self.assertTrue(
            self.instance._qbittorrent_entry_completed(
                {"state": "downloading", "progress": 1.0}
            )
        )
        self.assertFalse(
            self.instance._qbittorrent_entry_completed(
                {"state": "downloading", "progress": 0.5}
            )
        )
        self.assertFalse(
            self.instance._qbittorrent_entry_completed({"state": "stalledDL"})
        )

    def test_monitor_target_needs_session_qbittorrent_and_push(self):
        self.assertIsNotNone(
            self.instance._monitor_target(self.event, self.user_config)
        )
        self.assertIsNone(
            self.instance._monitor_target(FakeEvent("10001"), self.user_config)
        )
        self.assertIsNone(
            self.instance._monitor_target(
                self.event,
                {
                    "download_client": "utorrent",
                    "ut_url": "http://127.0.0.1:8081/",
                    "ut_username": "admin",
                    "ut_password": "pw",
                },
            )
        )
        self.assertIsNone(
            self.instance._monitor_target(
                self.event, {**self.user_config, "monitor_enabled": False}
            )
        )

    def test_monitor_note_explains_monitoring(self):
        monitor = self.instance._monitor_target(self.event, self.user_config)

        note = self.instance._monitor_note(self.user_config, monitor, 2)

        self.assertIn("已加入下载状态监控", note)
        self.assertIn("每 5 分钟汇报一次", note)
        self.assertEqual(self.instance._monitor_note(self.user_config, monitor, 0), "")

    def test_monitor_note_mentions_utorrent_limitation(self):
        user_config = {
            "download_client": "utorrent",
            "ut_url": "http://127.0.0.1:8081/",
        }

        note = self.instance._monitor_note(user_config, None, 1)

        self.assertIn("uTorrent 暂不支持下载状态监控", note)

    def test_watch_is_registered_once_per_hash_and_user(self):
        self.register_watch()
        self.register_watch(title="种子 A 改名")

        self.assertEqual(len(self.instance._download_watches), 1)
        watch = next(iter(self.instance._download_watches.values()))
        self.assertEqual(watch.title, "种子 A 改名")
        self.assertEqual(watch.endpoint, "http://127.0.0.1:8080")
        self.assertEqual(watch.session, self.event.unified_msg_origin)

    async def test_watch_registry_drops_oldest_over_limit(self):
        monitor = self.instance._monitor_target(self.event, self.user_config)
        for index in range(plugin_module.MONITOR_MAX_WATCHES + 3):
            self.instance._register_download_watch(
                monitor,
                self.user_config,
                f"种子 {index}",
                f"{index:040x}",
            )

        self.assertEqual(
            len(self.instance._download_watches), plugin_module.MONITOR_MAX_WATCHES
        )
        self.assertNotIn("种子 0", {watch.title for watch in self._watches()})

    def _watches(self):
        return sorted(
            self.instance._download_watches.values(),
            key=lambda watch: watch.registered_at,
        )

    def install_task_map(self, entries):
        async def fake_task_map(_watches):
            return entries

        self.instance._fetch_qbittorrent_task_map = fake_task_map

    async def test_completion_sends_fixed_message_with_at_once(self):
        self.register_watch()
        self.install_task_map(
            {
                "ab12" * 10: {
                    "state": "stalledUP",
                    "name": "种子 A",
                    "progress": 1,
                    "size": 1024,
                    "total_done": 1024,
                }
            }
        )

        await self.instance._monitor_once()
        await self.instance._monitor_once()

        self.assertEqual(self.instance._download_watches, {})
        sent = self.instance.context.sent
        self.assertEqual(len(sent), 1)
        session, chain = sent[0]
        self.assertEqual(session, self.event.unified_msg_origin)
        self.assertTrue(chain_text(chain).endswith(self.COMPLETE_MESSAGE))
        self.assertEqual(chain.chain[0].qq, 10001)

    async def test_progress_report_is_sent_while_downloading(self):
        self.register_watch()
        self.install_task_map(
            {
                "ab12" * 10: {
                    "state": "downloading",
                    "name": "种子 A",
                    "progress": 0.5,
                    "size": 2048,
                    "total_done": 1024,
                    "upspeed": 512,
                    "dlspeed": 1024,
                    "eta": 90,
                }
            }
        )

        await self.instance._monitor_once()

        self.assertEqual(len(self.instance._download_watches), 1)
        text = chain_text(self.instance.context.sent[0][1])
        self.assertIn("下载进度｜种子 A", text)
        self.assertIn("状态：下载中", text)
        self.assertIn("50.0%", text)
        self.assertIn("KiB", text)

    async def test_progress_report_can_be_disabled(self):
        self.instance.config["monitor_report_progress"] = False
        self.register_watch()
        self.install_task_map(
            {"ab12" * 10: {"state": "downloading", "name": "种子 A", "progress": 0.2}}
        )

        await self.instance._monitor_once()

        self.assertEqual(self.instance.context.sent, [])
        self.assertEqual(len(self.instance._download_watches), 1)

    async def test_missing_task_stops_monitoring(self):
        self.register_watch()
        self.install_task_map({})

        await self.instance._monitor_once()

        self.assertEqual(self.instance._download_watches, {})
        self.assertIn("已找不到", chain_text(self.instance.context.sent[0][1]))

    async def test_stale_task_stops_monitoring(self):
        self.instance.config["monitor_max_hours"] = 12
        self.register_watch()
        watch = next(iter(self.instance._download_watches.values()))
        watch.registered_at = time.time() - 13 * 3600
        self.install_task_map(
            {"ab12" * 10: {"state": "stalledDL", "name": "种子 A", "progress": 0.1}}
        )

        await self.instance._monitor_once()

        self.assertEqual(self.instance._download_watches, {})
        self.assertIn("超过 12 小时", chain_text(self.instance.context.sent[0][1]))

    async def test_monitor_view_and_stop_commands(self):
        self.register_watch(title="种子甲", torrent_hash="c" * 40)
        self.register_watch(title="种子乙", torrent_hash="d" * 40)

        view = await anext(self.instance.show_monitor_status(self.event))
        self.assertIn("正在监控 2 个下载任务", view)
        self.assertIn("种子甲", view)

        stopped = await anext(self.instance.stop_monitor(self.event, "1"))
        self.assertIn("已停止监控 1 个任务", stopped)
        self.assertEqual(len(self.instance._download_watches), 1)

        everything = await anext(self.instance.stop_monitor(self.event, "全部"))
        self.assertIn("已停止监控 1 个任务", everything)
        self.assertEqual(self.instance._download_watches, {})

    async def test_monitor_help_lists_commands(self):
        help_text = await anext(self.instance.monitor_help(self.event))

        self.assertIn("/种子监控 查看", help_text)
        self.assertIn("/种子监控 停止", help_text)

    async def test_resolve_prefers_expected_hash_when_present(self):
        client = self.make_qb_client(
            [
                {"hash": "OLD", "name": "旧", "added_on": 1},
                {"hash": "NEW1", "added_on": 2},
            ]
        )

        digest = await self.instance._resolve_qbittorrent_hash(
            client, {"old"}, "new1", "种子 A"
        )

        self.assertEqual(digest, "new1")

    async def test_resolve_picks_task_added_after_snapshot(self):
        client = self.make_qb_client(
            [
                {"hash": "old", "name": "旧", "added_on": 1},
                {"hash": "fresh", "name": "种子 A", "added_on": 2},
            ]
        )

        digest = await self.instance._resolve_qbittorrent_hash(
            client, {"old"}, "", "种子 A"
        )

        self.assertEqual(digest, "fresh")

    async def test_resolve_matches_title_and_added_on(self):
        client = self.make_qb_client(
            [
                {"hash": "a" * 40, "name": "别的种子", "added_on": 99},
                {"hash": "b" * 40, "name": "Seed A 1080p", "added_on": 3},
                {"hash": "c" * 40, "name": "Seed A 1080p x265", "added_on": 5},
            ]
        )

        digest = await self.instance._resolve_qbittorrent_hash(
            client, set(), "", "Seed A 1080p"
        )

        self.assertEqual(digest, "c" * 40)

    async def test_resolve_falls_back_to_expected_hash_on_error(self):
        class BrokenClient:
            async def get(self, *_args, **_kwargs):
                raise RuntimeError("connection refused")

        digest = await self.instance._resolve_qbittorrent_hash(
            BrokenClient(), set(), "d" * 40, "种子 A"
        )

        self.assertEqual(digest, "d" * 40)

    @staticmethod
    def make_qb_client(tasks):
        class FakeClient:
            async def get(self, _url, params=None, **_kwargs):
                return plugin_module.httpx.Response(200, json=tasks)

        return FakeClient()


class SmartDownloadTests(unittest.IsolatedAsyncioTestCase):
    MAGNET = "magnet:?xt=urn:btih:" + "ab12" * 10 + "&dn=Seed%20A"

    def setUp(self):
        self.instance = make_plugin({"monitor_interval_minutes": 5})
        self.user_config = {
            "download_client": "qbittorrent",
            "qb_url": "http://127.0.0.1:8080/",
            "aria2_rpc_url": "http://127.0.0.1:6800/jsonrpc",
            "aria2_rpc_token": "secret",
            "aria2_save_path": "/downloads/bot",
            "ytdlp_api_url": "http://127.0.0.1:5000",
            "ytdlp_api_key": "key123",
        }
        self.event = FakeEvent("10001", session="QQ|GroupMessage|group1|10001")

    def install_client(self, factory):
        calls = []
        original = plugin_module.httpx.AsyncClient
        plugin_module.httpx.AsyncClient = factory
        self.addCleanup(setattr, plugin_module.httpx, "AsyncClient", original)
        return calls

    def test_classifies_links_by_download_backend(self):
        cases = {
            self.MAGNET: "qbittorrent",
            "https://www.gay-torrents.net/torrentdetails.php?torrentid=a1&do=download": (
                "qbittorrent"
            ),
            "https://www.tjupt.org/download.php?id=12345": "qbittorrent",
            "https://files.example.com/movies/seed-a.torrent": "qbittorrent",
            "https://cdn.example.com/movie.mkv": "aria2",
            "https://cdn.example.com/video.mp4": "aria2",
            "https://www.youtube.com/watch?v=abc123": "ytdlp",
            "https://b23.tv/BV1xx411c7mD": "ytdlp",
            "https://live.twitch.tv/somechannel": "ytdlp",
            "https://archive.org/details/some-item": "aria2",
        }

        for uri, kind in cases.items():
            with self.subTest(uri=uri):
                self.assertEqual(
                    self.instance._classify_download_target(uri, self.user_config),
                    kind,
                )

    def test_custom_video_hosts_are_honoured(self):
        user_config = dict(self.user_config, video_hosts="example.com, .other.site")

        self.assertEqual(
            self.instance._classify_download_target(
                "https://watch.example.com/clip/9", user_config
            ),
            "ytdlp",
        )
        self.assertEqual(
            self.instance._classify_download_target(
                "https://sub.other.site/clip/9", user_config
            ),
            "ytdlp",
        )

    def test_rejects_unsupported_link_schemes(self):
        with self.assertRaises(plugin_module.UserFacingError):
            self.instance._classify_download_target("ftp://a/b", self.user_config)

    def test_resolved_kind_reports_the_reason(self):
        self.assertEqual(
            self.instance._resolve_download_kind(self.MAGNET, self.user_config),
            ("qbittorrent", "磁链或种子"),
        )
        self.assertEqual(
            self.instance._resolve_download_kind(
                "https://cdn.example.com/movie.mkv", self.user_config
            ),
            ("aria2", "普通直链"),
        )
        self.assertEqual(
            self.instance._resolve_download_kind(
                "https://www.youtube.com/watch?v=abc", self.user_config
            ),
            ("ytdlp", "视频网页"),
        )

    def test_ytdlp_without_service_url_explains_configuration(self):
        user_config = dict(self.user_config, ytdlp_api_url="")

        with self.assertRaises(plugin_module.UserFacingError) as ctx:
            self.instance._resolve_download_kind(
                "https://www.youtube.com/watch?v=abc", user_config
            )

        self.assertIn("ytdlp_api_url", str(ctx.exception))

    def test_missing_aria2_falls_back_to_qbittorrent(self):
        user_config = dict(self.user_config, aria2_rpc_url="")

        kind, reason = self.instance._resolve_download_kind(
            "https://cdn.example.com/movie.mkv", user_config
        )

        self.assertEqual(kind, "qbittorrent")
        self.assertIn("aria2 未配置", reason)

    def test_clean_uri_takes_the_first_token(self):
        self.assertEqual(
            self.instance._clean_uri("  <https://example.com/a.mkv> 备注 "),
            "https://example.com/a.mkv",
        )
        self.assertEqual(self.instance._clean_uri("   "), "")

    def install_post_client(self, payload, status_code=200):
        requests = []
        clients = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                clients.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def post(self, url, json=None, **kwargs):
                requests.append((url, json, kwargs))
                return plugin_module.httpx.Response(status_code, json=payload)

        self.install_client(FakeClient)
        return requests, clients

    async def test_add_to_aria2_uses_jsonrpc_with_options(self):
        requests, _clients = self.install_post_client({"result": "gid001"})

        gid = await self.instance._add_to_aria2(
            "https://cdn.example.com/movie.mkv", self.user_config
        )

        self.assertEqual(gid, "gid001")
        url, payload, _kwargs = requests[0]
        self.assertEqual(url, "http://127.0.0.1:6800/jsonrpc")
        self.assertEqual(payload["method"], "aria2.addUri")
        self.assertEqual(payload["params"][0], "token:secret")
        self.assertEqual(payload["params"][1], ["https://cdn.example.com/movie.mkv"])
        self.assertEqual(
            payload["params"][2], {"dir": "/downloads/bot", "out": "movie.mkv"}
        )

    async def test_add_to_aria2_omits_token_when_not_configured(self):
        requests, _clients = self.install_post_client({"result": "gid-2"})
        uri = "https://cdn.example.com/movie.mkv"
        config = dict(self.user_config)
        config["aria2_rpc_token"] = ""

        self.assertEqual(await self.instance._add_to_aria2(uri, config), "gid-2")
        self.assertEqual(requests[0][1]["params"][0], [uri])

    async def test_add_to_aria2_reports_rpc_errors(self):
        self.install_post_client({"error": {"message": "Parameter error"}})

        with self.assertRaises(plugin_module.UserFacingError) as ctx:
            await self.instance._add_to_aria2(
                "https://cdn.example.com/a.mkv", self.user_config
            )

        self.assertIn("aria2 返回错误", str(ctx.exception))

    async def test_fetch_aria2_task_map_sends_gid_as_string(self):
        requests, _clients = self.install_post_client(
            {
                "result": {
                    "status": "active",
                    "totalLength": "100",
                    "completedLength": "50",
                }
            }
        )
        watch = plugin_module.DownloadWatch(
            session="QQ|GroupMessage|g1|10001",
            user_id="10001",
            sender_id="10001",
            group=True,
            title="movie.mkv",
            task_ref="gid001",
            kind="aria2",
            endpoint="http://127.0.0.1:6800/jsonrpc",
            credential="secret",
        )

        entries = await self.instance._fetch_aria2_task_map([watch])

        payload = requests[0][1]
        self.assertEqual(payload["method"], "aria2.tellStatus")
        # aria2 要求 gid 是字符串参数，包成 [gid] 会被判成参数类型错误
        self.assertEqual(payload["params"][0], "token:secret")
        self.assertEqual(payload["params"][1], "gid001")
        self.assertIn("status", payload["params"][2])
        self.assertEqual(entries["gid001"]["progress"], 0.5)

    async def test_add_to_ytdlp_returns_task_id(self):
        requests, clients = self.install_post_client(
            {"status": "waiting", "task_id": "T123"}
        )

        task_id = await self.instance._add_to_ytdlp(
            "https://www.youtube.com/watch?v=abc", self.user_config
        )

        self.assertEqual(task_id, "T123")
        url, payload, _kwargs = requests[0]
        self.assertEqual(url, "http://127.0.0.1:5000/get_video")
        self.assertEqual(payload["url"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(clients[0]["headers"]["X-API-Key"], "key123")

    async def test_add_to_ytdlp_reports_rejected_links(self):
        self.install_post_client({"error": "Unsupported URL"})

        with self.assertRaises(plugin_module.UserFacingError) as ctx:
            await self.instance._add_to_ytdlp(
                "https://example.com/watch", self.user_config
            )

        self.assertIn("Unsupported URL", str(ctx.exception))

    def test_aria2_entries_are_normalised(self):
        entry = self.instance._aria2_entry(
            {
                "status": "active",
                "totalLength": "1000",
                "completedLength": "250",
                "downloadSpeed": "125",
                "files": [{"path": "/downloads/bot/movie.mkv"}],
            }
        )

        self.assertEqual(entry["name"], "movie.mkv")
        self.assertEqual(entry["state"], "active")
        self.assertAlmostEqual(entry["progress"], 0.25)
        self.assertEqual(entry["size"], 1000)
        self.assertEqual(entry["dlspeed"], 125)

        done = self.instance._aria2_entry({"status": "complete", "totalLength": "10"})
        self.assertEqual(done["progress"], 1.0)

    def test_ytdlp_entries_are_normalised(self):
        watch = plugin_module.DownloadWatch(
            session="QQ|GroupMessage|g1|10001",
            user_id="10001",
            sender_id="10001",
            group=True,
            title="视频｜youtube.com/watch",
            task_ref="T123",
            kind="ytdlp",
            endpoint="http://127.0.0.1:5000",
        )

        entry = self.instance._ytdlp_entry(
            {"status": "completed", "file": "/files/T123/video.mp4"}, watch
        )

        self.assertEqual(entry["name"], "video.mp4")
        self.assertEqual(entry["state"], "completed")
        self.assertEqual(entry["progress"], 1.0)

        failed = self.instance._ytdlp_entry(
            {"status": "error", "error": "Video unavailable"}, watch
        )
        self.assertIsNone(failed["progress"])
        self.assertEqual(failed["error"], "Video unavailable")
        self.assertEqual(failed["name"], watch.title)

    def test_completion_and_failure_are_per_backend(self):
        self.assertTrue(self.instance._entry_completed("aria2", {"status": "complete"}))
        self.assertFalse(self.instance._entry_completed("aria2", {"status": "active"}))
        self.assertTrue(
            self.instance._entry_completed("ytdlp", {"status": "completed"})
        )
        self.assertFalse(self.instance._entry_completed("ytdlp", {"status": "waiting"}))
        self.assertEqual(
            self.instance._entry_failure("ytdlp", {"status": "error", "error": "403"}),
            "403",
        )
        self.assertEqual(
            self.instance._entry_failure("qbittorrent", {"state": "missingFiles"}),
            "文件缺失",
        )
        self.assertEqual(
            self.instance._entry_failure("qbittorrent", {"state": "active"}), ""
        )

    def test_progress_text_differs_per_backend(self):
        watch = plugin_module.DownloadWatch(
            session="QQ|GroupMessage|g1|10001",
            user_id="10001",
            sender_id="10001",
            group=True,
            title="movie.mkv",
            task_ref="gid001",
            kind="aria2",
            endpoint="http://127.0.0.1:6800/jsonrpc",
            credential="secret",
        )
        aria2_text = self.instance._format_progress_text(
            watch,
            {
                "name": "movie.mkv",
                "state": "active",
                "status": "active",
                "progress": 0.5,
                "size": 1024,
                "received": 512,
                "dlspeed": 512,
                "upspeed": 0,
                "eta": 1,
            },
        )
        self.assertIn("状态：下载中", aria2_text)
        self.assertIn("50.0%", aria2_text)

        ytdlp_watch = plugin_module.DownloadWatch(
            session="QQ|GroupMessage|g1|10001",
            user_id="10001",
            sender_id="10001",
            group=True,
            title="视频｜youtube.com/watch",
            task_ref="T123",
            kind="ytdlp",
            endpoint="http://127.0.0.1:5000",
            credential="key123",
        )
        ytdlp_text = self.instance._format_progress_text(
            ytdlp_watch,
            {"name": "video.mp4", "state": "processing", "status": "processing"},
        )
        self.assertIn("状态：解析下载中", ytdlp_text)
        self.assertNotIn("进度：", ytdlp_text)

    async def test_monitor_once_reports_backend_completion(self):
        watches = {}
        for kind, task_ref, entry in (
            ("aria2", "gid001", {"status": "complete", "progress": 1.0}),
            ("ytdlp", "T123", {"status": "completed", "progress": 1.0}),
        ):
            watch = plugin_module.DownloadWatch(
                session="QQ|GroupMessage|g1|10001",
                user_id="10001",
                sender_id="10001",
                group=True,
                title=f"任务 {kind}",
                task_ref=task_ref,
                kind=kind,
                endpoint=f"http://127.0.0.1/{kind}",
            )
            watches[watch.watch_key] = watch
            self.instance._download_watches[watch.watch_key] = watch

        async def fake_fetch(_instance, group):
            first = group[0]
            if first.kind == "aria2":
                return {"gid001": {"status": "complete", "progress": 1.0}}
            return {"T123": {"status": "completed", "progress": 1.0}}

        original_fetch = plugin_module.QBittorrentManagerPlugin._fetch_task_map
        plugin_module.QBittorrentManagerPlugin._fetch_task_map = fake_fetch
        self.addCleanup(
            setattr,
            plugin_module.QBittorrentManagerPlugin,
            "_fetch_task_map",
            original_fetch,
        )

        await self.instance._monitor_once()

        sent = [chain_text(chain) for _, chain in self.instance.context.sent]
        self.assertEqual(len(sent), 2)
        for text in sent:
            self.assertTrue(text.endswith(DownloadMonitorTests.COMPLETE_MESSAGE), text)
        self.assertEqual(self.instance._download_watches, {})

    async def test_download_command_routes_to_every_backend(self):
        calls = []

        async def fake_aria2(uri, _user_config):
            calls.append(("aria2", uri))
            return "gid001"

        async def fake_ytdlp(uri, _user_config):
            calls.append(("ytdlp", uri))
            return "T123"

        async def fake_qb(uri, user_config, monitor=None, title=""):
            calls.append(("qb", uri))
            self.instance._register_download_watch(
                monitor, user_config, title, "ab12" * 10
            )
            return "ab12" * 10

        self.instance._user_config = lambda _event: self.user_config
        self.instance._add_to_aria2 = fake_aria2
        self.instance._add_to_ytdlp = fake_ytdlp
        self.instance._add_uri_to_download_client = fake_qb

        replies = []
        for uri in (
            self.MAGNET,
            "https://cdn.example.com/movie.mkv",
            "https://www.youtube.com/watch?v=abc",
        ):
            generator = self.instance.smart_download(self.event, uri).__aiter__()
            while True:
                try:
                    result = await anext(generator)
                except StopAsyncIteration:
                    break
                replies.append(result)

        self.assertEqual(
            [kind for kind, _ in calls],
            ["qb", "aria2", "ytdlp"],
        )
        self.assertIn("已提交给 qBittorrent（识别为磁链或种子）", str(replies[0]))
        self.assertIn("已提交给 aria2（识别为普通直链）", str(replies[1]))
        self.assertIn("已提交给 yt-dlp（识别为视频网页）", str(replies[2]))
        kinds = {watch.kind for watch in self.instance._download_watches.values()}
        self.assertEqual(kinds, {"qbittorrent", "aria2", "ytdlp"})

    async def test_download_command_explains_missing_argument(self):
        generator = self.instance.smart_download(self.event, "")
        result = await anext(generator)

        self.assertIn("请发送 /下载 链接", str(result))

    def test_split_download_backend_recognises_aliases(self):
        cases = {
            "aria2 https://example.com/a.mkv": ("aria2", "https://example.com/a.mkv"),
            "qb https://example.com/a.mkv": (
                "qbittorrent",
                "https://example.com/a.mkv",
            ),
            "qbt https://example.com/a.mkv": (
                "qbittorrent",
                "https://example.com/a.mkv",
            ),
            "ytdlp https://youtu.be/abc": ("ytdlp", "https://youtu.be/abc"),
            "yt-dlp https://youtu.be/abc": ("ytdlp", "https://youtu.be/abc"),
            f"<{self.MAGNET}> 备注": ("", self.MAGNET),
            "https://example.com/a.mkv": ("", "https://example.com/a.mkv"),
            "aria2": ("aria2", ""),
            "": ("", ""),
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.instance._split_download_backend(text), expected)

    def test_forced_backend_reports_missing_configuration(self):
        with self.assertRaises(plugin_module.UserFacingError) as ctx:
            self.instance._forced_download_kind(
                "aria2", dict(self.user_config, aria2_rpc_url="")
            )
        self.assertIn("aria2_rpc_url", str(ctx.exception))

        with self.assertRaises(plugin_module.UserFacingError) as ctx:
            self.instance._forced_download_kind(
                "ytdlp", dict(self.user_config, ytdlp_api_url="")
            )
        self.assertIn("ytdlp_api_url", str(ctx.exception))

        self.assertEqual(
            self.instance._forced_download_kind("qb", self.user_config),
            ("qbittorrent", "手动指定"),
        )

    async def test_download_command_forces_the_requested_backend(self):
        calls = []

        async def fake_aria2(uri, _user_config):
            calls.append(("aria2", uri))
            return "gid001"

        async def fake_ytdlp(uri, _user_config):
            calls.append(("ytdlp", uri))
            return "T123"

        async def fake_qb(uri, user_config, monitor=None, title=""):
            calls.append(("qb", uri))
            self.instance._register_download_watch(
                monitor, user_config, title, "ab12" * 10
            )
            return "ab12" * 10

        self.instance._user_config = lambda _event: self.user_config
        self.instance._add_to_aria2 = fake_aria2
        self.instance._add_to_ytdlp = fake_ytdlp
        self.instance._add_uri_to_download_client = fake_qb

        replies = []
        for text in (
            "aria2 https://www.youtube.com/watch?v=abc",
            "qb https://cdn.example.com/movie.mkv",
            "ytdlp https://cdn.example.com/movie.mkv",
        ):
            result = await anext(self.instance.smart_download(self.event, text))
            replies.append(result)

        self.assertEqual([kind for kind, _ in calls], ["aria2", "qb", "ytdlp"])
        self.assertIn("已提交给 aria2（识别为手动指定）", str(replies[0]))
        self.assertIn("已提交给 qBittorrent（识别为手动指定）", str(replies[1]))
        self.assertIn("已提交给 yt-dlp（识别为手动指定）", str(replies[2]))
        kinds = {watch.kind for watch in self.instance._download_watches.values()}
        self.assertEqual(kinds, {"qbittorrent", "aria2", "ytdlp"})

    async def test_download_command_asks_for_link_after_backend_alias(self):
        result = await anext(self.instance.smart_download(self.event, "aria2"))

        self.assertIn("请在 /下载 aria2 后面加上链接", str(result))

    def test_monitor_kind_follows_the_configured_client_for_qb(self):
        self.assertEqual(
            self.instance._monitor_kind("qbittorrent", {"download_client": "utorrent"}),
            "utorrent",
        )
        self.assertEqual(
            self.instance._monitor_kind(None, {"download_client": "qbittorrent"}),
            "qbittorrent",
        )
        self.assertEqual(self.instance._monitor_kind("aria2", {}), "aria2")

    def test_qb_alias_with_utorrent_says_monitoring_is_unsupported(self):
        note = self.instance._monitor_note(
            {"download_client": "utorrent", "monitor_enabled": True},
            None,
            1,
            "qbittorrent",
        )

        self.assertIn("uTorrent 暂不支持下载状态监控", note)


if __name__ == "__main__":
    unittest.main()
