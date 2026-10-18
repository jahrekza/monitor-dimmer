"""Entry point of the Windows build.

Everything is packed into one executable. Which of the programs it runs is
decided by `--role`; double-clicked, with no role at all, it is the
installer, because that is what someone who just downloaded it wants.

The programs themselves are unchanged — the build stages them under
importable names and this module calls their main().
"""
import sys


ROLES = ("setup", "daemon", "overlay", "settings", "uninstall")


def _role():
    argv = sys.argv[1:]
    if "--role" in argv:
        i = argv.index("--role")
        if i + 1 < len(argv):
            role = argv[i + 1]
            # hide the role from the program itself, it knows its own flags
            del sys.argv[i + 1:i + 3]
            return role
    return "setup"


def main():
    role = _role()
    if role not in ROLES:
        sys.exit(f"monitor-dimmer: unknown role {role!r}; expected one of "
                 + ", ".join(ROLES))
    if role == "daemon":
        import daemon_main
        daemon_main.main()
    elif role == "overlay":
        import overlay_main
        overlay_main.main()
    elif role == "settings":
        import wizard_main
        wizard_main.main()
    elif role == "uninstall":
        import uninstall_main
        uninstall_main.main()
    else:
        import setup_main
        setup_main.main()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.\n")
