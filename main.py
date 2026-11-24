"""Confirmed-flow momentum paper bot for the pump.fun replay sprint.

The harness speaks agent-fun/replay/v2: a hello message first (capital,
caps, fees, seed), then one batch per slot decision point. Each batch
carries the fills of earlier orders, the account, fresh marks and the raw
curve events. The orders returned for a batch execute two slots later, so
this bot only acts on flow it has already seen confirmed in the events of
previous batches, never on a single hopeful print.

Strategy in one paragraph: watch every young coin's trade flow, and be
ruthlessly selective about acting on it. A coin earns a buy only while it
is still young in slot age, after at least twenty prints inside the flow
window whose buy quote volume is at least ninety percent of the total,
and while its real quote reserves grew across the last two batches. The
stake is deliberately tiny (0.012 SOL) and at most one new buy goes out
per batch, because every order pays a fixed transaction fee plus curve
fees: in this replay most flow is noise, so the edge comes from trading
rarely and cheaply, not from trading often. Exits sell the whole position
at a +20% take-profit, cut it at a -8% stop-loss, or time-stop it after
900 slots, whichever comes first. Near the end of the window stop opening
positions entirely, since leftovers are flattened at the final marks.

Determinism: no clock, no environment, no randomness. Every decision is a
pure function of the hello and the batches seen so far, and every dict is
walked in insertion order.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402

SOL = makefun_bot.LAMPORTS_PER_SOL

# --- sizing -------------------------------------------------------------
STAKE_LAMPORTS = SOL * 12 // 1000     # 0.012 SOL per position: keep fee drag tiny
MIN_CAP_LAMPORTS = SOL // 50          # size cap must allow >= 0.02 SOL
CASH_RESERVE_LAMPORTS = SOL // 10     # 0.10 SOL always kept back for fees
MAX_NEW_BUYS_PER_BATCH = 1

# --- entry gates ----------------------------------------------------------
MAX_COIN_AGE_SLOTS = 4000             # only young coins, in slot age
FLOW_WINDOW_SLOTS = 600               # flow counts only the last ~600 slots
MIN_TRADES = 20                       # heavy confirmed flow only
MIN_BUY_SHARE_BPS = 9000              # buys >= 90% of window quote volume

# --- exits ----------------------------------------------------------------
TAKE_PROFIT_BPS = 2000                # sell at +20% mark gain
STOP_LOSS_BPS = 800                   # cut at -8% mark loss
MAX_HOLD_SLOTS = 900                  # time-stop: recycle stale positions

# --- late window ------------------------------------------------------------
# A 24h replay at Solana's ~0.4s slot time spans roughly 216k slots.
# Orders decided near the end fill two slots later and whatever is left
# gets flattened at the final marks, so stop opening positions in the
# last stretch and let the exits drain the book instead.
WINDOW_SLOTS = 216_000
LATE_CUTOFF_SLOTS = 3000
BUY_SLIPPAGE_BPS = 2000               # moderate: momentum moves fast
SELL_SLIPPAGE_BPS = 9000              # wide: an exit that fills beats a price


class CoinNote:
    """Running tallies for one mint, folded in batch by batch.

    prints holds (slot, is_buy, quote) for each TradeEvent inside the
    flow window; trail holds the real quote reserve at the close of each
    recent batch that mentioned the mint, newest last.
    """

    __slots__ = ("first_slot", "prints", "trail", "last_real_quote", "bought")

    def __init__(self, slot):
        self.first_slot = slot
        self.prints = deque()
        self.trail = deque(maxlen=4)
        self.last_real_quote = 0
        self.bought = False


notes = {}          # mint -> CoinNote, in first-seen order
entry_slot = {}     # mint -> slot our position was first reported
first_slot_seen = None


def note_for(mint, slot):
    note = notes.get(mint)
    if note is None:
        note = CoinNote(slot)
        notes[mint] = note
    return note


def fold_events(state, batch):
    """Fold this batch's events into the per-mint tallies.

    TradeEvent carries is_buy and sol_amount; when a field is missing the
    sign is recovered from the real quote reserve delta instead.
    """
    for event in batch["events"]:
        kind = event.get("event")
        mint = event.get("mint")
        if mint is None:
            continue
        note = note_for(mint, batch["slot"])
        if kind == "TradeEvent":
            real_quote = int(event.get("real_quote_reserves", "0"))
            amount = event.get("sol_amount", event.get("quote_amount"))
            if amount is None:
                amount = abs(real_quote - note.last_real_quote)
            else:
                amount = int(amount)
            is_buy = event.get("is_buy")
            if is_buy is None:
                is_buy = real_quote >= note.last_real_quote
            note.prints.append((batch["slot"], bool(is_buy), amount))
            note.last_real_quote = real_quote
        elif kind == "CreateEvent":
            note.last_real_quote = int(event.get("real_quote_reserves", "0"))
    # One reserve-trail point per mint per batch, in first-mention order,
    # so the entry gate can ask whether reserves grew across batches.
    seen = []
    for event in batch["events"]:
        if event.get("mint") is not None and event["mint"] not in seen:
            seen.append(event["mint"])
    for mark in batch["marks"]:
        if mark.get("mint") is not None and mark["mint"] not in seen:
            seen.append(mark["mint"])
    for mint in seen:
        curve = state.curves.get(mint)
        if curve is not None:
            notes[mint].trail.append(curve.real_quote_reserves)


def window_flow(note, slot):
    """(trades, buy_quote, sell_quote) over the recent flow window."""
    cutoff = slot - FLOW_WINDOW_SLOTS
    while note.prints and note.prints[0][0] < cutoff:
        note.prints.popleft()
    trades = 0
    buy_quote = 0
    sell_quote = 0
    for _, is_buy, amount in note.prints:
        trades += 1
        if is_buy:
            buy_quote += amount
        else:
            sell_quote += amount
    return trades, buy_quote, sell_quote


def mark_price(curve):
    """Spot price in lamports per token unit, from the virtual reserves."""
    if curve is None or curve.virtual_token_reserves <= 0:
        return 0
    return curve.virtual_quote_reserves * SOL // curve.virtual_token_reserves


def pending_mints(state, side):
    """Mints with an order of the given side still in flight."""
    found = []
    for pending in state.pending.values():
        if pending.order["side"] == side:
            found.append(pending.order["mint"])
    return found


def exit_orders(state, batch, budget):
    """Sell any held position whose mark hit the profit or loss band."""
    orders = []
    selling = pending_mints(state, "sell")
    for mint, holding in state.positions.items():
        if mint in selling or len(orders) >= budget:
            continue
        if mint not in entry_slot:
            entry_slot[mint] = batch["slot"]
        curve = state.curves.get(mint)
        price = mark_price(curve)
        if price <= 0 or holding.token_amount <= 0:
            continue
        # Mark value of the whole position vs what it cost us.
        value = price * holding.token_amount // SOL
        gain_bps = (value - holding.cost_lamports) * 10000 // holding.cost_lamports
        held_slots = batch["slot"] - entry_slot[mint]
        stale = held_slots >= MAX_HOLD_SLOTS
        if gain_bps >= TAKE_PROFIT_BPS or gain_bps <= -STOP_LOSS_BPS or stale:
            orders.append({
                "side": "sell",
                "mint": mint,
                "token_amount": holding.token_amount,
                "max_slippage_bps": SELL_SLIPPAGE_BPS,
            })
    return orders


def entry_orders(state, batch, budget, slots_left):
    """Buy young coins whose confirmed flow is strongly buy-dominant."""
    orders = []
    if budget <= 0 or slots_left <= 0:
        return orders
    tx = state.tx_fee_lamports()
    free = state.free_cash_lamports() - CASH_RESERVE_LAMPORTS
    buying = pending_mints(state, "buy")
    for mint, note in notes.items():
        if len(orders) >= budget or len(orders) >= MAX_NEW_BUYS_PER_BATCH:
            break
        if note.bought or mint in state.positions or mint in buying:
            continue
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        born = curve.created_slot if curve.created_slot is not None else note.first_slot
        if batch["slot"] - born > MAX_COIN_AGE_SLOTS:
            continue
        if MIN_TRADES > 0 or MIN_BUY_SHARE_BPS > 0:
            trades, buy_quote, sell_quote = window_flow(note, batch["slot"])
            if trades < MIN_TRADES:
                continue
            total_quote = buy_quote + sell_quote
            if total_quote <= 0:
                continue
            if buy_quote * 10000 < total_quote * MIN_BUY_SHARE_BPS:
                continue
        # Reserves must be growing across consecutive batches, not just
        # churned: the trail's newest close above the one before it, so
        # net new SOL is actually entering the curve.
        if len(note.trail) < 2 or note.trail[-1] <= note.trail[-2]:
            continue
        cap = state.max_buy_lamports(mint)
        if cap < MIN_CAP_LAMPORTS:
            continue
        amount = STAKE_LAMPORTS
        if amount > cap:
            amount = cap
        if free < amount + tx:
            continue
        orders.append({
            "side": "buy",
            "mint": mint,
            "quote_lamports": amount,
            "max_slippage_bps": BUY_SLIPPAGE_BPS,
        })
        note.bought = True
        free -= amount + tx
        slots_left -= 1
        if slots_left <= 0:
            break
    return orders


def on_batch(state, batch):
    global first_slot_seen
    if first_slot_seen is None:
        first_slot_seen = batch["slot"]
    fold_events(state, batch)
    # Forget entry slots of positions that are gone (sold or flattened),
    # so a later re-buy of the same mint starts a fresh holding clock.
    for mint in list(entry_slot):
        if mint not in state.positions:
            del entry_slot[mint]
    caps = state.hello["caps"]
    max_orders = caps["max_orders_per_batch"]
    exits = exit_orders(state, batch, max_orders)
    entries = []
    late = batch["slot"] - first_slot_seen > WINDOW_SLOTS - LATE_CUTOFF_SLOTS
    if not late:
        slots_left = caps["max_open_positions"] - state.committed_position_count()
        entries = entry_orders(state, batch, max_orders - len(exits), slots_left)
    return exits + entries


if __name__ == "__main__":
    makefun_bot.run(on_batch)
