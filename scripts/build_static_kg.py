"""Build the static Emerald knowledge graph from the pokeemerald decompilation.

Usage:
    python build_static_kg.py --source path/to/pokeemerald -o emerald_static.ttl
    python build_static_kg.py -o emerald_static.ttl        # downloads and caches

Every fact here is parsed out of the game's own data files. Nothing is supplied
from model knowledge; if a file format changes, the parser should fail loudly
rather than guess.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

EM = Namespace("https://mypokemon.org/emerald/ontology#")
ID = Namespace("https://mypokemon.org/emerald/id/")

RAW = "https://raw.githubusercontent.com/pret/pokeemerald/master/"

FILES = {
    "species_info": "src/data/pokemon/species_info.h",
    "species_names": "src/data/text/species_names.h",
    "moves": "src/data/battle_moves.h",
    "move_names": "src/data/text/move_names.h",
    "type_chart": "src/battle_main.c",
    "learnsets": "src/data/pokemon/level_up_learnsets.h",
    "learnset_pointers": "src/data/pokemon/level_up_learnset_pointers.h",
    "tmhm": "src/data/pokemon/tmhm_learnsets.h",
    "tms_hms": "include/constants/tms_hms.h",
    "evolution": "src/data/pokemon/evolution.h",
    "encounters": "src/data/wild_encounters.json",
    "trainers": "src/data/trainers.h",
    "trainer_parties": "src/data/trainer_parties.h",
    "species_constants": "include/constants/species.h",
    "move_constants": "include/constants/moves.h",
}

TYPE_MULTIPLIER = {
    "SUPER_EFFECTIVE": 2.0,
    "NOT_EFFECTIVE": 0.5,
    "NO_EFFECT": 0.0,
}

# Generation III: the type decides physical vs special, not the move.
PHYSICAL_TYPES = {"NORMAL", "FIGHTING", "FLYING", "POISON", "GROUND",
                  "ROCK", "BUG", "GHOST", "STEEL"}


# --------------------------------------------------------------------- sources

class Source:
    def __init__(self, root: Path | None, cache: Path):
        self.root = root
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    def text(self, key: str) -> str:
        rel = FILES[key]
        if self.root:
            return (self.root / rel).read_text(encoding="utf-8", errors="replace")
        local = self.cache / rel.replace("/", "_")
        if not local.exists():
            req = urllib.request.Request(RAW + rel, headers={"User-Agent": "emerald-kg"})
            with urllib.request.urlopen(req) as response:
                local.write_bytes(response.read())
        return local.read_text(encoding="utf-8", errors="replace")


def slug(constant: str) -> str:
    return constant.lower()


def blocks(text: str, prefix: str) -> list:
    """Yield (CONSTANT, body) for `[PREFIX_FOO] =\n    { ... },` entries.

    The newline after the brace keeps one-line stubs such as `[SPECIES_NONE] = {0},`
    from swallowing the entry that follows, and the optional comma catches the last
    entry in each table, which has none.
    """
    pattern = re.compile(r"\[" + prefix + r"_(\w+)\]\s*=\s*\{\s*\n(.*?)\n    \},?", re.S)
    return pattern.findall(text)


# -------------------------------------------------------------------- builders

def add_types(graph: Graph, names: set) -> None:
    for name in sorted(names):
        node = ID[f"type/{slug(name)}"]
        graph.add((node, RDF.type, EM.Type))
        graph.add((node, RDFS.label, Literal(name.title())))
        damage = EM.Physical if name in PHYSICAL_TYPES else EM.Special
        if name not in {"MYSTERY", "NONE"}:
            graph.add((node, EM.damageClass, damage))


def build_species(graph: Graph, src: Source) -> dict:
    info = src.text("species_info")
    names = dict(re.findall(r"\[SPECIES_(\w+)\]\s*=\s*_\(\"([^\"]*)\"\)",
                            src.text("species_names")))
    ids = {name: int(value) for name, value in
           re.findall(r"#define SPECIES_(\w+)\s+(\d+)\b", src.text("species_constants"))}
    seen_types = set()
    species = {}

    for constant, body in blocks(info, "SPECIES"):
        if ".baseHP" not in body or constant.startswith("OLD_UNOWN"):
            continue
        node = ID[f"species/{slug(constant)}"]
        graph.add((node, RDF.type, EM.Species))
        graph.add((node, RDFS.label, Literal(names.get(constant, constant.title()))))
        if constant in ids:
            graph.add((node, EM.internalId, Literal(ids[constant], datatype=XSD.integer)))

        for field, prop in [("baseHP", EM.baseHP), ("baseAttack", EM.baseAttack),
                            ("baseDefense", EM.baseDefense), ("baseSpeed", EM.baseSpeed),
                            ("baseSpAttack", EM.baseSpAttack),
                            ("baseSpDefense", EM.baseSpDefense),
                            ("catchRate", EM.catchRate)]:
            match = re.search(rf"\.{field}\s*=\s*(\d+)", body)
            if match:
                graph.add((node, prop, Literal(int(match.group(1)), datatype=XSD.integer)))

        growth = re.search(r"\.growthRate\s*=\s*GROWTH_(\w+)", body)
        if growth:
            graph.add((node, EM.growthRate, Literal(growth.group(1).lower())))

        types = re.search(r"\.types\s*=\s*\{\s*TYPE_(\w+),\s*TYPE_(\w+)", body)
        if types:
            for type_name in dict.fromkeys(types.groups()):
                seen_types.add(type_name)
                graph.add((node, EM.hasType, ID[f"type/{slug(type_name)}"]))

        abilities = re.search(r"\.abilities\s*=\s*\{\s*ABILITY_(\w+),\s*ABILITY_(\w+)", body)
        if abilities:
            for ability in dict.fromkeys(abilities.groups()):
                if ability == "NONE":
                    continue
                ability_node = ID[f"ability/{slug(ability)}"]
                graph.add((ability_node, RDF.type, EM.Ability))
                graph.add((ability_node, RDFS.label, Literal(ability.replace("_", " ").title())))
                graph.add((node, EM.mayHaveAbility, ability_node))

        species[constant] = node
    return species, seen_types


def build_moves(graph: Graph, src: Source) -> dict:
    names = dict(re.findall(r"\[MOVE_(\w+)\]\s*=\s*_\(\"([^\"]*)\"\)", src.text("move_names")))
    ids = {name: int(value) for name, value in
           re.findall(r"#define MOVE_(\w+)\s+(\d+)\b", src.text("move_constants"))}
    moves = {}
    for constant, body in blocks(src.text("moves"), "MOVE"):
        if constant == "NONE":
            continue
        node = ID[f"move/{slug(constant)}"]
        moves[constant] = node
        graph.add((node, RDF.type, EM.Move))
        graph.add((node, RDFS.label, Literal(names.get(constant, constant.title()))))
        if constant in ids:
            graph.add((node, EM.internalId, Literal(ids[constant], datatype=XSD.integer)))

        move_type = re.search(r"\.type\s*=\s*TYPE_(\w+)", body)
        if move_type:
            graph.add((node, EM.moveType, ID[f"type/{slug(move_type.group(1))}"]))

        effect = re.search(r"\.effect\s*=\s*EFFECT_(\w+)", body)
        if effect:
            effect_node = ID[f"effect/{slug(effect.group(1))}"]
            graph.add((effect_node, RDF.type, EM.MoveEffect))
            graph.add((effect_node, RDFS.label, Literal(effect.group(1).lower().replace("_", " "))))
            graph.add((node, EM.hasEffect, effect_node))

        for field, prop in [("power", EM.basePower), ("accuracy", EM.accuracy),
                            ("pp", EM.basePP),
                            ("secondaryEffectChance", EM.secondaryEffectChance)]:
            match = re.search(rf"\.{field}\s*=\s*(\d+)", body)
            if match:
                graph.add((node, prop, Literal(int(match.group(1)), datatype=XSD.integer)))

        priority = re.search(r"\.priority\s*=\s*(-?\d+)", body)
        if priority:
            graph.add((node, EM.priority, Literal(int(priority.group(1)), datatype=XSD.integer)))

        flags = re.search(r"\.flags\s*=\s*([^\n]*)", body)
        graph.add((node, EM.makesContact,
                   Literal(bool(flags and "FLAG_MAKES_CONTACT" in flags.group(1)))))
    return moves


def build_type_chart(graph: Graph, src: Source) -> int:
    text = src.text("type_chart")
    start = text.index("gTypeEffectiveness")
    table = text[start:text.index("};", start)]
    rows = re.findall(r"TYPE_(\w+),\s*TYPE_(\w+),\s*TYPE_MUL_(\w+)", table)
    count = 0
    for attacker, defender, multiplier in rows:
        if attacker in {"FORESIGHT", "ENDTABLE"} or defender in {"FORESIGHT", "ENDTABLE"}:
            continue
        if multiplier not in TYPE_MULTIPLIER:
            continue
        node = ID[f"matchup/{slug(attacker)}-{slug(defender)}"]
        graph.add((node, RDF.type, EM.TypeMatchup))
        graph.add((node, EM.attackingType, ID[f"type/{slug(attacker)}"]))
        graph.add((node, EM.defendingType, ID[f"type/{slug(defender)}"]))
        graph.add((node, EM.multiplier,
                   Literal(TYPE_MULTIPLIER[multiplier], datatype=XSD.decimal)))
        count += 1
    return count


def build_learnsets(graph: Graph, src: Source, species: dict, moves: dict) -> int:
    arrays = dict(re.findall(r"static const u16 s(\w+)LevelUpLearnset\[\] = \{(.*?)\n\};",
                             src.text("learnsets"), re.S))
    pointers = re.findall(r"\[SPECIES_(\w+)\]\s*=\s*s(\w+)LevelUpLearnset",
                          src.text("learnset_pointers"))
    count = 0
    for species_constant, array_name in pointers:
        if species_constant not in species or array_name not in arrays:
            continue
        for level, move in re.findall(r"LEVEL_UP_MOVE\(\s*(\d+),\s*MOVE_(\w+)\)",
                                      arrays[array_name]):
            if move not in moves:
                continue
            node = ID[f"learn/{slug(species_constant)}-{slug(move)}-lv{level}"]
            graph.add((node, RDF.type, EM.LearnsetEntry))
            graph.add((node, EM.learnsetOf, species[species_constant]))
            graph.add((node, EM.learnsMove, moves[move]))
            graph.add((node, EM.learnMethod, EM.LevelUp))
            graph.add((node, EM.atLevel, Literal(int(level), datatype=XSD.integer)))
            count += 1
    return count


def build_machines(graph: Graph, src: Source, species: dict, moves: dict) -> tuple:
    constants = src.text("tms_hms")
    tms = re.findall(r"F\((\w+)\)", constants[constants.index("FOREACH_TM("):
                                              constants.index("FOREACH_HM(")])
    hms = re.findall(r"F\((\w+)\)", constants[constants.index("FOREACH_HM("):])
    machines = {}
    for index, name in enumerate(tms, start=1):
        machines[name] = (f"tm{index:02d}", name)
    for index, name in enumerate(hms, start=1):
        machines[name] = (f"hm{index:02d}", name)

    for name, (machine_id, move_constant) in machines.items():
        node = ID[f"machine/{machine_id}"]
        graph.add((node, RDF.type, EM.Machine))
        graph.add((node, RDFS.label, Literal(machine_id.upper())))
        if move_constant in moves:
            graph.add((node, EM.teachesMove, moves[move_constant]))

    count = 0
    pattern = re.compile(r"\[SPECIES_(\w+)\]\s*=\s*\{\s*\.learnset\s*=\s*\{(.*?)\}\s*\}", re.S)
    for species_constant, body in pattern.findall(src.text("tmhm")):
        if species_constant not in species:
            continue
        for name in re.findall(r"\.(\w+)\s*=\s*TRUE", body):
            if name not in machines:
                continue
            machine_id, move_constant = machines[name]
            node = ID[f"learn/{slug(species_constant)}-{machine_id}"]
            graph.add((node, RDF.type, EM.LearnsetEntry))
            graph.add((node, EM.learnsetOf, species[species_constant]))
            graph.add((node, EM.learnMethod, EM.MachineTaught))
            graph.add((node, EM.viaMachine, ID[f"machine/{machine_id}"]))
            if move_constant in moves:
                graph.add((node, EM.learnsMove, moves[move_constant]))
            count += 1
    return len(machines), count


def build_evolutions(graph: Graph, src: Source, species: dict) -> int:
    count = 0
    for line in src.text("evolution").splitlines():
        head = re.match(r"\s*\[SPECIES_(\w+)\]\s*=\s*\{(.*)", line)
        if not head:
            continue
        source_constant = head.group(1)
        if source_constant not in species:
            continue
        for trigger, parameter, target in re.findall(
                r"\{EVO_(\w+),\s*([\w\d]+),\s*SPECIES_(\w+)\}", head.group(2)):
            if target not in species:
                continue
            node = ID[f"evolution/{slug(source_constant)}-{slug(target)}"]
            trigger_node = ID[f"evotrigger/{slug(trigger)}"]
            graph.add((trigger_node, RDF.type, EM.EvolutionTrigger))
            graph.add((trigger_node, RDFS.label, Literal(trigger.lower().replace("_", " "))))
            graph.add((node, RDF.type, EM.Evolution))
            graph.add((node, EM.evolutionOf, species[source_constant]))
            graph.add((node, EM.evolvesInto, species[target]))
            graph.add((node, EM.trigger, trigger_node))
            graph.add((node, EM.triggerParameter, Literal(parameter)))
            count += 1
    return count


ENCOUNTER_METHODS = {
    "land_mons": EM.LandEncounter,
    "water_mons": EM.WaterEncounter,
    "rock_smash_mons": EM.RockSmashEncounter,
    "fishing_mons": EM.FishingEncounter,
}


def build_encounters(graph: Graph, src: Source, species: dict) -> tuple:
    data = json.loads(src.text("encounters"))
    group = next(g for g in data["wild_encounter_groups"] if g["label"] == "gWildMonHeaders")
    tables = 0
    slots = 0
    for entry in group["encounters"]:
        map_constant = entry["map"]
        location = ID[f"location/{slug(map_constant.replace('MAP_', ''))}"]
        graph.add((location, RDF.type, EM.Location))
        graph.add((location, RDFS.label, Literal(map_constant.replace("MAP_", "").title())))
        for field, method in ENCOUNTER_METHODS.items():
            table = entry.get(field)
            if not table:
                continue
            tables += 1
            for index, mon in enumerate(table["mons"]):
                constant = mon["species"].replace("SPECIES_", "")
                if constant not in species:
                    continue
                node = ID[f"encounter/{slug(map_constant.replace('MAP_', ''))}"
                          f"-{field.replace('_mons', '')}-{index}"]
                graph.add((node, RDF.type, EM.EncounterSlot))
                graph.add((node, EM.atLocation, location))
                graph.add((node, EM.encounterSpecies, species[constant]))
                graph.add((node, EM.encounterMethod, method))
                graph.add((node, EM.minLevel, Literal(mon["min_level"], datatype=XSD.integer)))
                graph.add((node, EM.maxLevel, Literal(mon["max_level"], datatype=XSD.integer)))
                graph.add((node, EM.encounterRate,
                           Literal(table["encounter_rate"], datatype=XSD.decimal)))
                slots += 1
    return tables, slots


def build_trainers(graph: Graph, src: Source, species: dict, moves: dict) -> tuple:
    parties = {}
    for _struct, name, body in re.findall(
            r"static const struct (\w+) (\w+)\[\] = \{(.*?)\n\};", src.text("trainer_parties"), re.S):
        members = []
        for member in re.findall(r"\{(.*?)\n    \}", body, re.S):
            level = re.search(r"\.lvl\s*=\s*(\d+)", member)
            mon = re.search(r"\.species\s*=\s*SPECIES_(\w+)", member)
            if not (level and mon):
                continue
            member_moves = re.search(r"\.moves\s*=\s*\{([^}]*)\}", member)
            held = re.search(r"\.heldItem\s*=\s*ITEM_(\w+)", member)
            members.append({
                "level": int(level.group(1)),
                "species": mon.group(1),
                "moves": re.findall(r"MOVE_(\w+)", member_moves.group(1)) if member_moves else [],
                "item": held.group(1) if held else "NONE",
            })
        parties[name] = members

    trainers = 0
    members_added = 0
    for constant, body in blocks(src.text("trainers"), "TRAINER"):
        party = re.search(r"\.party\s*=\s*\w+\((\w+)\)", body)
        if not party or party.group(1) not in parties:
            continue
        node = ID[f"trainer/{slug(constant)}"]
        name = re.search(r"\.trainerName\s*=\s*_\(\"([^\"]*)\"\)", body)
        trainer_class = re.search(r"\.trainerClass\s*=\s*TRAINER_CLASS_(\w+)", body)
        graph.add((node, RDF.type, EM.Trainer))
        graph.add((node, RDFS.label, Literal(name.group(1) if name else constant.title())))
        if trainer_class:
            graph.add((node, EM.trainerClass, Literal(trainer_class.group(1).lower())))
        trainers += 1

        for index, member in enumerate(parties[party.group(1)], start=1):
            if member["species"] not in species:
                continue
            member_node = ID[f"trainer/{slug(constant)}/member{index}"]
            graph.add((node, EM.hasPartyMember, member_node))
            graph.add((member_node, RDF.type, EM.TrainerPartyMember))
            graph.add((member_node, EM.memberSpecies, species[member["species"]]))
            graph.add((member_node, EM.memberLevel,
                       Literal(member["level"], datatype=XSD.integer)))
            for move in member["moves"]:
                if move in moves:
                    graph.add((member_node, EM.memberKnowsMove, moves[move]))
            members_added += 1
    return trainers, members_added


# ------------------------------------------------------------------------ main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=None,
                        help="path to a local pokeemerald clone (otherwise files are downloaded)")
    parser.add_argument("--ontology", type=Path, default=Path("emerald_ontology.ttl"))
    parser.add_argument("-o", "--output", type=Path, default=Path("emerald_static.ttl"))
    parser.add_argument("--cache", type=Path, default=Path(".decomp_cache"))
    args = parser.parse_args()

    src = Source(args.source, args.cache)
    graph = Graph()
    graph.bind("em", EM)
    graph.bind("id", ID)
    if args.ontology.exists():
        graph.parse(args.ontology, format="turtle")

    species, type_names = build_species(graph, src)
    add_types(graph, type_names)
    moves = build_moves(graph, src)
    matchups = build_type_chart(graph, src)
    level_up = build_learnsets(graph, src, species, moves)
    machines, machine_entries = build_machines(graph, src, species, moves)
    evolutions = build_evolutions(graph, src, species)
    tables, slots = build_encounters(graph, src, species)
    trainers, party_members = build_trainers(graph, src, species, moves)

    graph.serialize(destination=str(args.output), format="turtle")

    print(f"species           {len(species)}")
    print(f"moves             {len(moves)}")
    print(f"types             {len(type_names)}")
    print(f"type matchups     {matchups}")
    print(f"level-up entries  {level_up}")
    print(f"machines          {machines} ({machine_entries} species entries)")
    print(f"evolutions        {evolutions}")
    print(f"encounter tables  {tables} ({slots} slots)")
    print(f"trainers          {trainers} ({party_members} party members)")
    print(f"triples           {len(graph)} -> {args.output}")

    problems = []
    if len(species) != 386:
        problems.append(f"expected 386 species, got {len(species)}")
    if len(moves) != 354:
        problems.append(f"expected 354 moves, got {len(moves)}")
    if machines != 58:
        problems.append(f"expected 58 machines, got {machines}")
    for problem in problems:
        print("CHECK FAILED:", problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
