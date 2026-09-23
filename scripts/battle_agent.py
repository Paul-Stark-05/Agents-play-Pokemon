"""Two battle agents over the same game, one grounded in the graph, one not.

    python battle_agent.py --grounded          # KG tools, writes to emerald-p1
    python battle_agent.py --parametric        # no tools, same prompt

Both see the same observation, use the same model, and choose from the same
actions. The only difference is whether the graph is reachable. Every turn is
appended to a JSONL log with the action, the reasoning and the factual claims
the agent made, so the two runs can be compared afterwards.

Needs OPENAI_API_KEY in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI

from emerald import Emerald
from kg import KG, LiveGraph
from battle_control import BattleController
from field_control import FieldController

# OpenAI model id. Both arms must use the same one or the comparison is meaningless.
MODEL = os.environ.get("EMERALD_MODEL", "gpt-5.6-luna")
# gpt-5.6 rejects function tools combined with reasoning in /v1/chat/completions, so
# reasoning is off here. Sent for BOTH arms: differing settings would make the
# comparison meaningless. To run with reasoning on, use the Responses API instead.
REASONING_EFFORT = os.environ.get("EMERALD_REASONING_EFFORT", "none")

SYSTEM = """You are playing a Pokémon Emerald link battle on a Game Boy Advance,
against another player. Each side has up to six Pokémon; you win when all of
the opponent's have fainted.

Each turn you receive the battle state and choose one action. Reply with a
single JSON object and nothing else:

{"action": {"type": "move", "index": 0} or {"type": "switch", "party_index": 2},
 "reasoning": "one or two sentences",
 "claims": ["each factual statement your choice depends on, one per string"]}

"index" is the 0-based slot of a move of your active Pokémon, as numbered in
the state. "party_index" is a Pokémon from "Your team", as numbered there; it
must not be fainted or already in battle. Switching uses your turn.

You see what a player would see: the opponent's species, level, remaining HP
as a percentage and status. Its types, ability and moves are not shown.

The claims list matters: state the type matchups, move properties or species
facts you relied on. Write them as plain assertions, for example
"Ember is Fire type" or "Fire is not very effective against Rock". Do not
include reasoning or hedging in the claims."""

FORCED_SYSTEM = """You are playing a Pokémon Emerald link battle. Your active Pokémon
has fainted and you must choose which one to send in next.

Reply with a single JSON object and nothing else:

{"action": {"type": "switch", "party_index": 2},
 "reasoning": "one or two sentences",
 "claims": ["each factual statement your choice depends on, one per string"]}

"party_index" is a Pokémon from "Your team" that has not fainted.
State the facts you relied on in "claims" as plain assertions."""

GROUNDED_EXTRA = """
You have tools that read a knowledge graph built directly from this game's own
data. Use them before you commit to an action whenever a fact would change your
choice. Prefer a tool result over your own recollection: the graph is the game's
ground truth, and Generation III differs from later generations."""

LEARN_SYSTEM = """A Pokémon of yours has levelled up and can learn a new move, but it
already knows four. You choose which move it drops, or refuse the new move.

Reply with a single JSON object and nothing else:

{"action": {"type": "replace", "slot": 0} or {"type": "skip"},
 "reasoning": "one or two sentences",
 "claims": ["each factual statement your choice depends on, one per string"]}

"slot" is the 0-based index of the move to delete, numbered as in the list you
are given. "skip" keeps the current four moves and refuses the new one. This is
permanent: a forgotten move is gone unless relearned later."""

SCHEMA_HELP = """The graph uses these prefixes:

PREFIX em: <https://mypokemon.org/emerald/ontology#>
PREFIX species: <https://mypokemon.org/emerald/id/species/>
PREFIX type: <https://mypokemon.org/emerald/id/type/>
PREFIX move: <https://mypokemon.org/emerald/id/move/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

