"""Model-selected language; Unicode script inspection never interprets intent."""
from __future__ import annotations

import re
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


# --- Latin Darija writing-system repair (format only; never meaning) ---------

def _arabic_letter(char: str) -> bool:
    return unicodedata.category(char).startswith("L") and "ARABIC" in unicodedata.name(char, "")


def arabic_words(text: str) -> list[str]:
    """Whitespace-separated words containing Arabic letters, in order, without repeats."""
    return list(dict.fromkeys(word for word in text.split() if any(_arabic_letter(c) for c in word)))


_STANDALONE_NUMBER = re.compile(r"(?<![^\W\d_])\d+(?:[.,]\d+)*(?![^\W\d_])")


def kept_references(draft: str, rewrite: str) -> bool:
    """True when every standalone number in the draft (order/case numbers, amounts,
    dates, days) is still in the rewrite. Arabizi digits inside words are ignored."""
    return set(_STANDALONE_NUMBER.findall(draft)) <= set(_STANDALONE_NUMBER.findall(rewrite))


_CONSONANTS = {
    "ب": "b", "ت": "t", "ث": "t", "ج": "j", "ح": "7", "خ": "kh", "د": "d", "ذ": "d", "ر": "r", "ز": "z",
    "س": "s", "ش": "ch", "ص": "s", "ض": "d", "ط": "t", "ظ": "d", "ع": "3", "غ": "gh", "ف": "f", "ق": "9",
    "ك": "k", "ک": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "ڤ": "v", "گ": "g", "ݣ": "g", "پ": "p",
    "چ": "ch", "ڭ": "g", "ء": "",
}
_VOWELS = {"ا": "a", "أ": "a", "إ": "i", "آ": "a", "ى": "a", "ة": "a", "ؤ": "o", "ئ": "i", "ٱ": "a"}
_PUNCTUATION = str.maketrans({"،": ",", "؛": ";", "؟": "?", "٪": "%", **{chr(0x0660 + i): str(i) for i in range(10)},
                              **{chr(0x06F0 + i): str(i) for i in range(10)}})
_SHADDA = "ّ"


def _arabizi_word(word: str) -> str:
    letters = [c for c in word if c == _SHADDA or not (unicodedata.category(c) == "Mn" or c == "ـ")]
    if word.startswith("ال") and len(word) > 3:
        letters = ["l", *letters[2:]]  # definite article
    out: list[str] = []
    base = [c for c in letters if c != _SHADDA]
    position = 0
    for char in letters:
        if char == _SHADDA:
            if out:
                out.append(out[-1])
            continue
        previous = base[position - 1] if position else None
        following = base[position + 1] if position + 1 < len(base) else None
        position += 1
        if char in ("و", "ي", "ی"):
            glide = previous is None or following in _VOWELS or following in ("و", "ي", "ی")
            out.append(("w" if char == "و" else "y") if glide else ("o" if char == "و" else "i"))
        elif char in _VOWELS:
            out.append(_VOWELS[char])
        else:
            out.append(_CONSONANTS.get(char, "" if _arabic_letter(char) else char))  # never emit Arabic
    return "".join(out)


def to_arabizi(text: str) -> str:
    """Last-resort writing-system conversion of remaining Arabic letters to Arabizi.

    Only Arabic-letter runs and Arabic punctuation/digits change; every Latin
    word, number and identifier is kept character for character.
    """
    text = text.translate(_PUNCTUATION)
    return re.sub(r"[؀-ۿݐ-ݿ]+", lambda m: _arabizi_word(m.group(0)), text)
