"""Open the desktop client; command-line arguments retain the existing CLI."""

import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] != "--gui":
        from hanzi_mask.cli import main as run
        return run(args)
    from ui.window import main as run
    return run(args[1:] if args else [])


if __name__ == "__main__":
    raise SystemExit(main())
