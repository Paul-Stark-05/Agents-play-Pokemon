"""Driving Emerald's battle menus from Python.

Polling the battle state isn't enough to know when the game wants input: the
same numbers are on screen during animations. Instead this reads the function
pointer the game has installed for the player's battler and compares it against
the input handlers, which says exactly which menu is open.

Addresses are from the pokeemerald symbol file; see CLAUDE.md.
"""

from __future__ import annotations

import time

GBATTLER_CONTROLLER_FUNCS = 0x03005D60  # 4 battlers, one function pointer each
GACTION_SELECTION_CURSOR = 0x020244AC   # 4 bytes, one cursor per battler
GMOVE_SELECTION_CURSOR = 0x020244B0
GBATTLE_OUTCOME = 0x0202433A
GBATTLE_TYPE_FLAGS = 0x02022FEC
# gBattleScripting.learnMoveState: non-zero while the game is asking whether to
# learn a move and which one to forget. Offset 31 in the struct; see CLAUDE.md.
GLEARN_MOVE_STATE = 0x02024474 + 31
GBATTLE_COMMUNICATION = 0x02024332
CURSOR_POSITION = 1          # gBattleCommunication[1] holds the yes/no cursor
GMOVE_TO_LEARN = 0x020244E2
GBATTLER_PARTY_INDEXES = 0x0202406E

# Thumb function pointers carry the low bit set.
HANDLERS = {
    0x08057588 | 1: "choose_action",
    0x08057BFC | 1: "choose_move",
    0x08057824 | 1: "choose_target",
}

OUTCOMES = {
    0: None, 1: "won", 2: "lost", 3: "drew", 4: "ran",
    5: "teleported", 6: "opponent_fled", 7: "caught",
    8: "no_safari_balls", 9: "forfeited", 10: "mon_teleported",
}

# Both battle menus are 2x2 grids: index 0 top-left, 1 top-right, 2 bottom-left,
# 3 bottom-right. Actions are FIGHT, BAG, POKEMON, RUN.
FIGHT, BAG, POKEMON, RUN = 0, 1, 2, 3


