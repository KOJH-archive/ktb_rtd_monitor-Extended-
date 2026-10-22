"""Read the live values of the supplied KTB Excel RTD workbook. No order routing."""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from analyze_snapshots import RollingRVAnalyzer, timestamp_kst
from extra_settings import ExtraSettings, load_extra_settings
from market_extensions import market_metrics
from monitor_launcher import AlreadyRunning, InstanceLock

# Importing xlwings can initialize the Excel/COM stack. Keep it out of analysis,
# dashboard, and test startup; only the collector needs it.
xw = None


def _load_xlwings():
    global xw
    if xw is None:
        try:
            import xlwings as xlwings_module
        except ImportError as exc:
            raise FeedUnavailable("xlwings is not installed. Run: pip install -r requirements.txt") from exc
        xw = xlwings_module
    return xw

WEIGHTS = (1.0, 0.7, 0.5, 0.3, 0.2)
COM_RETRY_DELAYS = (0.15, 0.35)


class FeedUnavailable(RuntimeError):
    """Excel is unavailable, or its RTD cells do not currently hold usable values."""


class ExcelConnectionUnavailable(FeedUnavailable):
    """Excel/COM could not be reached; retry less often than a bad RTD cell."""


class PollSchedule:
    """Normal polling and bounded backoff for consecutive Excel read failures."""

    def __init__(self, interval_ms: int) -> None:
        self.normal_seconds = interval_ms / 1000
        self.connection_failures = 0

    def success(self) -> float:
        self.connection_failures = 0
        return self.normal_seconds

    def failure(self, connection_error: bool) -> float:
        if not connection_error:
            self.connection_failures = 0
            return self.normal_seconds
        self.connection_failures += 1
        return float(min(5, 2 + self.connection_failures))


@dataclass(frozen=True)
class ContractLayout:
    name: str
    symbol: str
    ask_prices: str
    ask_qty: str
    bid_prices: str
    bid_qty: str
    trade_price: str


# Verified from the supplied workbook, Sheet1.
LAYOUTS = (
    ContractLayout("3Y", "KBFA020", "D2:D6", "C2:C6", "D7:D11", "E7:E11", "G2"),
    ContractLayout("10Y", "KXFA020", "L2:L6", "K2:K6", "L7:L11", "M7:M11", "O2"),
)


def _numbers(values: Iterable[object], label: str, expected_len: int = 5) -> list[float]:
    result: list[float] = []
    for value in values:
        value = value[0] if isinstance(value, list) and len(value) == 1 else value
        if value is None or isinstance(value, bool):
            raise FeedUnavailable(f"{label}: RTD value is blank")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise FeedUnavailable(f"{label}: non-numeric RTD value {value!r}") from exc
        if not math.isfinite(number) or number <= 0:
            raise FeedUnavailable(f"{label}: non-positive value {number}")
        result.append(number)
    if len(result) != expected_len:
        raise FeedUnavailable(f"{label}: expected {expected_len} levels, received {len(result)}")
    return result


def _metrics(prefix: str, bid_prices: list[float], bid_qty: list[float],
             ask_prices: list[float], ask_qty: list[float]) -> dict[str, float]:
    bid_prices = _numbers(bid_prices, f"{prefix} bid prices")
    ask_prices = _numbers(ask_prices, f"{prefix} ask prices")
    bid_qty = _numbers(bid_qty, f"{prefix} bid quantities")
    ask_qty = _numbers(ask_qty, f"{prefix} ask quantities")
    # Excel stores ask levels from 5th ask to best ask; normalize both sides to best→5th.
    ask_prices, ask_qty = list(reversed(ask_prices)), list(reversed(ask_qty))
    if bid_prices != sorted(bid_prices, reverse=True) or ask_prices != sorted(ask_prices):
        raise FeedUnavailable(f"{prefix}: book prices are not in expected bid/ask order")
    best_bid, best_ask, bid1_qty, ask1_qty = bid_prices[0], ask_prices[0], bid_qty[0], ask_qty[0]
    if best_ask <= best_bid:
        raise FeedUnavailable(f"{prefix}: crossed book ({best_bid} / {best_ask})")
    bid_depth, ask_depth = sum(bid_qty), sum(ask_qty)
    weighted_bid = sum(q * w for q, w in zip(bid_qty, WEIGHTS))
    weighted_ask = sum(q * w for q, w in zip(ask_qty, WEIGHTS))
    denominator = bid_depth + ask_depth
    weighted_denominator = weighted_bid + weighted_ask
    top_denominator = bid1_qty + ask1_qty
    row: dict[str, float] = {
        f"{prefix}_best_bid": best_bid, f"{prefix}_best_ask": best_ask,
        f"{prefix}_mid": (best_bid + best_ask) / 2, f"{prefix}_spread": best_ask - best_bid,
        f"{prefix}_bid_depth": bid_depth, f"{prefix}_ask_depth": ask_depth,
        f"{prefix}_obi": (bid_depth - ask_depth) / denominator,
        f"{prefix}_weighted_obi": (weighted_bid - weighted_ask) / weighted_denominator,
        f"{prefix}_microprice": (best_ask * bid1_qty + best_bid * ask1_qty) / top_denominator,
    }
    for level, (bp, bq, ap, aq) in enumerate(zip(bid_prices, bid_qty, ask_prices, ask_qty), start=1):
        row.update({f"{prefix}_bid{level}_price": bp, f"{prefix}_bid{level}_qty": bq,
                    f"{prefix}_ask{level}_price": ap, f"{prefix}_ask{level}_qty": aq})
    return row


