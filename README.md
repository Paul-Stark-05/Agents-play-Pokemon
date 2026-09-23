# Emerald KG Agents

**Work in progress.** Two LLM agents play the same Pokémon Emerald battle on real
hardware emulation. One can query a knowledge graph built from the game's own
data; the other works from what the model already knows. Every factual claim
either makes is logged and checked against the graph afterwards.

The question is narrow on purpose: **does grounding an agent in a knowledge graph
change the decisions it makes, and the things it asserts on the way there?**

---

## How it works

```
pokeemerald decompilation ──► static RDF graph (101k triples, Apache Jena Fuseki)
                                        │
mGBA ──► Lua bridge ──► Python client ──┤
  │         (TCP)         (RAM decode)  │
  │                                     ├──► grounded agent   (SPARQL tools)
  └────────── button presses ◄──────────┤
                                        └──► parametric agent (no tools)
                                                    │
                            turns.jsonl ──► claim checker ──► error rates
```

The two agents run in two mGBA windows linked by the emulator's cable
emulation, so the final demo is an actual 6v6 link battle in the Pokémon
Center, one agent per side.

### The graph

Built directly from [pret/pokeemerald](https://github.com/pret/pokeemerald),
not from a fan wiki, because the demo measures wrong facts and needs a source
of truth that cannot itself be wrong:

| | count |
|---|---|
| species | 386 |
| moves | 354 |
| type matchups | 110 |
| level-up learnset entries | 3,947 |
| TMs and HMs | 58 (8,927 species entries) |
| evolutions | 172 |
| wild encounter slots | 1,975 across 209 tables |
| trainers | 854 (1,825 party members) |
| **total triples** | **101,105** |

The ontology keeps Generation III mechanics honest. Damage class hangs off the
*type*, not the move, because that is how Gen III works. Type effectiveness,
learnsets, evolutions and encounters are reified so they can carry their
multipliers, levels and rates. Species are keyed on the game's internal ID —
Torchic is 280 in memory, 255 in the National Dex — with both Dex numbers as
properties.

### Reading the game

The bridge is deliberately thin: read memory, press buttons, take screenshots.
All decoding is in Python, including Gen III's encrypted party structure — 48
bytes per Pokémon XOR-encrypted with the personality value against the trainer
ID, with the four substructures permuted by `personality % 24`.

Menu timing comes from the game's own state rather than from the screen. The
controller reads the function pointer installed for the player's battler and
compares it against the input handlers, so it knows exactly when the game wants
an action, a move, or a Pokémon — and never presses A through a decision.

### Fairness rules

These exist because breaking any of them would produce a flattering result that
means nothing:

- **Same model, same prompt, same observations.** The only difference between
  the arms is whether the graph tools exist.
- **No hidden information.** The opponent's full team, movesets and exact HP are
  all readable from RAM. Agents see species, level, HP percentage and status —
  what a player sees. Types and abilities are not given: the grounded agent
  looks them up, the other has to recall them.
- **Memory is read-only.** The bridge has no write command.
- **Lockstep.** The emulator only advances through bridge commands, so model
  latency cannot affect the game.
- **Every claim is checked.** Claims are matched by pattern and adjudicated by
  the graph, never by a model, and each verdict prints the fact it used.
  Unrecognised claims are reported as unchecked, not as correct.

---

## Status

Working:

- static graph extraction, with competency questions as a regression suite
- live layer: per-step state diff and an append-only event log in Fuseki
- memory reading: party decryption, live battle state, ROM name tables
- battle control: action and move menus, move learning in battle and after
  evolution, switching
- the grounded agent, its SPARQL tools, and the claim checker
- two-window link battles in mGBA

Not done yet:

- the parametric arm has not been run head to head
- party-menu switching is verified against the decompilation but not on screen
- National and Hoenn Dex numbers, items, and per-slot encounter rates are not
  extracted
- no repeat runs, so nothing here is a measurement yet

### An example that makes the point

Asked whether to replace Peck with Double Kick, the grounded agent queried the
graph for both moves against Geodude and got:

```
DOUBLE KICK  Fighting  x2.0  STAB  effective power 90
PECK         Flying    x0.5  no STAB  effective power 17.5
```

and kept Double Kick for the Rock-type gym it had not yet reached. In the same
run it also asserted that a Pokémon's Special Defense was lower than its
Defense while the tool output in its context said both were 23. Grounding
constrained the decision; it did not eliminate the false claim. Counting how
often that happens, on both arms, is the point of the claim checker.

---

## Running it

You need a legally dumped English Pokémon Emerald ROM — SHA1
`f3ae088181bf583e55daf962a92bb46f4f1d07b7`, the build the decompilation
matches. **No ROM is included here and none will be.** Every memory address in
this repo assumes that exact build.

The agents read `OPENAI_API_KEY` from the environment. Nothing in this repo
stores a key, and `.gitignore` covers `.env` files.

```bash
pip install rdflib openai

# 1. build the static graph and check it
python build_static_kg.py -o emerald_static.ttl
python check_queries.py emerald_static.ttl

# 2. load it into Fuseki (one dataset per agent)
fuseki-server --tdb2 --loc=databases/emerald-p1 --update /emerald-p1
curl -X POST -H "Content-Type: text/turtle" \
  --data-binary @emerald_static.ttl http://localhost:3030/emerald-p1/data

# 3. in mGBA: Tools > Scripting > File > Load script > emerald_bridge.lua
#    first window binds 8888, second 8889

# 4. with a battle on screen
export OPENAI_API_KEY=...
python battle_agent.py --grounded    --port 8888 --endpoint http://localhost:3030/emerald-p1 --log turns_grounded.jsonl
python battle_agent.py --parametric  --port 8889 --endpoint http://localhost:3031/emerald-p2 --log turns_parametric.jsonl

# 5. check what they claimed
python claim_check.py turns_grounded.jsonl --graph emerald_static.ttl
```

## Files

| file | what it does |
|---|---|
| `emerald_ontology.ttl` | the schema: game data and live playthrough state |
| `build_static_kg.py` | decompilation → RDF, with count assertions |
| `check_queries.py` | competency questions with hand-checked answers |
| `emerald_bridge.lua` | thin transport inside mGBA: read memory, press buttons |
| `emerald.py` | Python client and all decoding |
| `kg.py` | Fuseki client, live state diff, event log |
| `battle_control.py` | battle menus, driven by the game's controller state |
| `field_control.py` | the move prompt inside the evolution scene |
| `battle_agent.py` | both agents, the tools, the turn loop, the logs |
| `claim_check.py` | claims vs the graph, with coverage reported |

## Credits and licence

Game data is extracted at build time from the
[pret/pokeemerald](https://github.com/pret/pokeemerald) decompilation project,
whose work made all of this possible. Emulation is
[mGBA](https://mgba.io); the triple store is
[Apache Jena Fuseki](https://jena.apache.org).

Pokémon is a trademark of Nintendo, Creatures Inc. and GAME FREAK Inc. This is
an unaffiliated research project. The code here is mine; the game and its data
are not.