Species have em:hasType, em:baseHP, em:baseAttack, em:baseDefense, em:baseSpeed,
em:baseSpAttack, em:baseSpDefense, em:mayHaveAbility, em:internalId.
Moves have em:moveType, em:basePower, em:accuracy, em:basePP, em:priority,
em:hasEffect, em:makesContact, em:internalId.
Type matchups are reified: ?m em:attackingType ?t ; em:defendingType ?d ;
em:multiplier ?x, where ?x is 2.0, 0.5 or 0.0. Neutral matchups are absent.
In Generation III the damage class is a property of the type, not the move:
?type em:damageClass em:Physical or em:Special.
Identifiers are lowercase, e.g. species:torchic, move:ember, type:rock."""

TOOLS = [
    {
        "name": "effectiveness",
        "description": "Everything that decides how hard a move hits: the type multiplier "
                       "against the defender, whether the same-type attack bonus applies, "
                       "the damage class, which base stats are compared, and a rough "
                       "effective power. Give the attacker for a complete answer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "move": {"type": "string", "description": "move name, e.g. EMBER"},
                "defender": {"type": "string", "description": "species name, e.g. GEODUDE"},
                "attacker": {"type": "string",
                             "description": "your species, e.g. COMBUSKEN. Needed for the "
                                            "same-type attack bonus and the stat comparison."},
            },
            "required": ["move", "defender"],
        },
    },
    {
        "name": "move_info",
        "description": "Type, power, accuracy, PP, priority, effect and damage class of a move.",
        "input_schema": {
            "type": "object",
            "properties": {"move": {"type": "string"}},
            "required": ["move"],
        },
    },
    {
        "name": "species_info",
        "description": "Types, base stats and possible abilities of a species.",
        "input_schema": {
            "type": "object",
            "properties": {"species": {"type": "string"}},
            "required": ["species"],
        },
    },
    {
        "name": "sparql",
        "description": "Run a SPARQL SELECT against the graph for anything the other tools "
                       "don't cover. " + SCHEMA_HELP,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
]

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["input_schema"],
        },
    }
    for tool in TOOLS
]


# ------------------------------------------------------------------ KG tools

class GraphTools:
    def __init__(self, kg: KG):
        self.kg = kg

    def _move_row(self, move: str) -> dict:
        rows = self.kg.select(f"""
        SELECT ?type ?power ?accuracy ?class WHERE {{
          ?m a em:Move ; rdfs:label "{move.upper()}" ; em:moveType ?t .
          ?t rdfs:label ?type .
          OPTIONAL {{ ?t em:damageClass ?c . BIND(REPLACE(STR(?c), "^.*#", "") AS ?class) }}
          OPTIONAL {{ ?m em:basePower ?power }}
          OPTIONAL {{ ?m em:accuracy ?accuracy }}
        }}""")
        return rows[0] if rows else {}

    def _species_row(self, species: str) -> dict:
        types = [row["type"] for row in self.kg.select(f"""
            SELECT ?type WHERE {{ ?s a em:Species ; rdfs:label "{species.upper()}" ;
                                     em:hasType ?t . ?t rdfs:label ?type }}""")]
        stats = self.kg.select(f"""
            SELECT ?attack ?defense ?spAttack ?spDefense ?speed WHERE {{
              ?s a em:Species ; rdfs:label "{species.upper()}" ;
                 em:baseAttack ?attack ; em:baseDefense ?defense ;
                 em:baseSpAttack ?spAttack ; em:baseSpDefense ?spDefense ;
                 em:baseSpeed ?speed }}""")
        return {"types": types, "stats": stats[0] if stats else {}}

    def effectiveness(self, move: str, defender: str, attacker: str | None = None) -> dict:
        move_row = self._move_row(move)
        if not move_row:
            return {"error": f"no move {move}"}
        defending = self._species_row(defender)
        if not defending["types"]:
            return {"error": f"no species {defender}"}

        rows = self.kg.select(f"""
        SELECT ?defendingType ?multiplier WHERE {{
          ?m a em:Move ; rdfs:label "{move.upper()}" ; em:moveType ?at .
          ?s a em:Species ; rdfs:label "{defender.upper()}" ; em:hasType ?dt .
          ?dt rdfs:label ?defendingType .
          OPTIONAL {{ ?match em:attackingType ?at ;
                            em:defendingType ?dt ;
                            em:multiplier ?multiplier }}
        }}""")
        total = 1.0
        per_type = {}
        for row in rows:
            value = float(row.get("multiplier", 1.0))
            per_type[row["defendingType"]] = value
            total *= value

        damage_class = (move_row.get("class") or "").lower()  # physical or special
        power = int(move_row.get("power") or 0)
        result = {
            "move": move.upper(),
            "move_type": move_row["type"],
            "damage_class": damage_class or "unknown",
            "base_power": power,
            "accuracy": int(move_row.get("accuracy") or 0),
            "defender": defender.upper(),
            "defender_types": list(per_type),
            "per_type": per_type,
            "type_multiplier": total,
        }

        if power == 0:
            result["note"] = "status move: it deals no damage, so the multiplier does not apply"

        if attacker:
            attacking = self._species_row(attacker)
            if not attacking["types"]:
                return {**result, "error": f"no species {attacker}"}
            stab = move_row["type"] in attacking["types"]
            result.update({
                "attacker": attacker.upper(),
                "attacker_types": attacking["types"],
                "same_type_attack_bonus": stab,
                "stab_multiplier": 1.5 if stab else 1.0,
                "effective_power": round(power * total * (1.5 if stab else 1.0), 1),
            })
            if damage_class in ("physical", "special"):
                offence = "attack" if damage_class == "physical" else "spAttack"
                defence = "defense" if damage_class == "physical" else "spDefense"
                result["stat_matchup"] = {
                    f"attacker base {offence}": attacking["stats"].get(offence),
                    f"defender base {defence}": defending["stats"].get(defence),
                    "note": "species base stats, not the levelled stats in battle",
                }
        return result

    def move_info(self, move: str) -> dict:
        rows = self.kg.select(f"""
        SELECT ?type ?power ?accuracy ?pp ?priority ?effect ?class WHERE {{
          ?m a em:Move ; rdfs:label "{move.upper()}" ; em:moveType ?t .
          ?t rdfs:label ?type .
          OPTIONAL {{ ?t em:damageClass ?c . BIND(REPLACE(STR(?c), "^.*#", "") AS ?class) }}
          OPTIONAL {{ ?m em:basePower ?power }}
          OPTIONAL {{ ?m em:accuracy ?accuracy }}
          OPTIONAL {{ ?m em:basePP ?pp }}
          OPTIONAL {{ ?m em:priority ?priority }}
          OPTIONAL {{ ?m em:hasEffect ?e . ?e rdfs:label ?effect }}
        }}""")
        return rows[0] if rows else {"error": f"no move {move}"}

    def species_info(self, species: str) -> dict:
        types = self.kg.select(f"""
        SELECT ?type WHERE {{
          ?s a em:Species ; rdfs:label "{species.upper()}" ; em:hasType ?t .
          ?t rdfs:label ?type }}""")
        stats = self.kg.select(f"""
        SELECT ?hp ?attack ?defense ?speed ?spAttack ?spDefense WHERE {{
          ?s a em:Species ; rdfs:label "{species.upper()}" ;
             em:baseHP ?hp ; em:baseAttack ?attack ; em:baseDefense ?defense ;
             em:baseSpeed ?speed ; em:baseSpAttack ?spAttack ; em:baseSpDefense ?spDefense }}""")
        abilities = self.kg.select(f"""
        SELECT ?ability WHERE {{
          ?s a em:Species ; rdfs:label "{species.upper()}" ; em:mayHaveAbility ?a .
          ?a rdfs:label ?ability }}""")
        if not types:
            return {"error": f"no species {species}"}
        return {
            "species": species.upper(),
            "types": [row["type"] for row in types],
            "base_stats": stats[0] if stats else {},
            "abilities": [row["ability"] for row in abilities],
        }

    def sparql(self, query: str) -> dict:
        try:
            return {"rows": self.kg.select(query)[:40]}
        except Exception as error:  # a malformed query is information, not a crash
            return {"error": str(error)}

    def call(self, name: str, arguments: dict) -> dict:
        return getattr(self, name)(**arguments)


# ---------------------------------------------------------------- observation

def describe(battle: dict, party: list, forced: bool = False) -> str:
    """What a player could see on screen, and nothing more.

    The opponent's exact HP, types, ability and moves all sit in memory, but a
    player sees only a species, a level, an HP bar and a status. Showing more
    would hand both agents information the game hides, and would also remove
    the very lookups that separate a grounded agent from one working from
    memory.
    """
    you = next(b for b in battle["battlers"] if b["side"] == "player")
    foe = next(b for b in battle["battlers"] if b["side"] == "opponent")
    active = you.get("personality")

    lines = []
    if not forced:
        lines += [
            f"Your active Pokémon: {you['name']} (level {you['level']}), "
            f"{you['hp']}/{you['max_hp']} HP, types {'/'.join(you['types'])}, "
            f"ability {you['ability']}, status {you['status']}.",
            "Its moves:",
        ]
        for index, move in enumerate(you["moves"]):
            lines.append(f"  index {index}: {move['name']} ({move['pp']} PP left)")
        if you["stat_stages"]:
            lines.append(f"Its stat changes: {you['stat_stages']}")

    percent = round(100 * foe["hp"] / foe["max_hp"]) if foe["max_hp"] else 0
    lines.append(
        f"Opponent's active Pokémon: {foe['name']} (level {foe['level']}), "
        f"{percent}% HP, status {foe['status']}.")
    if foe["stat_stages"]:
        lines.append(f"Opponent's stat changes: {foe['stat_stages']}")

    lines.append("Your team:")
    for index, mon in enumerate(party):
        tags = []
        if mon.personality == active and not forced:
            tags.append("in battle")
        if mon.hp == 0:
            tags.append("fainted")
        moves = ", ".join(move["name"] for move in mon.moves)
        lines.append(
            f"  party_index {index}: {mon.name} (level {mon.level}), "
            f"{mon.hp}/{mon.max_hp} HP, status {mon.status}"
            + (f" [{', '.join(tags)}]" if tags else "")
            + f" — moves: {moves}")
    return "\n".join(lines)


def validate(action: dict, battle: dict, party: list, forced: bool) -> str | None:
    """Return a reason the action can't be carried out, or None if it can."""
    you = next(b for b in battle["battlers"] if b["side"] == "player")
    if action.get("type") == "move":
        if forced:
            return "your Pokémon has fainted; you must choose one to send in"
        index = action.get("index")
        if not isinstance(index, int) or not 0 <= index < len(you["moves"]):
            return f"move index must be 0 to {len(you['moves']) - 1}"
        if you["moves"][index]["pp"] == 0:
            return f"{you['moves'][index]['name']} has no PP left"
        return None
    if action.get("type") == "switch":
        index = action.get("party_index")
        if not isinstance(index, int) or not 0 <= index < len(party):
            return f"party_index must be 0 to {len(party) - 1}"
        if party[index].hp == 0:
            return f"{party[index].name} has fainted"
        if party[index].personality == you.get("personality") and not forced:
            return f"{party[index].name} is already in battle"
        return None
    return "action type must be move or switch"


