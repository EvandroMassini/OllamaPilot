import sys


def _headless(argv: list[str]) -> bool:
    return any(arg == "--mensagem" or arg.startswith("--mensagem=") for arg in argv)


if _headless(sys.argv[1:]):
    from ollamapilot.cli import main
else:
    from ollamapilot.app import main

if __name__ == "__main__":
    raise SystemExit(main() if not _headless(sys.argv[1:]) else main(sys.argv[1:]))
