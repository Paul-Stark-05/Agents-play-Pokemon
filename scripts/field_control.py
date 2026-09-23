"""The move-learning prompt that appears outside battle.

Two different code paths can ask it, and they look nothing alike in memory:

* After an evolution, `Task_EvolutionScene` runs the whole scene, keeping its
  main state in data[0], the move sub-state in data[6], where "yes" leads in
  data[7] and the party slot in data[10]. Its yes/no cursor is
  gBattleCommunication[1], the same byte the in-battle prompt uses.
* After a Rare Candy or the move relearner, party-menu tasks drive it instead.

Addresses from the pokeemerald symbol file; state names from evolution_scene.c.
"""

from __future__ import annotations

import time

GTASKS = 0x03005E00
TASK_SIZE = 40
TASK_COUNT = 16
TASK_DATA = 8                      # struct Task: func, isActive, prev, next, priority
GMOVE_TO_LEARN = 0x020244E2
GBATTLE_COMMUNICATION = 0x02024332
EVO_CURSOR = GBATTLE_COMMUNICATION + 1   # 0 = Yes, 1 = No
GPARTY_MENU_SLOT_ID = 0x0203CEC8 + 9

TASK_EVOLUTION_SCENE = 0x0813E570 | 1
TASK_TRADE_EVOLUTION_SCENE = 0x0813F1B8 | 1

EVOSTATE_REPLACE_MOVE = 22

MVSTATE_HANDLE_YES_NO = 4
MVSTATE_SHOW_MOVE_SELECT = 5       # where "yes" leads for "delete an older move?"
MVSTATE_HANDLE_MOVE_SELECT = 6
MVSTATE_CANCEL = 11                # where "yes" leads for "stop learning?"

# Rare Candy / relearner path, from party_menu.c
PARTY_TASKS = {
    0x081B7028 | 1: "asking_input",        # Task_HandleReplaceMoveYesNoInput
    0x081C174C | 1: "list_input",          # Task_HandleReplaceMoveInput
    0x081B72C8 | 1: "confirm_stop_input",  # Task_HandleStopLearningMoveYesNoInput
    0x081B6FF4 | 1: "busy",
    0x081B7088 | 1: "busy",
    0x081B71D4 | 1: "busy",
    0x081B7294 | 1: "busy",
    0x081B6EB4 | 1: "busy",
    0x081B7704 | 1: "busy",
    0x081B77AC | 1: "busy",
}


