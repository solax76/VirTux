"""VirTux — a GTK4 desktop manager for KVM/libvirt virtual machines."""

__version__ = "0.1.0"

# Reverse-DNS application id. It is also the program name, the desktop entry's
# basename and the icon's name: shells pair a window with its desktop entry — and
# so with its icon — by matching these, so all four have to agree.
APP_ID = "it.dirida.VirTux"

__all__ = ["APP_ID", "__version__"]
