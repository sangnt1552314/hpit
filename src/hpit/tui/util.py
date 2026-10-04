from datetime import datetime
from typing import Any, Callable, Optional, Tuple

from rich.text import Text

from hpit.core import config
from hpit.core.errors import HPITError
from hpit.core.models import Job
from hpit.tui.theme import MUTED, STATE_STYLES


def call_backend(fn: Callable, *args: Any, **kwargs: Any) -> Tuple[Any, Optional[str]]:
    """Run a backend call; return (result, None) or (None, error message).

    Expected failures carry readable messages. Anything else is a bug:
    it is shown briefly, or raised with a traceback when HPIT_DEBUG=1.
    """
    try:
        return fn(*args, **kwargs), None
    except HPITError as exc:
        return None, str(exc)
    except Exception as exc:
        if config.DEBUG:
            raise
        return None, f"Unexpected error: {exc!r} (run with HPIT_DEBUG=1 for details)"


def state_text(job: Job) -> Text:
    icon, color = STATE_STYLES.get(job.state, ("•", MUTED))
    return Text(f"{icon} {job.state_name}", style=color)


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def format_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def format_age(timestamp: float) -> str:
    seconds = int(datetime.now().timestamp() - timestamp)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    days = seconds // 86400
    return "1 day ago" if days == 1 else f"{days} days ago"


def usage_bar(fraction: float, width: int, color: str = "#61AFEF") -> Text:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    bar = Text("█" * filled, style=color)
    bar.append("░" * (width - filled), style="#3A3F4B")
    return bar


def key_values(rows) -> Text:
    """Render (label, value) pairs as aligned lines."""
    width = max((len(label) for label, _ in rows), default=0) + 3
    text = Text()
    for i, (label, value) in enumerate(rows):
        if i:
            text.append("\n")
        text.append(label.ljust(width), style=MUTED)
        text.append_text(value if isinstance(value, Text) else Text(str(value)))
    return text
