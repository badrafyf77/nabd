"""Model-selected language; Unicode script inspection never interprets intent."""
from __future__ import annotations

import unicodedata
from typing import Literal

from pydantic import BaseModel, Field

Language = Literal["en", "fr", "ar", "ary", "mixed"]


class TurnLanguage(BaseModel):
    input_language: Language
    reply_language: Literal["en", "fr", "ar", "ary"]
    reply_script: Literal["latin", "arabic"]
    script_requested: bool = Field(default=False, description="True only when the customer explicitly requests "
                                  "a response script different from their current writing system, including "
                                  "a still-active explicit preference from history. Otherwise false.")

    def public(self, source_script: str | None = None) -> dict[str, str]:
        # English/French and standard Arabic have fixed writing systems.
        script = {"en": "latin", "fr": "latin", "ar": "arabic"}.get(self.reply_language, self.reply_script)
        if self.reply_language == "ary" and not self.script_requested and source_script in ("latin", "arabic"):
            script = source_script  # writing-system contract; no language/intent classification here
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


def violates_latin_script(text: str, language: dict[str, str] | None) -> bool:
    return bool(language and language.get("reply_language") == "ary" and language.get("reply_script") == "latin"
                and any(unicodedata.category(c).startswith("L") and "ARABIC" in unicodedata.name(c, "")
                        for c in text))
