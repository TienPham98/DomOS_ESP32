"""Small, deterministic vocabulary for launching DomOS apps from speech."""

import re
import unicodedata


APP_CONFIRMATIONS = {
    "codex-credit": "Đã mở thông tin hạn mức Codex rồi nhé!",
    "man-utd": "Đã mở lịch thi đấu Manchester United rồi nhé!",
    "wallpaper": "Đã mở ứng dụng hình nền rồi nhé!",
    "clock": "Đã mở ứng dụng đồng hồ rồi nhé!",
}
APP_ALIASES = {
    "codex-credit": r"(?:codex|co dex|code x)(?: credit(?: checking)?| usage| han muc| gioi han)?|code credit(?: checking)?",
    "man-utd": r"manchester(?: united)?|man utd|mu thi dau",
    "wallpaper": r"wallpaper|hinh nen",
    "clock": r"clock|dong ho",
}
# User-defined shortcuts are complete commands, not generic football aliases.
# A request for another team's schedule must not silently open Manchester United.
APP_COMMAND_SHORTCUTS = {
    "kiem tra lich thi dau bong da": "man-utd",
    "kiem tra codex credit": "codex-credit",
}
APP_COMMAND_HELP = (
    "Dom chưa nhận ra ứng dụng cần mở. Bạn có thể nói: mở Codex Credit, "
    "mở Manchester United, mở đồng hồ hoặc mở hình nền."
)
APP_LAUNCH_FAILED = "Dom chưa mở được ứng dụng trên thiết bị. Bạn thử lại nhé."


def normalize_command(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold()).replace("đ", "d")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _is_non_command(text: str) -> bool:
    return bool(re.search(
        r"\b(?:dung (?:mo|bat|khoi dong|chuyen|hien thi|kiem tra|xem)|"
        r"khong (?:mo|bat|can|muon|duoc|kiem tra|xem)|chua|do not|don t|not|never|"
        r"tai sao|vi sao|la gi|nhu the nao|huong dan|how to|why|"
        r"cach (?:de )?(?:mo|bat))\b", text,
    ))


def requested_app(transcript: str) -> str | None:
    """Require a known app and a command, or a short app-name utterance.

    Do not fuzzy-match arbitrary words such as 'connect' or financial credit.
    The narrow 'tracking topic credit' alias is a recorded on-device STT miss.
    """
    text = normalize_command(transcript)
    if _is_non_command(text):
        return None
    for command, app in APP_COMMAND_SHORTCUTS.items():
        if re.fullmatch(
            rf"(?:hey dom )?{command}(?: (?:cho|giup) (?:toi|minh))?(?: nhe| nha)?",
            text,
        ):
            return app
    if re.fullmatch(r"(?:mo |mo app |mo ung dung )?tracking topic credit", text):
        return "codex-credit"
    apps = [app for app, alias in APP_ALIASES.items()
            if re.search(rf"\b(?:{alias})\b", text)]
    if len(apps) != 1:
        return None
    app = apps[0]
    action = re.search(
        r"\b(?:mo|bat|khoi dong|chuyen sang|chuyen qua|chuyen den|"
        r"hien thi|xem|kiem tra|check|checking|track|tracking|open|launch|show)\b", text,
    )
    short_name = re.fullmatch(
        rf"(?:app |ung dung )?(?:{APP_ALIASES[app]})(?: app| checking| tracking)?", text,
    )
    return app if action or short_name else None


def is_unresolved_launch_request(transcript: str) -> bool:
    text = normalize_command(transcript)
    return not _is_non_command(text) and bool(re.search(
        r"\b(?:mo|bat|khoi dong|open|launch) (?:app|ung dung|application)\b", text,
    ))


def claims_app_launch(answer: str) -> bool:
    """Reject common fabricated confirmations when no app tool was executed."""
    text = normalize_command(answer)
    app_vocabulary = "|".join(APP_ALIASES.values())
    if not re.search(rf"\b(?:app|ung dung|application|{app_vocabulary})\b", text):
        return False
    return bool(re.search(
        r"\b(?:da (?:mo|bat|khoi dong|chuyen sang)|"
        r"(?:app|ung dung) .{0,60}(?:dang mo|da duoc mo)|"
        r"(?:opened|launched) (?:the )?(?:app|application|codex)|"
        r"(?:app|application) .{0,60}(?:opened|launched))\b", text,
    ))