class ExcelRtdReader:
    def __init__(self, workbook_name: str, sheet_name: str,
                 extra_settings: ExtraSettings | None = None) -> None:
        self.workbook_name, self.sheet_name = workbook_name, sheet_name
        self._sheet: xw.Sheet | None = None
        self._extra_sheets: dict[str, xw.Sheet] = {}
        self.extra_settings = extra_settings or ExtraSettings()

    def _connect(self) -> xw.Sheet:
        excel = _load_xlwings()
        wanted = Path(self.workbook_name).name.lower()
        for app in excel.apps:
            for book in app.books:
                if Path(book.name).name.lower() == wanted:
                    try:
                        sheet = book.sheets[self.sheet_name]
                        if sheet.api is None:
                            raise ExcelConnectionUnavailable(
                                f"Core sheet {self.sheet_name!r} returned no Excel object")
                        return sheet
                    except KeyError as exc:
                        raise ExcelConnectionUnavailable(f"Sheet {self.sheet_name!r} is not open in {book.name!r}") from exc
        raise ExcelConnectionUnavailable(f"Open {self.workbook_name!r} in desktop Excel first (with the RTD add-in connected).")

    def snapshot(self) -> dict[str, object]:
        """Read one coherent snapshot, absorbing only brief COM contention.

        Invalid market values fail immediately.  Excel/COM access errors get two
        short retries on the existing sheet object before the outer collector
        enters its slower 3/4/5-second reconnect schedule.
        """
        last_error: ExcelConnectionUnavailable | None = None
        for attempt in range(len(COM_RETRY_DELAYS) + 1):
            try:
                return self._snapshot_once()
            except ExcelConnectionUnavailable as exc:
                last_error = exc
                if attempt < len(COM_RETRY_DELAYS):
                    time.sleep(COM_RETRY_DELAYS[attempt])
        self._sheet = None
        self._extra_sheets.clear()
        raise ExcelConnectionUnavailable(
            f"Excel read failed after {len(COM_RETRY_DELAYS) + 1} attempts: {last_error}") from last_error

    def _snapshot_once(self) -> dict[str, object]:
        try:
            self._sheet = self._sheet or self._connect()
            if self._sheet.api is None:
                self._sheet = None
                self._extra_sheets.clear()
                raise ExcelConnectionUnavailable("Core sheet reference is no longer valid")
            # One COM transfer reduces (but cannot eliminate) asynchronous RTD skew.
            try:
                block = self._sheet.range("C2:O11").options(ndim=2).value
            except Exception as exc:
                raise ExcelConnectionUnavailable(f"core quote block C2:O11 failed: {exc}") from exc
            if len(block) != 10 or any(len(values) != 13 for values in block):
                raise FeedUnavailable("Unexpected C2:O11 shape")
            row: dict[str, object] = {"timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds")}
            for name, offset in (("3y", 0), ("10y", 8)):
                trade_price = block[0][offset + 4]
                try:
                    trade_price = float(trade_price) if not isinstance(trade_price, bool) else float("nan")
                except (TypeError, ValueError):
                    trade_price = float("nan")
                row[f"{name}_trade_price"] = trade_price if math.isfinite(trade_price) and trade_price > 0 else ""
                row.update(_metrics(name, [values[offset + 1] for values in block[5:]],
                                    [values[offset + 2] for values in block[5:]],
                                    [values[offset + 1] for values in block[:5]],
                                    [values[offset] for values in block[:5]]))
            try:
                row.update(self._extra_snapshot())
            except FeedUnavailable:
                raise
            except Exception as exc:
                raise ExcelConnectionUnavailable(f"extra RTD block failed: {exc}") from exc
            # Keep the persisted schema stable even when optional RTD values
            # are temporarily blank. A later valid value must not stop logging.
            row.update({"futures_spread": "", "cash_spread_bp": "",
                        "futures_yield_spread_bp": ""})
            # Persist the established derived schema only. New relative-value
            # metrics are deterministically recomputed from raw yields by the UI,
            # avoiding an in-day CSV header change after a software update.
            calculated = market_metrics(row)
            for key in ("futures_spread", "cash_spread_bp", "futures_yield_spread_bp"):
                if key in calculated:
                    row[key] = calculated[key]
            return row
        except FeedUnavailable:
            raise
        except Exception as exc:
            raise ExcelConnectionUnavailable(f"Excel read failed: {exc}") from exc

    @staticmethod
    def _cell_parts(address: str) -> tuple[int, int]:
        match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", address)
        if not match:
            raise FeedUnavailable(f"Invalid extra RTD cell: {address}")
        column = 0
        for char in match.group(1):
            column = column * 26 + ord(char) - 64
        return int(match.group(2)), column

    @staticmethod
    def _cell_address(row: int, column: int) -> str:
        letters = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row}"

    def _extra_snapshot(self) -> dict[str, object]:
        groups: dict[str, list[tuple[str, object]]] = {}
        for key, binding in self.extra_settings.bindings.items():
            if binding is not None:
                groups.setdefault(binding.sheet, []).append((key, binding))
        result: dict[str, object] = {}
        numeric = {"cash_3y_yield_pct", "cash_5y_yield_pct", "cash_10y_yield_pct",
                   "3y_foreign_buy", "3y_foreign_sell",
                   "10y_foreign_buy", "10y_foreign_sell", "3y_futures_yield_pct", "10y_futures_yield_pct"}
        yields = {"cash_3y_yield_pct", "cash_5y_yield_pct", "cash_10y_yield_pct",
                  "3y_futures_yield_pct", "10y_futures_yield_pct"}
        for sheet_name, bindings in groups.items():
            if sheet_name == self.sheet_name:
                sheet = self._sheet
            else:
                sheet = self._extra_sheets.get(sheet_name)
                if sheet is None or sheet.api is None:
                    self._extra_sheets.pop(sheet_name, None)
                    sheet = self._sheet.book.sheets[sheet_name]
                    if sheet.api is None:
                        raise ExcelConnectionUnavailable(
                            f"Extra RTD sheet {sheet_name!r} returned no Excel object")
                    self._extra_sheets[sheet_name] = sheet
            positions = {key: self._cell_parts(binding.cell) for key, binding in bindings}
            rows, columns = zip(*positions.values())
            top, bottom, left, right = min(rows), max(rows), min(columns), max(columns)
            address = f"{self._cell_address(top, left)}:{self._cell_address(bottom, right)}"
            try:
                values = sheet.range(address).options(ndim=2).value
            except Exception:
                self._extra_sheets.pop(sheet_name, None)
                raise
            if not isinstance(values, list):
                values = [[values]]
            for key, (row_number, column_number) in positions.items():
                value = values[row_number - top][column_number - left]
                if key in numeric:
                    if value in (None, "") or isinstance(value, bool):
                        result[key] = ""
                        continue
                    try:
                        number = float(value)
                    except (TypeError, ValueError):
                        number = float("nan")
                    # Optional RTD fields must not discard otherwise valid quotes.
                    # Zero is a valid foreign-volume count, but not a yield.
                    result[key] = (number if math.isfinite(number) and
                                   (number > 0 if key in yields else number >= 0) else "")
                else:
                    result[key] = "" if value is None else str(value).strip()
        return result


