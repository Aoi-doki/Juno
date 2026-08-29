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

    if not config.anthropic_key:
        logging.warning(
            "ANTHROPIC_API_KEY is not set — Juno will connect and record events "
            "but cannot reply. Set it and restart."
        )

    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
