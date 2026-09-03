#!/usr/bin/env python3
"""Felt & Brass -- networked host for LAN blackjack.
Run this on one PC; everyone else opens http://<this-pc-ip>:8787/ in a browser
on the same network. Stdlib only, no install needed.
"""
import json, os, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# random.SystemRandom pulls every value straight from os.urandom (/dev/urandom
# on Linux) instead of the seeded Mersenne Twister random.shuffle() normally
# uses -- no reproducible state, no seed to guess, fit for a real card shoe.
rng = secrets.SystemRandom()

PORT = 8787
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "table_state.json")
CLIENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client.html")

MIN_BET = 10
START_BANKROLL = 1000
NUM_DECKS = 6
DEALER_TIMEOUT = 20  # seconds of no polling before the dealer seat is considered abandoned
PLAYER_TIMEOUT = 30  # seconds of no polling before a player is dropped (closed tab, crashed browser, etc.)

SUITS = [("S", "black"), ("H", "red"), ("D", "red"), ("C", "black")]
RANKS = list("A23456789") + ["10", "J", "Q", "K"]

lock = threading.RLock()

def fresh_shoe():
    deck = []
    for _ in range(NUM_DECKS):
        for sym, color in SUITS:
            for rank in RANKS:
                deck.append({"rank": rank, "suit": sym, "color": color})
    rng.shuffle(deck)
    return deck

def card_value(rank):
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)

def hand_total(cards):
    total = sum(card_value(c["rank"]) for c in cards)
    aces = sum(1 for c in cards if c["rank"] == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def is_blackjack(cards):
    return len(cards) == 2 and hand_total(cards) == 21

def new_id():
    return secrets.token_hex(5)

STATE = {
    "dealer_id": None,
    "players": {},        # id -> {id,name,bankroll}
    "pending_bets": {},    # id -> amount
    "ready": {},           # id -> bool, cleared every betting phase
    "phase": "betting",    # betting | round
    "round": None,
    "round_seq": 0,        # bumped on every deal so clients can tell a fresh round from a card added to one already on screen
    "shoe": fresh_shoe(),
}

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "dealer_id": STATE["dealer_id"],
                "players": STATE["players"],
                "phase": "betting",  # never resume mid-round across restarts
            }, f)
    except Exception:
        pass

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        STATE["dealer_id"] = data.get("dealer_id")
        STATE["players"] = data.get("players", {})
    except Exception:
        pass

def draw_card():
    if len(STATE["shoe"]) < 20:
        STATE["shoe"] = fresh_shoe()
    return STATE["shoe"].pop()

def betting_player_ids():
    return [pid for pid in STATE["players"] if pid != STATE["dealer_id"]]

LOCALHOST_IPS = ("127.0.0.1", "::1")

def public_state(viewer_id, is_localhost=False):
    players = []
    for pid, p in STATE["players"].items():
        players.append({
            "id": pid,
            "name": p["name"],
            "bankroll": p["bankroll"],
            "isDealer": pid == STATE["dealer_id"],
            "pendingBet": STATE["pending_bets"].get(pid, 0),
            "ready": STATE["ready"].get(pid, False),
        })
    round_out = None
    r = STATE["round"]
    if r:
        dealer_hand = list(r["dealer_hand"])
        hidden = r["dealer_hidden"]
        if hidden:
            # nobody sees the hole card while it's face-down -- not even the
            # dealer's own screen, so there's no peeking ahead of the players
            dealer_hand = [dealer_hand[0], None]
        round_out = {
            "roundId": r["seq"],
            "order": r["order"],
            "turnIdx": r["turn_idx"],
            "phase": r["phase"],
            "dealerHand": dealer_hand,
            "dealerHidden": hidden,
            "dealerBlackjackEarly": r["dealer_blackjack_early"],
            "seats": r["seats"],
        }
    return {
        "dealerId": STATE["dealer_id"],
        "players": players,
        "phase": STATE["phase"],
        "round": round_out,
        "you": viewer_id,
        "canBeDealer": is_localhost,
    }

def bettors_awaiting():
    """Players who've put money down for this round but haven't hit Ready yet."""
    return [
        pid for pid in betting_player_ids()
        if STATE["pending_bets"].get(pid, 0) >= MIN_BET and not STATE["ready"].get(pid, False)
    ]

