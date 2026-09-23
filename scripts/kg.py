"""Knowledge graph client for the Emerald agents.

Talks to an Apache Jena Fuseki dataset over HTTP. The static graph sits in the
default graph; the live layer uses two named graphs:

    graph:state   what is true now, rewritten per step
    graph:events  append-only log, never rewritten

Usage:

    from emerald import Emerald
    from kg import KG, LiveGraph

    game = Emerald(8888)
    kg = KG("http://localhost:3030/emerald-p1")
    live = LiveGraph(kg, player="p1")

    events = live.sync(game.party(), game.battle(), game.frame())

Self-test without a server (runs the same SPARQL against rdflib in memory):

    python kg.py --selftest
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

EM = "https://mypokemon.org/emerald/ontology#"
ID = "https://mypokemon.org/emerald/id/"
GRAPH_STATE = "https://mypokemon.org/emerald/graph/state"
GRAPH_EVENTS = "https://mypokemon.org/emerald/graph/events"

PREFIXES = f"""PREFIX em: <{EM}>
PREFIX id: <{ID}>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
"""


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ----------------------------------------------------------------- transport

class KG:
    def __init__(self, endpoint: str = "http://localhost:3030/emerald-p1", timeout: float = 30.0):
        self.query_url = endpoint.rstrip("/") + "/sparql"
        self.update_url = endpoint.rstrip("/") + "/update"
        self.timeout = timeout
        self._species: dict = {}
        self._moves: dict = {}

    def _post(self, url: str, body: str, content_type: str, accept: str | None = None) -> bytes:
        request = urllib.request.Request(url, data=body.encode("utf-8"))
        request.add_header("Content-Type", content_type + "; charset=utf-8")
        if accept:
            request.add_header("Accept", accept)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def select(self, query: str) -> list:
        raw = self._post(self.query_url, PREFIXES + query,
                         "application/sparql-query", "application/sparql-results+json")
        results = json.loads(raw)
        return [
            {name: binding[name]["value"] for name in binding}
            for binding in results["results"]["bindings"]
        ]

    def ask(self, query: str) -> bool:
        raw = self._post(self.query_url, PREFIXES + query,
                         "application/sparql-query", "application/sparql-results+json")
        return json.loads(raw)["boolean"]

    def update(self, query: str) -> None:
        self._post(self.update_url, PREFIXES + query, "application/sparql-update")

    def count(self) -> int:
        return int(self.select("SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }")[0]["n"])

    # --- joining live RAM values to static entities, by internal ID
    def species_uri(self, internal_id: int) -> str | None:
        if internal_id not in self._species:
            rows = self.select(
                f"SELECT ?s WHERE {{ ?s a em:Species ; em:internalId {int(internal_id)} }}")
            self._species[internal_id] = rows[0]["s"] if rows else None
        return self._species[internal_id]

    def move_uri(self, internal_id: int) -> str | None:
        if internal_id not in self._moves:
            rows = self.select(
                f"SELECT ?s WHERE {{ ?s a em:Move ; em:internalId {int(internal_id)} }}")
            self._moves[internal_id] = rows[0]["s"] if rows else None
        return self._moves[internal_id]


# ----------------------------------------------------------------- live layer

class LiveGraph:
    """Writes the current state and appends events, one call per agent step."""

    def __init__(self, kg: KG, player: str = "p1"):
        self.kg = kg
        self.player = player
        self.player_uri = f"{ID}player/{player}"
        self._event_counter = 0

    # --- reading back what we last wrote
    def current_state(self) -> dict:
        rows = self.kg.select(f"""
        SELECT ?pid ?level ?hp ?status ?slot WHERE {{
          GRAPH <{GRAPH_STATE}> {{
            ?mon em:ownedBy <{self.player_uri}> ;
                 em:personality ?pid ;
                 em:level ?level ;
                 em:currentHP ?hp ;
                 em:statusCondition ?status ;
                 em:partySlot ?slot .
          }}
        }}""")
        return {
            int(row["pid"]): {
                "level": int(row["level"]),
                "hp": int(row["hp"]),
                "status": row["status"],
                "slot": int(row["slot"]),
            }
            for row in rows
        }

    def known_moves(self, personality: int) -> set:
        rows = self.kg.select(f"""
        SELECT ?move WHERE {{
          GRAPH <{GRAPH_STATE}> {{
            ?mon em:personality {int(personality)} ;
                 em:ownedBy <{self.player_uri}> ;
                 em:hasMoveSlot/em:slotMove ?move .
          }}
        }}""")
        return {row["move"] for row in rows}

    # --- writing
    def instance_uri(self, personality: int) -> str:
        return f"{ID}instance/{self.player}/{personality}"

    def _party_triples(self, party: list) -> str:
        lines = [f"<{self.player_uri}> a em:Player ."]
        for mon in party:
            uri = self.instance_uri(mon.personality)
            species = self.kg.species_uri(mon.species)
            lines += [
                f"<{uri}> a em:PokemonInstance ;",
                f"    em:ownedBy <{self.player_uri}> ;",
                f"    em:personality {mon.personality} ;",
                f'    em:nickname "{escape(mon.nickname)}" ;',
                f"    em:partySlot {mon.slot} ;",
                f"    em:level {mon.level} ;",
                f"    em:currentHP {mon.hp} ;",
                f"    em:maxHP {mon.max_hp} ;",
                f'    em:statusCondition "{escape(mon.status)}"',
            ]
            if species:
                lines.append(f"    ; em:instanceOf <{species}>")
            for index, move in enumerate(mon.moves, start=1):
                lines.append(f"    ; em:hasMoveSlot <{uri}/slot{index}>")
            lines.append(".")
            for index, move in enumerate(mon.moves, start=1):
                move_uri = self.kg.move_uri(move["id"])
                lines.append(f"<{uri}/slot{index}> a em:MoveSlot ; em:remainingPP {move['pp']}"
                             + (f" ; em:slotMove <{move_uri}>" if move_uri else "") + " .")
        return "\n".join(lines)

    def _delete_party(self, party: list) -> str:
        """Drop the previous triples for these instances before reinserting them."""
        statements = []
        for mon in party:
            uri = self.instance_uri(mon.personality)
            statements.append(f"DELETE WHERE {{ GRAPH <{GRAPH_STATE}> {{ <{uri}> ?p ?o }} }};")
            for index in range(1, 5):
                statements.append(
                    f"DELETE WHERE {{ GRAPH <{GRAPH_STATE}> {{ <{uri}/slot{index}> ?p ?o }} }};")
        return "\n".join(statements)

    def _event_triples(self, events: list, frame: int) -> str:
        lines = []
        for event in events:
            self._event_counter += 1
            uri = f"{ID}event/{self.player}/{frame}-{self._event_counter}"
            lines += [
                f"<{uri}> a em:{event['type']} ;",
                f"    em:atFrame {frame} ;",
                f"    em:subjectInstance <{self.instance_uri(event['personality'])}>",
            ]
            if "previous" in event:
                lines.append(f'    ; em:previousValue "{escape(str(event["previous"]))}"')
            if "new" in event:
                lines.append(f'    ; em:newValue "{escape(str(event["new"]))}"')
            if "move" in event and event["move"]:
                lines.append(f"    ; em:usedMove <{event['move']}>")
            lines.append(".")
        return "\n".join(lines)

    def diff(self, party: list) -> list:
        """Compare the party against what the graph currently holds."""
        previous = self.current_state()
        events = []
        for mon in party:
            before = previous.get(mon.personality)
            if before is None:
                events.append({"type": "CatchEvent", "personality": mon.personality,
                               "new": mon.name})
                continue
            if mon.level > before["level"]:
                events.append({"type": "LevelUpEvent", "personality": mon.personality,
                               "previous": before["level"], "new": mon.level})
            if mon.hp != before["hp"]:
                kind = "FaintEvent" if mon.hp == 0 else "DamageEvent"
                if mon.hp > before["hp"]:
                    kind = "DamageEvent"  # healing is a damage event with a rising value
                events.append({"type": kind, "personality": mon.personality,
                               "previous": before["hp"], "new": mon.hp})
            if mon.status != before["status"]:
                events.append({"type": "StatusEvent", "personality": mon.personality,
                               "previous": before["status"], "new": mon.status})
            known = self.known_moves(mon.personality)
            for move in mon.moves:
                uri = self.kg.move_uri(move["id"])
                if uri and uri not in known:
                    events.append({"type": "MoveLearnedEvent", "personality": mon.personality,
                                   "new": move["name"], "move": uri})
        return events

    def sync(self, party: list, battle: dict | None, frame: int) -> list:
        """One step: work out what changed, then write state and events."""
        events = self.diff(party)
        update = [
            self._delete_party(party),
            f"INSERT DATA {{ GRAPH <{GRAPH_STATE}> {{\n{self._party_triples(party)}\n}} }};",
        ]
        event_triples = self._event_triples(events, frame)
        if event_triples:
            update.append(f"INSERT DATA {{ GRAPH <{GRAPH_EVENTS}> {{\n{event_triples}\n}} }};")
        observation = f"{ID}observation/{self.player}/{frame}"
        update.append(f"""INSERT DATA {{ GRAPH <{GRAPH_EVENTS}> {{
  <{observation}> a em:Observation ;
      em:observedAtFrame {frame} ;
      em:observedPlayer <{self.player_uri}> .
}} }};""")
        self.kg.update("\n".join(update))
        return events

    def clear(self) -> None:
        """Wipe the live layer. Static graph is untouched."""
        self.kg.update(f"DROP SILENT GRAPH <{GRAPH_STATE}>; DROP SILENT GRAPH <{GRAPH_EVENTS}>;")


# ------------------------------------------------------------------ self-test

def _selftest() -> int:
    """Run the generated SPARQL against an in-memory rdflib dataset."""
    from dataclasses import dataclass, field
    from rdflib import Dataset

    dataset = Dataset()

    class FakeKG(KG):
        def __init__(self):
            self._species = {280: f"{ID}species/torchic"}
            self._moves = {52: f"{ID}move/ember", 45: f"{ID}move/growl"}

        def select(self, query):
            rows = []
            for row in dataset.query(PREFIXES + query):
                rows.append({str(var): str(value) for var, value in zip(row.labels, row)})
            return rows

        def update(self, query):
            dataset.update(PREFIXES + query)

    @dataclass
    class FakeMon:
        slot: int
        personality: int
        species: int
        name: str
        nickname: str
        level: int
        hp: int
        max_hp: int
        status: str = "ok"
        moves: list = field(default_factory=list)

    live = LiveGraph(FakeKG(), player="p1")

    first = [FakeMon(1, 12345, 280, "TORCHIC", "TORCHIC", 14, 38, 38,
                     moves=[{"id": 52, "name": "EMBER", "pp": 25}])]
    events = live.sync(first, None, 1000)
    assert [e["type"] for e in events] == ["CatchEvent"], events

    second = [FakeMon(1, 12345, 280, "TORCHIC", "TORCHIC", 15, 12, 40, "burned",
                      moves=[{"id": 52, "name": "EMBER", "pp": 24},
                             {"id": 45, "name": "GROWL", "pp": 40}])]
    events = live.sync(second, None, 1600)
    kinds = sorted(e["type"] for e in events)
    assert kinds == ["DamageEvent", "LevelUpEvent", "MoveLearnedEvent", "StatusEvent"], kinds

    state = live.current_state()
    assert state[12345]["level"] == 15 and state[12345]["hp"] == 12, state
    graphs = {str(g.identifier) for g in dataset.graphs()}
    assert GRAPH_STATE in graphs and GRAPH_EVENTS in graphs, graphs

    rows = live.kg.select(f"""
      SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH <{GRAPH_EVENTS}> {{ ?e a em:GameEvent }} }}""")
    print("state rows :", len(state))
    print("event kinds:", kinds)
    print("selftest passed")
    return 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    kg = KG(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3030/emerald-p1")
    print("triples in dataset:", kg.count())
