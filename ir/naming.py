"""Name domains — naming.md.

Three distinct facts, three distinct domains. Do not cross-use these
validators: a pin name and an object (designator/net/channel) name are
governed by different rules for a reason spelled out on naming.md.
"""

from __future__ import annotations

import re

_ATTR_KEY_RE = re.compile(r"^[A-Za-z0-9_#]+(\.[A-Za-z0-9_#]+)*$")


def validate_attr_key(name: str) -> None:
    """BOM.MANF#, SIM.TIME, TOLERANCE — attr.md, naming.md #домен-имени-сужен."""
    if not _ATTR_KEY_RE.match(name):
        raise ValueError(f"attribute key out of domain: {name!r}")


def validate_object_name(name: str) -> None:
    """Designator, channel name, net name — the OBJECT half of >OBJECT@ATTR."""
    if not name or any(ch.isspace() or ch in "@{}" for ch in name):
        raise ValueError(f"name out of domain (no space, '@', '{{', '}}'): {name!r}")


def validate_pin_or_pad_name(name: str) -> None:
    """pin.md / pad.md: no space, no braces. Colon and '@' are legal here."""
    if not name or any(ch.isspace() or ch in "{}" for ch in name):
        raise ValueError(f"pin/pad name out of domain (no space, '{{', '}}'): {name!r}")


def validate_catalog_name(name: str) -> None:
    """library/symbol/footprint/component `name` — naming.md
    #библиотечных-имён-правило-не-касается: exempt from the object-name
    domain above, the only thing forbidden is the pair separator.
    """
    if ":" in name:
        raise ValueError(f"catalog name must not contain ':' (it separates the library:name pair): {name!r}")


def sanitize_out_of_domain(name: str, forbidden: str) -> str:
    """naming.md #имя-вне-домена-чинится-подстановкой: every illegal
    character becomes '_', predictably, one for one.
    """
    return "".join("_" if ch.isspace() or ch in forbidden else ch for ch in name)
