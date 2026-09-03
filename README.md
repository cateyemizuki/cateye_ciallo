# Ciallo～(∠・ω< )⌒★

让麦麦可爱地打个招呼，发送 **Ciallo～(∠・ω< )⌒★**。

- 插件 ID：`github.cateye.ciallo`
- 作者：cateye
- 版本：1.0.0
- 适配：MaiBot Host 1.0.0 ~ 1.99.99 / maibot-plugin-sdk 2.0.0 ~ 2.99.99（1.2.3 + SDK 2.8.0 实测）
- 能力声明：`send.text` / `send.hybrid` / `chat.get_all_streams` / `api.call`

## 功能

### 1. LLM 工具 `send_ciallo`

- 工具描述：**可爱的打个招呼**。
- Planner 在用户明确要求打招呼 / 发送 Ciallo 时自主调用（deferred 池，经 `tool_search` 发现）。
- 不带参数：直接发送一条 Ciallo。
- 传入 `message_id`：以引用回复的形式对目标消息发送 Ciallo。

### 2. 命令 `/ciallo`

- 任何人可用，无权限限制。
- 直接发送 `/ciallo`（或 `ciallo`）：机器人发送一条 Ciallo。
- 引用某条消息发送 `/ciallo`：改为对被回复的那条消息发送 Ciallo（引用回复）。
- 命令命中后拦截后续消息处理，不会重复触发 LLM 回复。

### 3. 关键词自动回复（默认关闭）

在 WebUI 插件配置（或 `config.toml`）中启用后，任何消息文本包含关键词（不区分大小写）时，
机器人自动对那条消息引用回复一条 Ciallo。

防误触 / 防刷屏设计：

- 通知类消息（戳一戳、撤回等）不触发；
- 命令消息（`/` 开头或 `is_command`）不触发，避免 `/ciallo` 被双重响应；
- 与打招呼完全同款的文本不响应，避免与其他打招呼机器人互相问候死循环；
- 同一会话有冷却间隔（`cooldown_seconds`，默认 30 秒）。

### 4. 语音输出（默认关闭，可设概率）

在配置中启用 `[voice].enabled` 后，每条 Ciallo（命令 / LLM 工具 / 关键词自动回复）
按 `[voice].probability`（0~1，默认 `1.0`）**独立随机**决定是否替换为语音直接发出：
被选中的以语音发出且**不引用回复**任何消息；未被选中的按正常文本逻辑发送
（需要引用回复时仍会引用）。`probability=1.0` 即「全部语音」，`0` 即「全部文本」，
`0.5` 约一半语音。

**插件自带默认语音**（仓库内 `assets/ciallo.wav`，安装即用，无需手动放置）；
如要自定义，把同名文件放入插件数据目录即可覆盖（数据目录优先于内置文件，替换无需重启，
插件按文件 mtime 自动重载缓存）。语音文件缺失或发送失败时自动回退为文本发送并记录日志。
语音经 `send.hybrid` 的 voice 段走官方发送管线（入库 + Platform IO 路由），适配器编码为
`record` 段发出（NapCat 适配器已支持；非 silk 格式通常需要 NapCat 侧有 ffmpeg）。

**语音补录（内置，无需配置）**：语音发送成功后，插件会自动补一条 **bot 自己发送的
Ciallo 文本**记录入库（走 MessageGateway 注入 `is_notify=True` 合成消息 → 完整入站链入库，
WebUI 聊天记录显示「麦麦：Ciallo～(∠・ω< )⌒★」），不真发、不触发回复、不影响语音本身；
补录失败（如找不到会话）时静默降级，不影响语音已发出。

#### 语音文件查找顺序与自定义

| 项 | 值 |
|---|---|
| 内置默认语音（随插件分发） | `<插件目录>/assets/ciallo.wav` |
| 自定义覆盖路径（优先） | `<MaiBot根目录>/data/plugins/github.cateye.ciallo/` |
| 默认文件名 | `ciallo.wav`（可在 `[voice].file_name` 修改，仅允许纯文件名） |
| 完整自定义默认路径 | `<MaiBot根目录>/data/plugins/github.cateye.ciallo/ciallo.wav` |
| 支持格式 | wav / mp3 / silk 等（以 NapCat 支持为准，非 silk 格式通常需要其 ffmpeg） |