def maybe_auto_deal():
    active = [pid for pid in betting_player_ids() if STATE["pending_bets"].get(pid, 0) >= MIN_BET]
    if active and not bettors_awaiting():
        start_round()

def start_round():
    active = [pid for pid in betting_player_ids() if STATE["pending_bets"].get(pid, 0) >= MIN_BET]
    if not active:
        return False
    seats = {}
    for pid in active:
        bet = STATE["pending_bets"][pid]
        STATE["players"][pid]["bankroll"] -= bet
        seats[pid] = {"bet": bet, "cards": [], "status": "playing", "result": None}
    dealer_hand = []
    for _ in range(2):
        for pid in active:
            seats[pid]["cards"].append(draw_card())
        dealer_hand.append(draw_card())
    STATE["round_seq"] += 1
    STATE["round"] = {
        "seq": STATE["round_seq"],
        "order": active,
        "seats": seats,
        "dealer_hand": dealer_hand,
        "dealer_hidden": True,
        "turn_idx": 0,
        "phase": "player-turns",
        "dealer_blackjack_early": False,
    }
    STATE["phase"] = "round"
    STATE["ready"] = {}

    up = dealer_hand[0]
    shows_bj = up["rank"] == "A" or card_value(up["rank"]) == 10
    if shows_bj and is_blackjack(dealer_hand):
        STATE["round"]["dealer_hidden"] = False
        STATE["round"]["dealer_blackjack_early"] = True
        for pid in active:
            seat = seats[pid]
            if is_blackjack(seat["cards"]):
                seat["result"] = "push"
                STATE["players"][pid]["bankroll"] += seat["bet"]
            else:
                seat["result"] = "lose"
        STATE["round"]["phase"] = "resolved"
        save_state()
        return True

    for pid in active:
        if is_blackjack(seats[pid]["cards"]):
            seats[pid]["status"] = "blackjack"

    advance_turn(from_start=True)
    save_state()
    return True

def advance_turn(from_start=False):
    r = STATE["round"]
    if not from_start:
        r["turn_idx"] += 1
    while r["turn_idx"] < len(r["order"]):
        seat = r["seats"][r["order"][r["turn_idx"]]]
        if seat["status"] == "playing":
            return
        r["turn_idx"] += 1
    run_dealer_and_finish()

def run_dealer_and_finish():
    r = STATE["round"]
    r["phase"] = "dealer"
    r["dealer_hidden"] = False
    all_bust = all(r["seats"][pid]["status"] == "bust" for pid in r["order"])
    if not all_bust:
        while hand_total(r["dealer_hand"]) < 17:
            r["dealer_hand"].append(draw_card())
    dealer_total = hand_total(r["dealer_hand"])
    dealer_bust = dealer_total > 21
    for pid in r["order"]:
        seat = r["seats"][pid]
        p = STATE["players"][pid]
        if seat["status"] == "bust":
            seat["result"] = "lose"
            continue
        if seat["status"] == "blackjack":
            seat["result"] = "blackjack"
            p["bankroll"] += round(seat["bet"] * 2.5)
            continue
        total = hand_total(seat["cards"])
        if dealer_bust or total > dealer_total:
            seat["result"] = "win"
            p["bankroll"] += seat["bet"] * 2
        elif total == dealer_total:
            seat["result"] = "push"
            p["bankroll"] += seat["bet"]
        else:
            seat["result"] = "lose"
    r["phase"] = "resolved"

def remove_player(pid):
    """Drop a player, whether they left themselves or the dealer kicked them."""
    if not pid or pid not in STATE["players"]:
        return
    if STATE["dealer_id"] == pid:
        STATE["dealer_id"] = None
    STATE["pending_bets"].pop(pid, None)
    STATE["ready"].pop(pid, None)
    r = STATE["round"]
    in_round = r is not None and pid in r.get("seats", {})
    if in_round:
        seat = r["seats"][pid]
        if seat["status"] == "playing":
            seat["status"] = "stood"
            if r["phase"] == "player-turns" and r["order"][r["turn_idx"]] == pid:
                advance_turn()
        # keep the player record until the round is settled — the payout math needs it
        STATE["players"][pid]["leaving"] = True
    else:
        del STATE["players"][pid]
    if STATE["phase"] == "betting":
        maybe_auto_deal()
    save_state()

