"""离线自测：不启动 MaiBot，直接用已安装的 maibot_sdk 加载插件并模拟调用。

运行方式（MaiBot 运行环境的 venv）::

    E:/maibot/MaiBot/.venv/Scripts/python.exe tests/test_offline.py

覆盖点：命令普通/回复场景、before_send 引用注入、工具调用、
关键词回复的启停/命中/防重、语音模式（直接发出/不引用/回退文本）、
组件清单与命名冲突检查。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import plugin as plugin_module  # noqa: E402
from maibot_sdk.components import collect_components  # noqa: E402

CIALLO = plugin_module.CIALLO_TEXT
PASSED = 0


def ok(name: str) -> None:
    global PASSED
    PASSED += 1
    print(f"PASS  {name}")


class FakeLogger:
    def _log(self, level: str, *args: Any, **kwargs: Any) -> None:
        if args and isinstance(args[0], str) and args[0].find("%") != -1:
            print(f"  [{level}]", args[0] % args[1:] if len(args) > 1 else args[0])
        else:
            print(f"  [{level}]", *args)

    def info(self, *args: Any, **kwargs: Any) -> None:
        self._log("info", *args)

    def warning(self, *args: Any, **kwargs: Any) -> None:
        self._log("warn", *args)

    def exception(self, *args: Any, **kwargs: Any) -> None:
        self._log("exc", *args)

    def debug(self, *args: Any, **kwargs: Any) -> None:
        pass


class FakeSend:
    """模拟 ctx.send：记录调用；内部真实挂起以模拟 RPC 交错窗口。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def _record(self, entry: dict[str, Any]) -> bool:
        await asyncio.sleep(0)  # 模拟 RPC 让出控制权
        self.calls.append(entry)
        return True

    async def text(self, text: str, stream_id: str, **kwargs: Any) -> bool:
        return await self._record({"kind": "text", "text": text, "stream_id": stream_id, **kwargs})

    async def hybrid(self, segments: list[dict[str, Any]], stream_id: str, **kwargs: Any) -> bool:
        return await self._record(
            {"kind": "hybrid", "segments": segments, "stream_id": stream_id, **kwargs}
        )


class FakeChat:
    """模拟 ctx.chat：get_all_streams 返回可配置的会话列表。"""

    def __init__(self) -> None:
        self.streams: list[dict[str, Any]] = []

    async def get_all_streams(self) -> dict[str, Any]:
        return {"success": True, "streams": self.streams}


class FakeGateway:
    """模拟 ctx.gateway：记录 update_state / route_message 调用。"""

    def __init__(self) -> None:
        self.state_updates: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []
        self.accept_route: bool = True

    async def update_state(self, gateway_name: str, *, ready: bool, **kwargs: Any) -> bool:
        self.state_updates.append({"gateway_name": gateway_name, "ready": ready, **kwargs})
        return True

    async def route_message(
        self,
        gateway_name: str,
        message: dict[str, Any],
        *,
        route_metadata: dict[str, Any] | None = None,
        external_message_id: str = "",
        dedupe_key: str = "",
    ) -> bool:
        self.route_calls.append(
            {
                "gateway_name": gateway_name,
                "message": message,
                "route_metadata": route_metadata,
                "external_message_id": external_message_id,
                "dedupe_key": dedupe_key,
            }
        )
        return self.accept_route


class FakeApi:
    """模拟 ctx.api：call 返回可配置结果。"""

    def __init__(self) -> None:
        self.login_result: dict[str, Any] | None = {"data": {"nickname": "麦麦", "user_id": 10001}}
        self.calls: list[str] = []

    async def call(self, api_name: str, **kwargs: Any) -> Any:
        self.calls.append(api_name)
        if api_name == "adapter.napcat.system.get_login_info":
            return self.login_result
        return None


class FakeCtx:
    def __init__(self, data_dir: Path) -> None:
        self.send = FakeSend()
        self.chat = FakeChat()
        self.gateway = FakeGateway()
        self.api = FakeApi()
        self.logger = FakeLogger()
        self.paths = SimpleNamespace(data_dir=data_dir)


