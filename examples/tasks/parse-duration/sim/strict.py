import re

_PATTERN = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_duration(text: str) -> int:
    m = _PATTERN.fullmatch(text)
    if not text or m is None or not any(m.groups()):
        raise ValueError(f"not a duration: {text!r}")
    h, mins, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mins * 60 + s
