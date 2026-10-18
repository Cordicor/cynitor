"""The event a decoded (or counted) subject message becomes, as it travels on.

The scanner makes one per message; the telemetry manager caches and broadcasts
it, the event logger writes it to history and recordings, the WebSocket sends
it to the dashboards. Declared here, so that a type checker holds each of them
to one shape, which used to be agreed by convention only.
"""

from typing import Any, Optional, TypedDict


class _SubjectEventExtras(TypedDict, total=False):
    undecoded: bool  # counted from its frames while decoding on demand, not decoded: no attributes


class SubjectEvent(_SubjectEventExtras):
    subject_id: int
    timestamp: str             # local time with the server's UTC offset, to the second
    timestamp_unix: float      # when the transfer was received, as the transport stamped it
    rate: float                # this publisher's messages a second
    subject_rate: float        # the subject's, over every publisher still sending
    message_type: Optional[str]
    attributes: list[dict[str, Any]]  # {"attribute", "value", "unit"?}, values JSON-safe once cached
    publisher_node_id: Optional[int]  # None: an anonymous publisher
    unique_id: Optional[str]
    payload_bytes: Optional[int]
