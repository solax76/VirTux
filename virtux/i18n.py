"""Translation setup.

Every user-visible string in VirTux is wrapped in ``_()`` imported from here, so
the UI is English by default but ready to be translated: run
``scripts/update-po.sh`` to refresh ``po/virtux.pot``, translate a copy as
``po/<lang>.po``, then ``scripts/build-mo.sh`` to compile it into ``locale/``.

The UI stays English until a catalog is compiled, even on a non-English system,
because ``locale/`` is a build artifact and is not shipped in the checkout. Pick a
language explicitly with ``--lang it`` or ``VIRTUX_LANG=it``.
"""

from __future__ import annotations

import gettext
import os
from pathlib import Path

DOMAIN = "virtux"

_translation: gettext.NullTranslations = gettext.NullTranslations()


def _localedir() -> str:
    """Prefer the checkout's ``locale/`` directory, else the system one."""
    override = os.environ.get("VIRTUX_LOCALE_DIR")
    if override:
        return override
    local = Path(__file__).resolve().parent.parent / "locale"
    if local.is_dir():
        return str(local)
    return "/usr/share/locale"


def setup(language: str | None = None) -> None:
    """Load the catalog. Call this before importing any module that builds
    translated constants at import time (commands, window, dialogs, widgets).

    ``None`` selects English, ``"auto"`` follows the system locale the way most
    applications do, and anything else is used as a language code.
    """
    global _translation
    if language is None:
        language = os.environ.get("VIRTUX_LANG") or None

    if language is None:
        # English is the default even on a non-English system, as specified. The
        # source strings are English, so a catalog that does not exist is exactly
        # what we want here.
        languages: list[str] | None = ["en"]
    elif language == "auto":
        languages = None  # let gettext read LANGUAGE / LC_ALL / LANG
    else:
        languages = [language]

    _translation = gettext.translation(
        DOMAIN, localedir=_localedir(), languages=languages, fallback=True
    )


def _(message: str) -> str:
    """Translate a string.

    Deliberately a function rather than a rebound ``_translation.gettext``: other
    modules do ``from .i18n import _``, so the name they hold must keep resolving
    the *current* catalog rather than whichever one existed at import time.
    """
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)


__all__ = ["_", "ngettext", "setup", "DOMAIN"]
