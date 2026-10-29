"""Worker-owned incremental CSV cache; never accesses Tk widgets."""
import csv
import os
from datetime import datetime, timezone, timedelta


class ChartFeed:
    def __init__(self):
        self.path = None
        self.identity = None
        self.offset = 0
        self.header = []
        self.buckets = {}
        self.levels = {}
        self.day = None
        self.rows = []
        self.bytes_read = 0

    def read(self, path, reload=False, target_date=None):
        from spread_chart import aggregate_rows, aggregate_levels
        day = target_date or datetime.now(timezone(timedelta(hours=9))).date()
        try:
            handle = path.open('rb')
        except FileNotFoundError:
            # A new day must never retain the previous day's chart.
            if self.path != path or self.day != day:
                self.__init__()
                self.path, self.day = path, day
                return [], {}, True
            raise
        with handle:
            stat = os.fstat(handle.fileno())
            reset = (reload or path != self.path or day != self.day or
                     stat.st_ino != self.identity or stat.st_size < self.offset)
            if reset:
                raw_header = handle.readline()
                if not raw_header.endswith(b'\n'):
                    raise ValueError('Incomplete CSV header')
                header = next(csv.reader([raw_header.decode('utf-8-sig').strip()]))
                offset = handle.tell()
            else:
                header, offset = self.header, self.offset
                handle.seek(offset)
            # Read a fixed snapshot, leaving an unfinished last row for next time.
            data = handle.read(max(0, stat.st_size - offset))
            self.bytes_read += len(data)
            complete_end = data.rfind(b'\n') + 1
            complete = data[:complete_end]
            parsed = []
            for raw in complete.splitlines():
                if not raw.strip():
                    continue
                values = next(csv.reader([raw.decode('utf-8')]))
                if len(values) != len(header):
                    raise ValueError('CSV column count mismatch')
                parsed.append(dict(zip(header, values)))
            buckets = {} if reset else self.buckets.copy()
            levels = {} if reset else self.levels.copy()
            rows = aggregate_rows(parsed, day, buckets)
            levels = aggregate_levels(parsed, day, levels)
            changed = reset or rows != self.rows
            # Commit only after parsing succeeds, so a retry loses no rows.
            self.path, self.day, self.identity = path, day, stat.st_ino
            self.header, self.offset = header, offset + complete_end
            self.buckets, self.levels, self.rows = buckets, levels, rows
            return rows, levels.copy(), changed
