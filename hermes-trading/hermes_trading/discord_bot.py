"""Discord bot: push pending approvals to a channel with Approve/Reject buttons.

A standalone process — run it alongside the worker and dashboard:

    .venv/Scripts/python -m hermes_trading.discord_bot

It watches the shared SQLite DB (the same one the worker and dashboard use) and:
  * posts a message with **Approve / Reject** buttons whenever a new pending trade
    or strategy change appears,
  * on a button click, flips the pending row's status (exactly what the dashboard
    does — it never fills/applies itself; the worker reconciles on its next tick),
  * keeps the Discord message in sync if you act in the dashboard instead.

It is the *sole filler of nothing*: like the dashboard, it only moves rows from
`pending` to `approved`/`rejected`. The worker remains the only writer that fills
trades or applies strategy changes — so the human-approval invariant is preserved.

Setup (one-time):
  1. Create an application + bot at https://discord.com/developers/applications
  2. Copy the bot token into .env as DISCORD_BOT_TOKEN
  3. Invite the bot to your server with the "Send Messages" permission (scope: bot)
  4. Copy the target channel's ID into .env as DISCORD_CHANNEL_ID
     (enable Developer Mode -> right-click the channel -> Copy Channel ID)

No privileged intents are required (buttons use the interactions gateway).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import Any

try:
    import aiohttp
    import discord
except ModuleNotFoundError:  # pragma: no cover - friendly message if the extra isn't installed
    raise SystemExit(
        "discord.py is not installed. Install the optional extra:\n"
        '    .venv/Scripts/python -m pip install -e ".[discord]"'
    )

from . import db
from .paths import load_env

POLL_SECONDS = int(os.environ.get("DISCORD_POLL_SECONDS", "5"))
DASHBOARD_URL = os.environ.get("HERMES_DASHBOARD_URL", "http://localhost:8000")

GREEN = 0x3FB950
RED = 0xF85149
YELLOW = 0xD29922
BLUE = 0x58A6FF

# kind -> (table name). Both tables share the columns we touch (status, resolved_ts).
_TABLE = {"trade": "pending_trades", "strategy": "pending_strategy"}

# strategy ids with a backtest in flight — guards against double-clicking Backtest
_bt_running: set[int] = set()


# --------------------------------------------------------------------------- #
# DB helpers (sync — called via asyncio.to_thread so they never block the loop)
# --------------------------------------------------------------------------- #

def _ensure_posts_table() -> None:
    """Bot-private table mapping a pending row to the Discord message we posted.

    Kept out of db.SCHEMA so the core app stays unaware of Discord.
    """
    conn = db.connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS discord_posts ("
            "  kind        TEXT    NOT NULL,"      # 'trade' | 'strategy'
            "  ref_id      INTEGER NOT NULL,"      # pending_{trades,strategy}.id
            "  message_id  TEXT    NOT NULL,"
            "  resolved_ts REAL,"                  # set once the card is finalized
            "  PRIMARY KEY (kind, ref_id)"
            ")"
        )
        conn.commit()
    finally:
        conn.close()


def _unposted_pending(kind: str) -> list[dict[str, Any]]:
    table = _TABLE[kind]
    conn = db.connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE status='pending' AND id NOT IN "
            "(SELECT ref_id FROM discord_posts WHERE kind=?) ORDER BY id",
            (kind,),
        ).fetchall()
        return db.rows_to_dicts(rows)
    finally:
        conn.close()


def _record_post(kind: str, ref_id: int, message_id: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO discord_posts(kind, ref_id, message_id, resolved_ts) "
            "VALUES(?,?,?, NULL)",
            (kind, ref_id, str(message_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _open_posts() -> list[dict[str, Any]]:
    """Posted cards not yet finalized — used to reconcile dashboard-side actions."""
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT kind, ref_id, message_id FROM discord_posts WHERE resolved_ts IS NULL"
        ).fetchall()
        return db.rows_to_dicts(rows)
    finally:
        conn.close()


def _mark_resolved(kind: str, ref_id: int) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE discord_posts SET resolved_ts=? WHERE kind=? AND ref_id=?",
            (db.now(), kind, ref_id),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_row(kind: str, ref_id: int) -> dict[str, Any] | None:
    table = _TABLE[kind]
    conn = db.connect()
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (ref_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _status(kind: str, ref_id: int) -> str | None:
    row = _fetch_row(kind, ref_id)
    return row["status"] if row else None


def _resolve(kind: str, action: str, ref_id: int) -> tuple[str, str | None]:
    """Atomically flip a pending row to approved/rejected.

    Returns ('done', status) if we won the race, ('already', current_status) if it
    was handled elsewhere first, or ('missing', None) if the row is gone.
    """
    table = _TABLE[kind]
    status = "approved" if action == "approve" else "rejected"
    conn = db.connect()
    try:
        cur = conn.execute(
            f"UPDATE {table} SET status=?, resolved_ts=? WHERE id=? AND status='pending'",
            (status, db.now(), ref_id),
        )
        conn.commit()
        if cur.rowcount == 1:
            return ("done", status)
        row = conn.execute(f"SELECT status FROM {table} WHERE id=?", (ref_id,)).fetchone()
        return ("already", row["status"]) if row else ("missing", None)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Embeds
# --------------------------------------------------------------------------- #

def _money(x: Any) -> str:
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _pending_embed(kind: str, row: dict[str, Any]) -> "discord.Embed":
    if kind == "trade":
        e = discord.Embed(title="🟡 Trade needs approval", color=YELLOW)
        e.add_field(name="Symbol", value=str(row["symbol"]), inline=True)
        e.add_field(name="Side", value=str(row["side"]), inline=True)
        e.add_field(name="Size", value=f"{float(row['size']):.6f}", inline=True)
        e.add_field(name="Price", value=_money(row["price"]), inline=True)
        e.add_field(name="Stop", value=_money(row["stop_price"]), inline=True)
        # rotation uses a sentinel ~price*1e6 to mean "no target; rules exit"
        tgt = float(row["target_price"]) if row["target_price"] is not None else 0.0
        tgt_str = _money(tgt) if tgt and tgt < float(row["price"]) * 50 else "rules-based"
        e.add_field(name="Target", value=tgt_str, inline=True)
        if row.get("rsi") is not None:
            e.add_field(name="RSI", value=f"{float(row['rsi']):.1f}", inline=True)
        e.add_field(name="Strategy", value=f"v{row['strategy_version']}", inline=True)
        e.add_field(name="Proposed", value=f"<t:{int(float(row['proposed_ts']))}:R>", inline=True)
    else:
        e = discord.Embed(title="🧠 Strategy change needs approval", color=BLUE)
        e.add_field(name="Source", value=str(row["source"]), inline=True)
        e.add_field(
            name="Change",
            value=f"`{row['variable']}`  {row['old_value']} → **{row['new_value']}**",
            inline=False,
        )
        e.add_field(
            name="Version", value=f"v{row['from_version']} → v{row['to_version']}", inline=True
        )
        if row.get("rationale"):
            e.add_field(name="Rationale", value=str(row["rationale"])[:1000], inline=False)
    e.set_footer(text=f"#{row['id']} · open the dashboard: {DASHBOARD_URL}")
    return e


def _final_embed(kind: str, row: dict[str, Any] | None, status: str, who: str) -> "discord.Embed":
    approved = status != "rejected"
    title = "✅ Approved" if approved else "❌ Rejected"
    color = GREEN if approved else RED
    if kind == "trade" and row:
        desc = f"**{row['symbol']}** {row['side']} · {float(row['size']):.6f} @ {_money(row['price'])}"
    elif kind == "strategy" and row:
        desc = f"`{row['variable']}`  {row['old_value']} → **{row['new_value']}**  (v{row['to_version']})"
    else:
        desc = ""
    e = discord.Embed(title=title, description=desc, color=color)
    label = "trade" if kind == "trade" else "strategy change"
    e.set_footer(text=f"{label} #{row['id'] if row else '?'} · {status} by {who}")
    return e


# --------------------------------------------------------------------------- #
# Backtest (strategy cards) — mirrors the dashboard's current-vs-proposed compare
# --------------------------------------------------------------------------- #

def _run_backtest(proposed_yaml: str) -> dict[str, Any]:
    """Backtest current vs proposed over 10y (full period + held-out OOS). Blocking;
    call via asyncio.to_thread. Returns the compare() result or {'error': ...}."""
    import yaml

    from .backtest import compare
    from .config import load_strategy

    try:
        proposed = yaml.safe_load(proposed_yaml) or {}
        if proposed.get("type") != "relative_strength_rotation":
            return {"error": "backtest only supported for the rotation strategy"}
        return compare(load_strategy(), proposed, years=10)
    except Exception as exc:  # noqa: BLE001 — surface the message, never crash the bot
        return {"error": str(exc)[:300]}


def _save_backtest(ref_id: int, result: dict[str, Any]) -> None:
    """Cache the result on the row so the dashboard shows the same numbers."""
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE pending_strategy SET backtest_json=? WHERE id=?",
            (json.dumps(result), ref_id),
        )
        conn.commit()
    finally:
        conn.close()


def _pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _backtest_field(embed: "discord.Embed", bt: dict[str, Any] | None) -> None:
    """Add a backtest summary field to a strategy embed (full period + OOS verdict)."""
    if not bt or bt.get("error"):
        embed.add_field(
            name="📊 Backtest", value=f"⚠️ {bt.get('error', 'failed') if bt else 'failed'}",
            inline=False,
        )
        return
    c, p = bt["current"], bt["proposed"]
    lines = [
        f"**Full** {bt['start']}→{bt['end']}",
        f"CAGR {_pct(c['cagr'])}→**{_pct(p['cagr'])}** · "
        f"maxDD {_pct(c['max_drawdown'])}→**{_pct(p['max_drawdown'])}** · "
        f"Sharpe {c['sharpe']:.2f}→**{p['sharpe']:.2f}**",
    ]
    co, po = bt.get("current_oos"), bt.get("proposed_oos")
    if co and po:
        better = po["sharpe"] > co["sharpe"]
        verdict = "holds up out-of-sample ✅" if better else "does NOT improve OOS ❌ (likely overfit)"
        lines.append(
            f"\n**Out-of-sample** (from {bt['oos_start']} — the trustworthy test)\n"
            f"CAGR {_pct(co['cagr'])}→**{_pct(po['cagr'])}** · "
            f"maxDD {_pct(co['max_drawdown'])}→**{_pct(po['max_drawdown'])}** · "
            f"Sharpe {co['sharpe']:.2f}→**{po['sharpe']:.2f}** — {verdict}"
        )
    embed.add_field(name="📊 Backtest (10y)", value="\n".join(lines)[:1024], inline=False)


# --------------------------------------------------------------------------- #
# Interactive view (persistent across restarts)
# --------------------------------------------------------------------------- #

class ApprovalView(discord.ui.View):
    """Approve/Reject buttons bound to one pending row.

    Custom_ids are static so the view is persistent; the kind/ref_id live on the
    instance and are re-bound to each message via ``bot.add_view(..., message_id=)``
    on startup, so buttons keep working after a bot restart.
    """

    def __init__(self, kind: str, ref_id: int) -> None:
        super().__init__(timeout=None)
        self.kind = kind
        self.ref_id = ref_id
        # Backtest applies only to strategy proposals — drop it from trade cards.
        if kind != "strategy":
            item = self._child("hermes:backtest")
            if item is not None:
                self.remove_item(item)

    def _child(self, custom_id: str) -> "discord.ui.Item | None":
        for c in self.children:
            if getattr(c, "custom_id", None) == custom_id:
                return c
        return None

    async def _act(self, interaction: "discord.Interaction", action: str) -> None:
        outcome, info = await asyncio.to_thread(_resolve, self.kind, action, self.ref_id)
        row = await asyncio.to_thread(_fetch_row, self.kind, self.ref_id)
        who = interaction.user.display_name
        if outcome == "done":
            await interaction.response.edit_message(
                embed=_final_embed(self.kind, row, info or action, who), view=None
            )
            await asyncio.to_thread(_mark_resolved, self.kind, self.ref_id)
            self.stop()
        elif outcome == "already":
            await interaction.response.edit_message(
                embed=_final_embed(self.kind, row, info or "resolved", "the dashboard"), view=None
            )
            await interaction.followup.send(f"Already {info} — no change made.", ephemeral=True)
            await asyncio.to_thread(_mark_resolved, self.kind, self.ref_id)
            self.stop()
        else:
            await interaction.response.send_message("That item no longer exists.", ephemeral=True)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="hermes:approve")
    async def approve(self, interaction: "discord.Interaction", _button: "discord.ui.Button") -> None:
        await self._act(interaction, "approve")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="hermes:reject")
    async def reject(self, interaction: "discord.Interaction", _button: "discord.ui.Button") -> None:
        await self._act(interaction, "reject")

    @discord.ui.button(label="Backtest 10y", style=discord.ButtonStyle.secondary, custom_id="hermes:backtest")
    async def backtest(self, interaction: "discord.Interaction", button: "discord.ui.Button") -> None:
        ref_id = self.ref_id
        if ref_id in _bt_running:
            await interaction.response.send_message(
                "A backtest is already running for this proposal…", ephemeral=True
            )
            return
        row = await asyncio.to_thread(_fetch_row, "strategy", ref_id)
        if not row or row["status"] != "pending":
            await interaction.response.send_message(
                "This proposal is no longer pending.", ephemeral=True
            )
            return

        # ack within 3s: disable the button + show progress, keep Approve/Reject live
        _bt_running.add(ref_id)
        button.disabled = True
        button.label = "Backtesting… (~30s)"
        await interaction.response.edit_message(view=self)
        try:
            result = await asyncio.to_thread(_run_backtest, row["proposed_yaml"])
            await asyncio.to_thread(_save_backtest, ref_id, result)
        finally:
            _bt_running.discard(ref_id)
            button.disabled = False
            button.label = "Backtest 10y"

        # if it was approved/rejected while the backtest ran, leave the final card alone
        fresh = await asyncio.to_thread(_fetch_row, "strategy", ref_id)
        if not fresh or fresh["status"] != "pending":
            return
        embed = _pending_embed("strategy", fresh)
        _backtest_field(embed, result)
        try:
            await interaction.edit_original_response(embed=embed, view=self)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #

class HermesBot(discord.Client):
    def __init__(self, channel_id: int, *, connector: "aiohttp.BaseConnector | None" = None) -> None:
        # default (non-privileged) intents are enough — button interactions are not
        # gated by gateway intents, and we only need guild/channel access to post.
        super().__init__(intents=discord.Intents.default(), connector=connector)
        self.channel_id = channel_id
        self._poller: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        # Re-bind persistent views to their messages so clicks work after a restart.
        for post in await asyncio.to_thread(_open_posts):
            self.add_view(
                ApprovalView(post["kind"], int(post["ref_id"])),
                message_id=int(post["message_id"]),
            )
        self._poller = self.loop.create_task(self._poll_loop())

    async def on_ready(self) -> None:
        print(
            f"discord bot online as {self.user} — watching channel {self.channel_id}",
            flush=True,
        )

    async def _get_channel(self) -> "discord.abc.Messageable":
        ch = self.get_channel(self.channel_id)
        if ch is None:
            ch = await self.fetch_channel(self.channel_id)
        return ch  # type: ignore[return-value]

    async def _poll_loop(self) -> None:
        await self.wait_until_ready()
        try:
            channel = await self._get_channel()
        except Exception as exc:  # noqa: BLE001
            print(f"FATAL: cannot access channel {self.channel_id}: {exc}", flush=True)
            await self.close()
            return

        while not self.is_closed():
            try:
                # 1) post newly-pending items
                for kind in ("trade", "strategy"):
                    for row in await asyncio.to_thread(_unposted_pending, kind):
                        msg = await channel.send(
                            embed=_pending_embed(kind, row),
                            view=ApprovalView(kind, int(row["id"])),
                        )
                        await asyncio.to_thread(_record_post, kind, int(row["id"]), msg.id)

                # 2) reconcile anything resolved in the dashboard (or filled by the worker)
                for post in await asyncio.to_thread(_open_posts):
                    status = await asyncio.to_thread(_status, post["kind"], int(post["ref_id"]))
                    if status and status != "pending":
                        row = await asyncio.to_thread(_fetch_row, post["kind"], int(post["ref_id"]))
                        msg = channel.get_partial_message(int(post["message_id"]))
                        try:
                            await msg.edit(
                                embed=_final_embed(post["kind"], row, status, "the dashboard"),
                                view=None,
                            )
                        except discord.NotFound:
                            pass
                        await asyncio.to_thread(_mark_resolved, post["kind"], int(post["ref_id"]))
            except Exception as exc:  # noqa: BLE001 — never let the poller die
                print(f"poll error: {exc}", flush=True)
            await asyncio.sleep(POLL_SECONDS)


def main() -> None:
    load_env()
    db.init_db()
    _ensure_posts_table()

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    channel_raw = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not token or not channel_raw:
        raise SystemExit(
            "Discord not configured. Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID in .env "
            "(see the module docstring for the one-time setup)."
        )
    try:
        channel_id = int(channel_raw)
    except ValueError:
        raise SystemExit(f"DISCORD_CHANNEL_ID must be a number, got {channel_raw!r}")

    try:
        asyncio.run(_run_bot(token, channel_id))
    except KeyboardInterrupt:
        pass


async def _run_bot(token: str, channel_id: int) -> None:
    # Force aiohttp's threaded (OS getaddrinfo) resolver. The default c-ares/aiodns
    # resolver can't read DNS servers on some Windows setups and fails every request
    # with "Could not contact DNS servers" — even though the OS resolver works fine.
    # Building the connector inside the running loop binds it to the right loop.
    discord.utils.setup_logging()  # standard discord console logs (run() used to do this)
    connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    bot = HermesBot(channel_id, connector=connector)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    main()
