"""Small, offline script-family detector for moderation quality statistics."""

import unicodedata


def detect_language_group(text: str | None) -> str:
    """Return a privacy-safe language/script group without making a network request."""
    counts = {
        "ko": 0, "ja": 0, "zh": 0, "latin": 0,
        "cyrillic": 0, "arabic": 0, "devanagari": 0, "thai": 0,
    }
    for char in text or "":
        code = ord(char)
        if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            counts["ko"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["ja"] += 1
        elif 0x4E00 <= code <= 0x9FFF:
            counts["zh"] += 1
        elif 0x0400 <= code <= 0x052F:
            counts["cyrillic"] += 1
        elif 0x0600 <= code <= 0x06FF:
            counts["arabic"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0E00 <= code <= 0x0E7F:
            counts["thai"] += 1
        elif "LATIN" in unicodedata.name(char, ""):
            counts["latin"] += 1

    active = sorted(((count, name) for name, count in counts.items() if count), reverse=True)
    if not active:
        return "und"
    total = sum(count for count, _ in active)
    # Han plus Kana is Japanese text even when Han characters are the majority.
    if counts["ja"] and counts["zh"]:
        return "ja"
    if len(active) > 1 and active[1][0] / total >= 0.20:
        return "mixed"
    return active[0][1]
