from emerald import Emerald
from kg import KG, LiveGraph

game = Emerald(8888)
live = LiveGraph(KG("http://localhost:3030/emerald-p1"), player="p1")

for mon in game.party():
    print(f"{mon.name:<12} Lv{mon.level:<3} {mon.hp}/{mon.max_hp}")

events = live.sync(game.party(), game.battle(), game.frame())
print("events:", events)