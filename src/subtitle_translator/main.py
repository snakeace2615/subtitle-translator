import uvicorn

from subtitle_translator.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "subtitle_translator.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    run()

