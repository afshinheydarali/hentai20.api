import builtins
import mimetypes
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from rubpy import Client as RubikaClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

SESSION_NAME = os.getenv("RUBIKA_SESSION", "rubika_session").strip() or "rubika_session"
SESSION_PATH = Path(SESSION_NAME)
if not SESSION_PATH.is_absolute():
    SESSION_PATH = PROJECT_ROOT / SESSION_PATH

RUBPY_SESSION_PATH = SESSION_PATH.with_suffix("") if SESSION_PATH.suffix == ".rp" else SESSION_PATH
SESSION_WORKDIR = RUBPY_SESSION_PATH.parent
SESSION_CLIENT_NAME = RUBPY_SESSION_PATH.name

DEFAULT_TARGET = os.getenv("RUBIKA_TARGET", "me").strip() or "me"
CAPTION_LIMIT = int(os.getenv("RUBIKA_CAPTION_LIMIT", "900") or "900")


@contextmanager
def pushd(path: Path):
    previous = Path.cwd()
    path.mkdir(parents=True, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def session_candidates(session_path: Path) -> list[Path]:
    base = session_path.with_suffix("") if session_path.suffix == ".rp" else session_path
    return [
        session_path,
        Path(f"{base}.rp"),
        base,
        Path(f"{base}.session"),
        Path(f"{base}.sqlite"),
        Path(f"{base}.rub"),
    ]


def has_session() -> bool:
    return any(path.exists() for path in session_candidates(SESSION_PATH))


def _read_value(obj: Any, key: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _find_guid(obj: Any) -> Optional[str]:
    if obj is None:
        return None

    for key in ["user_guid", "object_guid", "chat_guid", "group_guid", "channel_guid", "guid"]:
        value = _read_value(obj, key)
        if isinstance(value, str) and value:
            return value

    for container_key in ["user", "data", "result", "User", "me"]:
        found = _find_guid(_read_value(obj, container_key))
        if found:
            return found

    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_guid(item)
            if found:
                return found

    return None


def _find_message_id(obj: Any) -> str:
    if obj is None:
        return ""

    for key in ["message_id", "msg_id", "mid"]:
        value = _read_value(obj, key)
        if value:
            return str(value)

    for container_key in ["message", "data", "result", "update"]:
        found = _find_message_id(_read_value(obj, container_key))
        if found:
            return found

    if isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_message_id(item)
            if found:
                return found

    return ""


def guarded_start(client: RubikaClient, allow_interactive_login: bool = False) -> None:
    if allow_interactive_login:
        client.start()
        return

    def fail_input(prompt: str = "") -> str:
        raise RuntimeError(
            "Rubika session is missing or invalid. Run `python app/uploaders/rubika.py login` "
            "once on the server, then restart the bot."
        )

    old_input = builtins.input
    builtins.input = fail_input
    try:
        client.start()
    finally:
        builtins.input = old_input


def resolve_target(client: RubikaClient, target: str) -> str:
    target = (target or DEFAULT_TARGET or "me").strip()
    if target.lower() not in {"me", "self", "saved", "saved_messages", "saved-messages"}:
        return target

    me = client.get_me()
    guid = _find_guid(me)
    if not guid:
        raise RuntimeError(
            f"Could not resolve RUBIKA_TARGET={target!r}. Set RUBIKA_TARGET to your Rubika user_guid."
        )
    return guid


def guess_mime(file_path: Path) -> str:
    explicit = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".zip": "application/zip",
        ".rar": "application/vnd.rar",
        ".7z": "application/x-7z-compressed",
    }
    suffix = file_path.suffix.lower()
    if suffix in explicit:
        return explicit[suffix]
    return mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"


def upload_file(
    file_path: Path,
    caption: str = "",
    target: str = "",
    allow_interactive_login: bool = False,
) -> str:
    file_path = Path(file_path)
    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError(f"File not found: {file_path}")

    if not allow_interactive_login and not has_session():
        raise RuntimeError(
            "Rubika session file was not found. Run `python app/uploaders/rubika.py login` "
            "once on the server before using the bot uploader."
        )

    client = RubikaClient(name=SESSION_CLIENT_NAME)

    with pushd(SESSION_WORKDIR):
        try:
            guarded_start(client, allow_interactive_login=allow_interactive_login)
            target_guid = resolve_target(client, target or DEFAULT_TARGET)
            safe_caption = (caption or "")[:CAPTION_LIMIT] or None
            result = client.send_message(
                object_guid=target_guid,
                text=safe_caption,
                file_inline=str(file_path),
                type="File",
                thumb=False,
                file_name=file_path.name,
                mime=guess_mime(file_path),
            )
            message_id = _find_message_id(result)
            if message_id:
                return f"Rubika sent to {target_guid} | message_id={message_id}"
            return f"Rubika sent to {target_guid}"
        finally:
            try:
                client.disconnect()
            except Exception:
                pass


def login() -> None:
    client = RubikaClient(name=SESSION_CLIENT_NAME)
    with pushd(SESSION_WORKDIR):
        try:
            guarded_start(client, allow_interactive_login=True)
            me = client.get_me()
            guid = _find_guid(me) or "unknown"
            print(f"Rubika login successful. user_guid={guid}")
            print(f"Session path: {SESSION_PATH}")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "login":
        login()
        return

    if len(sys.argv) < 2:
        print("Usage: python app/uploaders/rubika.py login", file=sys.stderr)
        print("   or: python app/uploaders/rubika.py /path/to/file.zip [caption]", file=sys.stderr)
        raise SystemExit(1)

    file_path = Path(sys.argv[1])
    caption = " ".join(sys.argv[2:]).strip()
    print(upload_file(file_path, caption=caption))


if __name__ == "__main__":
    main()
