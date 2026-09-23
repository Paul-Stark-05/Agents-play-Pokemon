"""Python side of the Emerald bridge: transport plus all decoding.

Everything game-specific lives here, not in the Lua script. Addresses come from
the pokeemerald symbol file and struct offsets from its headers; see CLAUDE.md.

    from emerald import Emerald
    p1 = Emerald(8888)
    print(p1.party())
    print(p1.battle())
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field

# --- addresses (English Emerald, sha1 f3ae088181bf583e55daf962a92bb46f4f1d07b7)
PLAYER_PARTY = 0x020244EC
PLAYER_COUNT = 0x020244E9
ENEMY_PARTY = 0x02024744
BATTLE_MONS = 0x02024084
BATTLERS_COUNT = 0x0202406C
BATTLE_TYPE = 0x02022FEC

SPECIES_NAMES = (0x083185C8, 11, 412)
MOVE_NAMES = (0x0831977C, 13, 355)
TYPE_NAMES = (0x0831AE38, 7, 18)
ABILITY_NAMES = (0x0831B6DB, 13, 78)

MON_SIZE = 100
BATTLE_MON_SIZE = 88
PARTY_SIZE = 6

BATTLE_TYPE_DOUBLE = 1 << 0
BATTLE_TYPE_LINK = 1 << 1
BATTLE_TYPE_TRAINER = 1 << 3

# personality % 24 -> physical slot of substructs (growth, attacks, evs, misc)
SUBSTRUCT_ORDER = [
    (0, 1, 2, 3), (0, 1, 3, 2), (0, 2, 1, 3), (0, 3, 1, 2),
    (0, 2, 3, 1), (0, 3, 2, 1), (1, 0, 2, 3), (1, 0, 3, 2),
    (2, 0, 1, 3), (3, 0, 1, 2), (2, 0, 3, 1), (3, 0, 2, 1),
    (1, 2, 0, 3), (1, 3, 0, 2), (2, 1, 0, 3), (3, 1, 0, 2),
    (2, 3, 0, 1), (3, 2, 0, 1), (1, 2, 3, 0), (1, 3, 2, 0),
    (2, 1, 3, 0), (3, 1, 2, 0), (2, 3, 1, 0), (3, 2, 1, 0),
]

STATUS_BITS = [
    (0x07, "asleep"), (0x80, "badly_poisoned"), (0x08, "poisoned"),
    (0x10, "burned"), (0x20, "frozen"), (0x40, "paralyzed"),
]

# gBattleMons stores stat stages offset by DEFAULT_STAT_STAGE (6), max 12.
STAT_STAGE_NAMES = ["hp", "attack", "defense", "speed",
                    "sp_attack", "sp_defense", "accuracy", "evasion"]
DEFAULT_STAT_STAGE = 6


def decode_text(raw: bytes) -> str:
    """Gen 3 character encoding (charmap.txt in the decompilation)."""
    out = []
    for b in raw:
        if b == 0xFF:
            break
        if b == 0x00:
            out.append(" ")
        elif 0xA1 <= b <= 0xAA:
            out.append(chr(ord("0") + b - 0xA1))
        elif 0xBB <= b <= 0xD4:
            out.append(chr(ord("A") + b - 0xBB))
        elif 0xD5 <= b <= 0xEE:
            out.append(chr(ord("a") + b - 0xD5))
        else:
            out.append("?")
    return "".join(out).strip()


def status_name(status1: int) -> str:
    for mask, name in STATUS_BITS:
        if status1 & mask:
            return name
    return "ok"


def _u16(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 2], "little")


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


@dataclass
class Mon:
    slot: int
    personality: int
    species: int
    name: str
    nickname: str
    level: int
    hp: int
    max_hp: int
    status: str
    stats: dict = field(default_factory=dict)
    moves: list = field(default_factory=list)
    evs: dict = field(default_factory=dict)
    ivs: dict = field(default_factory=dict)
    held_item: int = 0
    experience: int = 0


class Emerald:
    def __init__(self, port: int = 8888, host: str = "127.0.0.1", timeout: float = 30.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.file = self.sock.makefile("rw", newline="\n", encoding="utf-8")
        self._names: dict = {}

    # ----------------------------------------------------------- transport
    def command(self, line: str) -> str:
        self.file.write(line + "\n")
        self.file.flush()
        reply = self.file.readline()
        if not reply:
            raise ConnectionError("emulator closed the connection")
        reply = reply.strip()
        if reply.startswith("error"):
            raise RuntimeError(f"{line!r} -> {reply}")
        return reply

    def ping(self) -> bool:
        return self.command("ping") == "pong"

    def frame(self) -> int:
        return int(self.command("frame"))

    def read(self, addr: int, length: int) -> bytes:
        out = bytearray()
        while length > 0:
            chunk = min(length, 4096)
            out += bytes.fromhex(self.command(f"read {addr} {chunk}"))
            addr += chunk
            length -= chunk
        return bytes(out)

    def press(self, button: str, hold: int = 8, wait: int = 8) -> None:
        self.command(f"press {button} {hold} {wait}")

    def wait(self, frames: int = 30) -> None:
        self.command(f"wait {frames}")

    def screenshot(self, path: str) -> None:
        self.command(f"screenshot {path}")

    def save_state(self, path: str) -> None:
        self.command(f"savestate {path}")

    def load_state(self, path: str) -> None:
        self.command(f"loadstate {path}")

    def close(self) -> None:
        self.file.close()
        self.sock.close()

    # --------------------------------------------------------- name tables
    def _table(self, key: str, spec: tuple) -> list:
        if key not in self._names:
            addr, width, count = spec
            raw = self.read(addr, width * count)
            self._names[key] = [
                decode_text(raw[i * width:(i + 1) * width]) for i in range(count)
            ]
        return self._names[key]

    def species_name(self, i: int) -> str:
        return self._table("species", SPECIES_NAMES)[i]

    def move_name(self, i: int) -> str:
        return self._table("move", MOVE_NAMES)[i]

    def type_name(self, i: int) -> str:
        return self._table("type", TYPE_NAMES)[i]

    def ability_name(self, i: int) -> str:
        return self._table("ability", ABILITY_NAMES)[i]

    # ------------------------------------------------------------ decoding
    def _decode_mon(self, raw: bytes, slot: int) -> Mon | None:
        personality = _u32(raw, 0)
        key = personality ^ _u32(raw, 4)
        secure = raw[0x20:0x50]
        plain = b"".join(
            (_u32(secure, i * 4) ^ key).to_bytes(4, "little") for i in range(12)
        )
        order = SUBSTRUCT_ORDER[personality % 24]
        growth, attacks, evs, misc = (plain[s * 12:(s + 1) * 12] for s in order)

        species = _u16(growth, 0)
        if species == 0:
            return None

        moves = []
        for i in range(4):
            move_id = _u16(attacks, i * 2)
            if move_id:
                moves.append({
                    "id": move_id,
                    "name": self.move_name(move_id),
                    "pp": attacks[8 + i],
                })

        iv_word = _u32(misc, 4)
        return Mon(
            slot=slot,
            personality=personality,
            species=species,
            name=self.species_name(species),
            nickname=decode_text(raw[8:18]),
            level=raw[0x54],
            hp=_u16(raw, 0x56),
            max_hp=_u16(raw, 0x58),
            status=status_name(_u32(raw, 0x50)),
            stats={
                "attack": _u16(raw, 0x5A), "defense": _u16(raw, 0x5C),
                "speed": _u16(raw, 0x5E), "sp_attack": _u16(raw, 0x60),
                "sp_defense": _u16(raw, 0x62),
            },
            moves=moves,
            evs={
                "hp": evs[0], "attack": evs[1], "defense": evs[2],
                "speed": evs[3], "sp_attack": evs[4], "sp_defense": evs[5],
            },
            ivs={
                "hp": iv_word & 0x1F, "attack": (iv_word >> 5) & 0x1F,
                "defense": (iv_word >> 10) & 0x1F, "speed": (iv_word >> 15) & 0x1F,
                "sp_attack": (iv_word >> 20) & 0x1F, "sp_defense": (iv_word >> 25) & 0x1F,
            },
            held_item=_u16(growth, 2),
            experience=_u32(growth, 4),
        )

    def _read_party(self, addr: int) -> list:
        raw = self.read(addr, MON_SIZE * PARTY_SIZE)
        party = []
        for i in range(PARTY_SIZE):
            mon = self._decode_mon(raw[i * MON_SIZE:(i + 1) * MON_SIZE], i + 1)
            if mon is None:  # first empty slot ends the party
                break
            party.append(mon)
        return party

    def party(self) -> list:
        """The player's party. Safe to show an agent: it's their own team."""
        return self._read_party(PLAYER_PARTY)

    def enemy_party(self) -> list:
        """The opponent's FULL team, including Pokemon not yet sent out.

        Hidden information. Use for logging and for checking agent claims,
        never as agent input.
        """
        return self._read_party(ENEMY_PARTY)

    def battle(self) -> dict | None:
        """Live state of the Pokemon on the field, or None outside a battle."""
        flags = _u32(self.read(BATTLE_TYPE, 4), 0)
        if flags == 0:
            return None
        count = self.read(BATTLERS_COUNT, 1)[0]
        raw = self.read(BATTLE_MONS, BATTLE_MON_SIZE * max(count, 1))
        battlers = []
        for i in range(count):
            b = raw[i * BATTLE_MON_SIZE:(i + 1) * BATTLE_MON_SIZE]
            moves = []
            for m in range(4):
                move_id = _u16(b, 0x0C + m * 2)
                if move_id:
                    moves.append({
                        "id": move_id, "name": self.move_name(move_id), "pp": b[0x24 + m]
                    })
            types = [self.type_name(b[0x21]), self.type_name(b[0x22])]
            battlers.append({
                "battler": i,
                "side": "player" if i % 2 == 0 else "opponent",
                "species": _u16(b, 0),
                "name": self.species_name(_u16(b, 0)),
                "level": b[0x2A],
                "hp": _u16(b, 0x28),
                "max_hp": _u16(b, 0x2C),
                "types": types if types[0] != types[1] else types[:1],
                "ability": self.ability_name(b[0x20]),
                "status": status_name(_u32(b, 0x4C)),
                "stat_stages": {
                    name: b[0x18 + i] - DEFAULT_STAT_STAGE
                    for i, name in enumerate(STAT_STAGE_NAMES)
                    if b[0x18 + i] != DEFAULT_STAT_STAGE
                },
                "moves": moves,
            })
        return {
            "flags": flags,
            "is_link": bool(flags & BATTLE_TYPE_LINK),
            "is_trainer": bool(flags & BATTLE_TYPE_TRAINER),
            "is_double": bool(flags & BATTLE_TYPE_DOUBLE),
            "battlers": battlers,
        }


if __name__ == "__main__":
    game = Emerald(8888)
    print("ping:", game.ping(), "| frame:", game.frame())

    for mon in game.party():
        moves = ", ".join(f"{m['name']}({m['pp']})" for m in mon.moves)
        print(f"{mon.name:<12} Lv{mon.level:<3} {mon.hp}/{mon.max_hp}  {moves}")

    state = game.battle()
    print(json.dumps(state, indent=2) if state else "not in a battle")
    game.close()