class FieldController:
    def __init__(self, game):
        self.game = game
        self._source = None   # "evolution" or "party"

    # ------------------------------------------------------------- inspection
    def _tasks(self) -> list:
        raw = self.game.read(GTASKS, TASK_SIZE * TASK_COUNT)
        tasks = []
        for index in range(TASK_COUNT):
            base = index * TASK_SIZE
            if not raw[base + 4]:  # isActive
                continue
            pointer = int.from_bytes(raw[base:base + 4], "little")

            def data(slot: int, base=base) -> int:
                offset = base + TASK_DATA + slot * 2
                return int.from_bytes(raw[offset:offset + 2], "little", signed=True)

            tasks.append({"func": pointer, "data": data})
        return tasks

    def _evolution(self) -> dict | None:
        for task in self._tasks():
            if task["func"] in (TASK_EVOLUTION_SCENE, TASK_TRADE_EVOLUTION_SCENE):
                return {
                    "state": task["data"](0),
                    "move_state": task["data"](6),
                    "yes_leads_to": task["data"](7),
                    "party_id": task["data"](10),
                }
        return None

    def phase(self) -> str | None:
        """What the learn-move flow is doing, or None if it isn't running.

        "asking_input"       waiting on "delete an older move?"
        "confirm_stop_input" waiting on "stop learning?"
        "list_input"         the move list is open and waiting
        "busy"               mid-sequence; do not press anything
        "evolving"           the evolution animation is playing
        """
        evolution = self._evolution()
        if evolution:
            self._source = "evolution"
            if evolution["state"] != EVOSTATE_REPLACE_MOVE:
                return "evolving"
            move_state = evolution["move_state"]
            if move_state == MVSTATE_HANDLE_YES_NO:
                return ("asking_input"
                        if evolution["yes_leads_to"] == MVSTATE_SHOW_MOVE_SELECT
                        else "confirm_stop_input")
            if move_state == MVSTATE_HANDLE_MOVE_SELECT:
                return "list_input"
            return "busy"

        found = [PARTY_TASKS[task["func"]] for task in self._tasks()
                 if task["func"] in PARTY_TASKS]
        if found:
            self._source = "party"
            for name in ("asking_input", "list_input", "confirm_stop_input", "busy"):
                if name in found:
                    return name
        return None

    def waiting_for_answer(self) -> bool:
        return self.phase() == "asking_input"

    def move_to_learn(self) -> int:
        return int.from_bytes(self.game.read(GMOVE_TO_LEARN, 2), "little")

    def party_index(self) -> int:
        evolution = self._evolution()
        if evolution:
            return evolution["party_id"]
        return self.game.read(GPARTY_MENU_SLOT_ID, 1)[0]

    def cursor(self) -> int:
        return self.game.read(EVO_CURSOR, 1)[0]

    # ---------------------------------------------------------------- driving
    def _wait_for(self, wanted: set, timeout: float = 20.0) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            phase = self.phase()
            if phase in wanted:
                return phase
            self.game.wait(6)
        return None

    def _answer(self, yes: bool) -> None:
        """Move the yes/no cursor where we want it, then confirm."""
        if self._source == "evolution":
            for _ in range(3):
                position = self.cursor()
                if position == (0 if yes else 1):
                    break
                self.game.press("DOWN" if yes is False else "UP", hold=4, wait=8)
            self.game.press("A", hold=4, wait=25)
        else:
            self.game.press("A" if yes else "B", hold=4, wait=25)

    def decline(self) -> None:
        """Refuse the new move, then confirm stopping."""
        if not self.waiting_for_answer():
            raise RuntimeError("no replace-move prompt open")
        self._answer(yes=False)
        if self._wait_for({"confirm_stop_input"}, timeout=12):
            self._answer(yes=True)
        self.settle()

    def replace(self, slot: int) -> None:
        """Accept, then forget the move in `slot` (0-3).

        The list opens on the first move and DOWN steps one row; its cursor
        cannot be read, so the caller must verify the party afterwards.
        """
        if not 0 <= slot <= 3:
            raise ValueError("slot must be 0-3")
        if not self.waiting_for_answer():
            raise RuntimeError("no replace-move prompt open")
        self._answer(yes=True)

        if not self._wait_for({"list_input"}, timeout=25):
            raise RuntimeError("move list never opened")
        # The list is "open" before it is ready for input: presses sent during
        # the fade are dropped, which silently selects the first row.
        self.game.wait(90)
        for _ in range(slot):
            self.game.press("DOWN", hold=6, wait=24)
        self.game.wait(20)
        self.game.press("A", hold=6, wait=40)
        self.settle()

    def back_out_of_list(self) -> None:
        """Cancel a move list nobody chose to open. B selects nothing."""
        self.game.press("B", hold=4, wait=20)
        self.settle()

    def settle(self, timeout: float = 60.0) -> None:
        """Let the sequence finish, without answering anything new."""
        deadline = time.time() + timeout
        last = None
        stagnant = 0.0
        while time.time() < deadline:
            phase = self.phase()
            if phase in ("asking_input", "list_input", "confirm_stop_input", None):
                return
            if phase == last:
                stagnant += 0.35
                if stagnant > 2.5:       # a message is waiting on A
                    self.game.press("A", hold=4, wait=8)
                    stagnant = 0.0
                    continue
            else:
                last, stagnant = phase, 0.0
            self.game.wait(20)

    def finish(self, timeout: float = 60.0) -> bool:
        """Press through the rest of the scene until it is gone for good."""
        deadline = time.time() + timeout
        quiet = 0
        while time.time() < deadline:
            phase = self.phase()
            if phase in ("asking_input", "list_input", "confirm_stop_input"):
                return False          # something still wants an answer
            if phase is None:
                quiet += 1
                if quiet >= 3:
                    return True
                self.game.wait(20)
                continue
            quiet = 0
            self.game.press("A", hold=4, wait=10)
        return False

    def advance(self, seconds: float = 30.0) -> str | None:
        """Carry the post-battle sequence forward and stop at any decision.

        Presses A only when nothing in the learn flow is running, or when a
        message has clearly stalled. Never presses B: B cancels an evolution.
        """
        deadline = time.time() + seconds
        last = None
        stagnant = 0.0
        while time.time() < deadline:
            phase = self.phase()
            if phase == "asking_input":
                return "asking"
            if phase in ("list_input", "confirm_stop_input"):
                return phase
            if phase in ("busy", "evolving"):
                if phase == last:
                    stagnant += 0.35
                    if stagnant > 2.5:
                        self.game.press("A", hold=4, wait=8)
                        stagnant = 0.0
                        continue
                else:
                    last, stagnant = phase, 0.0
                self.game.wait(20)
                continue
            # Nothing running: confirm twice before pressing, so A cannot land
            # on a prompt that appears between the check and the press.
            last, stagnant = None, 0.0
            self.game.wait(4)
            if self.phase() is not None:
                continue
            self.game.press("A", hold=4, wait=8)
        return None
