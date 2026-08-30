# Subtitle Translator

使用 DeepSeek API 将带时间轴的原文字幕翻译为中文字幕。这个仓库只负责翻译，不读取视频，
也不执行语音识别。

生产翻译固定使用 `deepseek-v4-flash` 的 OpenAI 兼容 Chat Completions 接口，显式关闭
思考模式并启用 JSON 输出。输入和输出均为 `subtitle-document/v1` JSON，时间轴和片段 ID
保持不变。

## 运行条件

翻译端不会调用 Whisper，也不会自行创建提取任务。运行批处理前需要满足：

- 提取端已经在 `/home/simon/subtitle-output/jobs/` 中生成状态为 `complete` 的
  `extract.state.json` 和有效的 `source.subtitle.json`。
- 已创建可用的 DeepSeek API Key，账户具备足够余额，并确认字幕文本允许发送到 DeepSeek。
- `/home/simon/modeling-video` 已挂载且可写，以便在视频旁边原子发布 SRT；程序不会修改
  视频文件。

手工集成测试前可以用官方模型列表接口确认模型标识：

```bash
curl https://api.deepseek.com/models \
  -H "Authorization: Bearer $SUBTITLE_TRANSLATOR_LLM_API_KEY"
```

返回结果必须包含 `deepseek-v4-flash`。

## 安装与配置

```bash
cd /home/simon/ai/subtitle-translator/subtitle-translator
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

至少填写 `.env` 中的 `SUBTITLE_TRANSLATOR_LLM_API_KEY`，并检查共享数据目录、媒体根目录
和挂载配置。API 密钥及机器特有的路径只放在 `.env` 中，不要提交到仓库。API Key 为空、
仍为本地占位值、模型不是 `deepseek-v4-flash` 或思考模式未关闭时，生产批处理会在修改任务
状态前终止。

## 批量翻译

直接运行默认命令，或显式使用 `scan`：

```bash
subtitle-translator
# 等价命令
subtitle-translator scan
```

默认命令会扫描一次共享任务目录，顺序翻译所有符合条件的任务，输出 JSON 汇总后退出。
它不会轮询目录，也不会常驻后台。示例汇总：

```json
{
  "status": "complete",
  "discovered": 3,
  "completed": 1,
  "skipped": 1,
  "failed": 0,
  "busy": 1
}
```

退出码含义：

- `0`：本次运行没有失败任务。
- `1`：至少一个任务处理失败；其他可处理任务仍会继续运行。
- `2`：挂载、配置或扫描初始化失败，批处理没有正常启动。

需要诊断任务选择、LLM 请求或发布问题时，将全局日志参数放在子命令之前：

```bash
subtitle-translator --log-level DEBUG scan
```

## 只翻译一个视频

只测试一个视频时，可以不传视频名称：

```bash
subtitle-translator translate-one
```

程序会按任务路径排序，选择第一个尚未完成、未超过重试次数且不存在 SRT 命名冲突的有效
任务。也可以传入 `extract.state.json` 中精确的 `source.relative_path` 来指定视频：

```bash
subtitle-translator translate-one "Tanks/Painting Panther.mp4"
```

两种方式都只创建或更新所选视频的翻译状态、内部字幕 JSON 和最终 SRT，不处理或修改其他任务。
它仍会执行正常的挂载、输入校验、任务锁、配置指纹和 SRT 命名冲突检查。找不到对应任务或
发现多个匹配任务时以启动错误退出。

参数必须是 `extract.state.json` 中 `source.relative_path` 的精确值，而不是绝对路径，也不是
只包含文件名的模糊匹配。

## 挂载与输出

程序启动时会先确认 `/home/simon/modeling-video` 已挂载且实际可写。默认按照
`.env.example` 将 Windows 的 `Y:\模型制作视频` 以 `drvfs,rw` 挂载到该目录；未挂载时
会调用 `sudo mount`，终端可能要求输入 sudo 密码。挂载或写权限检查失败时程序以退出码
2 结束，不扫描任务，也不调用 LLM。将
`SUBTITLE_TRANSLATOR_MEDIA_MOUNT_SOURCE` 留空可禁用自动挂载，但配置的媒体根目录仍须
存在且可写。

翻译结果保存在 `/home/simon/subtitle-output/jobs/<分片>/<job_id>/`：

```text
translate.zh-CN.state.json
zh-CN.subtitle.json
```

最终 SRT 发布到视频所在目录，例如：

```text
/home/simon/modeling-video/Tanks/Painting Panther.mp4
/home/simon/modeling-video/Tanks/Painting Panther.srt
```

相同原文和翻译配置再次运行时会跳过；若内部翻译 JSON 有效而 SRT 发布失败，下次只重试
发布，不再次调用 LLM。程序不会覆盖来源不明或被用户修改过的 SRT。

批处理默认读取纳入版本控制的 `config/glossary.json`。正式格式是版本化 UTF-8 JSON：

```json
{
  "version": 1,
  "terms": [
    {
      "source": "weathering",
      "target": "旧化",
      "aliases": ["weathered", "weathering effects"],
      "case_sensitive": false
    }
  ]
}
```

仍可读取旧的 `{"weathering":"旧化"}` 扁平对象。使用下列命令可安全维护或迁移术语表；
这些命令不检查媒体挂载、不调用模型，也不修改任务状态：

```bash
subtitle-translator glossary list
subtitle-translator glossary set "weathering" "旧化"
subtitle-translator glossary remove "weathering"
subtitle-translator glossary validate
subtitle-translator glossary import terms.json
```

每条命令输出术语数量和规范化内容指纹。翻译时只发送当前批次实际命中的术语；若首次响应
按原顺序漏掉部分 ID、译文为空、缺少 `text`，或未包含指定目标词，只对失败片段修复一次并
合并回原批次，仍失败则拒绝发布。新增、重复或重排 ID 不会自动修复。修改术语表、模型、
提示词版本或其他影响译文的配置后，配置指纹会变化，已完成任务将在下次扫描时重新翻译。

每个成功批次都会原子保存到 `translate.<locale>.progress.json`。请求中断后从第一个未完成
批次继续；原文或翻译配置变化时不会复用旧进度。完整翻译 JSON 安全写入后会删除进度文件。

## 可选 API

    subtitle-translator api

API 默认监听 http://127.0.0.1:8012，交互文档位于 `/docs`。

## API 示例

    curl -X POST http://127.0.0.1:8012/v1/translations \
      -H 'Content-Type: application/json' \
      -d '{
        "document": {
          "schema_version": "subtitle-document/v1",
          "media_file": "demo.mp4",
          "source_language": "en",
          "segments": [{"id": 0, "start": 0, "end": 2, "text": "Hello world"}]
        },
        "target_language": "zh-CN",
        "glossary": {"weathering": "旧化"}
      }'

API 请求同样固定使用 `deepseek-v4-flash`。

## 测试方法

### 自动测试

默认测试全部使用临时目录、假客户端或 `httpx` 模拟传输，不访问 DeepSeek、不产生费用，
也不需要 GPU、SMB 挂载或提取端的 Whisper 环境，不会修改真实任务、视频或 SRT：

```bash
cd /home/simon/ai/subtitle-translator/subtitle-translator
source .venv/bin/activate
pytest
```

可以按范围运行：

```bash
pytest tests/test_batch.py -q       # 扫描、跳过、重试、锁和安全发布
pytest tests/test_service.py -q     # 翻译分批及时间轴保持
pytest tests/test_llm_client.py -q  # DeepSeek 请求、响应和重试
pytest tests/test_glossary.py -q    # 术语表契约、匹配和 CLI
pytest tests/test_srt.py -q         # SRT 渲染和时间格式
pytest tests/test_mounting.py -q    # 挂载与写权限检查
pytest tests/test_main.py -q        # CLI、汇总和退出码
```

提交前同时执行静态检查和格式检查：

```bash
ruff check .
ruff format --check .
```

### DeepSeek 手工集成测试

该测试会调用真实 DeepSeek API、发送字幕文本并产生费用，也会写入所选任务的翻译状态、
内部 JSON 和最终 SRT。只应在明确配置 API Key、确认账户余额和数据使用范围后，对专门准备
的测试视频执行，并先确认视频旁边没有需要保留的同名 SRT。

```bash
# 1. 确认官方模型列表
curl https://api.deepseek.com/models \
  -H "Authorization: Bearer $SUBTITLE_TRANSLATOR_LLM_API_KEY"

