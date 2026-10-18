"""Subject types the user sets, for subjects no register names; kept across sessions, and guessed from payloads.

Held by the session (main.CANSession) as ``user_types``. A type set here
decodes the subject from then on and in later sessions (the data folder's
SUBJECT_TYPES_FILE); ``guess`` listens to an undecoded subject and ranks the
compiled types its payloads fit (see type_guess).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from data_dir import SUBJECT_TYPES_FILE
from type_guess import load_candidates, rank_types

if TYPE_CHECKING:
    from main import CANSession

logger = logging.getLogger(__name__)


class UserSubjectTypes:
    """The subject types the user set, saved in the session's data folder."""

    def __init__(self, session: CANSession) -> None:
        self._session = session

    GUESS_LISTEN_S = 3.0  # how long guess listens for payloads

    @property
    def file(self) -> Path:
        return self._session.data_dir / SUBJECT_TYPES_FILE

    def saved(self) -> dict[int, str]:
        """{subject_id: DSDL type name} as saved in the data folder."""
        try:
            saved = json.loads(self.file.read_text(encoding="utf-8"))
            return {int(sid): str(name) for sid, name in saved.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def set(self, subject_id: int, type_name: Optional[str]) -> None:
        """Decode ``subject_id`` as ``type_name`` from now on and in later sessions; None forgets it.

        RuntimeError if CAN is not connected or the subject's registers name
        its type; ValueError if the type is not a compiled message type.
        """
        if not self._session.is_running:
            raise RuntimeError("CAN is not connected")
        self._session._live_scanner.set_subject_type(subject_id, type_name)
        saved = self.saved()
        if type_name is None:
            saved.pop(subject_id, None)
        else:
            saved[subject_id] = type_name
        try:
            self.file.write_text(
                json.dumps({str(sid): name for sid, name in sorted(saved.items())}, indent=1), encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not save the subject types: %s", exc)

    def apply_saved(self) -> None:
        """Decode the subjects as set in earlier sessions, those the scanner takes."""
        for subject_id, type_name in self.saved().items():
            try:
                self._session._live_scanner.set_subject_type(subject_id, type_name)
            except (RuntimeError, ValueError) as exc:
                logger.warning("Subject %d: not decoding it as %s, which was set earlier: %s",
                               subject_id, type_name, exc)

    async def guess(self, subject_id: int, types: list[dict]) -> dict:
        """Listen to an undecoded subject and rank the ``types`` its payloads fit (see type_guess).

        {samples, matches, candidates: [{type, custom, preview}]}; no samples
        if nothing was published on it meanwhile.
        """
        session = self._session
        if not session.is_running:
            raise RuntimeError("CAN is not connected")
        payloads = await session._live_scanner.sample_subject(subject_id, self.GUESS_LISTEN_S)
        if not payloads:
            return {"samples": 0, "matches": 0, "candidates": []}
        ranked = await asyncio.to_thread(lambda: rank_types(payloads, load_candidates(types), session.can_fd))
        return {"samples": len(payloads), **ranked}
