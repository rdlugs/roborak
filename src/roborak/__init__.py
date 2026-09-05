"""roborak - AI code review from the terminal."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("roborak")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
