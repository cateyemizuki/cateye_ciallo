"""Ciallo～(∠・ω< )⌒★ —— 让麦麦可爱地打个招呼。

三种打招呼方式：

1. LLM 工具 ``send_ciallo``：Planner 在用户明确要求打招呼时调用，可传入
   ``message_id`` 对指定消息引用回复；
2. 命令 ``/ciallo``：任何人可用。**仅当整条消息就是 ``/ciallo``（允许首尾
   空白）时触发**。宿主对命令用 ``re.search`` 匹配处理后的整段文本
   （``processed_plain_text``），引用某条消息时被引用内容会拼进该文本，
   因此「引用消息 + 输入 /ciallo」不会作为命令触发；需要对指定消息引用
   回复请让 LLM 调用工具 ``send_ciallo(message_id=...)``，或开启关键词
   自动回复；
3. 关键词自动回复（默认关闭）：消息命中关键词时，自动对那条消息引用回复
   一条 Ciallo。

「引用回复」均指 QQ 的引用指定消息来回复：工具路径取 LLM 传入的
``message_id``，关键词路径取命中消息的 ``message_id``，最终由宿主构建
ReplyComponent、适配器编码为平台引用段。

语音输出（默认关闭）：``[voice].enabled`` 开启后，每条 Ciallo 独立以
``[voice].probability``（0~1，默认 1.0 = 全部语音）的概率**替换为语音发送**
（经 ``send.hybrid`` 的 voice 段走官方发送管线，适配器编码为 record 段）；
被替换时直接发出、不引用回复任何消息，未替换时按正常文本逻辑发送（含引用
回复）。插件**自带默认语音** ``assets/ciallo.wav``（安装即用，无需手动放置）。
文件名可在配置中修改（仅允许纯文件名）；如需自定义，把同名文件放入数据目录
``data/plugins/github.cateye.ciallo/`` 即可覆盖（数据目录优先于内置 assets）。

实现说明：``ctx.send.text`` 无法直接指定被引用消息（宿主 send 链路的
``reply_message_id`` 不对外暴露），因此文本引用回复采用「挂起目标 + 出站
钩子注入」的方式：发送前把目标消息 ID 挂起到 ``_pending_replies``（按
会话多槽存放，带 TTL 时间窗、每会话上限与发送者一致性校验），由
``send_service.before_send`` 钩子对本插件发出的 Ciallo 消息注入
``set_reply=True`` 与 ``reply_message_id``（宿主 ``_send_via_platform_io``
会读取这两个键并构建 ReplyComponent）。
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
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

SUPPORTED_CONFIG_VERSION = "1.0.3"  # 与 _manifest.json 的 version 保持同步

CIALLO_TEXT = "Ciallo～(∠・ω< )⌒★"


def _en_i18n(en_label: str, en_hint: str = "") -> dict[str, str]:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n['en']['label'/'hint'] 取用）。"""
    entry: dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}

# 挂起的引用回复目标最长存活时间（秒），超时未消费即丢弃，避免错误注入到后续消息
_PENDING_REPLY_TTL_SEC = 60.0
# 同一会话挂起的引用回复目标上限，防止异常堆积泄漏
_PENDING_REPLY_MAX_PER_STREAM = 4
# 语音文件名白名单：仅允许字母/数字/下划线/连字符/点组成的纯文件名
# （拒绝 `:`（Windows ADS 形态）、路径分隔符等一切其他字符）
_VOICE_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置（plugin 配置节）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "waving_hand"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "hint": "插件总开关",
            **_en_i18n("Enable plugin", "Master switch for the plugin."),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "hidden": True,
            "disabled": True,
            "label": "配置版本",
            "hint": "配置版本，勿改",
            **_en_i18n("Config version", "Config version, do not modify."),
        },
    )