# --------------------------------------------------------------------- agent

class Agent:
    def __init__(self, grounded: bool, tools: GraphTools | None):
        self.client = OpenAI()
        self.grounded = grounded
        self.tools = tools
        self.system = SYSTEM + (GROUNDED_EXTRA if grounded else "")

    def _create(self, messages: list):
        request = {
            "model": MODEL,
            "messages": messages,
            "max_completion_tokens": 2048,
        }
        if self.grounded:
            request["tools"] = OPENAI_TOOLS
        request["reasoning_effort"] = REASONING_EFFORT
        return self.client.chat.completions.create(**request)

    def decide(self, observation: str, system: str | None = None) -> dict:
        messages = [
            {"role": "system", "content": (system or self.system)
                + (GROUNDED_EXTRA if (system and self.grounded) else "")},
            {"role": "user", "content": observation},
        ]
        tool_calls = []

        for _ in range(8):
            message = self._create(messages).choices[0].message

            if message.tool_calls:
                messages.append(message.model_dump(exclude_none=True))
                for call in message.tool_calls:
                    arguments = json.loads(call.function.arguments or "{}")
                    output = self.tools.call(call.function.name, arguments)
                    tool_calls.append({"tool": call.function.name, "input": arguments,
                                       "output": output})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(output),
                    })
                continue

            text = message.content or ""
            try:
                decision = json.loads(text[text.index("{"):text.rindex("}") + 1])
            except (ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"could not parse a decision from: {text!r}") from error
            decision["tool_calls"] = tool_calls
            return decision

        raise RuntimeError("agent kept calling tools without deciding")