class SnapshotLogger:
    def __init__(self, output_dir: Path, csv_enabled: bool, sqlite_enabled: bool,
                 flush_interval: float = 1.0, file_stem: str = "snapshots_with_trades") -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if not re.fullmatch(r"[a-z0-9_]+", file_stem):
            raise ValueError("Invalid output file stem")
        self.csv_path = output_dir / f"{file_stem}.csv"
        db_name = f"{file_stem}.sqlite"
        self.csv_enabled, self.sqlite_enabled = csv_enabled, sqlite_enabled
        self._header_written = self.csv_path.exists() and self.csv_path.stat().st_size > 0
        self.db = sqlite3.connect(output_dir / db_name) if sqlite_enabled else None
        if self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
        self.flush_interval = flush_interval
        self._last_flush = time.monotonic()
        self._csv_handle = None
        self._csv_writer = None
        self._db_insert_sql: str | None = None
        self._columns: list[str] | None = None
        self._dirty = False

    def write(self, row: dict[str, object]) -> None:
        columns = list(row)
        if any(not column.replace("_", "").isalnum() for column in columns):
            raise ValueError("Invalid snapshot column identifier")
        if self._columns is not None and columns != self._columns:
            raise ValueError("Snapshot schema changed during collection")
        if self._columns is None:
            if self.csv_enabled and self._header_written:
                with self.csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
                    if next(csv.reader(handle)) != columns:
                        raise ValueError("Existing CSV schema mismatch; use a new output directory")
                with self.csv_path.open("rb") as handle:
                    handle.seek(-1, 2)
                    if handle.read(1) != b"\n":
                        raise ValueError("Existing CSV ends in a partial row; preserve and repair separately")
            if self.db:
                existing = [item[1] for item in self.db.execute("PRAGMA table_info(snapshots)")]
                if existing and existing != columns:
                    raise ValueError("Existing SQLite schema mismatch; use a new output directory")
            self._columns = columns
        if self.csv_enabled:
            if self._csv_handle is None:
                self._csv_handle = self.csv_path.open("a", newline="", encoding="utf-8")
                self._csv_writer = csv.DictWriter(self._csv_handle, fieldnames=list(row))
            if not self._header_written:
                self._csv_writer.writeheader(); self._header_written = True
            self._csv_writer.writerow(row)
        if self.db:
            if self._db_insert_sql is None:
                self._columns = list(row)
                definitions = ", ".join('"' + column + '" TEXT' for column in self._columns)
                identifiers = ", ".join('"' + column + '"' for column in self._columns)
                placeholders = ", ".join("?" for _ in self._columns)
                self.db.execute(f"CREATE TABLE IF NOT EXISTS snapshots ({definitions})")
                self._db_insert_sql = f"INSERT INTO snapshots ({identifiers}) VALUES ({placeholders})"
            self.db.execute(self._db_insert_sql, [str(row[c]) for c in self._columns])
        self._dirty = True
        self.flush()

    def flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not self._dirty or (not force and now - self._last_flush < self.flush_interval):
            return
        if self._csv_handle:
            self._csv_handle.flush()
        if self.db:
            self.db.commit()
        self._last_flush = now
        self._dirty = False

    def close(self) -> None:
        self.flush(force=True)
        if self._csv_handle:
            self._csv_handle.close()
        if self.db:
            self.db.close()


