# 我老婆

为 AstrBot 添加「今日老婆」玩法：每天抽取一位角色，可以换老婆、结婚或离婚。抽取和婚姻变动会存档；玩家可以翻看自己的历史，管理员可以在 AstrBot 后台查看和管理记录。

角色库只使用 [monbed/wife](https://github.com/monbed/wife)，覆盖动漫、Galgame、手游和其他游戏角色，以及部分虚拟主播。回复可选择 **LLM 模式**或**固定模板模式**。QQ 官方 Bot 支持单条 Markdown 图文和玩法快捷按钮。

## 安装与开始使用

需要 **AstrBot 4.29.4 或更新的 4.x 版本**，运行环境为 Python 3.12 或更高版本。

1. 在 AstrBot 管理面板打开「插件」，选择通过仓库地址安装。
2. 填入 `https://github.com/Whereis-Alice/astrbot_plugin_mywife`，安装后启用「我老婆」。
3. 在插件配置中选择回复模式。想直接使用固定文案，选择 `template`；想让机器人用当前人设说话，选择 `llm`，并确保 AstrBot 已配置聊天模型。
4. 发送 `jrlp` 或 `今日老婆`。

安装包内置角色清单，启用后即可抽取。**不需要先下载整个图库。** 每次只下载抽中角色的图片，之后优先使用本地缓存。首次使用图片需要联网；在线清单更新失败时会继续使用已有清单，图片无法获取时保留文字回复。

## 两种回复模式：二选一

| 模式 | 抽取、换老婆、结婚、离婚等业务文案 | 是否调用聊天模型 |
|---|---|---|
| `llm`：LLM 模式（默认） | 由当前模型结合当前会话人设生成，输入包含实际抽中角色的姓名和出处 | 是 |
| `template`：固定模板 | 使用内置文案，可自定义首次抽取模板 | 否 |

固定模板的默认效果：

> 爱丽丝和艾拉的SuperStar，你今天的老婆是来自《赛马娘》的小栗帽，请好好珍惜哦~

LLM 模式不会在模型回复后拼接一段固定的角色介绍。模型请求失败、超时、返回空内容，或回复遗漏角色姓名／出处时，会改用对应的固定模板；抽中角色和次数不会因此改变。帮助、状态、图库刷新、图鉴和历史等工具信息采用固定说明，两种模式共用。

### 自定义固定模板

在配置项 `template_draw` 中填写：

```text
{player}，你今天的老婆是{source_phrase}{wife_name}，请好好珍惜哦~
```

| 占位符 | 含义 |
|---|---|
| `{player}` | 发送者昵称；无法获取时使用「你」 |
| `{wife_name}` | 角色姓名 |
| `{source}` | 作品或分类的原始标签 |
| `{source_phrase}` | 自动生成的出处短语，推荐使用 |

`source_phrase` 对作品生成「来自《赛马娘》的」，对 Vtuber 等分类生成「「Vtuber」分类中的」。这样不会把分类误写成作品名称。不要在占位符里添加属性访问或格式转换；模板无效时自动使用默认文案。QQ 官方 Bot 的模板可写 Markdown，昵称、姓名和出处会作为普通文字插入。

## 指令

原有指令保持不变，抽取历史可以按页查看。

| 发送内容 | 功能 |
|---|---|
| `jrlp` / `今日老婆` | 抽取今日老婆；当天再次查看保持角色和图片不变 |
| `hlp` / `换老婆` | 更换今日老婆；默认每天可换 5 次 |
| `结婚` / `jrlp 结婚` / `jrlp marry` | 与当前老婆结婚，默认维持 7 天 |
| `离婚` / `jrlp 离婚` / `jrlp divorce` | 解除婚姻，保留当前角色 |
| `jrlp status` / `jrlp 状态` / `jrlp状态` | 查看回复模式、图库和玩法规则 |
| `jrlp download` / `jrlp 下载图库` / `下载图库` | 刷新在线角色清单；图片继续在抽取时下载 |
| `jrlp force-download` / `jrlp 强制下载` | 管理员强制刷新角色清单 |
| `jrlp help` / `jrlp 帮助` / `jrlp帮助` | 查看使用帮助 |
| `jrlp 图鉴 关键词` / `jrlp catalog 关键词` | 按角色或作品搜索，最多显示前 10 条 |
| `jrlp 历史 [页码]` / `jrlp history [页码]` | 按页查看自己在当前范围内的抽取与更换记录；默认第一页，每页 10 条 |
| `jrlp 重置次数` / `重置次数` / `重置换` / `jrlp reset` | AstrBot 管理员重置自己在当前范围的今日换老婆次数 |

支持 `~jrlp`、`.jrlp`、`/jrlp`、`~hlp` 等形式，也支持「和今日老婆结婚」「与今日老婆离婚」等原有中文快捷指令。群聊中能否直接发送无前缀指令，仍取决于平台是否将消息交给 AstrBot；QQ 官方 Bot 请按平台要求 @机器人或使用已配置的命令入口。

## 玩法规则

- 每天第一次抽取不占「换老婆」次数。默认可另外更换 5 次，设为 0 可禁止更换。
- 同一角色的多张图片只占一个抽取名额。更换时会排除当前角色；筛选后只剩当前角色时，不更换也不扣次数。
- 每天按配置时区的零点重置当前抽取和更换次数。默认跟随 AstrBot 全局时区，无效时回退到 `Asia/Shanghai`。
- 已婚状态跨天保留，婚姻按连续 24 小时计算。婚姻期间不能换老婆；到期自动解除。
- 修改婚期只影响之后新建立的婚姻。离婚后保留当前角色，可继续查看或在剩余次数内更换。
- 新安装默认使用 `state_scope=session`，按 AstrBot 会话标识和用户隔离：同一用户在不同会话的当前角色、更换次数、婚姻和历史各自独立。即使 AstrBot 关闭了“群内唯一会话”，同群不同用户仍各有自己的记录。
- 可选择 `group`，让同一用户在同群共用记录；或选择 `user`，在同一平台、同一 Bot 下跨群共享。不同 Bot 始终隔离。升级保留已有的 `group/user` 配置与记录；需要会话隔离时在面板选择 `session`。切换范围或更改 AstrBot 会话划分会使用另一组记录，不会复制、删除旧记录。
- 会话隔离采用 AstrBot 的会话来源标识，不随 LLM 的 `/new` 或切换人设额外重置。WebChat 的不同聊天本身拥有不同会话标识。

## QQ 官方 Bot：Markdown 和快捷按钮

启用 `qq_official_markdown` 后，角色文字与图片嵌入同一条原生 Markdown 消息，图片按比例显示。启用 `qq_official_buttons` 后，消息底部按状态显示「结婚」「换老婆」「历史」等按钮，历史可以翻页。按钮仅原指令发送者可用，点击会发送对应的普通指令。已婚或换人次数用完时会省略不适用的按钮；离婚仍需主动输入指令。

Bot 从所选 monbed 来源下载、验证原图，再使用当前 QQ 官方平台身份上传到 QQ 的图片存储。同一条 Markdown 消息内嵌入 QQ 专用图片引用，QQ 客户端无需直接访问 GitHub 或第三方镜像；不需要另外配置图床或 COS 密钥。上传结果按 Bot 和图片内容缓存，避免重复上传。

`image_source` 可选择 `github`、`jsdelivr` 或 `jsdmirror`，只影响 Bot 下载原图。如果 QQ 拒绝按钮，插件会尝试保留 Markdown 图文并附上手动指令；如果 QQ 拒绝 Markdown 或图片上传不可用，则转为普通图文。原图无法获取时仍会回复文字。

支持 AstrBot 的 QQ 官方 WebSocket 和 Webhook 适配器。按钮与 Markdown 的可用性取决于 QQ 开放平台授予该 Bot 的能力和聊天场景；普通消息仍可输入同样的指令。

## 抽取账本

在已登录的 AstrBot 管理面板，从侧栏「扩展功能」打开「抽取账本」。可以查看今日抽取量、存档总量、有效婚姻、每位玩家的当前状态，以及抽取、换人、结婚或离婚流水。支持搜索、筛选、分页，也可以从玩家列表直接进入该玩家的流水。

管理页可为指定玩家重置今日更换次数。重置只影响选定的存档范围，保留当前角色、有效婚姻和所有流水。页面使用 AstrBot 后台登录，无需另开端口或设置密码。

## 管理员重置次数

发送 `jrlp 重置次数`、`重置次数`、`重置换` 或 `jrlp reset`，仅将自己的今日更换计数归零。当前角色、图片、婚姻和抽取历史保留；已婚时仍不能更换。命令只作用于当前 `state_scope` 对应的记录，不会批量重置全群，也不支持 @其他用户。普通用户会收到权限提示。

这里的管理员指 **AstrBot 管理员**，不是 QQ 群管理员；请在 AstrBot 的管理员设置中添加相应用户。

## 配置项

配置在 AstrBot 插件面板中修改，保存后按面板提示重载。

| 配置 | 默认值 | 说明 |
|---|---|---|
| `reply_mode` | `llm` | 二选一：`llm` 或 `template` |
| `provider_id` | 留空 | 留空使用当前会话模型，也可指定模型提供商 |
| `llm_timeout_seconds` | `45` | 模型等待上限，超时使用模板 |
| `template_draw` | 上述默认模板 | 自定义首次抽取文案 |
| `qq_official_markdown` | `true` | 官 Bot 使用单条 Markdown 图文 |
| `qq_official_buttons` | `true` | 在支持的 QQ Markdown 消息里显示玩法按钮 |
| `enable_images` | `true` | 关闭后只回复文字 |
| `image_source` | `github` | Bot 下载 monbed 原图的来源，可选 `jsdelivr` / `jsdmirror` |
| `auto_refresh_catalog` | `true` | 启动时在后台检查角色清单 |
| `refresh_interval_hours` | `24` | 已有清单缓存未到此间隔时，启动检查会跳过 |
| `source_filters` | `[]` | 允许抽取的准确作品／分类标签；留空使用整个库 |
| `exclude_characters` | `[]` | 排除姓名，或指定「出处!姓名」 |
| `daily_hlp_limit` | `5` | 每天可更换次数；首次抽取不计入 |
| `marriage_duration` | `7` | 新建立婚姻的维持天数 |
| `state_scope` | `session` | `session` 按会话和用户隔离；`group` 同群同用户共用；`user` 在同一 Bot 下跨群共享 |
| `timezone` | 留空 | 跟随 AstrBot 全局时区，或显式填写 IANA 时区 |
| `genie_tts_enabled` | `false` | 可选 GENIE 语音联动；仅 LLM 模式启用 |

筛选示例：`source_filters` 填入 `赛马娘`、`可塑性记忆`、`anemoi`，只抽取这些来源。排除 `艾拉` 会排除所有同名角色；排除 `明日方舟!艾拉` 只影响这一条。筛选和排除设置只影响之后的随机抽取，已抽中或已婚角色仍可查看。

## 角色库与数据

唯一角色库是 [monbed/wife](https://github.com/monbed/wife)。2026-11-25 的内置快照含 **2,717 张图片路径**，合并同角色多图和 `Little_Busters` 标签后得到 **2,544 条记录**。记录数不等于严格核验后的独立角色人数：原库仍有系列标签、译名和合照，出处沿用图库标签，不由 LLM 猜测。

用户状态、婚姻和流水保存到 `records.sqlite3`；清单与图片缓存也位于插件数据目录：

```text
data/plugin_data/astrbot_plugin_mywife/
```

从 v1.1.0 升级时会自动导入旧 KV 中仍在的角色、计数、婚姻和历史，保留旧数据备份。旧版已截掉的历史无法恢复；新版抽取与更换不再按 20 条截断。旧存档只有范围哈希和日期时，管理页先按存档标识显示；玩家继续使用后会补全平台、会话和昵称。备份时建议先停止 AstrBot，再复制 `records.sqlite3`。

插件使用独立的识别名、类名、事件标记和数据空间，不读取原插件的用户数据或图库。**原有命令名称仍相同，因此请停用其他使用 `jrlp`／`hlp`／「今日老婆」／「换老婆」的插件处理器，避免同名命令由其他插件先响应。** 原项目专用的外部多 Bot 协调服务不接入；本插件按平台、Bot、群和用户隔离状态。

## 开发、来源与许可

玩法参考 [Set-suei/galgame-LLM-jrlp](https://github.com/Set-suei/galgame-LLM-jrlp)，管理页参考 [Heximiao/astrbot-plugin-wifepicker](https://github.com/Heximiao/astrbot-plugin-wifepicker)；本项目代码独立实现。角色元数据与图片来源感谢 [monbed/wife](https://github.com/monbed/wife)。仓库包含轻量清单，不包含原 galgame 图库或整个 monbed 图片包。

插件代码采用 [MIT License](LICENSE)。monbed 清单保留其[原版权文本](data/LICENSE.monbed-wife)和[来源说明](data/NOTICE.md)。图片作品的版权归相应权利人；图库没有逐张作者、授权或内容分级字段，公开使用可通过作品筛选和角色排除整理适合自己机器人的角色池。

开发验证：安装 `requirements.txt`、AstrBot 4.29.4、`pytest`、`pytest-asyncio` 与 `ruff`，运行 `python -m pytest` 和 `ruff check .`。测试覆盖旧数据导入、并发抽取、记录隔离、历史分页、管理页权限、QQ 图文和快捷按钮降级。

遇到问题请在本仓库提交 [Issue](https://github.com/Whereis-Alice/astrbot_plugin_mywife/issues)，说明 AstrBot 版本、平台、回复模式、触发指令和错误现象；请勿公开 API Key 或 Bot 凭据。