> 查找顺序：先找数据目录（用户自定义）`<数据目录>/<file_name>`，未找到再用插件内置
> `<插件目录>/assets/<file_name>`；两者都无才回退文本。数据目录按**插件 ID** 命名
> （`github.cateye.ciallo`），与插件文件夹名 `cateye_ciallo` 不同。

## 安装

1. 将整个 `cateye_ciallo/` 目录复制到 MaiBot 的 `plugins/` 目录下（如 `<MaiBot根目录>/plugins/cateye_ciallo`）。
2. 启动 / 重启 MaiBot，日志中应出现 `Ciallo 插件已加载`。
3. 在 WebUI（默认 `http://127.0.0.1:8001`）插件管理中确认插件已启用；运行时配置文件
   `config.toml` 由 Runner 在插件目录下自动生成，请勿手工预置或提交。

> 首次安装即包含 manifest，无需额外操作；后续若修改 `_manifest.json`（如能力声明），必须完整重启 MaiBot（manifest 不热重载）。

## 配置

| 配置节 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `[plugin]` | `enabled` | `true` | 是否启用插件 |
| `[plugin]` | `config_version` | `"1.0.0"` | 配置版本（WebUI 隐藏，勿改） |
| `[keyword_reply]` | `enabled` | `false` | 是否启用关键词匹配自动回复 |
| `[keyword_reply]` | `keywords` | `["Ciallo"]` | 触发关键词列表，消息文本包含任一关键词（不区分大小写）即触发 |
| `[keyword_reply]` | `cooldown_seconds` | `30.0` | 同一会话两次关键词回复的最小间隔（秒） |
| `[voice]` | `enabled` | `false` | 开启后每条 Ciallo 按 `probability` 概率以语音直接发出（替换文本）；未替换时仍走文本逻辑 |
| `[voice]` | `probability` | `1.0` | 替换为语音发送的概率（0~1）：`1.0` 全部语音 / `0` 全部文本 / `0.5` 约一半语音 |
| `[voice]` | `file_name` | `"ciallo.wav"` | 语音文件名（纯文件名）；内置 assets 自带同名默认语音，如需自定义把同名文件放入数据目录即可覆盖 |

修改 `config.toml` 保存即可热重载，日志出现 `Ciallo 配置已热更新`。

## 验证步骤

1. **命令测试**：聊天中发送 `/ciallo` → 机器人发送一条 Ciallo；引用某条消息发送 `/ciallo` → 对那条消息引用回复。
2. **工具测试**：聊天中明确说「打个招呼吧 / 发个 Ciallo」，观察 Planner 日志 `plugin.invoke_tool` 调用 `send_ciallo`。
3. **关键词测试**：WebUI 开启 `[keyword_reply].enabled`，发送含 `Ciallo` 的消息 → 收到引用回复；再连续发送，确认冷却生效。
4. **语音测试**：开启 `[voice].enabled` 发送 `/ciallo` → 机器人直接发出内置默认语音（不引用回复），且聊天记录出现一条 bot 的 Ciallo 文本补录；自定义语音：往数据目录放入同名文件再触发 → 使用自定义文件；删除数据目录文件再触发 → 回退内置语音。
5. **热重载**：修改 `config.toml` 保存 → 观察日志 `Ciallo 配置已热更新`。
6. **卸载测试**：在 WebUI 停用插件，确认日志 `Ciallo 插件已卸载` 且无残留报错。

## 实现说明（引用回复）

MaiBot 1.2.3 的 `ctx.send.text` 不支持直接指定被引用消息（宿主 send 链路的
`reply_message_id` 不对外暴露，直传 `set_reply=True` 会因缺少被引用消息而发送失败）。
本插件采用官方钩子通道实现引用回复：

