"""Check the claims agents made against the game's own data.

    python claim_check.py turns.jsonl --graph emerald_static.ttl
    python claim_check.py turns.jsonl --endpoint http://localhost:3030/emerald-p1

Every claim is matched against a set of patterns for the kinds of statement
these agents actually make: move properties, type matchups, base stats, STAB.
A claim that no pattern recognises is reported as unchecked rather than quietly
counted as correct — the coverage number matters as much as the error count.

Matching is deterministic and auditable on purpose. No model is asked whether a
claim is true; the graph decides, and every verdict names the fact it used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

STAT_WORDS = {
    "hp": "hp", "attack": "attack", "defense": "defense", "defence": "defense",
    "speed": "speed", "special attack": "spAttack", "sp. attack": "spAttack",
    "special defense": "spDefense", "special defence": "spDefense",
    "sp. defense": "spDefense", "spatk": "spAttack", "spdef": "spDefense",
}

EFFECTIVENESS_WORDS = [
    (r"super[- ]effective", 2.0),
    (r"not very effective|resisted by|resists", 0.5),
    (r"no effect|immune|does not affect", 0.0),
    (r"neutral|no type advantage|neutrally", 1.0),
]

SUPPORTED, CONTRADICTED, UNCHECKED = "supported", "contradicted", "unchecked"


class Facts:
    """Everything the checker needs, pulled from the graph once."""

    def __init__(self, select):
        self.moves = {}
        for row in select("""
            SELECT ?name ?type ?power ?accuracy ?class ?effect WHERE {
              ?m a em:Move ; rdfs:label ?name ; em:moveType ?t .
              ?t rdfs:label ?type .
              OPTIONAL { ?t em:damageClass ?c . BIND(REPLACE(STR(?c), "^.*#", "") AS ?class) }
              OPTIONAL { ?m em:basePower ?power }
              OPTIONAL { ?m em:accuracy ?accuracy }
              OPTIONAL { ?m em:hasEffect ?e . ?e rdfs:label ?effect }
            }"""):
            self.moves[row["name"].upper()] = {
                "type": row["type"].lower(),
                "power": int(row.get("power", 0)),
                "accuracy": int(row.get("accuracy", 0)),
                "class": (row.get("class") or "").lower(),
                "effect": (row.get("effect") or "").lower(),
            }

        self.species = {}
        for row in select("""
            SELECT ?name ?hp ?attack ?defense ?speed ?spAttack ?spDefense WHERE {
              ?s a em:Species ; rdfs:label ?name ;
                 em:baseHP ?hp ; em:baseAttack ?attack ; em:baseDefense ?defense ;
                 em:baseSpeed ?speed ; em:baseSpAttack ?spAttack ; em:baseSpDefense ?spDefense .
            }"""):
            self.species[row["name"].upper()] = {
                "stats": {key: int(row[key]) for key in
                          ("hp", "attack", "defense", "speed", "spAttack", "spDefense")},
                "types": [],
            }
        for row in select("""
            SELECT ?name ?type WHERE {
              ?s a em:Species ; rdfs:label ?name ; em:hasType ?t . ?t rdfs:label ?type }"""):
            entry = self.species.setdefault(row["name"].upper(), {"stats": {}, "types": []})
            entry["types"].append(row["type"].lower())

        self.matchups = {}
        for row in select("""
            SELECT ?attacking ?defending ?multiplier WHERE {
              ?m a em:TypeMatchup ; em:attackingType ?a ; em:defendingType ?d ;
                 em:multiplier ?multiplier .
              ?a rdfs:label ?attacking . ?d rdfs:label ?defending }"""):
            self.matchups[(row["attacking"].lower(), row["defending"].lower())] = \
                float(row["multiplier"])

        self.types = {name for _, name in self.matchups} | {a for a, _ in self.matchups}

    def multiplier(self, attacking: str, defending: str) -> float:
        return self.matchups.get((attacking, defending), 1.0)

    def against(self, attacking: str, target: str) -> float | None:
        """Multiplier against a type or a species name."""
        target = target.lower()
        if target in self.types:
            return self.multiplier(attacking, target)
        species = self.species.get(target.upper())
        if not species:
            return None
        total = 1.0
        for defending in species["types"]:
            total *= self.multiplier(attacking, defending)
        return total


def find_move(text: str, facts: Facts) -> str | None:
    """Longest move name mentioned, so DOUBLE KICK beats KICK."""
    best = None
    for name in facts.moves:
        if re.search(rf"\b{re.escape(name)}\b", text.upper()) and (
                best is None or len(name) > len(best)):
            best = name
    return best


def find_species(text: str, facts: Facts) -> str | None:
    best = None
    for name in facts.species:
        if re.search(rf"\b{re.escape(name)}\b", text.upper()) and (
                best is None or len(name) > len(best)):
            best = name
    return best


# ------------------------------------------------------------------ checkers
# Each returns a list of (status, detail) for whatever it recognised.

def check_move_type(claim: str, facts: Facts) -> list:
    move = find_move(claim, facts)
    if not move:
        return []
    results = []
    for match in re.finditer(r"\b(?:is|is a|a)\s+([A-Za-z]+)[- ]type\b", claim, re.I):
        claimed = match.group(1).lower()
        if claimed not in facts.types:
            continue
        actual = facts.moves[move]["type"]
        results.append((SUPPORTED if claimed == actual else CONTRADICTED,
                        f"{move} is {actual} type"))
    for match in re.finditer(r"\b([A-Za-z]+)\s+(?:physical|special)\s+move\b", claim, re.I):
        claimed = match.group(1).lower()
        if claimed in facts.types:
            actual = facts.moves[move]["type"]
            results.append((SUPPORTED if claimed == actual else CONTRADICTED,
                            f"{move} is {actual} type"))
    return results


def check_move_numbers(claim: str, facts: Facts) -> list:
    move = find_move(claim, facts)
    if not move:
        return []
    data = facts.moves[move]
    results = []
    for pattern, field, label in [
        (r"(\d+)[- ]power", "power", "base power"),
        (r"(\d+)\s*(?:base power|power)\b", "power", "base power"),
        (r"(\d+)\s*%?\s*[- ]?accura", "accuracy", "accuracy"),
    ]:
        for match in re.finditer(pattern, claim, re.I):
            claimed = int(match.group(1))
            actual = data[field]
            results.append((SUPPORTED if claimed == actual else CONTRADICTED,
                            f"{move} {label} is {actual}"))
    if re.search(r"no (?:base )?power|deals no damage|non-?damaging", claim, re.I):
        results.append((SUPPORTED if data["power"] == 0 else CONTRADICTED,
                        f"{move} base power is {data['power']}"))
    return results


def check_damage_class(claim: str, facts: Facts) -> list:
    move = find_move(claim, facts)
    if not move or not facts.moves[move]["class"]:
        return []
    actual = facts.moves[move]["class"]
    results = []
    for word in ("physical", "special"):
        if re.search(rf"\b{word}\b", claim, re.I):
            results.append((SUPPORTED if word == actual else CONTRADICTED,
                            f"{move} is {actual} in Generation III"))
    return results


def _clause_effectiveness(clause: str, facts: Facts) -> list:
    verdict = None
    for pattern, value in EFFECTIVENESS_WORDS:
        if re.search(pattern, clause, re.I):
            verdict = value
            break
    if verdict is None:
        return []

    match = re.search(r"(?:against|versus|vs\.?|to|by)\s+(.+)$", clause, re.I)
    if not match:
        return []
    tail = match.group(1)
    head = clause[:match.start()]

    attacking = None
    move = find_move(head, facts)
    if move:
        attacking = facts.moves[move]["type"]
    else:
        for word in re.findall(r"[A-Za-z]+", head):
            if word.lower() in facts.types:
                attacking = word.lower()
                break
    if attacking is None:
        return []

    # "Rock-types", "both Fighting and Grass types", "Geodude"
    # "Rock-types", "both Fighting and Grass types", "Geodude and Nosepass"
    targets = []
    for chunk in re.split(r"\band\b|,|/", tail):
        chunk = re.sub(r"[-\s]*types?\b", " ", chunk, flags=re.I)
        for token in re.findall(r"[A-Za-z]+", chunk):
            if token.lower() in facts.types:
                targets.append(token.lower())
        species = find_species(chunk, facts)
        if species:
            targets.append(species)

    results = []
    for target in targets:
        actual = facts.against(attacking, target)
        if actual is None:
            continue
        results.append((SUPPORTED if abs(actual - verdict) < 1e-9 else CONTRADICTED,
                        f"{attacking} against {target.lower()} is x{actual:g}"))
    return results


def check_effectiveness(claim: str, facts: Facts) -> list:
    """Claims often pack two matchups into one sentence, so check each clause."""
    results = []
    for clause in re.split(r"\bwhereas\b|\bwhile\b|\bbut\b|;", claim, flags=re.I):
        results.extend(_clause_effectiveness(clause, facts))
    return results


def check_move_effect(claim: str, facts: Facts) -> list:
    move = find_move(claim, facts)
    if not move:
        return []
    effects = {"burn": "burn", "flinch": "flinch", "paralyz": "paralyz",
               "freeze": "freeze", "confus": "confus", "poison": "poison",
               "lowers the target's attack": "attack down",
               "critical": "focus energy", "hits twice": "double hit",
               "double hit": "double hit"}
    label = None
    for row in [facts.moves[move]]:
        label = row.get("effect")
    if label is None:
        return []
    results = []
    for phrase, needle in effects.items():
        if re.search(phrase, claim, re.I):
            results.append((SUPPORTED if needle in label else CONTRADICTED,
                            f"{move} effect is \"{label}\""))
    return results


def check_base_stat(claim: str, facts: Facts) -> list:
    species = find_species(claim, facts)
    if not species or not facts.species[species]["stats"]:
        return []
    stats = facts.species[species]["stats"]
    results = []

    for match in re.finditer(r"base\s+([A-Za-z. ]+?)\s+(?:is|of)\s+(\d+)", claim, re.I):
        key = STAT_WORDS.get(match.group(1).strip().lower())
        if key:
            actual = stats[key]
            results.append((SUPPORTED if int(match.group(2)) == actual else CONTRADICTED,
                            f"{species} base {key} is {actual}"))
    for match in re.finditer(r"([A-Za-z. ]+?)\s+(?:of\s+)?(\d+)\s+and\s+(\d+)", claim, re.I):
        pass  # "70 and 60 respectively" is too loose to bind reliably

    comparison = re.search(
        r"higher\s+base\s+([A-Za-z. ]+?)\s+than\s+(?:base\s+)?([A-Za-z. ]+?)(?:[,.]|$)",
        claim, re.I)
    lower = re.search(
        r"lower\s+base\s+([A-Za-z. ]+?)\s+than\s+(?:base\s+)?([A-Za-z. ]+?)(?:[,.]|$)",
        claim, re.I)
    for match, wanted_greater in ((comparison, True), (lower, False)):
        if not match:
            continue
        first = STAT_WORDS.get(match.group(1).strip().lower())
        second = STAT_WORDS.get(match.group(2).strip().lower())
        if not (first and second):
            continue
        holds = stats[first] > stats[second] if wanted_greater else stats[first] < stats[second]
        results.append((SUPPORTED if holds else CONTRADICTED,
                        f"{species} base {first} {stats[first]} vs base {second} {stats[second]}"))
    return results


def check_species_type(claim: str, facts: Facts) -> list:
    species = find_species(claim, facts)
    if not species:
        return []
    match = re.search(r"is\s+(?:a\s+)?([A-Za-z]+)(?:/([A-Za-z]+))?[- ]type", claim, re.I)
    if not match:
        return []
    claimed = {group.lower() for group in match.groups() if group}
    if not claimed <= facts.types:
        return []
    actual = set(facts.species[species]["types"])
    return [(SUPPORTED if claimed == actual else CONTRADICTED,
             f"{species} is {'/'.join(sorted(actual))}")]


def check_stab(claim: str, facts: Facts) -> list:
    if not re.search(r"same[- ]type attack bonus|\bSTAB\b", claim, re.I):
        return []
    move = find_move(claim, facts)
    species = find_species(claim, facts)
    if not (move and species):
        return []
    actual = facts.moves[move]["type"] in facts.species[species]["types"]
    denied = re.search(r"\bno\b|\bnot\b|does not|lacks|without", claim, re.I)
    claimed = not denied
    return [(SUPPORTED if claimed == actual else CONTRADICTED,
             f"{move} is {facts.moves[move]['type']}, {species} is "
             f"{'/'.join(facts.species[species]['types'])}")]


CHECKERS = [check_move_type, check_move_numbers, check_damage_class, check_effectiveness,
            check_base_stat, check_species_type, check_stab, check_move_effect]


def check_claim(claim: str, facts: Facts) -> dict:
    findings = []
    for checker in CHECKERS:
        findings.extend(checker(claim, facts))
    if not findings:
        return {"claim": claim, "status": UNCHECKED, "evidence": []}
    status = CONTRADICTED if any(s == CONTRADICTED for s, _ in findings) else SUPPORTED
    return {"claim": claim, "status": status,
            "evidence": [detail for _, detail in findings],
            "verdicts": [s for s, _ in findings]}


# --------------------------------------------------------------------- main

def make_select(args):
    if args.endpoint:
        from kg import KG
        kg = KG(args.endpoint)
        return kg.select
    from rdflib import Graph
    graph = Graph()
    graph.parse(args.graph, format="turtle")
    prefixes = """PREFIX em: <https://mypokemon.org/emerald/ontology#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    """

    def select(query: str) -> list:
        return [{str(var): str(value) for var, value in zip(row.labels, row)
                 if value is not None}
                for row in graph.query(prefixes + query)]
    return select


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path, help="turns.jsonl from a run")
    parser.add_argument("--graph", type=Path, default=Path("emerald_static.ttl"))
    parser.add_argument("--endpoint", default=None, help="use Fuseki instead of a file")
    parser.add_argument("--out", type=Path, default=Path("claims_report.jsonl"))
    parser.add_argument("--show", choices=["contradicted", "unchecked", "all"],
                        default="contradicted")
    args = parser.parse_args()

    facts = Facts(make_select(args))
    print(f"loaded {len(facts.moves)} moves, {len(facts.species)} species, "
          f"{len(facts.matchups)} matchups\n")

    totals: dict = {}
    with args.out.open("w", encoding="utf-8") as report:
        for line in args.log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            arm = record.get("arm", "?")
            claims = record.get("decision", {}).get("claims", []) or []
            counts = totals.setdefault(arm, Counter())
            for claim in claims:
                result = check_claim(claim, facts)
                counts[result["status"]] += 1
                result.update({"arm": arm, "kind": record.get("kind", "battle_turn"),
                               "frame": record.get("frame")})
                report.write(json.dumps(result) + "\n")
                if args.show == "all" or result["status"] == args.show:
                    marker = {"contradicted": "WRONG", "unchecked": "  ?  ",
                              "supported": "  ok "}[result["status"]]
                    print(f"[{marker}] {claim}")
                    for evidence in result["evidence"]:
                        print(f"          graph says: {evidence}")

    print()
    for arm, counts in totals.items():
        checked = counts[SUPPORTED] + counts[CONTRADICTED]
        total = checked + counts[UNCHECKED]
        rate = (counts[CONTRADICTED] / checked * 100) if checked else 0.0
        print(f"{arm:<12} {total:>4} claims | {checked:>4} checkable | "
              f"{counts[CONTRADICTED]:>3} contradicted ({rate:.1f}%) | "
              f"{counts[UNCHECKED]:>3} unchecked")
    print(f"\nfull report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
