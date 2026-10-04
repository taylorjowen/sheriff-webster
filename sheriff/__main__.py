"""Run the bot: `python -m sheriff` (reads DISCORD_TOKEN from the environment or .env)."""
from __future__ import annotations

import logging
import os
import sys

from .config import PROJECT_ROOT, Config


def load_dotenv(path=PROJECT_ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        sys.exit("DISCORD_TOKEN is not set (put it in .env). Use a BOT token, never a user token.")
    cfg = Config.load()
    from .bot.client import SheriffBot
    SheriffBot(cfg).run(token, log_handler=None)


if __name__ == "__main__":
    main()
