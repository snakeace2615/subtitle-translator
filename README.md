# Subtitle Translator

使用本地大语言模型将带时间轴的原文字幕翻译为中文字幕。这个仓库只负责翻译，不读取视频，也不执行语音识别。

默认调用 llama-server 提供的 OpenAI 兼容接口，适合接入已经部署好的 Qwen3.8 27B。输入和输出均为 subtitle-document/v1 JSON，时间轴和片段 ID 保持不变。

## 本地启动

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e '.[dev]'
    cp .env.example .env
    subtitle-translator

服务默认监听 http://127.0.0.1:8012，API 文档位于 /docs。

## 示例

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

环境变量中的模型名需要与你启动 llama-server 时暴露的模型名一致。

