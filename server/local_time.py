"""The server's local time as an API gives it: ISO 8601 with its UTC offset."""

import datetime
from typing import Union


def local_isoformat(value: Union[datetime.datetime, float], timespec: str = "auto") -> str:
    """``value`` (a naive local datetime, or a Unix time) with the server's UTC offset.

    A viewer in another timezone reads a string without an offset as its own
    local time. Windows cannot convert times near 1970 to local time (OSError);
    those, which only tests make, take the offset in force now.
    """
    if not isinstance(value, datetime.datetime):
        value = datetime.datetime.fromtimestamp(value)
    try:
        aware = value.astimezone()
    except (OSError, OverflowError, ValueError):
        aware = value.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
    return aware.isoformat(timespec=timespec)