def handle_move_learning(game: Emerald, control, agent: Agent,
                         log: Path, arm: str, field: bool = False,
                         seen: set | None = None) -> dict | None:
    """Let the agent decide which move to forget instead of mashing through it.

    Works for both prompts: the in-battle one driven by learnMoveState, and the
    post-evolution one driven by party-menu tasks.
    """
    if field:
        if not control.waiting_for_answer():
            return None
        index = control.party_index()
    else:
        if control.settle_learning() != 1:
            return None  # the flow finished on its own; nothing to decide
        index = control.active_party_index()
    offered = control.move_to_learn()
    party = game.party()
    if index >= len(party):
        print(f"[{arm}] cannot tell which Pokémon is learning; leaving it to you")
        return None
    mon = party[index]
    offered_name = game.move_name(offered)

    # The game does not always clear its state after a prompt is answered, so
    # the same offer can appear to still be open. Answer each one once.
    key = (mon.personality, offered)
    if seen is not None and key in seen:
        print(f"[{arm} learn] {offered_name} was already answered; not asking again")
        if not field:
            control.settle_learning()
        return None
    if any(move["id"] == offered for move in mon.moves):
        print(f"[{arm} learn] {mon.name} already knows {offered_name}; nothing to decide")
        return None
    if len(mon.moves) < 4:
        print(f"[{arm} learn] {mon.name} has a free slot; no choice needed")
        return None

    lines = [f"{mon.name} (level {mon.level}) can learn {offered_name}.",
             "It already knows:"]
    for slot, move in enumerate(mon.moves):
        lines.append(f"  slot {slot}: {move['name']}")
    lines.append(f"New move on offer: {offered_name}")
    observation = "\n".join(lines)

    decision = agent.decide(observation, system=LEARN_SYSTEM)
    action = decision["action"]
    print(f"[{arm} learn] {action} — {decision['reasoning']}")
    if seen is not None:
        seen.add(key)

    before = [move["name"] for move in mon.moves]
    failure = None
    try:
        if action["type"] == "skip":
            control.decline() if field else control.learn_decline()
        else:
            slot = int(action["slot"])
            control.replace(slot) if field else control.learn_replace(slot)
    except (RuntimeError, TimeoutError) as error:
        # Leave the game somewhere sane rather than dying mid-prompt.
        failure = str(error)
        print(f"[{arm} learn] could not carry that out ({failure}); backing out")
        try:
            control.decline() if field else control.learn_decline()
        except (RuntimeError, TimeoutError):
            if not field:
                control.settle_learning()

    game.wait(60)
    after_party = game.party()
    after = ([move["name"] for move in after_party[index].moves]
             if index < len(after_party) else [])
    learned = offered_name in after
    outcome = {"before": before, "after": after,
               "as_asked": learned == (action["type"] == "replace")}
    if action["type"] == "replace" and learned:
        # Check the move that disappeared is the one it chose to drop.
        dropped = [name for name in before if name not in after]
        wanted = before[int(action["slot"])] if int(action["slot"]) < len(before) else None
        outcome["dropped"] = dropped
        outcome["wanted_dropped"] = wanted
        if dropped != [wanted]:
            outcome["as_asked"] = False
    if failure:
        outcome["failure"] = failure
    if not outcome["as_asked"]:
        print(f"[{arm} learn] WARNING: moves are now {after}, which is not what was asked")

    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "arm": arm, "kind": "move_learning",
            "source": "overworld" if field else "battle", "frame": game.frame(),
            "time": datetime.now(timezone.utc).isoformat(),
            "observation": observation,
            "decision": {k: v for k, v in decision.items() if k != "tool_calls"},
            "tool_calls": decision.get("tool_calls", []),
            "result": outcome,
        }) + "\n")
    return outcome


