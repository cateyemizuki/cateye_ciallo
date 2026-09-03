"""Ciallo～(∠・ω< )⌒★ —— 让麦麦可爱地打个招呼。

三种打招呼方式：

1. LLM 工具 ``send_ciallo``：Planner 在用户明确要求打招呼时调用，可传入
   ``message_id`` 对指定消息引用回复；
2. 命令 ``/ciallo``：任何人可用；引用某条消息发送命令时，改为对那条消息
   发送 Ciallo；
3. 关键词自动回复（默认关闭）：消息命中关键词时，自动对那条消息引用回复
   一条 Ciallo。

「引用回复」均指 QQ 的引用指定消息来回复：命令路径取命令消息 reply 段的
``target_message_id``，工具路径取 LLM 传入的 ``message_id``，最终由宿主
构建 ReplyComponent、适配器编码为平台引用段。

语音输出（默认关闭）：``[voice].enabled`` 开启后，每条 Ciallo 独立以
``[voice].probability``（0~1，默认 1.0 = 全部语音）的概率**替换为语音发送**
（经 ``send.hybrid`` 的 voice 段走官方发送管线，适配器编码为 record 段）；
被替换时直接发出、不引用回复任何消息，未替换时按正常文本逻辑发送（含引用
回复）。插件**自带默认语音** ``assets/ciallo.wav``（安装即用，无需手动放置）。
文件名可在配置中修改（仅允许纯文件名）；如需自定义，把同名文件放入数据目录
``data/plugins/github.cateye.ciallo/`` 即可覆盖（数据目录优先于内置 assets）。

实现说明：``ctx.send.text`` 无法直接指定被引用消息（宿主 send 链路的
``reply_message_id`` 不对外暴露），因此文本引用回复采用「挂起目标 + 出站
钩子注入」的方式：发送前把目标消息 ID 挂起到 ``_pending_replies``，由
``send_service.before_send`` 钩子对本插件发出的 Ciallo 消息注入
``set_reply=True`` 与 ``reply_message_id``（宿主 ``_send_via_platform_io``
会读取这两个键并构建 ReplyComponent）。
"""

from __future__ import annotations

import base64
import random
import time
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from maibot_sdk.types import (
    CONFIG_RELOAD_SCOPE_SELF,
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

SUPPORTED_CONFIG_VERSION = "1.0.0"  # 与 _manifest.json 的 version 保持同步

CIALLO_TEXT = "Ciallo～(∠・ω< )⌒★"

# 挂起的引用回复目标最长存活时间（秒），超时未消费即丢弃，避免错误注入到后续消息
_PENDING_REPLY_TTL_SEC = 30.0


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "waving_hand"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class KeywordReplySectionConfig(PluginConfigBase):
    """关键词自动回复配置。"""

    __ui_label__ = "关键词回复"
    __ui_icon__ = "auto_awesome"
    __ui_order__ = 1

    enabled: bool = Field(
        default=False,
        description="是否启用关键词匹配自动回复",
    )
    keywords: list[str] = Field(
        default_factory=lambda: ["Ciallo"],
        description="触发关键词列表：消息文本包含任一关键词（不区分大小写）即自动回复一条 Ciallo",
    )
    cooldown_seconds: float = Field(
        default=30.0,
        ge=0,
        description="同一会话两次关键词回复的最小间隔（秒），防止刷屏",
    )


class VoiceSectionConfig(PluginConfigBase):
    """语音输出配置。"""

    __ui_label__ = "语音输出"
    __ui_icon__ = "graphic_eq"
    __ui_order__ = 2

    enabled: bool = Field(
        default=False,
        description="开启后每条 Ciallo 按 probability 概率以语音直接发出（替换文本），未替换时仍走文本逻辑",
    )
    probability: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="替换为语音发送的概率（0~1）：1.0 = 全部语音，0 = 全部文本，0.5 = 约一半语音",
    )
    file_name: str = Field(
        default="ciallo.wav",
        description="语音文件名（仅允许纯文件名，不支持子目录）；插件自带同名默认语音（assets/），如需自定义可把同名文件放入数据目录 data/plugins/github.cateye.ciallo/ 覆盖",
    )


class CialloPluginConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    keyword_reply: KeywordReplySectionConfig = Field(default_factory=KeywordReplySectionConfig)
    voice: VoiceSectionConfig = Field(default_factory=VoiceSectionConfig)


class CialloPlugin(MaiBotPlugin):
    """Ciallo 打招呼插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = CialloPluginConfig

    def __init__(self) -> None:
        super().__init__()
        # stream_id -> (被引用消息ID, 挂起时刻 monotonic)
        self._pending_replies: dict[str, tuple[str, float]] = {}
        # stream_id -> 上次关键词回复时刻（monotonic）
        self._keyword_reply_last_at: dict[str, float] = {}
        # 语音文件缓存：(路径, mtime, base64)；文件变更自动重载
        self._voice_cache: tuple[Path, float, str] | None = None
        self._voice_missing_logged: bool = False
        # 语音补录用机器人昵称缓存：(nickname, expires 时刻)
        self._bot_nickname_cache: tuple[str, float] | None = None

    # ------------------------------------------------------------------
    # 组件：语音补录网关（MessageGateway receive，route_message 注入合成消息入库）
    # ------------------------------------------------------------------

    @MessageGateway(
        "receive",
        name="ciallo_voice_recorder",
        description="语音 Ciallo 发送成功后注入一条 bot 的文本记录（走完整入站链入库，WebUI 可见、不真发）",
    )
    async def gateway_voice_recorder(self, **kwargs: Any) -> Any:
        """接收网关载体：不处理外部消息，route_message 由语音补录方法调用。"""
        del kwargs
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        keyword_cfg = self.config.keyword_reply
        voice_cfg = self.config.voice
        try:
            self.ctx.paths.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # 上报消息网关就绪（route_message 注入需要网关 ready；platform 留空由运行时动态补全）
        try:
            await self.ctx.gateway.update_state("ciallo_voice_recorder", ready=True)
        except Exception as exc:
            self.ctx.logger.warning("[Ciallo] 上报语音补录网关状态失败：%s", exc)
        self.ctx.logger.info(
            "Ciallo 插件已加载：/ciallo 命令与 send_ciallo 工具就绪；关键词回复%s，关键词=%s",
            "已启用" if keyword_cfg.enabled else "未启用",
            keyword_cfg.keywords,
        )
        if voice_cfg.enabled:
            self.ctx.logger.info(
                "Ciallo 语音输出已启用：每条 Ciallo 以 %.0f%% 概率替换为语音直接发出（不引用回复），语音文件：%s",
                max(0.0, min(1.0, float(voice_cfg.probability))) * 100,
                self._voice_file_path() or "<配置的文件名非法>",
            )

    async def on_unload(self) -> None:
        # 网关下线
        try:
            await self.ctx.gateway.update_state("ciallo_voice_recorder", ready=False)
        except Exception:
            pass
        self._pending_replies.clear()
        self._keyword_reply_last_at.clear()
        self._voice_cache = None
        self.ctx.logger.info("Ciallo 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        keyword_cfg = self.config.keyword_reply
        self.ctx.logger.info(
            "Ciallo 配置已热更新（version=%s）：关键词回复%s，关键词=%s，冷却 %.0f 秒",
            version,
            "已启用" if keyword_cfg.enabled else "未启用",
            keyword_cfg.keywords,
            keyword_cfg.cooldown_seconds,
        )

    # ------------------------------------------------------------------
    # 发送核心
    # ------------------------------------------------------------------

    def _voice_enabled_now(self) -> bool:
        """本次 Ciallo 是否以语音发出：voice.enabled 开启后按 probability 独立随机。

        开启但 probability=0 时恒为 False（等价于全部文本）；默认 1.0 维持「全部语音」。
        """
        if not self.config.voice.enabled:
            return False
        probability = max(0.0, min(1.0, float(self.config.voice.probability)))
        if probability <= 0:
            return False
        if probability >= 1:
            return True
        return random.random() < probability

    async def _send_ciallo(self, stream_id: str, reply_to: str = "") -> bool:
        """发送一条 Ciallo。

        语音输出开启时，本次若被概率选中（``_voice_enabled_now``）则以语音直接
        发出，不引用回复任何消息；未选中或未开启时按文本逻辑发送（``reply_to``
        非空时以引用回复形式发送）。

        ``ctx.send.text`` 不支持直接指定被引用消息，因此文本引用回复先把
        目标挂起到 ``_pending_replies``，由 ``send_service.before_send``
        钩子在出站链上注入 ``set_reply`` / ``reply_message_id``。
        """
        if not stream_id:
            self.ctx.logger.warning("[Ciallo] 缺少 stream_id，无法发送")
            return False
        if self._voice_enabled_now():
            return await self._send_voice_ciallo(stream_id)
        target_id = str(reply_to or "").strip()
        if not target_id:
            return bool(await self.ctx.send.text(CIALLO_TEXT, stream_id))

        self._pending_replies[stream_id] = (target_id, time.monotonic())
        try:
            return bool(await self.ctx.send.text(CIALLO_TEXT, stream_id))
        finally:
            self._pending_replies.pop(stream_id, None)

    def _voice_file_candidates(self) -> list[Path]:
        """语音文件查找链：数据目录用户自定义文件优先，其次插件内置 assets/ 兜底。

        文件名仅允许纯文件名（防路径穿越）；非法时返回空列表。
        """
        file_name = str(self.config.voice.file_name or "").strip()
        if not file_name or Path(file_name).name != file_name:
            self.ctx.logger.warning(
                "[Ciallo] 语音文件名非法：%r（仅允许纯文件名，不支持子目录）", file_name
            )
            return []
        candidates = [(self.ctx.paths.data_dir / file_name).resolve()]
        bundled = (Path(__file__).resolve().parent / "assets" / file_name).resolve()
        if bundled not in candidates:
            candidates.append(bundled)
        return candidates

    def _voice_file_path(self) -> Path | None:
        """语音文件绝对路径（数据目录优先，无则内置 assets/）；用于日志提示。"""
        candidates = self._voice_file_candidates()
        return candidates[0] if candidates else None

    def _load_voice_base64(self) -> str:
        """读取语音文件并转 base64（按 mtime 缓存）；不可用时返回空串。

        查找顺序：数据目录（用户自定义，优先）→ 插件内置 assets/ 打包语音。
        """
        candidates = self._voice_file_candidates()
        if not candidates:
            return ""
        for file_path in candidates:
            try:
                file_mtime = file_path.stat().st_mtime
            except OSError:
                continue
            cached = self._voice_cache
            if cached is not None and cached[0] == file_path and cached[1] == file_mtime:
                return cached[2]
            try:
                audio_base64 = base64.b64encode(file_path.read_bytes()).decode("ascii")
            except OSError:
                continue
            self._voice_cache = (file_path, file_mtime, audio_base64)
            self._voice_missing_logged = False
            return audio_base64
        if not self._voice_missing_logged:
            self._voice_missing_logged = True
            self.ctx.logger.warning(
                "[Ciallo] 语音文件不存在，暂时回退为文本发送；已查找：%s",
                " / ".join(str(p) for p in candidates),
            )
        return ""

    async def _send_voice_ciallo(self, stream_id: str) -> bool:
        """以语音形式直接发出 Ciallo（不引用回复）；不可用时回退文本发送。

        经 ``send.hybrid`` 的 voice 段走官方发送管线（入库 + Platform IO 路由），
        宿主把 VoiceComponent 序列化为 voice 段，适配器编码为 record 段发送。
        """
        audio_base64 = self._load_voice_base64()
        if audio_base64:
            try:
                sent = await self.ctx.send.hybrid(
                    [{"type": "voice", "content": audio_base64}],
                    stream_id,
                    processed_plain_text=CIALLO_TEXT,
                )
                if sent:
                    # 语音真发成功：补录一条 bot 的 Ciallo 文本记录（入站链入库，
                    # 使 WebUI/聊天记录显示 bot 发过 Ciallo；补录失败不影响语音本身）
                    await self._record_voice_ciallo(stream_id)
                    return True
                self.ctx.logger.warning(
                    "[Ciallo] 语音发送失败（stream=%s），回退为文本发送", stream_id
                )
            except Exception:
                self.ctx.logger.exception(
                    "[Ciallo] 语音发送异常（stream=%s），回退为文本发送", stream_id
                )
        return bool(await self.ctx.send.text(CIALLO_TEXT, stream_id))

    # ------------------------------------------------------------------
    # 语音补录：构造 bot 的 Ciallo 文本入库记录（不真发，仅入库）
    # ------------------------------------------------------------------

    async def _record_voice_ciallo(self, stream_id: str) -> None:
        """语音发送成功后，注入一条 bot 自己发送 Ciallo 文本的记录。

        机制（与工作区 set_msg_emoji_like 插件同款，官方文档 §10.4）：MaiBot
        无「仅入库不发送」API；把 ``is_notify=True`` 合成通知消息经本插件的
        ``ciallo_voice_recorder`` 接收网关注入完整入站链 → heartflow
        ``process_message`` 无条件 ``store_message_to_db_async`` 入库，
        通知语义不触发 LLM 回复、不会真发到平台。

        身份：user_info 用机器人自己（self_id / 昵称），群聊附带 group_info，
        使记录归属到与语音发送相同的会话。
        """
        try:
            stream = await self._find_stream_info(stream_id)
            if not stream:
                self.ctx.logger.warning("[Ciallo] 语音补录失败：未找到会话 %s 的流信息", stream_id)
                return
            platform = str(stream.get("platform") or "qq")
            self_id = str(stream.get("account_id") or stream.get("self_id") or "").strip()
            if not self_id:
                self.ctx.logger.warning(
                    "[Ciallo] 语音补录失败：会话 %s 缺少机器人账号(account_id/self_id)", stream_id
                )
                return
            bot_nickname = await self._fetch_bot_nickname(self_id)

            message_info: dict[str, Any] = {
                "user_info": {
                    "user_id": self_id,
                    "user_nickname": bot_nickname or self_id,
                    "user_cardname": None,
                },
                "additional_config": {
                    "self_id": self_id,
                    "platform_io_account_id": self_id,
                    "plugin_injected_notice": "ciallo_voice_record",
                },
            }
            group_id = str(stream.get("group_id") or "").strip()
            if group_id:
                message_info["group_info"] = {
                    "group_id": group_id,
                    "group_name": str(stream.get("group_name") or group_id),
                }
                message_info["additional_config"]["platform_io_target_group_id"] = group_id
            else:
                # 私聊：目标为对方用户（BotChatSession.user_id），平台路由据此落到原会话
                target_user_id = str(stream.get("user_id") or "").strip()
                if target_user_id:
                    message_info["additional_config"]["platform_io_target_user_id"] = target_user_id

            notice: dict[str, Any] = {
                "message_id": f"ciallo-voice-record-{uuid4().hex}",
                "timestamp": str(time.time()),
                "platform": platform,
                "message_info": message_info,
                "raw_message": [{"type": "text", "data": CIALLO_TEXT}],
                "is_mentioned": False,
                "is_at": False,
                "is_emoji": False,
                "is_picture": False,
                "is_command": False,
                "is_notify": True,
                "session_id": "",
                "processed_plain_text": CIALLO_TEXT,
                "display_message": CIALLO_TEXT,
            }
            accepted = await self.ctx.gateway.route_message(
                "ciallo_voice_recorder",
                notice,
                route_metadata={"self_id": self_id, "platform": platform},
                external_message_id=str(notice.get("message_id") or ""),
                dedupe_key=f"ciallo-voice-record-{stream_id}-{uuid4().hex}",
            )
            if accepted:
                self.ctx.logger.info("[Ciallo] 语音 Ciallo 已补录 bot 文本记录（stream=%s）", stream_id)
            else:
                self.ctx.logger.warning("[Ciallo] 语音补录被宿主拒绝（stream=%s）", stream_id)
        except Exception as exc:
            self.ctx.logger.warning("[Ciallo] 语音补录异常（stream=%s）：%s", stream_id, exc)

    async def _find_stream_info(self, stream_id: str) -> dict[str, Any] | None:
        """在活跃会话列表中按 session_id/stream_id 查找目标流的序列化信息。"""
        if not str(stream_id or "").strip():
            return None
        try:
            result = await self.ctx.chat.get_all_streams()
        except Exception as exc:
            self.ctx.logger.warning("[Ciallo] 查询聊天流失败（stream=%s）：%s", stream_id, exc)
            return None
        streams = result
        if isinstance(result, dict):
            streams = result.get("streams") or result.get("result") or []
        if not isinstance(streams, list):
            return None
        for item in streams:
            if not isinstance(item, dict):
                continue
            if str(item.get("session_id") or item.get("stream_id") or "") == str(stream_id):
                return item
        return None

    async def _fetch_bot_nickname(self, self_id: str) -> str:
        """取机器人昵称：优先 NapCat get_login_info（缓存 1 小时），兜底 self_id。"""
        now = time.monotonic()
        cached = self._bot_nickname_cache
        if cached is not None and cached[1] > now:
            return cached[0]
        nickname = self_id
        try:
            result = await self.ctx.api.call("adapter.napcat.system.get_login_info")
            if isinstance(result, dict):
                data = result.get("data") if isinstance(result.get("data"), dict) else result
                nickname = str(data.get("nickname") or data.get("user_nickname") or "").strip() or self_id
        except Exception as exc:
            self.ctx.logger.warning("[Ciallo] 获取机器人昵称失败（self_id=%s）：%s", self_id, exc)
        self._bot_nickname_cache = (nickname, now + 3600)
        return nickname

    @staticmethod
    def _outbound_is_ciallo(message: dict[str, Any]) -> bool:
        """判断出站消息是否为本插件发送的 Ciallo 文本。"""
        if str(message.get("processed_plain_text") or "") == CIALLO_TEXT:
            return True
        raw_segments = message.get("raw_message")
        if isinstance(raw_segments, list):
            for segment in raw_segments:
                if (
                    isinstance(segment, dict)
                    and segment.get("type") == "text"
                    and str(segment.get("data") or "") == CIALLO_TEXT
                ):
                    return True
        return False

    @staticmethod
    def _outbound_has_voice(message: dict[str, Any]) -> bool:
        """判断出站消息是否包含语音段（voice/record），有则不应注入引用。"""
        raw_segments = message.get("raw_message")
        if isinstance(raw_segments, list):
            for segment in raw_segments:
                if isinstance(segment, dict) and segment.get("type") in ("voice", "record"):
                    return True
        return False

    @staticmethod
    def _extract_reply_target(message: Any) -> str:
        """从入站消息的 raw_message 中提取被回复消息的 ID。"""
        if not isinstance(message, dict):
            return ""
        raw_segments = message.get("raw_message")
        if not isinstance(raw_segments, list):
            return ""
        for segment in raw_segments:
            if isinstance(segment, dict) and segment.get("type") == "reply":
                data = segment.get("data")
                if isinstance(data, dict):
                    target_id = str(data.get("target_message_id") or "").strip()
                    if target_id:
                        return target_id
        return ""

    # ------------------------------------------------------------------
    # 组件 1：LLM 工具
    # ------------------------------------------------------------------

    @Tool(
        "send_ciallo",
        brief_description="可爱的打个招呼",
        detailed_description=(
            "让机器人可爱地打个招呼，发送一条「Ciallo～(∠・ω< )⌒★」。\n"
            "仅当用户明确要求打招呼/发送 Ciallo 时调用。\n"
            "参数 message_id：可选。传入需要回复的目标消息 ID 时，将以引用回复的形式"
            "对目标消息发送「Ciallo～(∠・ω< )⌒★」；不传或传空则直接发送一条。"
        ),
        parameters=[
            ToolParameterInfo(
                name="message_id",
                param_type=ToolParamType.STRING,
                description="要回复的目标消息 ID（可选）；传入时以引用回复发送",
                required=False,
                default="",
            ),
        ],
    )
    async def tool_send_ciallo(self, message_id: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "")
        sent = await self._send_ciallo(stream_id, reply_to=str(message_id or ""))
        if sent:
            return {"success": True, "content": f"已发送：{CIALLO_TEXT}"}
        return {"success": False, "content": "Ciallo 发送失败，请检查日志。"}

    # ------------------------------------------------------------------
    # 组件 2：/ciallo 命令（任何人可用，不做权限限制）
    # ------------------------------------------------------------------

    @Command(
        "ciallo",
        description="发送一个Ciallo～(∠・ω< )⌒★；引用某条消息发送本命令时，改为对那条消息回复",
        pattern=r"(?<!\S)/?ciallo\s*$",
    )
    async def cmd_ciallo(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        reply_to = self._extract_reply_target(kwargs.get("message"))
        await self._send_ciallo(stream_id, reply_to=reply_to)
        return True, "ciallo", True

    # ------------------------------------------------------------------
    # 组件 3：引用回复注入钩子
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.before_send",
        name="ciallo_reply_injector",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_inject_reply(self, **kwargs: Any) -> dict[str, Any]:
        """把挂起的引用目标注入本插件正在发送的 Ciallo 文本消息。

        语音概率模式：未被概率选中的 Ciallo 走文本发送，仍应正常注入引用；
        因此本钩子不依赖语音开关，只对「文本 Ciallo 出站消息」注入。语音出站
        消息（raw_message 含 voice/record 段）一律跳过，防止残留挂起误注入。
        """
        message = kwargs.get("message")
        if isinstance(message, dict):
            stream_id = str(message.get("session_id") or "")
            pending = self._pending_replies.get(stream_id)
            if pending is not None and self._outbound_is_ciallo(message):
                if self._outbound_has_voice(message):
                    # 语音出站消息不注入引用（语音始终直接发出）
                    self._pending_replies.pop(stream_id, None)
                    return {"action": "continue", "modified_kwargs": kwargs}
                self._pending_replies.pop(stream_id, None)
                target_id, created_at = pending
                if time.monotonic() - created_at <= _PENDING_REPLY_TTL_SEC:
                    modified_kwargs = dict(kwargs)
                    modified_kwargs["set_reply"] = True
                    modified_kwargs["reply_message_id"] = target_id
                    return {"action": "continue", "modified_kwargs": modified_kwargs}
        return {"action": "continue", "modified_kwargs": kwargs}

    # ------------------------------------------------------------------
    # 组件 4：关键词自动回复钩子（默认关闭，由配置启用）
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="ciallo_keyword_reply",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_keyword_reply(self, **kwargs: Any) -> None:
        """消息命中关键词时，在后台对那条消息引用回复一条 Ciallo。"""
        try:
            message = kwargs.get("message")
            if isinstance(message, dict):
                await self._maybe_keyword_reply(message)
        except Exception:
            self.ctx.logger.exception("[Ciallo] 关键词自动回复处理异常")

    async def _maybe_keyword_reply(self, message: dict[str, Any]) -> None:
        keyword_cfg = self.config.keyword_reply
        if not keyword_cfg.enabled:
            return
        # 通知类消息（戳一戳/撤回等）与命令消息不触发，避免 /ciallo 被重复响应
        if message.get("is_notify") or message.get("is_command"):
            return
        text = str(message.get("processed_plain_text") or "").strip()
        if not text or text.lstrip().startswith("/"):
            return
        # 完全同款的打招呼文本（其他机器人自动回复/平台回显）不再响应，防互相问候死循环
        if text == CIALLO_TEXT:
            return
        keywords = [str(kw).strip() for kw in (keyword_cfg.keywords or []) if str(kw).strip()]
        if not keywords:
            return
        lowered_text = text.lower()
        if not any(keyword.lower() in lowered_text for keyword in keywords):
            return

        stream_id = str(
            message.get("session_id") or message.get("stream_id") or message.get("chat_id") or ""
        )
        if not stream_id:
            return

        cooldown = max(0.0, float(keyword_cfg.cooldown_seconds))
        now = time.monotonic()
        last_at = self._keyword_reply_last_at.get(stream_id, 0.0)
        if cooldown > 0 and now - last_at < cooldown:
            self.ctx.logger.debug("[Ciallo] 会话 %s 关键词回复冷却中，跳过", stream_id)
            return

        self._keyword_reply_last_at[stream_id] = now
        reply_to = str(message.get("message_id") or "").strip()
        sent = await self._send_ciallo(stream_id, reply_to=reply_to)
        if sent:
            self.ctx.logger.info(
                "[Ciallo] 已对命中关键词的消息自动回复（stream=%s, message_id=%s）",
                stream_id,
                reply_to or "-",
            )
        else:
            self.ctx.logger.warning("[Ciallo] 关键词自动回复发送失败（stream=%s）", stream_id)


def create_plugin() -> CialloPlugin:
    """Runner 加载入口。"""
    return CialloPlugin()
