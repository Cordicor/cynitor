"""Whether a newer Cynitor release is out, asked of GitHub once at start and once a day.

Only GitHub's public API is asked, for the latest release, and nothing is
sent about the user or the bus. Without internet access the check fails
quietly. --no-update-check, or CYNITOR_NO_UPDATE_CHECK=1, turns it off.
"""

import asyncio
import json
import logging
import re
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RELEASES_URL = "https://api.github.com/repos/OpenCyphal-Garage/cynitor/releases/latest"
RELEASE_PAGE_PREFIX = "https://github.com/OpenCyphal-Garage/cynitor/releases/"
DISABLE_ENV = "CYNITOR_NO_UPDATE_CHECK"


def parse_version(text: object) -> Optional[tuple[int, ...]]:
    """ "v1.10.0" -> (1, 10, 0); None for anything else, a pre-release ("1.4.0-rc1") among them."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(text or "").strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def fetch_latest_release(user_agent: str, timeout: float = 5.0) -> dict:
    """GitHub's answer for the latest release (drafts and pre-releases are never it)."""
    request = urllib.request.Request(RELEASES_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class UpdateCheck:
    """``available``: {"version", "url"} of a release newer than ``current``, else None."""

    CHECK_EVERY_S = 24 * 3600

    def __init__(self, current: str, fetch: Optional[Callable[[], dict]] = None) -> None:
        self.current = current
        self._fetch = fetch or (lambda: fetch_latest_release(f"cynitor-server/{current}"))
        self.available: Optional[dict] = None
        self._task: Optional[asyncio.Task] = None

    async def check(self) -> None:
        try:
            release = await asyncio.to_thread(self._fetch)
            tag, url = release.get("tag_name"), release.get("html_url")
        except Exception as exc:  # offline, a proxy, GitHub's rate limit: no news is no update
            logger.debug("Update check failed: %s", exc)
            return
        latest, current = parse_version(tag), parse_version(self.current)
        if latest is None or current is None or latest <= current \
                or not isinstance(url, str) or not url.startswith(RELEASE_PAGE_PREFIX):
            self.available = None
            return
        version = ".".join(map(str, latest))
        if (self.available or {}).get("version") != version:
            logger.info("Cynitor %s is available (this is %s): %s", version, self.current, url)
        self.available = {"version": version, "url": url}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await self.check()
            await asyncio.sleep(self.CHECK_EVERY_S)
