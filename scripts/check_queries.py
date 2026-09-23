"""Competency questions for the static Emerald graph, as runnable tests.

    python check_queries.py emerald_static.ttl

Each query is one of the things the grounded agent must be able to ask. The
expected answers were checked by hand against the decompilation, so this file
doubles as a regression suite for the extractor and as the starting point for
the agent's retrieval layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

from rdflib import Graph

PREFIXES = """
PREFIX em: <https://mypokemon.org/emerald/ontology#>
PREFIX species: <https://mypokemon.org/emerald/id/species/>
PREFIX type: <https://mypokemon.org/emerald/id/type/>
PREFIX loc: <https://mypokemon.org/emerald/id/location/>
PREFIX trainer: <https://mypokemon.org/emerald/id/trainer/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

QUERIES = {
    "land encounters on Route 102": (
        """
        SELECT DISTINCT ?name WHERE {
          ?slot em:atLocation loc:route102 ;
                em:encounterMethod em:LandEncounter ;
                em:encounterSpecies ?species .
          ?species rdfs:label ?name .
        } ORDER BY ?name
        """,
        {"LOTAD", "POOCHYENA", "RALTS", "SEEDOT", "WURMPLE", "ZIGZAGOON"},
    ),
    "types super-effective against Rock": (
        """
        SELECT ?name WHERE {
          ?m em:defendingType type:rock ;
             em:attackingType ?type ;
             em:multiplier 2.0 .
          ?type rdfs:label ?name .
        } ORDER BY ?name
        """,
        {"Fighting", "Grass", "Ground", "Steel", "Water"},
    ),
    "Torchic's moves by level 15": (
        """
        SELECT ?move ?level WHERE {
          ?entry em:learnsetOf species:torchic ;
                 em:learnMethod em:LevelUp ;
                 em:learnsMove ?m ;
                 em:atLevel ?level .
          ?m rdfs:label ?move .
          FILTER(?level <= 15)
        } ORDER BY ?level
        """,
        {"SCRATCH", "GROWL", "FOCUS ENERGY", "EMBER"},
    ),
    "Roxanne's team": (
        """
        SELECT ?name ?level WHERE {
          trainer:roxanne_1 em:hasPartyMember ?member .
          ?member em:memberSpecies ?species ; em:memberLevel ?level .
          ?species rdfs:label ?name .
        } ORDER BY ?level
        """,
        {"GEODUDE", "NOSEPASS"},
    ),
    "damaging moves Torchic knows by level 15 that beat Rock": (
        """
        SELECT DISTINCT ?move WHERE {
          ?entry em:learnsetOf species:torchic ;
                 em:learnMethod em:LevelUp ;
                 em:learnsMove ?m ;
                 em:atLevel ?level .
          FILTER(?level <= 15)
          ?m rdfs:label ?move ; em:moveType ?type ; em:basePower ?power .
          FILTER(?power > 0)
          ?matchup em:attackingType ?type ;
                   em:defendingType type:rock ;
                   em:multiplier 2.0 .
        }
        """,
        set(),  # Torchic has nothing that beats Rock this early: the honest answer
    ),
    "what Poochyena evolves into": (
        """
        SELECT ?name ?param WHERE {
          ?evo em:evolutionOf species:poochyena ;
               em:evolvesInto ?species ;
               em:triggerParameter ?param .
          ?species rdfs:label ?name .
        }
        """,
        {"MIGHTYENA"},
    ),
}


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "emerald_static.ttl")
    graph = Graph()
    graph.parse(path, format="turtle")
    print(f"loaded {len(graph)} triples from {path}\n")

    failures = 0
    for question, (query, expected) in QUERIES.items():
        rows = list(graph.query(PREFIXES + query))
        answers = {str(row[0]) for row in rows}
        ok = answers == expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {question}")
        for row in rows:
            print("        " + "  ".join(str(value) for value in row))
        if not ok:
            print(f"        expected {sorted(expected)}")
        print()

    print("all competency questions answered correctly" if not failures
          else f"{failures} failing")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
