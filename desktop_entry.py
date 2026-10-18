"""Ponto de entrada do executável. O PyInstaller analisa este arquivo."""

from ollamapilot.app import main

if __name__ == "__main__":
    raise SystemExit(main())
