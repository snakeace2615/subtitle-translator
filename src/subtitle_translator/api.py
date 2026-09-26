from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException

from subtitle_translator.config import Settings, get_settings
from subtitle_translator.llm_client import DeepSeekAPIError
from subtitle_translator.models import TranslationRequest, TranslationResponse
from subtitle_translator.service import TranslationInputError, translate_document


def create_app() -> FastAPI:
    app = FastAPI(title="Subtitle Translator", version="0.1.0")

    @app.get("/health")
    def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
        return {"status": "ok", "model": settings.llm_model}

    @app.post("/v1/translations", response_model=TranslationResponse)
    async def translate(
        request: TranslationRequest,
        settings: Annotated[Settings, Depends(get_settings)],
    ) -> TranslationResponse:
        try:
            document = await translate_document(request, settings)
        except TranslationInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (httpx.HTTPError, DeepSeekAPIError) as exc:
            raise HTTPException(status_code=502, detail=f"DeepSeek request failed: {exc}") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=502, detail=f"Invalid LLM response: {exc}") from exc
        return TranslationResponse(model=settings.llm_model, document=document)

    return app


app = create_app()
