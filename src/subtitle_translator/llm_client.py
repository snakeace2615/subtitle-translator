import json
from collections.abc import Sequence

import httpx

from subtitle_translator.config import Settings
from subtitle_translator.models import SubtitleSegment, TranslatedItem


SYSTEM_PROMPT = """你是专业字幕翻译器。将输入字幕翻译为简体中文。
要求：
1. 保留语气、专有名词和上下文，不要解释。
2. 译文应简洁、自然，适合屏幕阅读。
3. 必须返回 JSON 数组，每项只有 id 和 text；id 不得改变或遗漏。
4. 不要返回 Markdown 代码块或数组以外的内容。
"""


class QwenClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def translate_batch(
        self,
        segments: Sequence[SubtitleSegment],
        source_language: str,
        target_language: str,
        glossary: dict[str, str],
    ) -> list[TranslatedItem]:
        payload = {
            "source_language": source_language,
            "target_language": target_language,
            "glossary": glossary,
            "subtitles": [{"id": item.id, "text": item.text} for item in segments],
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        body = {
            "model": self.settings.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        async with httpx.AsyncClient(timeout=self.settings.timeout_seconds) as client:
            response = await client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        parsed = json.loads(self._strip_code_fence(content))
        translated = [TranslatedItem.model_validate(item) for item in parsed]
        expected_ids = [item.id for item in segments]
        actual_ids = [item.id for item in translated]
        if actual_ids != expected_ids:
            raise ValueError(f"Model changed subtitle ids: expected {expected_ids}, got {actual_ids}")
        return translated

    @staticmethod
    def _strip_code_fence(content: str) -> str:
        value = content.strip()
        fence = chr(96) * 3
        if value.startswith(fence):
            lines = value.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == fence:
                return "\n".join(lines[1:-1]).strip()
        return value

