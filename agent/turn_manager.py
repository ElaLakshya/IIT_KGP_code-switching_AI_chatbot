"""
Turn + cancellation-token management.

This is the hook point for Component 1 (FastAPI cancellation-token tool-call
system). The full implementation will live in the FastAPI backend and be
called via HTTP/websocket from here; this stub keeps the in-process state
LiveKit needs (current turn id, cancellation flags) so `main.py` has
something concrete to call today.

Each conversational turn gets a monotonically increasing `turn_id`. Any
tool call (e.g. DB lookup for a cancellation charge) should be started with
the `turn_id` that was active when the user asked for it. Before acting on a
tool result, the caller must check `is_current(turn_id)` — if the turn has
moved on (user interrupted or changed their request), the result is
discarded rather than spoken.
"""

import itertools
import logging

logger = logging.getLogger("turn-manager")


class TurnManager:
    def __init__(self):
        self._counter = itertools.count(1)
        self._current_turn_id = next(self._counter)
        self._cancelled_turns: set[int] = set()

    @property
    def current_turn_id(self) -> int:
        return self._current_turn_id

    def start_new_turn(self) -> int:
        self._current_turn_id = next(self._counter)
        logger.info("Started turn %d", self._current_turn_id)
        return self._current_turn_id

    def cancel_current_turn(self, reason: str = "") -> None:
        logger.info("Cancelling turn %d (%s)", self._current_turn_id, reason)
        self._cancelled_turns.add(self._current_turn_id)
        # Immediately open a new turn so subsequent user speech is attributed
        # to a fresh, non-cancelled turn.
        self.start_new_turn()

    def is_current(self, turn_id: int) -> bool:
        """Call this before speaking a tool result: has this turn been superseded?"""
        return turn_id == self._current_turn_id and turn_id not in self._cancelled_turns