class KeywordReplySectionConfig(PluginConfigBase):
    """关键词自动回复配置（keyword_reply 配置节）。"""

    __ui_label__ = "关键词回复"
    __ui_icon__ = "auto_awesome"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en": {
            "title": "Keyword Reply",
            "description": "Auto-reply with a Ciallo when a message contains a keyword.",
        }
    }

    enabled: bool = Field(
        default=False,
        description="是否启用关键词匹配自动回复",
        json_schema_extra={
            "label": "启用关键词回复",
            "hint": "关键词自动回复开关",
            **_en_i18n("Enable keyword reply", "Toggle keyword auto-reply."),
        },
    )
    keywords: list[str] = Field(
        default_factory=lambda: ["ciallo"],
        description="触发关键词列表：消息文本包含任一关键词（不区分大小写）即自动回复一条 Ciallo",
        json_schema_extra={
            "label": "触发关键词",
            "hint": "触发关键词，每行一个",
            **_en_i18n("Trigger keywords", "Keywords, one per line."),
        },
    )
    cooldown_seconds: float = Field(
        default=30.0,
        ge=0,
        description="同一会话两次关键词回复的最小间隔（秒），防止刷屏",
        json_schema_extra={
            "label": "回复冷却间隔（秒）",
            "hint": "回复最短间隔（秒）",
            **_en_i18n("Reply cooldown (seconds)", "Minimum interval between replies in one chat."),
        },
    )


