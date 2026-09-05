"""Upload normalized local Codex usage to a remote DomOS gateway."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from services.codex_usage_service import codex_usage_service  # noqa: E402
from config import settings  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=settings.CODEX_USAGE_SYNC_URL,
        help="DomOS Python gateway base URL",
    )
    parser.add_argument(
        "--token",
        default=settings.CODEX_USAGE_SYNC_TOKEN,
        help="Bearer token configured on the gateway",
    )
    args = parser.parse_args(argv)
    if not args.url:
        raise SystemExit("CODEX_USAGE_SYNC_URL is required (.env or --url)")
    if not args.token:
        raise SystemExit("CODEX_USAGE_SYNC_TOKEN is required")
    return args


async def main() -> None:
    args = parse_args()

    snapshot = await codex_usage_service.collect_local()
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{args.url.rstrip('/')}/api/codex/usage/sync",
            json=snapshot,
            headers={"Authorization": f"Bearer {args.token}"},
        )
        response.raise_for_status()
    print(
        "Codex usage synchronized: "
        f"5h={snapshot['five_hour']['remaining_percent']}% "
        f"weekly={snapshot['weekly']['remaining_percent']}%"
    )


if __name__ == "__main__":
    asyncio.run(main())