class SnapshotThrottle:
    """Persist market changes immediately and unchanged heartbeats only occasionally."""

    def __init__(self, heartbeat_seconds: float = 5.0) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self._signature: tuple[tuple[str, object], ...] | None = None
        self._last_write = 0.0

    def should_write(self, row: dict[str, object], now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        signature = tuple((key, value) for key, value in row.items() if key != "timestamp_utc")
        changed = signature != self._signature
        heartbeat_due = now - self._last_write >= self.heartbeat_seconds
        if changed or heartbeat_due:
            self._signature = signature
            self._last_write = now
            return True
        return False


class RollingRV:
    """In-memory relative-value display state; does not alter persisted rows."""
    def __init__(self, window: int = 120, beta: float = 2.88, min_samples: int | None = None) -> None:
        self.window, self.beta = window, beta
        self.min_samples = window if min_samples is None else min_samples
        self.analyzer = RollingRVAnalyzer(window, beta, self.min_samples)
        self.session_date = None

    def update(self, row: dict[str, object]) -> tuple[float, float, float, int, int] | None:
        date = timestamp_kst(row["timestamp_utc"]).date()
        if date != self.session_date:
            self.analyzer.reset()
        self.session_date = date
        result = self.analyzer.update(row)
        return (result["latest_rv_change"], result["level_mean"], result["zscore"],
                result["samples"], self.min_samples) if result else None


def display(row: dict[str, object], interval_ms: int, rv: tuple[float, float, float, int, int] | None = None,
            clear_screen: bool = True) -> None:
    if clear_screen:
        print("\033[2J\033[H", end="")
    print(f"KTB RTD monitor  |  {row['timestamp_utc']}  |  {interval_ms} ms")
    for name in ("3y", "10y"):
        trade = row[name + "_trade_price"]
        trade_text = f"{float(trade):.3f}" if trade not in (None, "") else "N/A"
        print(f"{name.upper():>3}  bid {row[name+'_best_bid']:.3f}  ask {row[name+'_best_ask']:.3f}  "
              f"mid {row[name+'_mid']:.3f}  trade {trade_text}  spr {row[name+'_spread']:.3f}")
        print(f"     OBI {row[name+'_obi']:+.3f}  WOBI {row[name+'_weighted_obi']:+.3f}  "
              f"micro {row[name+'_microprice']:.4f}  depth B/A {row[name+'_bid_depth']:.0f}/{row[name+'_ask_depth']:.0f}")
    if rv is not None:
        print(f"RV level z-score {rv[2]:+.3f}  |  samples {rv[3]}/{rv[4]}  |  latest Δ {rv[0]:+.6f}")
    else:
        print("RV warming up (waiting for a second snapshot)")
    print("Ctrl+C to stop. Data collection only; no order functionality is present.")


def main() -> int:
    parser = argparse.ArgumentParser(description="KTB Excel RTD snapshot collector (no trading).")
    parser.add_argument("--workbook", default="국채선물 호가창.xlsx")
    parser.add_argument("--sheet", default="Sheet1")
    parser.add_argument("--extra-settings", type=Path, default=Path("extra_settings.json"))
    parser.add_argument("--interval-ms", type=int, default=1000,
                        help="Normal Excel polling interval in ms (100-2000; default: 1000)")
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--log", choices=("csv", "sqlite", "both"), default="both")
    parser.add_argument("--flush-ms", type=int, default=1000,
                        help="Flush CSV/SQLite writes in batches (default: 1000 ms)")
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0,
                        help="Write an unchanged-book heartbeat this often (default: 5 seconds)")
    parser.add_argument("--log-all", action="store_true", help="Persist every poll, including duplicates")
    parser.add_argument("--quiet", action="store_true", help="Do not print every snapshot")
    parser.add_argument("--no-clear", action="store_true", help="Do not emit ANSI screen-clear codes")
    args = parser.parse_args()
    if not 100 <= args.interval_ms <= 2000:
        parser.error("--interval-ms must be between 100 and 2000")
    if args.flush_ms < 100: parser.error("--flush-ms must be at least 100")
    if not math.isfinite(args.heartbeat_seconds) or args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be finite and positive")
    extra_settings = load_extra_settings(args.extra_settings)
    reader = ExcelRtdReader(args.workbook, args.sheet, extra_settings)
    enhanced = any(binding is not None for binding in extra_settings.bindings.values())
    logger: SnapshotLogger | None = None
    log_day: str | None = None
    throttle = SnapshotThrottle(args.heartbeat_seconds)
    rv_state = RollingRV()
    poll_schedule = PollSchedule(args.interval_ms)
    last_error_times: dict[str, float] = {}
    collector_lock = InstanceLock(args.output_dir.resolve() / "collector.lock")
    try:
        collector_lock.__enter__()
    except AlreadyRunning:
        print("[collector] Another collector already owns this output directory; exiting.", file=sys.stderr)
        return 2
    try:
        while True:
            started = time.perf_counter()
            next_interval = poll_schedule.normal_seconds
            connection_error = False
            try:
                row = reader.snapshot()
                rv = None if args.quiet else rv_state.update(row)
                if args.log_all or throttle.should_write(row):
                    day = timestamp_kst(row["timestamp_utc"]).strftime("%Y%m%d")
                    if day != log_day:
                        if logger is not None:
                            logger.close()
                        stem = "snapshots_enhanced" if enhanced else "snapshots_with_trades"
                        logger = SnapshotLogger(args.output_dir, args.log in ("csv", "both"),
                                                args.log in ("sqlite", "both"),
                                                flush_interval=args.flush_ms / 1000,
                                                file_stem=f"{stem}_{day}")
                        log_day = day
                    logger.write(row)
                if logger is not None:
                    logger.flush()
                if not args.quiet:
                    display(row, args.interval_ms, rv, not args.no_clear)
                last_error_times.clear()
                next_interval = poll_schedule.success()
            except FeedUnavailable as exc:
                connection_error = isinstance(exc, ExcelConnectionUnavailable)
                next_interval = poll_schedule.failure(connection_error)
                message = str(exc)
                now = time.monotonic()
                if now - last_error_times.get(message, -float("inf")) >= 60:
                    stamp = timestamp_kst(datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S KST")
                    print(f"[{stamp}] [waiting for Excel; retry {next_interval:g}s] {exc}", file=sys.stderr)
                    last_error_times[message] = now
            finally:
                if logger is not None:
                    logger.flush()
            time.sleep(next_interval if connection_error else
                       max(0.0, next_interval - (time.perf_counter() - started)))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if logger is not None:
            logger.close()
        collector_lock.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