# 2. 精确选择一个已经完成提取的任务
subtitle-translator --log-level DEBUG translate-one "Tanks/Painting Panther.mp4"

# 3. 再运行一次；输入和配置未变化时应显示 skipped，且不再调用 LLM
subtitle-translator translate-one "Tanks/Painting Panther.mp4"
```

验收时检查以下内容：

- job 目录中的 `translate.zh-CN.state.json` 状态为 `complete`。
- `zh-CN.subtitle.json` 的片段数量、ID、顺序和时间轴与 `source.subtitle.json` 一致，只有文本
  和目标语言发生变化。
- 视频目录中生成同名 `.srt`，播放器能够正常显示中文和时间轴。
- 第二次运行汇总为 `skipped: 1`；改变词汇表或翻译配置后会重新翻译。

### API 手工测试

在一个终端启动服务：

```bash
subtitle-translator api
```

在另一个终端检查健康状态并发送 README 上面的示例请求：

```bash
curl http://127.0.0.1:8012/health
curl http://127.0.0.1:8012/docs
```

API 测试只验证单个 JSON 文档的翻译接口；共享任务扫描、状态恢复和 SRT 发布应使用
`translate-one` 或 `scan` 验证。

### 两端端到端测试

1. 将一个短测试视频放入提取端配置的媒体目录。
2. 在 `subtitle-extractor` 仓库运行 `subtitle-extractor`，确认对应 job 中生成完整的
   `source.subtitle.json`。
3. 在本仓库运行 `subtitle-translator translate-one "<媒体相对路径>"`。
4. 按上述验收项检查翻译状态、内部字幕 JSON 和视频旁边的 SRT。

完整的批处理状态、安全覆盖和故障恢复约定见
[`docs/BATCH_TRANSLATION_DESIGN.md`](docs/BATCH_TRANSLATION_DESIGN.md)。