# ---------------------------------------------------------------------- loop

def play_battle(game: Emerald, control: BattleController, agent: Agent,
                live: LiveGraph | None, log: Path, arm: str) -> str | None:
    answered: set = set()
    turn = 0
    while not control.finished():
        menu = control.wait_for_input()
        if menu is None:
            if control.learning_move() and not control.finished():
                handle_move_learning(game, control, agent, log, arm, seen=answered)
                continue
            break
        battle = game.battle()
        if battle is None:
            break
        party = game.party()
        forced = menu == "choose_pokemon"

        observation = describe(battle, party, forced)
        started = time.time()
        decision = agent.decide(observation, system=FORCED_SYSTEM if forced else None)
        problem = validate(decision["action"], battle, party, forced)
        if problem:
            # One retry, with the reason. Logged, since an illegal action is
            # itself a sign of a wrong belief about the state.
            print(f"[{arm}] rejected {decision['action']}: {problem}")
            retry = agent.decide(observation + f"\n\nYour previous choice was not "
                                 f"possible: {problem}. Choose again.",
                                 system=FORCED_SYSTEM if forced else None)
            decision["rejected"] = {"action": decision["action"], "reason": problem}
            decision["action"] = retry["action"]
            decision["reasoning"] = retry["reasoning"]
            decision["claims"] = decision.get("claims", []) + retry.get("claims", [])
            decision["tool_calls"] = decision.get("tool_calls", []) + retry.get("tool_calls", [])
        elapsed = time.time() - started
        turn += 1

        record = {
            "arm": arm,
            "kind": "forced_switch" if forced else "battle_turn",
            "turn": turn,
            "frame": game.frame(),
            "time": datetime.now(timezone.utc).isoformat(),
            "observation": observation,
            "decision": {k: v for k, v in decision.items() if k != "tool_calls"},
            "tool_calls": decision.get("tool_calls", []),
            "seconds": round(elapsed, 2),
        }
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

        print(f"[{arm} turn {turn}] {decision['action']} — {decision['reasoning']}")

        action = decision["action"]
        if validate(action, battle, party, forced):
            # Still illegal after a retry: fall back to something legal rather
            # than hang the link battle, and say so.
            fallback = next((i for i, mon in enumerate(party)
                             if mon.hp > 0 and not validate(
                                 {"type": "switch", "party_index": i}, battle, party, forced)),
                            None) if forced else 0
            print(f"[{arm}] still illegal; falling back to "
                  f"{'party_index ' + str(fallback) if forced else 'move 0'}")
            action = ({"type": "switch", "party_index": fallback} if forced
                      else {"type": "move", "index": 0})

        if action["type"] == "switch":
            target = party[int(action["party_index"])]
            if not forced:
                control.open_party()
            control.send_in(target.personality)
        else:
            control.use_move(int(action["index"]))

        if live:
            live.sync(game.party(), game.battle(), game.frame())

    # Carry the battle through its ending: level-up text, move prompts, the
    # evolution scene (which starts before the battle flags clear) and the fade.
    field = FieldController(game)
    for _ in range(10):
        status = control.wrap_up(field=field)
        if status == "learning":
            handle_move_learning(game, control, agent, log, arm, seen=answered)
            continue
        if status == "field_learning":
            handle_move_learning(game, field, agent, log, arm, field=True, seen=answered)
            continue
        if status == "list_input":
            print(f"[{arm}] a move list opened without a decision; cancelling it")
            field.back_out_of_list()
            continue
        break

    outcome = control.outcome()
    print(f"[{arm}] battle over: {outcome}")

    # Anything still pending once the battle flags have cleared.
    for _ in range(6):
        phase = field.advance(seconds=20)
        if phase == "asking":
            handle_move_learning(game, field, agent, log, arm, field=True, seen=answered)
            continue
        if phase == "list_input":
            print(f"[{arm}] a move list opened without a decision; cancelling it")
            field.back_out_of_list()
            continue
        break

    # Clear the evolution congratulations and anything after it, so the game is
    # back on the overworld rather than sitting on a message.
    field.finish()

    if live:
        live.sync(game.party(), None, game.frame())
    for mon in game.party():
        print(f"  {mon.name:<12} Lv{mon.level:<3} "
              + ", ".join(move["name"] for move in mon.moves))
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grounded", action="store_true")
    parser.add_argument("--parametric", action="store_true")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--endpoint", default="http://localhost:3030/emerald-p1")
    parser.add_argument("--player", default="p1")
    parser.add_argument("--log", type=Path, default=Path("turns.jsonl"))
    args = parser.parse_args()

    if args.grounded == args.parametric:
        parser.error("pick exactly one of --grounded or --parametric")

    game = Emerald(args.port)
    control = BattleController(game)
    kg = KG(args.endpoint)
    live = LiveGraph(kg, player=args.player)
    agent = Agent(grounded=args.grounded, tools=GraphTools(kg) if args.grounded else None)

    if not control.in_battle():
        print("Not in a battle. Walk into some grass, then start this again.")
        return 1

    play_battle(game, control, agent, live, args.log,
                "grounded" if args.grounded else "parametric")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
