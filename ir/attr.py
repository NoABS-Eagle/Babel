"""<attr> — attr.md."""

from __future__ import annotations

from dataclasses import dataclass

from .naming import validate_attr_key


@dataclass
class Attr:
    name: str
    value: str = ""

    def __post_init__(self) -> None:
        validate_attr_key(self.name)

    @property
    def is_formula(self) -> bool:
        """value starting with '=' — placeholders.md #вычисление."""
        return self.value.startswith("=")

    @property
    def namespace(self) -> str | None:
        """Part before the first dot — attr.md #предназначение--префикс-имени."""
        head, sep, _ = self.name.partition(".")
        return head if sep else None