def build_plugin(data_dir: Path) -> plugin_module.CialloPlugin:
    p = plugin_module.create_plugin()
    p._set_context(FakeCtx(data_dir))
    p.set_plugin_config({})
    return p


def enable_voice(p: plugin_module.CialloPlugin, file_name: str = "ciallo.wav", probability: float = 1.0) -> None:
    p.set_plugin_config(
        {
            "plugin": {"enabled": True, "config_version": plugin_module.SUPPORTED_CONFIG_VERSION},
            "keyword_reply": {"enabled": False},
            "voice": {"enabled": True, "file_name": file_name, "probability": probability},
        }
    )


def base_config() -> dict[str, Any]:
    return {
        "plugin": {"enabled": True, "config_version": plugin_module.SUPPORTED_CONFIG_VERSION},
        "keyword_reply": {"enabled": True, "keywords": ["Ciallo"], "cooldown_seconds": 0},
    }


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        p = build_plugin(data_dir)
        await p.on_load()

        # 1) 组件清单与命名唯一性
        comps = collect_components(p)
        comp_pairs = sorted((c["type"].lower(), c["name"]) for c in comps)
        assert len(comp_pairs) == len({name for _, name in comp_pairs}), f"组件名重复: {comp_pairs}"
        assert ("command", "ciallo") in comp_pairs, comp_pairs
        assert ("tool", "send_ciallo") in comp_pairs, comp_pairs
        hook_names = {name for ctype, name in comp_pairs if ctype == "hook_handler"}
        assert hook_names == {"ciallo_reply_injector", "ciallo_keyword_reply"}, comp_pairs
        gw_names = {name for ctype, name in comp_pairs if ctype == "message_gateway"}
        assert gw_names == {"ciallo_voice_recorder"}, comp_pairs
        ok("组件清单（1 工具 + 1 命令 + 2 钩子 + 1 语音补录网关，命名无冲突）")

        # 2) 命令普通发送：无 reply 段 → 普通文本，ctx.send 不携带引用参数
        ret = await p.cmd_ciallo(stream_id="s1", message={"raw_message": []})
        assert ret == (True, "ciallo", True), ret
        assert p.ctx.send.calls[-1] == {
            "kind": "text",
            "text": CIALLO,
            "stream_id": "s1",
        }, p.ctx.send.calls[-1]
        ok("命令普通发送（/ciallo）")

        # 3) 命令回复场景（QQ 引用指定消息）：raw_message 带 reply 段 → 挂起目标并在发送后清理
        await p.cmd_ciallo(
            stream_id="s1",
            message={
                "raw_message": [
                    {"type": "reply", "data": {"target_message_id": "-123"}},
                    {"type": "text", "data": "/ciallo"},
                ]
            },
        )
        call = p.ctx.send.calls[-1]
        assert call["stream_id"] == "s1" and call["text"] == CIALLO, call
        assert "set_reply" not in call and "reply_message_id" not in call, call
        assert not p._pending_replies, p._pending_replies
        ok("命令回复场景（reply 段解析 + 挂起清理）")

        # 4) before_send 钩子注入：模拟宿主在发送链上调用钩子
        async def send_via_host_chain(stream_id: str, reply_to: str) -> bool:
            task = asyncio.create_task(p._send_ciallo(stream_id, reply_to=reply_to))
            await asyncio.sleep(0)  # 此时 _send_ciallo 已挂起目标并进入 ctx.send
            pending = p._pending_replies.get(stream_id)
            assert pending is not None and pending[0] == reply_to, p._pending_replies
            hook_ret = await p.hook_inject_reply(
                message={
                    "session_id": stream_id,
                    "processed_plain_text": CIALLO,
                    "raw_message": [{"type": "text", "data": CIALLO}],
                },
                typing=False,
                set_reply=False,
                storage_message=True,
                show_log=True,
                reply_message_id=None,
            )
            assert hook_ret["action"] == "continue"
            modified = hook_ret["modified_kwargs"]
            assert modified["set_reply"] is True and modified["reply_message_id"] == reply_to, modified
            assert modified.get("message") and "typing" in modified, "modified_kwargs 必须完整替换"
            assert not p._pending_replies, "命中后挂起应被消费"
            return bool(await task)

        assert await send_via_host_chain("s2", "-456") is True
        ok("before_send 引用注入（set_reply + reply_message_id）")

        # 5) 钩子不误伤其他消息：非 Ciallo 文本即使有挂起也不注入
        ret = await p.hook_inject_reply(
            message={"session_id": "s3", "processed_plain_text": "别的内容"},
            typing=False,
            set_reply=False,
            storage_message=True,
            show_log=True,
        )
        assert ret["modified_kwargs"].get("set_reply", False) is False, ret
        ok("before_send 对非 Ciallo 出站消息不注入")

        # 6) 工具：不带 message_id → 普通发送；带 message_id → 挂起引用
        r = await p.tool_send_ciallo(stream_id="s4")
        assert r["success"] is True and p.ctx.send.calls[-1]["stream_id"] == "s4", r
        r = await p.tool_send_ciallo(message_id="789", stream_id="s4")
        assert r["success"] is True, r
        ok("工具调用（send_ciallo 带/不带 message_id）")

        # 7) 关键词回复默认关闭：命中也不发送
        calls_before = len(p.ctx.send.calls)
        await p.hook_keyword_reply(
            message={"session_id": "s5", "processed_plain_text": "来点 ciallo", "message_id": "m1"}
        )
        assert len(p.ctx.send.calls) == calls_before
        ok("关键词回复默认关闭")

        # 8) 启用关键词回复：命中并引用回复目标消息
        p.set_plugin_config(base_config())
        await p.hook_keyword_reply(
            message={
                "session_id": "s5",
                "processed_plain_text": "来点 Ciallo 吧",
                "message_id": "m1",
                "is_command": False,
            }
        )
        assert len(p.ctx.send.calls) == calls_before + 1
        ok("关键词命中 → 自动引用回复")

        # 9) 命令消息 / 同款 Ciallo 文本 / 通知消息不触发关键词回复
        calls_before = len(p.ctx.send.calls)
        await p.hook_keyword_reply(
            message={"session_id": "s5", "processed_plain_text": "/ciallo", "message_id": "m2", "is_command": True}
        )
        await p.hook_keyword_reply(
            message={"session_id": "s5", "processed_plain_text": CIALLO, "message_id": "m3"}
        )
        await p.hook_keyword_reply(
            message={"session_id": "s5", "processed_plain_text": "Ciallo", "message_id": "m4", "is_notify": True}
        )
        await p.hook_keyword_reply(message={"session_id": "s5", "processed_plain_text": "无关消息", "message_id": "m5"})
        assert len(p.ctx.send.calls) == calls_before, p.ctx.send.calls
        ok("命令/同款文本/通知/无关消息均不触发")

        # 10) 冷却：同会话连续命中在冷却窗口内只回复一次
        p.set_plugin_config(
            {
                "plugin": {"enabled": True, "config_version": plugin_module.SUPPORTED_CONFIG_VERSION},
                "keyword_reply": {"enabled": True, "keywords": ["Ciallo"], "cooldown_seconds": 60},
            }
        )
        p._keyword_reply_last_at.clear()
        await p.hook_keyword_reply(
            message={"session_id": "s6", "processed_plain_text": "ciallo~", "message_id": "m6"}
        )
        calls_after_first = len(p.ctx.send.calls)
        await p.hook_keyword_reply(
            message={"session_id": "s6", "processed_plain_text": "again ciallo", "message_id": "m7"}
        )
        assert len(p.ctx.send.calls) == calls_after_first, "冷却窗口内不应重复回复"
        ok("同会话冷却防刷屏")

        # 11) 语音模式：内置 assets 默认语音兜底；用户目录与内置均缺失才回退文本
        enable_voice(p)
        # 11a) 默认文件名、数据目录为空 → 使用插件内置 assets/ciallo.wav 直接发出
        bundled_wav = PLUGIN_DIR / "assets" / "ciallo.wav"
        assert bundled_wav.exists(), "插件必须内置默认语音 assets/ciallo.wav"
        await p._send_ciallo("s7", reply_to="-999")
        last = p.ctx.send.calls[-1]
        assert last["kind"] == "hybrid" and last["stream_id"] == "s7", last
        expected_b64 = base64.b64encode(bundled_wav.read_bytes()).decode("ascii")
        assert last["segments"] == [{"type": "voice", "content": expected_b64}], "应使用内置默认语音"
        assert not p._pending_replies, p._pending_replies
        ok("语音模式 → 无用户文件时使用内置 assets 默认语音（安装即用）")

        # 11b) 自定义文件名在数据目录与内置 assets 均不存在 → 回退文本，且不挂起引用
        enable_voice(p, file_name="definitely_missing.wav")
        await p._send_ciallo("s7", reply_to="-999")
        last = p.ctx.send.calls[-1]
        assert last["kind"] == "text" and last["text"] == CIALLO and last["stream_id"] == "s7", last
        assert not p._pending_replies, p._pending_replies
        ok("语音模式 + 文件缺失（用户目录与内置均无）→ 回退文本且不引用")

        # 12) 语音模式：数据目录用户文件存在 → 优先于内置 assets，经 send.hybrid 发 voice 段
        enable_voice(p)  # 恢复默认文件名 ciallo.wav
        (data_dir / "ciallo.wav").write_bytes(b"RIFF0000fake-audio")
        p._voice_cache = None  # 模拟重载
        ok_calls = await p._send_ciallo("s8", reply_to="-888")
        assert ok_calls is True
        last = p.ctx.send.calls[-1]
        assert last["kind"] == "hybrid" and last["stream_id"] == "s8", last
        segs = last["segments"]
        assert len(segs) == 1 and segs[0]["type"] == "voice", segs
        assert segs[0]["content"] == "UklGRjAwMDBmYWtlLWF1ZGlv", segs  # base64(RIFF0000fake-audio)，数据目录文件优先于内置 assets
        assert last["processed_plain_text"] == CIALLO, last
        assert not p._pending_replies, "语音模式不应挂起引用目标"
        assert not any(c["kind"] == "text" for c in p.ctx.send.calls[-1:]), "不应回退文本"
        ok("语音模式 → 数据目录用户文件存在时优先于内置 assets")

        # 13) 语音模式：命令/工具/关键词全链路都走语音（probability=1 时全语音）
        calls_before = len(p.ctx.send.calls)
        await p.cmd_ciallo(
            stream_id="s9",
            message={"raw_message": [{"type": "reply", "data": {"target_message_id": "-1"}}]},
        )
        assert p.ctx.send.calls[-1]["kind"] == "hybrid", p.ctx.send.calls[-1]
        r = await p.tool_send_ciallo(message_id="-2", stream_id="s9")
        assert r["success"] is True and p.ctx.send.calls[-1]["kind"] == "hybrid", r
        p.set_plugin_config({**base_config(), "voice": {"enabled": True, "file_name": "ciallo.wav", "probability": 1.0}})
        p._keyword_reply_last_at.clear()
        await p.hook_keyword_reply(
            message={"session_id": "s9", "processed_plain_text": "ciallo!", "message_id": "m9"}
        )
        assert p.ctx.send.calls[-1]["kind"] == "hybrid", p.ctx.send.calls[-1]
        assert len(p.ctx.send.calls) == calls_before + 3
        ok("语音概率=1 → 命令/工具/关键词全链路全走语音")

        # 13b) 语音概率=0：enabled 开启但概率 0 → 永不语音，命令/工具/关键词全部按文本发送
        calls_before = len(p.ctx.send.calls)
        enable_voice(p, probability=0.0)
        await p.cmd_ciallo(stream_id="s10", message={"raw_message": []})
        r = await p.tool_send_ciallo(message_id="-3", stream_id="s10")
        assert r["success"] is True
        p.set_plugin_config({**base_config(), "voice": {"enabled": True, "file_name": "ciallo.wav", "probability": 0.0}})
        p._keyword_reply_last_at.clear()
        await p.hook_keyword_reply(
            message={"session_id": "s10", "processed_plain_text": "ciallo!", "message_id": "m10"}
        )
        new_calls = p.ctx.send.calls[calls_before:]
        assert len(new_calls) == 3 and all(c["kind"] == "text" for c in new_calls), new_calls
        ok("语音概率=0 → 命令/工具/关键词全部文本发送（不语音）")

        # 13c) 语音概率 0.5：多次发送中出现语音也出现文本（随机命中）
        enable_voice(p, probability=0.5)
        calls_before = len(p.ctx.send.calls)
        for i in range(40):
            await p._send_ciallo("s11")
        kinds = {c["kind"] for c in p.ctx.send.calls[calls_before:]}
        assert kinds == {"hybrid", "text"}, f"0<p<1 应混合出现语音与文本: {kinds}"
        ok("语音概率 0.5 → 混合出现语音与文本")

        # 13d) 语音概率越界值：SDK 配置校验拒绝（ge=0 / le=1），合法范围内外均不抛错
        import pydantic

        for bad in (1.5, -0.5):
            try:
                enable_voice(p, probability=bad)
                raise AssertionError(f"概率 {bad} 应被配置校验拒绝")
            except pydantic.ValidationError:
                pass
        ok("语音概率越界值：SDK 配置校验拒绝（仅接受 0~1）")

        # 17) 语音补录：群聊语音发送成功 → route_message 注入 bot 的 Ciallo 文本记录
        p.ctx.chat.streams = [
            {
                "session_id": "s12",
                "stream_id": "s12",
                "platform": "qq",
                "user_id": "222",
                "user_nickname": "群友",
                "group_id": "123456",
                "group_name": "测试群",
                "account_id": "10001",
                "is_group_session": True,
            }
        ]
        p.ctx.gateway.route_calls.clear()
        p.ctx.gateway.state_updates.clear()
        p.ctx.api.login_result = {"data": {"nickname": "麦麦", "user_id": 10001}}
        p._bot_nickname_cache = None
        enable_voice(p)  # probability=1 全语音，默认文件名 → 命中数据目录 fake-audio
        (data_dir / "ciallo.wav").write_bytes(b"RIFF0000fake-audio")
        p._voice_cache = None
        ok_calls = await p._send_ciallo("s12")
        assert ok_calls is True
        # 应有一次对 ciallo_voice_recorder 的 route_message 注入
        assert len(p.ctx.gateway.route_calls) == 1, p.ctx.gateway.route_calls
        rc = p.ctx.gateway.route_calls[0]
        assert rc["gateway_name"] == "ciallo_voice_recorder", rc
        msg = rc["message"]
        assert msg["is_notify"] is True, msg
        assert msg["processed_plain_text"] == CIALLO, msg
        mi = msg["message_info"]
        # 发送者是 bot 自己（account_id/self_id + 昵称）
        assert mi["user_info"]["user_id"] == "10001", mi
        assert mi["user_info"]["user_nickname"] == "麦麦", mi
        # 群聊带 group_info，定位到原会话
        assert mi["group_info"]["group_id"] == "123456", mi
        assert mi["additional_config"].get("self_id") == "10001", mi
        assert mi["additional_config"].get("plugin_injected_notice") == "ciallo_voice_record", mi
        assert rc["route_metadata"] == {"self_id": "10001", "platform": "qq"}, rc
        ok("语音补录 → 群聊语音发送后注入 bot 的 Ciallo 文本记录（is_notify）")

        # 17b) 语音补录在宿主拒绝时不影响语音发送结果（已发出）
        p.ctx.gateway.route_calls.clear()
        p.ctx.gateway.accept_route = False
        ok_calls = await p._send_ciallo("s12")
        assert ok_calls is True, "宿主拒绝补录不应影响语音发送本身"
        assert len(p.ctx.gateway.route_calls) == 1
        p.ctx.gateway.accept_route = True
        ok("语音补录 → 宿主拒绝时静默降级，不影响语音已发出")

        # 17c) 未找到流信息 → 补录降级 warning，不影响语音发送
        p.ctx.gateway.route_calls.clear()
        p.ctx.chat.streams = []
        ok_calls = await p._send_ciallo("s12")
        assert ok_calls is True
        assert len(p.ctx.gateway.route_calls) == 0, "无流信息时不应注入"
        ok("语音补录 → 找不到会话时降级，不注入且不影响语音")

        # 17d) on_load 会向宿主上报补录网关 ready（on_load 幂等，可重复调用验证）
        p.ctx.gateway.state_updates.clear()
        await p.on_load()
        assert any(
            u["gateway_name"] == "ciallo_voice_recorder" and u["ready"] is True
            for u in p.ctx.gateway.state_updates
        ), p.ctx.gateway.state_updates
        ok("语音补录网关 on_load 已上报 ready")

        # 17e) 私聊语音补录：无 group_info，additional_config 携带 platform_io_target_user_id
        p.ctx.gateway.route_calls.clear()
        p.ctx.chat.streams = [
            {
                "session_id": "s13",
                "stream_id": "s13",
                "platform": "qq",
                "user_id": "888",
                "user_nickname": "好友",
                "group_id": "",
                "group_name": "",
                "account_id": "10001",
                "is_group_session": False,
            }
        ]
        p._bot_nickname_cache = None
        await p._send_ciallo("s13")
        assert len(p.ctx.gateway.route_calls) == 1, p.ctx.gateway.route_calls
        msg = p.ctx.gateway.route_calls[0]["message"]
        mi = msg["message_info"]
        assert "group_info" not in mi, mi
        assert mi["additional_config"].get("platform_io_target_user_id") == "888", mi
        assert mi["user_info"]["user_id"] == "10001", mi
        ok("语音补录 → 私聊语音发送后注入（带 platform_io_target_user_id）")

        # 14) 语音出站消息：hook 对语音段消息跳过注入引用（即使存在残留挂起）
        p._pending_replies["s9"] = ("-777", __import__("time").monotonic())
        ret = await p.hook_inject_reply(
            message={
                "session_id": "s9",
                "processed_plain_text": CIALLO,
                "raw_message": [{"type": "voice", "data": "", "binary_data_base64": "xx"}],
            },
            typing=False,
            set_reply=False,
            storage_message=True,
            show_log=True,
        )
        assert ret["modified_kwargs"].get("set_reply", False) is False, ret
        assert ret["modified_kwargs"].get("reply_message_id") in (None, ""), ret
        assert not p._pending_replies, "语音出站消息应消费掉残留挂起"
        ok("语音出站消息 → before_send 跳过引用注入（清理残留挂起）")

        # 14b) 概率模式：语音 enabled 但未被选中时文本 Ciallo 仍正常注入引用
        enable_voice(p, probability=0.5)
        p._voice_cache = None
        p._pending_replies["s9"] = ("-555", __import__("time").monotonic())
        ret = await p.hook_inject_reply(
            message={
                "session_id": "s9",
                "processed_plain_text": CIALLO,
                "raw_message": [{"type": "text", "data": CIALLO}],
            },
            typing=False,
            set_reply=False,
            storage_message=True,
            show_log=True,
        )
        assert ret["modified_kwargs"].get("set_reply") is True, ret
        assert ret["modified_kwargs"].get("reply_message_id") == "-555", ret
        p._pending_replies.clear()
        ok("语音概率模式 → 未被选中的文本 Ciallo 仍正常注入引用")

        # 15) 语音模式：非法文件名（路径穿越）→ 回退文本
        enable_voice(p, file_name="../evil.wav", probability=1.0)
        await p._send_ciallo("s10")
        last = p.ctx.send.calls[-1]
        assert last["kind"] == "text", last
        ok("非法语音文件名 → 回退文本（防路径穿越）")

        # 16) 默认配置生成：config_version 存在、关键词与语音均默认关闭、probability 默认 1.0
        default_cfg = p.get_default_config()
        assert default_cfg["plugin"]["config_version"] == plugin_module.SUPPORTED_CONFIG_VERSION
        assert default_cfg["keyword_reply"]["enabled"] is False
        assert default_cfg["voice"]["enabled"] is False
        assert default_cfg["voice"]["file_name"] == "ciallo.wav"
        assert default_cfg["voice"]["probability"] == 1.0
        ok("默认配置：关键词/语音均默认关闭，语音概率默认 1.0")

        await p.on_unload()
        assert not p._pending_replies and not p._keyword_reply_last_at and p._voice_cache is None
        ok("on_unload 清理挂起与语音缓存")

    print(f"\n全部 {PASSED} 项通过 ✔  Ciallo～(∠・ω< )⌒★")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
