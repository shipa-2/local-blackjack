# Felt & Brass

A local-network multiplayer blackjack table. One machine hosts the game;
everyone else on the same network joins from a browser and plays hot-seat
style, with every player's hand, bet, and status visible to the whole table
in real time.

No build step, no dependencies, no accounts — just Python's standard
library on the host and a browser on every device.

## Running it

On the host machine:

```bash
python3 server.py
```

The console prints two URLs:

```
On this PC:        http://127.0.0.1:8787/
For other PCs/LAN: http://<host-ip>:8787/
```

Open the first on the host, and the second on every other device on the
same network (same Wi-Fi / LAN). Everyone picks a name and joins.

## Playing

- **Dealer** — only a browser running on the host machine itself
  (`127.0.0.1`) can claim the dealer seat, from the "Run the table" button.
  The dealer doesn't bet; they run the shoe and can hand out or take back
  chips freely from the dealer console.
- **No dealer?** The table still plays: once everyone who's placed a bet
  hits **Ready**, the round deals itself and the house plays by standard
  rules (hits under 17, stands on 17+).
- **Everyone sees everyone** — bets, hands, and status (playing / stood /
  bust / blackjack / win / lose / push) are visible to the whole table at
  all times, the same way a real table works.
- **Leaving** — closing the tab (or losing connection) drops a player from
  the table automatically after about 30 seconds of no activity; the
  dealer seat frees up the same way after 20 seconds, so a closed host tab
  doesn't lock the table forever. The dealer can also kick a player
  directly.

## Rules

- Six-deck shoe, reshuffled once it runs low, shuffled with
  `random.SystemRandom()` (reads from `/dev/urandom`) rather than a seeded
  PRNG.
- Blackjack pays 3:2. Dealer peeks for blackjack on an Ace/10 upcard and
  settles naturals immediately. The dealer's hole card is hidden from
  everyone — including the dealer — until every player has acted.
- Hit, stand, and double down (on the first two cards, for players who can
  cover the extra bet). No splits.

## Files

- `server.py` — the whole backend: game state, rules, and a small JSON API
  over `http.server`. No third-party packages.
- `client.html` — the single-page frontend. Polls the server for state and
  renders the table; no build tooling involved.

Game state (players, bankrolls, who's dealing) is written to
`table_state.json` next to `server.py` after every change, so restarting
the host doesn't reset the table.
