# DeepSeek V4 Flash 翻译与术语表开发计划

> 历史开发计划：当前已迁移至 DeepSeek V4.1 Flash（`deepseek-flash`）。
> 最新配置和迁移行为请参见 [README](../README.md#从-v4-flash-迁移到-v41-flash)。

## 1. 目标

将翻译后端从本地 Qwen/llama-server 切换为 DeepSeek API，固定使用
`deepseek-v4-flash`，同时建立可维护、可验证的术语表机制，并提高长视频翻译的
失败恢复能力。

## 2. 开发前置约束

开发前必须先对照 [DeepSeek 中文官方 API 文档](https://api-docs.deepseek.com/zh-cn/)，
核实当前接口路径、请求参数、响应格式、思考模式、JSON 输出、超时、限流、错误码和
重试建议。官方文档是 DeepSeek API 行为的唯一实施依据；如文档与本计划存在差异，
应以开发时的官方文档为准，并同步更新本文档。

2026-08-30 开发核对结论：官方 OpenAI 格式基址仍为 `https://api.deepseek.com`，模型列表
包含 `deepseek-v4-flash`；Chat Completions 的思考模式默认开启，非思考模式须显式传入
`{"thinking":{"type":"disabled"}}`；JSON 输出须传入
`{"response_format":{"type":"json_object"}}`，并在提示词中包含 JSON 字样和目标格式示例。
官方错误码仍将 400、401、402、422 定义为请求或账户问题，将 429、500、503 定义为限速或
服务端问题，与本计划的立即失败及有限重试分类一致。核对页面：
[模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、
[Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)、
[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)、
[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)、
[错误码](https://api-docs.deepseek.com/zh-cn/quick_start/error_codes/)。

本项目的 DeepSeek 模型必须指定为：

```text
deepseek-v4-flash
```

不得使用过时别名或其他模型名代替。开发和手工集成验证时，应通过官方模型列表
接口再次确认该模型标识可用。

## 3. 推荐架构

```text
批处理任务
   |
   +-- 加载并匹配术语表
   v
DeepSeekClient
   |
   +-- 非思考模式
   +-- JSON 输出
   +-- 超时与 API 重试
   v
结构校验 + 术语一致性校验
   |
   v
批次进度保存 -> 字幕 JSON -> SRT
```

## 4. 阶段一：重构模型客户端与配置

- 将当前专用的 `QwenClient` 拆分为通用翻译客户端接口和 `DeepSeekClient`。
- 继续使用现有 `httpx`，不增加 OpenAI SDK 依赖。
- 保留可配置的 API 基址，但生产默认指向 DeepSeek 官方 API。
- API Key 为空或仍为本地占位值时，必须在修改任务状态前报出明确的配置错误。
- API Key 不得写入日志、状态文件或翻译配置指纹。

建议配置：

```dotenv
SUBTITLE_TRANSLATOR_LLM_PROVIDER=deepseek
SUBTITLE_TRANSLATOR_LLM_BASE_URL=https://api.deepseek.com
SUBTITLE_TRANSLATOR_LLM_MODEL=deepseek-v4-flash
SUBTITLE_TRANSLATOR_LLM_API_KEY=
SUBTITLE_TRANSLATOR_LLM_THINKING=false
SUBTITLE_TRANSLATOR_LLM_MAX_OUTPUT_TOKENS=4096
SUBTITLE_TRANSLATOR_TIMEOUT_SECONDS=300
```

## 5. 阶段二：实现 DeepSeek 请求契约

- 使用官方 OpenAI 兼容的 Chat Completions 接口。
- 请求中必须使用 `deepseek-v4-flash`。
- 字幕翻译显式关闭思考模式，降低延迟、费用和超时概率。
- 启用官方 JSON 输出能力，并在提示词中同时明确约定 JSON 格式。
- 将现有顶层数组响应改为对象契约：

```json
{
  "translations": [
    {"id": 1, "text": "译文"}
  ]
}
```

- 校验 HTTP 状态、停止原因、JSON 结构、字幕 ID 数量、顺序和取值。
- 不允许模型遗漏、增加或重排字幕片段。
- 响应按原顺序漏掉部分 ID，或单个译文为空、只有空白、为 `null`、缺少 `text` 时，只对
  失败片段执行一次修复请求；修复后仍不完整时明确失败，不重复请求整个批次。新增、重复或
  重排 ID 仍立即失败，禁止猜测式合并。
- 记录每批请求的 token 用量和耗时，但不记录完整请求体或完整字幕。

## 6. 阶段三：建立正式术语表契约

将现有简单的“原文到译文”JSON 对象升级为版本化格式：

```json
{
  "version": 1,
  "terms": [
    {
      "source": "weathering",
      "target": "旧化",
      "aliases": ["weathered", "weathering effects"],
      "case_sensitive": false
    },
    {
      "source": "airbrush",
      "target": "喷笔",
      "aliases": [],
      "case_sensitive": false
    }
  ]
}
```

- 支持读取旧的扁平 JSON 对象，并提供迁移能力。
- 检查空值、重复词、别名冲突和同一原文对应多个译文。
- 术语表使用 UTF-8 和同文件系统原子写入。
- 建议正式文件位于 `config/glossary.json`，并纳入版本控制。
- 规范化后的术语表内容必须纳入翻译配置指纹。
- 修改术语表后，已有翻译结果应根据新指纹自动失效并重新翻译。

## 7. 阶段四：增加术语表维护命令

计划增加：

```bash
subtitle-translator glossary list
subtitle-translator glossary set "weathering" "旧化"
subtitle-translator glossary remove "weathering"
subtitle-translator glossary validate
subtitle-translator glossary import terms.json
```

这些命令必须：

- 不调用模型。
- 不修改任何提取或翻译任务状态。
- 不要求挂载媒体目录。
- 写入前验证完整术语表。
- 输出术语数量和文件指纹，便于确认生效版本。

同时保留直接编辑 JSON 的能力，编辑后可使用 `glossary validate` 验证。

## 8. 阶段五：强制保证术语翻译

仅将术语表放入提示词不能保证模型一定遵守，因此需要增加结果校验：

1. 每批只发送该批原文中实际出现的术语。
2. 拉丁字符术语默认忽略大小写，并优先匹配最长短语。
3. 若原文命中术语，译文必须包含指定的目标词。
4. 首次不符合时，只对失败片段执行一次修复请求。
5. 修复后仍不符合时，任务必须明确失败，不得静默发布错误术语。
6. 不使用无条件字符串替换，避免破坏中文语序或处理错误的子串命中。

## 9. 阶段六：云端 API 重试与批次恢复

- 对连接错误、读取超时、429、500 和 503 执行有限次数的指数退避重试。
- 对 400、401、402 和 422 立即失败，不盲目重试。
- 当响应含有 `Retry-After` 时按其要求等待。
- 保持项目既定的任务级串行处理，不为提高 API 并发量而改为并行批处理。
- 原子保存批次级进度，包含原文指纹、翻译配置指纹、已完成字幕 ID 和已校验译文。
- 重试时从第一个未完成批次继续，避免重复调用付费 API。
- 原文、模型、提示词、输出契约或术语表变化时，不得复用旧进度。

## 10. 阶段七：测试与文档

单元测试必须使用假客户端或 `httpx` 模拟传输，不得访问真实 DeepSeek API 或产生费用。

新增覆盖：

- DeepSeek 请求地址、认证头、模型名和非思考模式。
- API Key 不出现在错误、日志或状态中。
- JSON 正常响应、截断、空响应和非法结构。
- 429、500、503 重试，401、402 不重试。
- 批次中断后继续，不重复调用已完成批次。
- 术语表增删改查、旧格式迁移、冲突检查和原子写入。
- 术语约束正常、修复成功和修复失败。
- 修改术语表后配置指纹变化并触发重新翻译。
- 已管理 SRT 的安全替换以及未管理 SRT 的保护。

提交前执行：

```bash
pytest
ruff check .
ruff format --check .
```

真实 DeepSeek API 集成测试必须单独执行，且只在明确配置 API Key、账户余额和专用测试视频后
进行。验收时需要确认：

1. 使用 `deepseek-v4-flash` 完成整个视频的翻译。
2. 字幕片段数量、ID、顺序和时间轴与原文完全一致。
3. 所有命中的术语均使用指定译文。
4. SRT 在媒体目录安全发布。
5. 相同原文和配置再次执行时正确跳过。
6. 修改术语表后相关结果正确失效并重新翻译。

## 11. 安全与运行要求

- 真实 DeepSeek API Key 只能放入 `.env` 或受控的运行环境变量，不得提交。
- 错误日志不得包含 API Key 或完整字幕请求体。
- 切换到云端 API 意味着字幕文本将发送到 DeepSeek，部署和使用前必须确认数据使用范围。
- 批处理和 API 模式在调用模型或修改任务状态前，仍须完成现有媒体挂载与写权限检查。
- 翻译任务仍顺序执行，一个任务失败不得阻止后续任务。

## 12. 完成标准

- 主要翻译路径不再依赖本地 llama-server。
- 所有生产翻译请求均使用 `deepseek-v4-flash`。
- 术语表可通过 CLI 或 JSON 安全维护。
- 命中术语时不会静默发布错误译文。
- 单批 API 失败后可恢复，不重复请求已完成批次。
- API Key 不进入 Git、日志、状态文件或配置指纹。
- 默认自动测试不访问 DeepSeek、不产生 API 费用。
- `pytest`、`ruff check .` 和 `ruff format --check .` 全部通过。
