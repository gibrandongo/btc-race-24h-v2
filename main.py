"""V-reclaim bot for the make.fun BTC race (agent-fun/bars-replay/v1).

Strategy, all integer math: watch the last 20 one-minute closes. When the
20-bar close range is wide (>= 80 bps of the window high), the trough is
fresh, and the latest close has reclaimed >= 30 bps above the trough while
still sitting below the window high, buy with all cash. Exit at +60 bps
target, -50 bps stop (both from the entry fill price), or after 120 bars,
whichever comes first. A 10-bar cooldown follows every exit, and no new
entry is taken after batch 1380.

Protocol: the harness sends hello, then one batch per minute, then done.
Every batch gets exactly one reply echoing its seq. Orders fill at the next
bar's open, so decisions use closes and the fill prices the harness reports.
"""

import json
import sys

BPS = 10000
SYMBOL = "CBBTC"

WINDOW = 20            # closes in the rolling window
RANGE_BPS = 80         # min hi-lo range, in bps of the window high
RECLAIM_BPS = 30       # min reclaim above the trough close, in bps
TARGET_BPS = 60        # exit target above the entry fill, in bps
STOP_BPS = 50          # exit stop below the entry fill, in bps
MAX_HOLD = 120         # max bars to hold, counted from the fill batch
COOLDOWN = 10          # bars to wait after an exit fill before re-entering
LAST_ENTRY_SEQ = 1380  # no entries on batches after this seq
MAX_SLIPPAGE_BPS = 1000


def main():
    closes = []  # (seq, close) pairs, in the order the bars arrived
    entry_price = None  # fill price of the open position, if any
    entry_seq = None    # seq of the batch that reported the entry fill
    cooldown_until = -1  # earliest seq a new entry may be decided on
    min_notional = 1000000  # smallest buy the replay accepts, from hello
    holding_since = None  # first seq a position was seen with no entry fill
    order_no = 0
    out = sys.stdout

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mtype = msg.get("type")
        if mtype == "done":
            break
        if mtype == "hello":
            try:
                min_notional = int(msg["fees"]["min_notional_quote_units"])
            except (KeyError, TypeError, ValueError):
                pass
            continue
        if mtype != "batch":
            continue
        seq = msg["seq"]

        # Learn what actually happened from the fills this batch reports.
        # A rejected order leaves no fill and changes nothing.
        for fill in msg.get("fills", []):
            if fill.get("status") != "filled":
                continue
            if fill["side"] == "buy":
                entry_price = int(fill["price"])
                entry_seq = seq
                holding_since = None
            elif fill["side"] == "sell":
                entry_price = None
                entry_seq = None
                cooldown_until = seq + COOLDOWN

        # The account is ground truth for what is held right now.
        account = msg["account"]
        pos_qty = 0
        for pos in account["positions"]:
            if pos["symbol"] == SYMBOL:
                pos_qty = int(pos["qty"])
        if pos_qty == 0:
            entry_price = None
            entry_seq = None
            holding_since = None

        # Record this batch's close, if the symbol has a bar this minute.
        for bar in msg.get("bars", []):
            if bar["symbol"] == SYMBOL:
                closes.append((seq, int(bar["c"])))

        orders = []
        if pos_qty > 0:
            # Holding: exit on target, stop, or max hold, whichever first.
            # If the entry fill price was never observed, fall back to a
            # max-hold exit counted from the first batch the position was
            # seen, so a position can never ride to the end flatten just
            # because a fill record was missed.
            should_exit = False
            if entry_price is not None and closes:
                close = closes[-1][1]
                hit_target = close * BPS >= entry_price * (BPS + TARGET_BPS)
                hit_stop = close * BPS <= entry_price * (BPS - STOP_BPS)
                too_long = entry_seq is not None and seq - entry_seq >= MAX_HOLD
                should_exit = hit_target or hit_stop or too_long
            elif entry_price is None:
                if holding_since is None:
                    holding_since = seq
                elif seq - holding_since >= MAX_HOLD:
                    should_exit = True
            if should_exit:
                order_no += 1
                orders.append({
                    "side": "sell",
                    "client_id": "s%d" % order_no,
                    "symbol": SYMBOL,
                    "qty": str(pos_qty),
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                })
        elif pos_qty == 0 and len(closes) >= WINDOW:
            # Flat: look for a fresh trough reclaim inside a wide window.
            if seq <= LAST_ENTRY_SEQ and seq >= cooldown_until:
                window = closes[-WINDOW:]
                high = max(c for _, c in window)
                trough_seq, trough = min(window, key=lambda item: item[1])
                close = closes[-1][1]
                wide = (high - trough) * BPS >= RANGE_BPS * high
                fresh = seq - trough_seq <= WINDOW
                reclaimed = (close - trough) * BPS >= RECLAIM_BPS * trough
                below_high = close < high
                if wide and fresh and reclaimed and below_high:
                    cash = int(account["cash_quote_units"])
                    if cash >= min_notional:
                        order_no += 1
                        orders.append({
                            "side": "buy",
                            "client_id": "b%d" % order_no,
                            "symbol": SYMBOL,
                            "notional_quote_units": str(cash),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })

        out.write(json.dumps({"type": "reply", "seq": seq, "orders": orders}) + "\n")
        out.flush()


main()
