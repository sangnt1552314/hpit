"""Settings with Hopper defaults.

Precedence: real environment variable > .env file > default.
The .env file location comes from HPIT_ENV_FILE; otherwise the first that
exists of ~/.config/hpit/.env and the .env in the HPIT checkout.
"""

import getpass
import os
import stat
from typing import Dict, Optional

# Keys whose values must never be shown, logged, or put in os.environ.
SECRET_KEYS = ("HPIT_AMGR_PASSWORD",)


def _read_env_file(path: str) -> Dict[str, str]:
    """Parse simple KEY=VALUE lines; '#' comments and quotes allowed."""
    values = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                values[key] = value
    except OSError:
        pass
    return values


def _env_file_problem(path: str) -> Optional[str]:
    """Why secrets in this file must not be used, or None if it is safe."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} is readable by other users; run: chmod 600 {path}"
    return None


def _find_env_file() -> str:
    if os.environ.get("HPIT_ENV_FILE"):
        return os.path.expanduser(os.environ["HPIT_ENV_FILE"])
    checkout = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    candidates = [
        os.path.expanduser("~/.config/hpit/.env"),
        os.path.join(checkout, ".env"),
    ]
    return next((p for p in candidates if os.path.exists(p)), candidates[0])


ENV_FILE = _find_env_file()
ENV_FILE_PROBLEM = _env_file_problem(ENV_FILE)
_FILE_VALUES = _read_env_file(ENV_FILE)


def _get(name: str, default: str) -> str:
    return os.environ.get(name, _FILE_VALUES.get(name, default))


def _int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _flag(name: str) -> bool:
    return _get(name, "").lower() not in ("", "0", "false", "no")


def secret(name: str) -> Optional[str]:
    """A secret from the .env file, only if the file is private (chmod 600)."""
    if ENV_FILE_PROBLEM:
        return None
    return _FILE_VALUES.get(name) or None


USER = getpass.getuser()
CLUSTER_NAME = _get("HPIT_CLUSTER_NAME", "Hopper")
SCRATCH = _get("HPIT_SCRATCH", f"/scratch/{USER}")

# Seconds between automatic job refreshes; 0 disables auto-refresh.
REFRESH_INTERVAL = _int("HPIT_REFRESH_INTERVAL", 60)
LOG_LINES = _int("HPIT_LOG_LINES", 200)

MOCK = _flag("HPIT_MOCK")
DEBUG = _flag("HPIT_DEBUG")

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "hpit",
)


def settings() -> dict:
    if not os.path.exists(ENV_FILE):
        env_file = f"{ENV_FILE} (not found)"
    elif ENV_FILE_PROBLEM:
        env_file = f"{ENV_FILE} (unsafe: {ENV_FILE_PROBLEM.split('; ')[1]})"
    else:
        env_file = ENV_FILE

    return {
        "HPIT_ENV_FILE": env_file,
        "HPIT_CLUSTER_NAME": CLUSTER_NAME,
        "HPIT_SCRATCH": SCRATCH,
        "HPIT_REFRESH_INTERVAL": str(REFRESH_INTERVAL),
        "HPIT_LOG_LINES": str(LOG_LINES),
        "HPIT_MOCK": "on" if MOCK else "off",
        "HPIT_DEBUG": "on" if DEBUG else "off",
        "HPIT_AMGR_PASSWORD": "set" if secret("HPIT_AMGR_PASSWORD") else "not set",
    }
