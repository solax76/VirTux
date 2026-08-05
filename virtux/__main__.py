"""Entry point: parse the command line and start the GTK application."""

from __future__ import annotations

import sys

from . import i18n


def _preparse_language(argv: list[str]) -> str | None:
    """Find --lang before anything else is imported.

    Several modules build translated constants (button labels, state names) at
    import time, so the catalog has to be chosen before they are imported —
    which is earlier than argparse can run.
    """
    for index, argument in enumerate(argv):
        if argument == "--lang" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--lang="):
            return argument.split("=", 1)[1]
    return None


def _integrate(install: bool) -> int:
    """Run --install-desktop-entry / --uninstall-desktop-entry and report what changed.

    Imported here rather than at module level so the GTK-free path stays GTK-free.
    """
    from . import integration
    from .i18n import _

    try:
        paths = integration.install() if install else integration.uninstall()
    except OSError as error:
        print(_("Desktop integration failed: {error}").format(error=error), file=sys.stderr)
        return 1

    if not paths:
        print(_("Nothing was installed, so there is nothing to remove."))
        return 0
    template = _("Installed {path}") if install else _("Removed {path}")
    for path in paths:
        print(template.format(path=path))
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    i18n.setup(_preparse_language(arguments))

    # Imported only now, so their translated constants use the chosen catalog.
    import argparse

    from . import __version__
    from .app import VirTuxApplication
    from .i18n import _

    parser = argparse.ArgumentParser(
        prog="virtux",
        description=_("Manage KVM/libvirt virtual machines."),
    )
    parser.add_argument(
        "-c",
        "--connect",
        metavar="URI",
        help=_("libvirt connection URI for this run, e.g. qemu:///system"),
    )
    parser.add_argument(
        "--lang",
        metavar="CODE",
        help=_(
            "interface language: a code such as “it”, or “auto” to follow the "
            "system locale. Defaults to English."
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"VirTux {__version__}")
    desktop = parser.add_mutually_exclusive_group()
    desktop.add_argument(
        "--install-desktop-entry",
        action="store_true",
        help=_(
            "copy the desktop entry and the icon to ~/.local/share, so the application "
            "menu and the task bar show VirTux with its own icon, then exit"
        ),
    )
    desktop.add_argument(
        "--uninstall-desktop-entry",
        action="store_true",
        help=_("remove what --install-desktop-entry copied, then exit"),
    )
    args = parser.parse_args(arguments)

    if args.install_desktop_entry or args.uninstall_desktop_entry:
        return _integrate(install=args.install_desktop_entry)

    application = VirTuxApplication(args.connect)
    # GTK must not see our own arguments.
    return application.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