class VoiceSectionConfig(PluginConfigBase):
    """语音输出配置（voice 配置节）。"""

    __ui_label__ = "语音输出"
    __ui_icon__ = "graphic_eq"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[dict[str, dict[str, str]]] = {
        "en": {
            "title": "Voice Output",
            "description": "Replace Ciallo text with voice messages by probability.",
        }
    }

    enabled: bool = Field(
        default=False,
        description="开启后每条 Ciallo 按 probability 概率以语音直接发出（替换文本），未替换时仍走文本逻辑",
        json_schema_extra={
            "label": "启用语音输出",
            "hint": "语音输出总开关",
            **_en_i18n("Enable voice output", "Master switch for voice output."),
        },
    )
    probability: float = Field(
        default=1.0,
        ge=0,
        le=1,
        description="替换为语音发送的概率（0~1）：1.0 = 全部语音，0 = 全部文本，0.5 = 约一半语音",
        json_schema_extra={
            "label": "语音概率",
            "hint": "语音发送概率（0~1）",
            **_en_i18n("Voice probability", "Chance to send as voice (0~1)."),
        },
    )
    file_name: str = Field(
        default="ciallo.wav",
        description="语音文件名（仅允许字母/数字/下划线/连字符/点组成的纯文件名，不支持子目录）；插件自带同名默认语音（assets/），如需自定义可把同名文件放入数据目录 data/plugins/github.cateye.ciallo/ 覆盖",
        json_schema_extra={
            "label": "语音文件名",
            "hint": "语音文件名",
            **_en_i18n("Voice file name", "Voice file name (file name only)."),
        },
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
        # stream_id -> [挂起条目]；每条含 token / 被引用消息ID / 挂起时刻 / 期望发送者，
        # 支持同会话并发的多次带引用发送（发送结束按 token 精确清理，TTL 兜底）
        self._pending_replies: dict[str, list[dict[str, Any]]] = {}
        # stream_id -> 上次关键词回复时刻（monotonic）
        self._keyword_reply_last_at: dict[str, float] = {}
        # 关键词后台回复任务引用（防 GC；unload 时统一取消）
        self._keyword_reply_tasks: set[asyncio.Task[None]] = set()
        # 语音文件缓存：(路径, mtime, base64)；文件变更自动重载
        self._voice_cache: tuple[Path, float, str] | None = None
        self._voice_missing_logged: bool = False
        # 语音补录用机器人昵称缓存：(nickname, expires 时刻)
        self._bot_nickname_cache: tuple[str, float] | None = None
        # 获取机器人昵称失败是否已告警过（非 QQ 平台每小时重试，仅首次告警，之后静默）
        self._bot_nickname_warned: bool = False

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
            "Ciallo 插件已加载：命令 /ciallo（仅整条消息触发）与工具 send_ciallo 就绪；关键词回复%s，关键词=%s",
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
        for task in list(self._keyword_reply_tasks):
            task.cancel()
        self._keyword_reply_tasks.clear()
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

    async def _send_ciallo(self, stream_id: str, reply_to: str = "", reply_sender_id: str = "") -> bool:
        """发送一条 Ciallo。

        语音输出开启时，本次若被概率选中（``_voice_enabled_now``）则以语音直接
        发出，不引用回复任何消息；未选中或未开启时按文本逻辑发送（``reply_to``
        非空时以引用回复形式发送）。

        ``ctx.send.text`` 不支持直接指定被引用消息，因此文本引用回复先把
        目标挂起到 ``_pending_replies``（按 token 多槽存放，``reply_sender_id``
        为触发上下文已知的机器人账号，供钩子做发送者一致性校验），由
        ``send_service.before_send`` 钩子在出站链上注入 ``set_reply`` /
        ``reply_message_id``。
        """
        if not stream_id:
            self.ctx.logger.warning("[Ciallo] 缺少 stream_id，无法发送")
            return False
        if self._voice_enabled_now():
            return await self._send_voice_ciallo(stream_id)
        target_id = str(reply_to or "").strip()
        if not target_id:
            return bool(await self.ctx.send.text(CIALLO_TEXT, stream_id))

        token = self._register_pending_reply(stream_id, target_id, sender_id=reply_sender_id)
        try:
            return bool(await self.ctx.send.text(CIALLO_TEXT, stream_id))
        finally:
            self._pop_pending_reply(stream_id, token)

    def _register_pending_reply(self, stream_id: str, target_id: str, sender_id: str = "") -> str:
        """登记一条挂起的引用回复目标并返回本次挂起的 token。

        同一会话允许并存多条挂起（上限 ``_PENDING_REPLY_MAX_PER_STREAM``，
        满时丢弃最旧的），登记时顺手清理已过期的条目，防堆积泄漏。
        """
        now = time.monotonic()
        entries = self._pending_replies.setdefault(stream_id, [])
        entries[:] = [
            entry for entry in entries if now - entry["created_at"] <= _PENDING_REPLY_TTL_SEC
        ]
        while len(entries) >= _PENDING_REPLY_MAX_PER_STREAM:
            entries.pop(0)
        token = uuid4().hex
        entries.append(
            {"token": token, "target_id": target_id, "created_at": now, "sender_id": sender_id}
        )
        return token

    def _pop_pending_reply(self, stream_id: str, token: str) -> None:
        """按 token 精确移除挂起条目（发送完成即清理）。"""
        entries = self._pending_replies.get(stream_id)
        if not entries:
            return
        remaining = [entry for entry in entries if entry["token"] != token]
        if remaining:
            self._pending_replies[stream_id] = remaining
        else:
            self._pending_replies.pop(stream_id, None)

    def _claim_pending_reply(self, stream_id: str, sender_id: str) -> dict[str, Any] | None:
        """为一条 Ciallo 文本出站消息认领一条挂起目标（认领即移除）。

        按先进先出取第一条「未过期且发送者一致」的挂起；``sender_id`` 为出站
        消息的发送者（机器人账号），与登记时记录的期望发送者做一致性校验，
        任一方未知则放行（此时仅靠 TTL 时间窗兜底）。
        """
        entries = self._pending_replies.get(stream_id)
        if not entries:
            return None
        now = time.monotonic()
        claimed: dict[str, Any] | None = None
        remaining: list[dict[str, Any]] = []
        for entry in entries:
            if now - entry["created_at"] > _PENDING_REPLY_TTL_SEC:
                # 已过期：顺手丢弃，防堆积
                continue
            if claimed is None and self._sender_consistent(entry["sender_id"], sender_id):
                claimed = entry
            else:
                remaining.append(entry)
        if remaining:
            self._pending_replies[stream_id] = remaining
        else:
            self._pending_replies.pop(stream_id, None)
        return claimed

    @staticmethod
    def _sender_consistent(expected: str, actual: str) -> bool:
        """发送者一致性校验：两侧均已知时必须一致，任一侧未知则放行。"""
        if not expected or not actual:
            return True
        return expected == actual

    def _voice_file_candidates(self) -> list[Path]:
        """语音文件查找链：数据目录用户自定义文件优先，其次插件内置 assets/ 兜底。

        文件名走白名单校验：仅允许字母/数字/下划线/连字符/点组成的纯文件名，
        拒绝 ``:``（Windows ADS 形态如 ``xxx.wav:ads``）、路径分隔符、空白等
        一切其他字符；非法时返回空列表。
        """
        file_name = str(self.config.voice.file_name or "").strip()
        if (
            not file_name
            or file_name in (".", "..")
            or not _VOICE_FILE_NAME_RE.fullmatch(file_name)
            or Path(file_name).name != file_name
        ):
            self.ctx.logger.warning(
                "[Ciallo] 语音文件名非法：%r（仅允许字母/数字/下划线/连字符/点组成的纯文件名，"
                "不支持子目录等其它形态）",
                file_name,
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
            # 非 QQ 平台/适配器不支持该动作时会每次缓存过期都失败：仅首次告警，之后静默降级
            if not self._bot_nickname_warned:
                self._bot_nickname_warned = True
                self.ctx.logger.warning(
                    "[Ciallo] 获取机器人昵称失败（self_id=%s）：%s；后续将静默重试，不再重复告警",
                    self_id,
                    exc,
                )
            else:
                self.ctx.logger.debug("[Ciallo] 获取机器人昵称失败（self_id=%s）：%s", self_id, exc)
        self._bot_nickname_cache = (nickname, now + 3600)
        return nickname

    @staticmethod
    def _outbound_sender_id(message: dict[str, Any]) -> str:
        """取出站消息的发送者（机器人账号 user_id），取不到返回空串。"""
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            user_info = message_info.get("user_info")
            if isinstance(user_info, dict):
                return str(user_info.get("user_id") or "").strip()
        return ""

    @staticmethod
    def _inbound_self_id(message: dict[str, Any]) -> str:
        """取入站消息所属适配器的机器人账号（additional_config 的 self_id 等键）。"""
        message_info = message.get("message_info")
        additional_config = (
            message_info.get("additional_config") if isinstance(message_info, dict) else None
        )
        if isinstance(additional_config, dict):
            for key in ("self_id", "platform_io_account_id", "account_id"):
                value = str(additional_config.get(key) or "").strip()
                if value:
                    return value
        return ""

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
    #
    # 触发语义（严格整条）：宿主用 re.search() 对 processed_plain_text 匹配
    # pattern（_process_commands 用未 strip 的文本、_is_command_candidate 用
    # strip 后的文本做前置筛选），因此 ^\s*...\s*$ 只允许首尾空白。引用某条
    # 消息时 processed_plain_text = 被回复内容 + 本次输入，故「引用 + /ciallo」
    # 不会命中本命令；hello ciallo、say /ciallo、裸 ciallo、/ciallo 带其它文字
    # 也一律不触发。确保只有整条消息就是 /ciallo 时才会被当命令拦截。
    # ------------------------------------------------------------------

    @Command(
        "ciallo",
        description="发送一个Ciallo～(∠・ω< )⌒★（仅当整条消息为 /ciallo 时触发）",
        pattern=r"^\s*/ciallo\s*$",
    )
    async def cmd_ciallo(self, **kwargs: Any) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or "")
        await self._send_ciallo(stream_id)
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

        认领规则（收紧后）：挂起目标本身携带从工具参数/命中消息上下文取到的
        精确目标 ID，是主要的认领依据；「出站文本 == Ciallo」仅作为识别本插件
        消息的回退门控，认领还需同时满足 TTL 时间窗与发送者一致性校验，避免
        其他来源的同款文本消息被误注入。

        语音概率模式：未被概率选中的 Ciallo 走文本发送，仍应正常注入引用；
        因此本钩子不依赖语音开关，只对「文本 Ciallo 出站消息」注入。语音出站
        消息（raw_message 含 voice/record 段）一律跳过且不清除挂起（语音发送
        不登记挂起，此时存在的挂起属于同会话并发的文本发送，交由其自身
        finally 清理或 TTL 兜底）。
        """
        message = kwargs.get("message")
        if isinstance(message, dict):
            stream_id = str(message.get("session_id") or "")
            if stream_id and self._pending_replies.get(stream_id) and self._outbound_is_ciallo(message):
                if self._outbound_has_voice(message):
                    # 语音出站消息不注入引用（语音始终直接发出），不动挂起条目
                    return {"action": "continue", "modified_kwargs": kwargs}
                claimed = self._claim_pending_reply(
                    stream_id, self._outbound_sender_id(message)
                )
                if claimed is not None:
                    modified_kwargs = dict(kwargs)
                    modified_kwargs["set_reply"] = True
                    modified_kwargs["reply_message_id"] = str(claimed["target_id"])
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
        """消息命中关键词时，创建后台任务对那条消息引用回复一条 Ciallo。

        完整发送链（语音 base64 读盘、平台 I/O、补录、api.call）整体挪到
        ``asyncio.create_task`` 后台执行，不阻塞入站消息处理链（适配器慢时
        也不会拖住入站）；任务引用存入 ``_keyword_reply_tasks`` 防 GC，
        unload 时统一取消，异常在本插件侧记录日志。
        """
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        task = asyncio.create_task(self._keyword_reply_task(message))
        self._keyword_reply_tasks.add(task)
        task.add_done_callback(self._keyword_reply_tasks.discard)

    async def _keyword_reply_task(self, message: dict[str, Any]) -> None:
        try:
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
        sent = await self._send_ciallo(
            stream_id,
            reply_to=reply_to,
            reply_sender_id=self._inbound_self_id(message),
        )
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
