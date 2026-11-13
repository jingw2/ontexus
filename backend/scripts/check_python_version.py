"""Enforce the supported Python floor before any third-party import."""

import sys

MIN_SUPPORTED = (3, 11)
MAX_SUPPORTED_EXCLUSIVE = (3, 13)
UNSUPPORTED_PYTHON_VERSION = "UNSUPPORTED_PYTHON_VERSION"


def require_supported_python():
    if sys.version_info[:2] < MIN_SUPPORTED or sys.version_info[:2] >= MAX_SUPPORTED_EXCLUSIVE:
        detected = ".".join(str(part) for part in sys.version_info[:3])
        minimum = ".".join(str(part) for part in MIN_SUPPORTED)
        maximum = ".".join(str(part) for part in MAX_SUPPORTED_EXCLUSIVE)
        raise SystemExit(
            f"{UNSUPPORTED_PYTHON_VERSION}: detected Python {detected}, "
            f"required >= {minimum}, < {maximum}"
        )
    return True


if __name__ == "__main__":
    require_supported_python()
