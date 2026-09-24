"""Descriptive volume, timing, and execution diagnostics for candle exports."""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _items(value):
    if isinstance(value, list):
        return value
    if pd.isna(value):
        return []
    return json.loads(value)


def trade_diagnostics(df, timezone="Europe/London"):
    """Build one observation per completed trade, including all partial exits."""
    entries, exits = [], []
    columns = ["timestamp", "volume", "volume_average", "orders", "closed_positions"]
    for timestamp, volume, average, orders, closed in df[columns].itertuples(index=False, name=None):
        for order in _items(orders):
            if order["kind"] == "ENTRY" and order["status"] == "FILLED":
                entries.append({"trade_id": order["trade_id"], "entry_time": timestamp,
                                "entry_volume": volume,
                                "relative_volume": volume / average if average > 0 else np.nan})
        for position in _items(closed):
            exits.append({"trade_id": position["trade_id"], "exit_time": timestamp,
                          "direction": position["direction"], "exit_reason": position["exit_reason"],
                          "pnl_cash": position["position_total_pnl_cash"],
                          "return_on_exposure": position["position_return"],
                          "tp1_hit": position["tp1_hit"], "tp2_hit": position["tp2_hit"],
                          "tp3_hit": position["tp3_hit"]})
    if not entries or not exits:
        raise ValueError("No completed trades with entry records in this DataFrame.")
    entry_frame, exit_frame = pd.DataFrame(entries), pd.DataFrame(exits)
    trades = entry_frame.merge(exit_frame, on="trade_id", validate="one_to_one")
    if trades.empty:
        raise ValueError("No matching entries and exits; include complete trade histories.")
    trades["entry_time"] = pd.to_datetime(trades.entry_time, utc=True).dt.tz_convert(timezone)
    trades["exit_time"] = pd.to_datetime(trades.exit_time, utc=True).dt.tz_convert(timezone)
    trades["entry_hour"] = trades.entry_time.dt.hour
    trades["entry_weekday"] = trades.entry_time.dt.dayofweek
    trades["holding_minutes"] = (trades.exit_time - trades.entry_time).dt.total_seconds() / 60
    trades["win"] = trades.pnl_cash > 1e-9
    trades["loss"] = trades.pnl_cash < -1e-9
    trades["stop_exit"] = trades.exit_reason.eq("SL")
    trades["stop_before_tp1"] = trades.stop_exit & ~trades.tp1_hit
    trades["stop_after_tp1"] = trades.stop_exit & trades.tp1_hit
    trades.attrs["unmatched_entries"] = len(entries) - len(trades)
    trades.attrs["unmatched_exits"] = len(exits) - len(trades)
    return trades


def _summary(trades, group):
    return trades.groupby(group, observed=True).agg(
        trades=("pnl_cash", "size"), net_pnl=("pnl_cash", "sum"), mean_pnl=("pnl_cash", "mean"),
        win_rate=("win", "mean"), loss_rate=("loss", "mean"), stop_rate=("stop_exit", "mean"),
        stop_before_tp1_rate=("stop_before_tp1", "mean"), stop_after_tp1_rate=("stop_after_tp1", "mean"),
        mean_relative_volume=("relative_volume", "mean"), mean_holding_minutes=("holding_minutes", "mean"),
    )