1. 发送前把目标消息 ID 挂起（按 `stream_id` 存放，30 秒 TTL，发送结束即清理）；
2. 在 `send_service.before_send` 阻塞钩子中，识别出本插件发出的 Ciallo 出站消息，
   向 Hook 的 `modified_kwargs` 注入 `set_reply=True` 与 `reply_message_id=<目标>`；
3. 宿主 `_send_via_platform_io` 读取这两个键后构建 ReplyComponent，经适配器编码为平台引用段发出。

该通道不耦合具体适配器（任何支持 reply 段的适配器均适用），消息照常入库、同步 Maisaka 上下文。

语音输出则经 `send.hybrid` 的 voice 段发送：宿主把 base64 音频构造为 `VoiceComponent`
（官方发送管线，入库 + Platform IO 路由），出站序列化为 voice 段，NapCat 适配器编码为
OneBot `record` 段（`base64://`）发送。语音替换按概率独立随机（`_voice_enabled_now`），
命中语音的 Ciallo 直接发出、不挂起引用目标；未命中的文本 Ciallo 仍正常走引用注入。
`before_send` 钩子对语音出站消息（含 voice/record 段）一律跳过注入，杜绝残留挂起误引用。

语音补录机制：语音发送成功后，插件经 `chat.get_all_streams` 找到语音目标会话，构造
`is_notify=True` 合成文本消息（user_info 为机器人自己：`self_id` + 昵称，昵称经
`adapter.napcat.system.get_login_info` 查询缓存 1 小时；群聊带 `group_info`、私聊带
`platform_io_target_user_id`），通过本插件的 `ciallo_voice_recorder` 接收网关
`route_message` 注入完整入站链 → heartflow `process_message` 自动写 DB（WebUI 可见、
不真发、通知语义不触发 LLM 回复）。注入消息带 `plugin_injected_notice` 标记；
关键词自动回复对 `is_notify` 消息与同款 Ciallo 文本双重防触发。

## 目录结构

```
cateye_ciallo/
├── _manifest.json        # 插件清单（Manifest v2）
├── plugin.py             # 入口：工具 send_ciallo + 命令 /ciallo + 2 个 Hook + 语音补录网关
├── assets/
│   └── ciallo.wav        # 内置默认语音（随插件分发，安装即用；可被数据目录文件覆盖）
├── LICENSE               # MIT
├── README.md
├── .gitignore            # 忽略 /config.toml、/ISSUE_SUBMISSION.md（运行时生成/本地使用）
└── tests/
    └── test_offline.py   # 离线自测（用 MaiBot venv 运行，无需启动机器人）
```

离线自测运行方式：

```bash
<MaiBot根目录>/.venv/Scripts/python.exe tests/test_offline.py
```

覆盖：组件清单与命名唯一性、命令普通/回复场景、before_send 引用注入、
工具调用、关键词启停/命中/防重、语音概率（1/0/0.5/越界）、内置语音与自定义覆盖、
默认配置、on_unload 清理。

## 故障排查

| 现象 | 处置 |
|---|---|
| 日志报 `E_CAPABILITY_DENIED ... send.text` / `send.hybrid` | manifest 能力声明缺失（本插件已声明），修改过 manifest 需完整重启 MaiBot |
| `/ciallo` 无反应 | 命令用 `re.search` 匹配且先经过违禁词过滤，确认文本为 `/ciallo`（结尾）；查主进程日志 |
| 引用回复变成普通发送 | 确认适配器支持 reply 段编码；确认 `[voice] enabled=false`（语音模式下永远不引用回复）；查主进程日志 `[cap.send.text]` 相关报错 |
| 语音不发出 / 回退为文本 | 看日志提示的语音文件路径，确认文件已放置、`[voice].file_name` 与实际文件名一致；格式不被 NapCat 支持时安装/确认其 ffmpeg |
| 关键词不触发 | 确认 `[keyword_reply] enabled=true` 已热重载生效、消息非命令/通知、且不在冷却窗口内 |
| 关键词刷屏 | 调大 `cooldown_seconds`；关键词不要设置得过短过泛 |
