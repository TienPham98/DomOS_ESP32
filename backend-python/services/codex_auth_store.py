"""Private, encrypted Codex auth persistence across cloud container restarts.

Only an administrator's CLI can initiate login. No HTTP route exposes login,
credentials, or arbitrary Codex commands. A PostgreSQL lock serializes token
refreshes across rolling deployments and the interactive login helper.
"""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path

from config import settings


class CodexAuthError(RuntimeError):
    pass


def cli_environment() -> dict[str, str]:
    # The usage reader must not inherit LLM API keys or database/MQTT secrets.
    allowed = {
        "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT",
        "WINDIR", "COMSPEC", "TEMP", "TMP", "LANG", "LC_ALL", "CODEX_HOME",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed or key in allowed}
    if settings.CODEX_USAGE_AUTH_DIR:
        env["CODEX_HOME"] = str(Path(settings.CODEX_USAGE_AUTH_DIR).resolve())
    return env


def checked_auth(payload: bytes) -> bytes:
    if len(payload) > 65536:
        raise CodexAuthError("Codex authentication cache is too large")
    try:
        data = json.loads(payload)
        if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
            raise ValueError
        if not data["tokens"].get("access_token"):
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise CodexAuthError("A ChatGPT login is required for Codex usage") from exc
    return payload


def write_private_auth(path: Path, payload: bytes) -> None:
    checked_auth(payload)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
    temporary.chmod(0o600)
    temporary.replace(path)


@asynccontextmanager
async def codex_environment():
    env = cli_environment()
    if not settings.CODEX_USAGE_AUTH_DIR:
        yield env
        return
    if not settings.DATABASE_URL or not settings.CODEX_USAGE_AUTH_ENCRYPTION_KEY:
        raise CodexAuthError("Cloud Codex requires PostgreSQL and an auth encryption key")

    import psycopg
    from cryptography.fernet import Fernet, InvalidToken

    try:
        cipher = Fernet(settings.CODEX_USAGE_AUTH_ENCRYPTION_KEY.encode("ascii"))
    except (ValueError, UnicodeError) as exc:
        raise CodexAuthError("Invalid Codex auth encryption key") from exc

    home = Path(env["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    auth = home / "auth.json"
    connection = None
    try:
        connection = await psycopg.AsyncConnection.connect(settings.DATABASE_URL, connect_timeout=5)
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS domos_codex_auth ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), ciphertext BYTEA NOT NULL, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await connection.commit()
        await connection.execute("SET LOCAL lock_timeout = '5s'")
        await connection.execute("SELECT pg_advisory_xact_lock(1146047827, 1)")
        cursor = await connection.execute("SELECT ciphertext FROM domos_codex_auth WHERE id = 1")
        row = await cursor.fetchone()
        if row:
            write_private_auth(auth, cipher.decrypt(bytes(row[0])))
        else:
            # PostgreSQL is authoritative: a stale pod must not resurrect a
            # session that the administrator logged out from another pod.
            auth.unlink(missing_ok=True)
        try:
            yield env
        finally:
            # Save rotated tokens even if the quota request itself failed.
            # Commit before releasing the distributed lock.
            if auth.exists():
                encrypted = cipher.encrypt(checked_auth(auth.read_bytes()))
                await connection.execute(
                    "INSERT INTO domos_codex_auth (id, ciphertext) VALUES (1, %s) "
                    "ON CONFLICT (id) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()",
                    (encrypted,),
                )
            elif row:
                # The administrator's logout command removed the local cache.
                await connection.execute("DELETE FROM domos_codex_auth WHERE id = 1")
            await connection.commit()
    except (psycopg.Error, InvalidToken, OSError) as exc:
        # Never expose DSNs, tokens, or the CLI auth cache in HTTP errors/logs.
        raise CodexAuthError("Codex authentication storage is unavailable") from exc
    finally:
        if connection is not None:
            await connection.close()
