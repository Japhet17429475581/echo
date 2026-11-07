"""Use packaged runtime files in a wheel and repository files in an editable checkout."""

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent
_BUNDLE = _PACKAGE / "_runtime"
ROOT = _BUNDLE if _BUNDLE.is_dir() else _PACKAGE.parents[1]