class BattleController:
    def __init__(self, game, battler: int = 0):
        self.game = game
        self.battler = battler

    # ----------------------------------------------------------------- state
    def menu(self) -> str | None:
        """Which menu is waiting for input, or None if the game is busy."""
        raw = self.game.read(GBATTLER_CONTROLLER_FUNCS + self.battler * 4, 4)
        pointer = int.from_bytes(raw, "little")
        return HANDLERS.get(pointer)

    def in_battle(self) -> bool:
        return int.from_bytes(self.game.read(GBATTLE_TYPE_FLAGS, 4), "little") != 0

    def outcome(self) -> str | None:
        return OUTCOMES.get(self.game.read(GBATTLE_OUTCOME, 1)[0])

    def learning_move(self) -> bool:
        """True while a level-up move-learning prompt is on screen."""
        return self.learn_state() != 0

    def learn_state(self) -> int:
        """gBattleScripting.learnMoveState: 1 = yes/no box, 3 = move list open."""
        return self.game.read(GLEARN_MOVE_STATE, 1)[0]

    def move_to_learn(self) -> int:
        return int.from_bytes(self.game.read(GMOVE_TO_LEARN, 2), "little")

    def active_party_index(self) -> int:
        """Party slot of the Pokémon on the field, which is normally the one
        gaining the level. A benched Pokémon can be the one learning if it
        gained experience another way, so callers should verify afterwards."""
        return self.game.read(GBATTLER_PARTY_INDEXES + self.battler * 2, 1)[0]

    def yesno_cursor(self) -> int:
        """0 = Yes, 1 = No."""
        return self.game.read(GBATTLE_COMMUNICATION + CURSOR_POSITION, 1)[0]

    def _wait_for_learn_state(self, wanted: int, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.learn_state() == wanted:
                return True
            self.game.wait(4)
        return False

    def settle_learning(self, timeout: float = 45.0) -> int:
        """Wait until the learn flow is either idle (0) or asking (1).

        States 2 and 3 are the move list opening and on screen: never press
        anything there, since A would pick whichever move the cursor is on.
        From state 4 the choice is already made and only text remains
        ("1, 2, and... Poof!"), so A is safe. The game does not reset the state
        to 0 afterwards, so 4 and above count as finished once the text is
        cleared.
        """
        deadline = time.time() + timeout
        presses = 0
        while time.time() < deadline:
            state = self.learn_state()
            if state in (0, 1):
                return state
            if state >= 4:
                if presses >= 8:
                    return state  # done; the state simply stays where it is
                self.game.press("A", hold=4, wait=10)
                presses += 1
            else:
                self.game.wait(6)
        return self.learn_state()

    def learn_decline(self) -> None:
        """Answer No: keep the current four moves."""
        if self.settle_learning() != 1:
            raise RuntimeError("no yes/no box open")
        self.game.press("B", hold=4, wait=20)
        self.settle_learning()

    def learn_replace(self, slot: int) -> None:
        """Answer Yes, then replace the move in `slot` (0-3) on the move list.

        The list opens on the first move and each DOWN steps one row; the fifth
        row is the new move, so selecting it means 'don't learn'. HM moves
        cannot be forgotten and the game will refuse.
        """
        if not 0 <= slot <= 3:
            raise ValueError("slot must be 0-3")
        if self.settle_learning() != 1:
            raise RuntimeError("no yes/no box open")
        if self.yesno_cursor() != 0:
            self.game.press("UP", hold=4, wait=8)
        self.game.press("A", hold=4, wait=30)

        if not self._wait_for_learn_state(3, timeout=30):
            raise RuntimeError("move list never opened")
        # Presses sent while the list is still fading in get dropped, which
        # silently picks the first row.
        self.game.wait(90)
        for _ in range(slot):
            self.game.press("DOWN", hold=6, wait=24)
        self.game.wait(20)
        self.game.press("A", hold=6, wait=40)
        self.settle_learning()

    def finished(self) -> bool:
        return not self.in_battle() or self.outcome() is not None

    def action_cursor(self) -> int:
        return self.game.read(GACTION_SELECTION_CURSOR + self.battler, 1)[0]

    def move_cursor(self) -> int:
        return self.game.read(GMOVE_SELECTION_CURSOR + self.battler, 1)[0]

    # ------------------------------------------------------------- waiting
    def wait_for_menu(self, wanted: str = "choose_action", timeout: float = 60.0) -> bool:
        """Advance text until the wanted menu opens, or the battle reaches a
        point where pressing buttons would decide something on its own.

        Returns True only when `wanted` is open. Returns False when the battle
        is over or a prompt appeared that the caller must handle. It never
        presses A through a move-learning prompt: mashing there silently picks
        which move to delete.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.finished():
                return False
            if self.learning_move():
                return False
            current = self.menu()
            if current == wanted:
                return True
            if current is not None:
                return False  # a different menu is open; caller decides
            self.game.press("A", hold=4, wait=6)
        raise TimeoutError(f"never reached {wanted}")

    # ------------------------------------------------------------- choosing
    def _move_cursor_to(self, current: int, target: int, read_back) -> None:
        """Walk a 2x2 grid cursor to the target cell, checking as we go."""
        for _ in range(6):
            if current == target:
                return
            if (current % 2) != (target % 2):
                self.game.press("RIGHT" if target % 2 else "LEFT", hold=4, wait=6)
            elif (current // 2) != (target // 2):
                self.game.press("DOWN" if target // 2 else "UP", hold=4, wait=6)
            current = read_back()
        raise RuntimeError(f"cursor stuck at {current}, wanted {target}")

    def use_move(self, index: int) -> None:
        """From the action menu: FIGHT, then the move in slot `index` (0-3)."""
        if self.menu() != "choose_action":
            raise RuntimeError(f"not at the action menu (menu={self.menu()})")
        self._move_cursor_to(self.action_cursor(), FIGHT, self.action_cursor)
        self.game.press("A", hold=4, wait=20)

        if not self.wait_for_menu("choose_move", timeout=10):
            raise RuntimeError("move menu never opened")
        self._move_cursor_to(self.move_cursor(), index, self.move_cursor)
        self.game.press("A", hold=4, wait=20)

    def run_away(self) -> None:
        if self.menu() != "choose_action":
            raise RuntimeError("not at the action menu")
        self._move_cursor_to(self.action_cursor(), RUN, self.action_cursor)
        self.game.press("A", hold=4, wait=20)

    def wrap_up(self, timeout: float = 120.0, field=None) -> str:
        """After the last turn, carry the battle through to the overworld.

        The evolution scene starts while gBattleTypeFlags is still set, so a
        FieldController must be passed in: without it this loop would press A
        straight through the evolution and any move prompt inside it.

        Returns "done" when the battle is over, "learning" for the in-battle
        prompt, "field_learning" for the one in the evolution scene, or the name
        of a menu that opened unbidden.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.in_battle():
                return "done"
            state = self.learn_state()
            if state == 1:
                return "learning"
            if state in (2, 3):
                self.game.wait(6)
                continue
            if field is not None:
                phase = field.phase()
                if phase == "asking_input":
                    return "field_learning"
                if phase in ("list_input", "confirm_stop_input"):
                    return phase
                if phase is not None:      # evolving, or mid-sequence
                    self.game.wait(10)
                    continue
            self.game.press("A", hold=4, wait=8)
        return "timeout"

    def skip_text(self, presses: int = 1) -> None:
        for _ in range(presses):
            self.game.press("A", hold=4, wait=8)