def prune_stale_players():
    """A closed tab / crashed browser just stops polling -- there's no reliable
    'the user left' signal from the client, so treat a long silence as a leave."""
    now = time.time()
    stale = [pid for pid, p in STATE["players"].items()
             if now - p.get("last_seen", now) > PLAYER_TIMEOUT]
    for pid in stale:
        remove_player(pid)

# ---------------- HTTP handling ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _is_localhost(self):
        return self.client_address[0] in LOCALHOST_IPS

    def _touch(self, pid):
        p = STATE["players"].get(pid)
        if p:
            p["last_seen"] = time.time()

    def _reply(self, pid):
        self._touch(pid)
        self._send_json(public_state(pid, self._is_localhost()))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self._serve_client()
            return
        if parsed.path == "/api/state":
            qs = parse_qs(parsed.query)
            viewer = (qs.get("id") or [None])[0]
            with lock:
                prune_stale_players()
                self._reply(viewer)
            return
        self.send_response(404)
        self.end_headers()

    def _serve_client(self):
        try:
            with open(CLIENT_FILE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            body = b"client.html missing"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        data = self._read_json()
        with lock:
            prune_stale_players()
            if parsed.path == "/api/join":
                self._join(data)
            elif parsed.path == "/api/claim_dealer":
                self._claim_dealer(data)
            elif parsed.path == "/api/release_dealer":
                self._release_dealer(data)
            elif parsed.path == "/api/bet":
                self._bet(data)
            elif parsed.path == "/api/clear_bet":
                self._clear_bet(data)
            elif parsed.path == "/api/ready":
                self._ready(data)
            elif parsed.path == "/api/deal":
                self._deal(data)
            elif parsed.path == "/api/action":
                self._action(data)
            elif parsed.path == "/api/next_round":
                self._next_round(data)
            elif parsed.path == "/api/adjust":
                self._adjust(data)
            elif parsed.path == "/api/rename":
                self._rename(data)
            elif parsed.path == "/api/leave":
                self._leave(data)
            elif parsed.path == "/api/kick":
                self._kick(data)
            else:
                self._send_json({"error": "not found"}, 404)

    def _join(self, data):
        name = (data.get("name") or "").strip()[:18] or "Player"
        pid = data.get("id")
        if pid and pid in STATE["players"]:
            self._reply(pid)
            return
        pid = new_id()
        STATE["players"][pid] = {"id": pid, "name": name, "bankroll": START_BANKROLL, "last_seen": time.time()}
        save_state()
        self._reply(pid)

    def _claim_dealer(self, data):
        pid = data.get("id")
        if pid not in STATE["players"]:
            self._send_json({"error": "unknown player"}, 400)
            return
        if not self._is_localhost():
            self._send_json({"error": "only the host machine can deal"}, 403)
            return
        current = STATE["dealer_id"]
        current_stale = (
            current is not None
            and current != pid
            and time.time() - STATE["players"].get(current, {}).get("last_seen", 0) > DEALER_TIMEOUT
        )
        if current is not None and current != pid and current_stale:
            # the previous dealer stopped polling (closed tab, server restart, etc.) — vacate it
            STATE["dealer_id"] = None
            current = None
        if STATE["dealer_id"] is None:
            STATE["dealer_id"] = pid
            STATE["pending_bets"].pop(pid, None)
            save_state()
        self._reply(pid)

    def _release_dealer(self, data):
        pid = data.get("id")
        if STATE["dealer_id"] == pid:
            STATE["dealer_id"] = None
            save_state()
        self._reply(pid)

    def _bet(self, data):
        pid, amount = data.get("id"), int(data.get("amount", 0))
        p = STATE["players"].get(pid)
        if not p or pid == STATE["dealer_id"] or STATE["phase"] != "betting":
            self._reply(pid); return
        current = STATE["pending_bets"].get(pid, 0)
        if current + amount <= p["bankroll"]:
            STATE["pending_bets"][pid] = current + amount
            STATE["ready"][pid] = False  # changing your bet un-readies you
            save_state()
        self._reply(pid)

    def _clear_bet(self, data):
        pid = data.get("id")
        STATE["pending_bets"][pid] = 0
        STATE["ready"][pid] = False
        self._reply(pid)

    def _ready(self, data):
        pid = data.get("id")
        p = STATE["players"].get(pid)
        if not p or pid == STATE["dealer_id"] or STATE["phase"] != "betting":
            self._reply(pid); return
        if STATE["pending_bets"].get(pid, 0) < MIN_BET:
            self._reply(pid); return
        STATE["ready"][pid] = not STATE["ready"].get(pid, False)
        maybe_auto_deal()
        save_state()
        self._reply(pid)

    def _deal(self, data):
        pid = data.get("id")
        no_live_dealer = STATE["dealer_id"] is None
        allowed = (pid == STATE["dealer_id"]) or (no_live_dealer and pid in STATE["players"])
        if not allowed or STATE["phase"] != "betting":
            self._reply(pid); return
        start_round()
        self._reply(pid)

    def _action(self, data):
        pid, act = data.get("id"), data.get("action")
        r = STATE["round"]
        if not r or r["phase"] != "player-turns":
            self._reply(pid); return
        if not r["order"] or r["order"][r["turn_idx"]] != pid:
            self._reply(pid); return
        seat = r["seats"][pid]
        p = STATE["players"][pid]
        if act == "hit":
            seat["cards"].append(draw_card())
            total = hand_total(seat["cards"])
            if total > 21:
                seat["status"] = "bust"; advance_turn()
            elif total == 21:
                seat["status"] = "stood"; advance_turn()
        elif act == "stand":
            seat["status"] = "stood"; advance_turn()
        elif act == "double":
            if len(seat["cards"]) == 2 and p["bankroll"] >= seat["bet"]:
                p["bankroll"] -= seat["bet"]
                seat["bet"] *= 2
                seat["cards"].append(draw_card())
                seat["status"] = "bust" if hand_total(seat["cards"]) > 21 else "stood"
                advance_turn()
        save_state()
        self._reply(pid)

    def _next_round(self, data):
        pid = data.get("id")
        # with a live dealer only they can call it; with no dealer seated (the
        # host chose to sit down as a player) the house deals itself, so any
        # player at the table can wave the round on
        if STATE["dealer_id"] is not None and pid != STATE["dealer_id"]:
            self._reply(pid); return
        if pid not in STATE["players"]:
            self._reply(pid); return
        STATE["pending_bets"] = {}
        STATE["ready"] = {}
        broke = [p for p in betting_player_ids() if STATE["players"][p]["bankroll"] <= 0]
        left = [p for p in STATE["players"] if STATE["players"][p].get("leaving")]
        for b in set(broke) | set(left):
            del STATE["players"][b]
        STATE["round"] = None
        STATE["phase"] = "betting"
        save_state()
        self._reply(pid)

    def _leave(self, data):
        pid = data.get("id")
        remove_player(pid)
        self._send_json({"ok": True})

    def _kick(self, data):
        pid, target = data.get("id"), data.get("target")
        if pid != STATE["dealer_id"] or not target or target == pid:
            self._send_json({"ok": False}); return
        remove_player(target)
        self._reply(pid)

    def _adjust(self, data):
        pid, target, delta = data.get("id"), data.get("target"), int(data.get("delta", 0))
        if pid != STATE["dealer_id"] or target not in STATE["players"]:
            self._reply(pid); return
        STATE["players"][target]["bankroll"] = max(0, STATE["players"][target]["bankroll"] + delta)
        save_state()
        self._reply(pid)

    def _rename(self, data):
        pid, name = data.get("id"), (data.get("name") or "").strip()[:18]
        if pid in STATE["players"] and name:
            STATE["players"][pid]["name"] = name
            save_state()
        self._reply(pid)


def local_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


if __name__ == "__main__":
    load_state()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = local_ip()
    print(f"Felt & Brass dealer host running.")
    print(f"  On this PC:        http://127.0.0.1:{PORT}/")
    print(f"  For other PCs/LAN: http://{ip}:{PORT}/")
    server.serve_forever()
