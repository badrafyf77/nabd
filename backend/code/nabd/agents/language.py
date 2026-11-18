"""Model-selected language; Unicode script inspection never interprets intent."""
from __future__ import annotations

import unicodedata
from typing import Literal

from pydantic import BaseModel

Language = Literal["en", "fr", "ar", "ary", "mixed"]


class TurnLanguage(BaseModel):
    input_language: Language
    reply_language: Literal["en", "fr", "ar", "ary"]
    reply_script: Literal["latin", "arabic"]

    def public(self) -> dict[str, str]:
        # English/French and standard Arabic have fixed writing systems.
        script = {"en": "latin", "fr": "latin", "ar": "arabic"}.get(self.reply_language, self.reply_script)
        return {"input_language": self.input_language, "reply_language": self.reply_language,
                "reply_script": script}


class MessageLanguage(BaseModel):
    message_id: str
    language: Language


def original_script(text: str) -> str | None:
    """Writing systems of letters in exact original text, ignoring numbers/punctuation."""
    scripts = set()
    for char in text:
        if not unicodedata.category(char).startswith("L"):
            continue
        name = unicodedata.name(char, "")
        if "ARABIC" in name:
            scripts.add("arabic")
        elif "LATIN" in name:
            scripts.add("latin")
    if len(scripts) > 1:
        return "mixed"
    return next(iter(scripts), None)
