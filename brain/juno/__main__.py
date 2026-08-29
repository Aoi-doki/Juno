"""Entry point: ``python -m juno``."""

from __future__ import annotations

import logging
import sys

import uvicorn

from juno.config import Config
from juno.orchestrator import create_app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        config = Config.load()
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    usable = [
        name for name, spec in config.models.engines.items()
        if (spec.kind == "openai" and spec.base_url) or (spec.kind == "anthropic" and spec.api_key)
    ]
    if not usable:
        logging.warning(
            "No thinking engine is configured — Juno will connect and record "
            "events but cannot reply. Set GEMINI_API_KEY, or run Ollama locally."
        )

    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