def plot_trade_diagnostics(df, timezone="Europe/London", output_dir=None):
    """Return summary tables and three figures; rates in tables are decimals.

    Uses the supplied rows only. Pass training rows to restrict the analysis to
    training data. Time-of-day statistics are grouped by entry time, not exit time.
    """
    trades = trade_diagnostics(df, timezone)
    hourly = _summary(trades, "entry_hour").reindex(range(24))
    volume_trades = trades.loc[np.isfinite(trades.relative_volume)].copy()
    if not volume_trades.empty:
        bins = pd.qcut(volume_trades.relative_volume, 4, duplicates="drop")
        if bins.isna().all():
            volume_trades["volume_bucket"] = "All equal"
        else:
            volume_trades["volume_bucket"] = bins
        by_volume = _summary(volume_trades, "volume_bucket")
    else:
        by_volume = pd.DataFrame()

    times = pd.to_datetime(df.timestamp, utc=True).dt.tz_convert(timezone)
    volume = pd.DataFrame({"minute": times.dt.hour * 60 + times.dt.minute, "volume": df.volume})
    daily_volume = volume.groupby("minute").volume.agg(["mean", "sum", "count"])
    hours = np.arange(24)
    positive, negative = "#19876a", "#c74646"

    def pnl_bars(ax, values, labels, title):
        ax.bar(np.arange(len(values)), values, color=[positive if v >= 0 else negative for v in values])
        ax.set_xticks(np.arange(len(values)), labels)
        ax.axhline(0, color="black", linewidth=0.7)
        ax.set(title=title, ylabel="GBP")

    with plt.rc_context({"axes.spines.top": False, "axes.spines.right": False,
                         "axes.titlesize": 12, "font.size": 10}):
        time_fig, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")
        time_fig.suptitle(f"Time of day | {timezone} | {len(trades):,} completed trades", fontsize=16)
        ax = axes[0, 0]
        ax.plot(daily_volume.index / 60, daily_volume["mean"], color="#3974ae")
        ax.set(title="Average candle volume across dates", xlabel="Time of day", ylabel="Source volume",
               xlim=(0, 24), xticks=range(0, 25, 2))
        ax = axes[0, 1]
        directions = pd.crosstab(trades.entry_hour, trades.direction).reindex(hours, fill_value=0)
        bottom = np.zeros(24)
        for direction, color in [("BUY", positive), ("SELL", "#d4982c")]:
            values = directions[direction].to_numpy() if direction in directions else np.zeros(24)
            ax.bar(hours, values, bottom=bottom, label=direction, color=color)
            bottom += values
        ax.set(title="Executed entries by hour", xlabel="Entry hour", ylabel="Trades", xticks=hours)
        ax.legend()
        labels = [f"{hour:02d}\nn={int(n) if pd.notna(n) else 0}" for hour, n in hourly.trades.items()]
        pnl_bars(axes[1, 0], hourly.mean_pnl, labels, "Average final P&L by entry hour")
        axes[1, 0].tick_params(axis="x", labelsize=7)
        ax = axes[1, 1]
        ax.bar(hours, hourly.stop_before_tp1_rate * 100, label="SL before TP1", color=negative)
        ax.bar(hours, hourly.stop_after_tp1_rate * 100, bottom=hourly.stop_before_tp1_rate * 100,
               label="SL after TP1", color="#d4982c")
        ax.plot(hours, hourly.loss_rate * 100, marker="o", label="Net losing trades", color="#333333")
        ax.set(title="Stop exits versus losing trades", xlabel="Entry hour", ylabel="% of trades", ylim=(0, 105), xticks=hours)
        ax.legend(fontsize=9)

        volume_fig, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")
        volume_fig.suptitle("Entry volume and trade outcomes", fontsize=16)
        ax = axes[0, 0]
        for direction, marker, color in [("BUY", "o", positive), ("SELL", "x", "#d4982c")]:
            subset = volume_trades[volume_trades.direction == direction]
            ax.scatter(subset.relative_volume, subset.pnl_cash, s=18, alpha=0.45, label=direction, marker=marker, color=color)
        ax.axhline(0, color="black", linewidth=0.7)
        ax.set(title="Entry relative volume versus final P&L", xlabel="Volume / 20-candle average", ylabel="GBP")
        ax.legend()
        if not by_volume.empty:
            labels = [f"{bucket}\nn={int(count)}" for bucket, count in by_volume.trades.items()]
            pnl_bars(axes[0, 1], by_volume.mean_pnl, labels, "Average P&L by relative-volume quartile")
            axes[0, 1].tick_params(axis="x", labelsize=8)
            ax = axes[1, 0]
            x = np.arange(len(by_volume))
            ax.bar(x - 0.18, by_volume.win_rate * 100, width=0.36, color=positive, label="Win rate")
            ax.bar(x + 0.18, by_volume.stop_rate * 100, width=0.36, color=negative, label="SL exit rate")
            ax.set(xticks=x, xticklabels=labels, title="Win and stop rates by volume", ylabel="% of trades", ylim=(0, 105))
            ax.tick_params(axis="x", labelsize=8)
            ax.legend()
        else:
            for ax in (axes[0, 1], axes[1, 0]):
                ax.text(0.5, 0.5, "No valid relative-volume observations", ha="center", transform=ax.transAxes)
        ax = axes[1, 1]
        heat = trades.pivot_table(index="entry_weekday", columns="entry_hour", values="pnl_cash", aggfunc="mean").reindex(index=range(7), columns=range(24))
        counts = pd.crosstab(trades.entry_weekday, trades.entry_hour).reindex(index=range(7), columns=range(24), fill_value=0)
        scale = max(float(np.nanmax(np.abs(heat.to_numpy()))), 0.01)
        im = ax.imshow(np.ma.masked_invalid(heat.to_numpy()), aspect="auto", cmap="RdYlGn", vmin=-scale, vmax=scale)
        for day, hour in zip(*np.where(counts.to_numpy() > 0)):
            ax.text(hour, day, str(counts.iloc[day, hour]), ha="center", va="center", fontsize=7)
        ax.set(title="Mean P&L by weekday/hour (labels = trade count)", xlabel="Entry hour",
               xticks=hours, yticks=range(7), yticklabels=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        volume_fig.colorbar(im, ax=ax, label="Mean P&L (GBP)", shrink=0.75)

        outcome_fig, axes = plt.subplots(2, 2, figsize=(15, 10), layout="constrained")
        outcome_fig.suptitle("Execution and profitability", fontsize=16)
        ax = axes[0, 0]
        ax.plot(times, df.equity, color="#3974ae")
        ax.set(title="Account equity, including open P&L", ylabel="GBP")
        ax.tick_params(axis="x", rotation=30)
        ax = axes[0, 1]
        ax.hist(trades.pnl_cash, bins="auto", color="#3974ae", edgecolor="white")
        ax.axvline(0, color=negative, linewidth=1)
        ax.set(title="Final trade P&L, including partial exits", xlabel="GBP", ylabel="Trades")
        categories = trades.exit_reason.mask(trades.stop_before_tp1, "SL before TP1").mask(trades.stop_after_tp1, "SL after TP1")
        outcomes = trades.assign(outcome=categories).groupby("outcome").pnl_cash.agg(["size", "sum"])
        ax = axes[1, 0]
        ax.barh(outcomes.index, outcomes["size"] / len(trades) * 100, color="#8877aa")
        for i, count in enumerate(outcomes["size"]):
            ax.text(count / len(trades) * 100 + 0.6, i, f"n={count}", va="center", fontsize=9)
        ax.set(title="How trades finished", xlabel="% of completed trades", xlim=(0, 110))
        pnl_bars(axes[1, 1], outcomes["sum"], outcomes.index, "Total P&L by final exit reason")
        axes[1, 1].tick_params(axis="x", rotation=20, labelsize=8)

    figures = {"time_of_day": time_fig, "volume_outcomes": volume_fig, "trade_outcomes": outcome_fig}
    if output_dir is not None:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        for name, figure in figures.items():
            figure.savefig(directory / f"{name}.png", dpi=160, bbox_inches="tight")
    summary = pd.Series({"completed_trades": len(trades), "net_pnl_gbp": trades.pnl_cash.sum(),
                         "win_rate": trades.win.mean(), "loss_rate": trades.loss.mean(),
                         "stop_exit_rate": trades.stop_exit.mean(),
                         "stop_before_tp1_rate": trades.stop_before_tp1.mean(),
                         "stop_after_tp1_rate": trades.stop_after_tp1.mean(),
                         "unmatched_entries": trades.attrs["unmatched_entries"],
                         "unmatched_exits": trades.attrs["unmatched_exits"]}, name="value")
    return {"summary": summary, "trades": trades, "by_hour": hourly, "by_volume": by_volume,
            "volume_by_time": daily_volume, "figures": figures}
