"""Administrator-only device login, status and logout for the cloud usage reader."""

import argparse
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings  # noqa: E402
from services.codex_auth_store import CodexAuthError, codex_environment  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("login", "status", "logout"), default="status", nargs="?")
    args = parser.parse_args()
    if not settings.CODEX_USAGE_AUTH_DIR:
        raise SystemExit("CODEX_USAGE_AUTH_DIR is required; this helper never changes the PC profile")
    command = [settings.CODEX_CLI_PATH, "-c", 'cli_auth_credentials_store="file"']
    if args.action == "login":
        command += ["login", "--device-auth"]
    elif args.action == "status":
        command += ["login", "status"]
    else:
        command += ["logout"]
    try:
        async with codex_environment() as env:
            process = await asyncio.create_subprocess_exec(*command, env=env)
            try:
                return await asyncio.wait_for(process.wait(), timeout=900)
            finally:
                if process.returncode is None:
                    process.terminate()
                    await process.wait()
    except CodexAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
