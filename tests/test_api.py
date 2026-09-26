import asyncio

import httpx

from subtitle_translator.api import create_app
from subtitle_translator.config import Settings, get_settings


def test_empty_subtitles_return_422_before_llm_client_creation(monkeypatch) -> None:
    def unexpected_client(*args):
        raise AssertionError("empty input must not create an LLM client")

    monkeypatch.setattr("subtitle_translator.service.DeepSeekClient", unexpected_client)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)

    async def send_request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/v1/translations",
                json={
                    "document": {"media_file": "empty.mp4", "source_language": "en", "segments": []}
                },
            )

    response = asyncio.run(send_request())
    assert response.status_code == 422
    assert "Source subtitle has no segments" in response.json()["detail"]
