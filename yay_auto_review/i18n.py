"""Small, dependency-free message catalogs with deterministic locale selection.

English messages are the stable catalog keys. New languages add a UTF-8 JSON
catalog, language registry entries and locale aliases in normalize_language.
Missing keys or an unavailable catalog fall back to the English message.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
import json
import os
from pathlib import Path
from string import Formatter
from typing import Iterator, Mapping

SUPPORTED_LANGUAGES = ("en", "zh_CN")
LANGUAGE_NAMES = {"en": "English", "zh_CN": "Simplified Chinese"}


def normalize_language(value: str | None) -> str | None:
    """Resolve common locale spellings; unsupported locales return None."""
    if not value or value.lower() == "auto":
        return None
    name = value.split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if name in {"c", "posix"} or name == "en" or name.startswith("en_"):
        return "en"
    if name in {"zh", "zh_cn", "zh_sg", "zh_hans"} or name.startswith("zh_hans_"):
        return "zh_CN"
    return None


def resolve_language(*, environ: Mapping[str, str] | None = None) -> str:
    """Resolve LANG alone; missing and unsupported values use English."""
    env = os.environ if environ is None else environ
    return normalize_language(env.get("LANG")) or "en"


# Capture the language once. Later environment changes cannot change the UI,
# Codex report language, or review cache identity during the process.
_LANGUAGE = resolve_language()


def get_language() -> str:
    return _LANGUAGE


def _fields(message: str) -> tuple[str, ...]:
    return tuple(sorted(field for _, field, _, _ in Formatter().parse(message)
                        if field is not None))


@lru_cache(maxsize=None)
def _catalog(language: str) -> dict[str, str]:
    if language == "en":
        return {}
    try:
        catalog = {}
        directory = Path(__file__).with_name("locales")
        for path in sorted(directory.glob(language + "*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                catalog.update(data)
        return {key: value for key, value in catalog.items()
                if isinstance(key, str) and isinstance(value, str)
                and _fields(key) == _fields(value)}
    except (OSError, UnicodeError, ValueError):
        return {}


def t(message: str, *args: object, **kwargs: object) -> str:
    """Translate before formatting so catalog keys never contain user data."""
    translated = _catalog(get_language()).get(message, message)
    return translated.format(*args, **kwargs) if args or kwargs else translated


@contextmanager
def localized_argparse() -> Iterator[None]:
    """Route argparse's standard headings/errors through the same catalog.

    Used only while the single-threaded CLI constructs and parses arguments;
    restore argparse's process-global gettext hooks immediately afterward.
    """
    import argparse
    plural_name = "_ngettext" if hasattr(argparse, "_ngettext") else "ngettext"
    original = argparse._, getattr(argparse, plural_name)
    argparse._ = t
    setattr(argparse, plural_name, lambda singular, plural, count: t(singular if count == 1 else plural))
    try:
        yield
    finally:
        argparse._ = original[0]
        setattr(argparse, plural_name, original[1])
