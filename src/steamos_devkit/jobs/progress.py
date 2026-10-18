"""rsync output parsers (spec section 7.3).

``--info=progress2`` emits carriage-return-separated overall-progress records;
``--itemize-changes`` emits one line per entry with an 11-column flag field.
Both parsers are total functions over messy byte streams: unrecognized lines
are ignored, never fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from steamos_devkit.models import JobProgress

# "     29,974,559  67%    2.79GB/s    0:00:00 (xfr#3, to-chk=4/13)"
_PROGRESS2 = re.compile(
    r"^\s*(?P<bytes>[\d,]+)\s+"
    r"(?P<pct>\d{1,3})%\s+"
    r"(?P<rate>[\d.]+)(?P<unit>[kMGTPE]?i?B/s)\s+"
    r"(?P<eta>[\d:.]+)"
    r"(?:\s*\(xfr#(?P<xfr>\d+),\s*(?:ir|to)-chk=(?P<chk_left>\d+)/(?P<chk_total>\d+)\))?"
)

_RATE_UNITS = {
    "B/s": 1.0,
    "kB/s": 1e3,
    "KB/s": 1e3,
    "kiB/s": 1024.0,
    "KiB/s": 1024.0,
    "MB/s": 1e6,
    "MiB/s": 2**20,
    "GB/s": 1e9,
    "GiB/s": 2**30,
    "TB/s": 1e12,
    "TiB/s": 2**40,
    "PB/s": 1e15,
    "PiB/s": 2**50,
    "EB/s": 1e18,
    "EiB/s": 2**60,
}

_STATS_TRANSFERRED = re.compile(r"Total transferred file size:\s*([\d,]+)\s*bytes")


def split_records(text: str) -> list[str]:
    """progress2 records are separated by \\r as well as \\n."""
    return [part for part in re.split(r"[\r\n]+", text) if part.strip()]


def parse_progress2_line(line: str, *, phase: str = "transferring") -> JobProgress | None:
    """Parse one progress2 record; None for non-progress lines."""
    m = _PROGRESS2.match(line)
    if not m:
        return None
    bytes_done = int(m["bytes"].replace(",", ""))
    pct = int(m["pct"])
    bytes_total = round(bytes_done * 100 / pct) if pct > 0 else None
    rate = float(m["rate"]) * _RATE_UNITS.get(m["unit"], 1.0)
    items_done = int(m["xfr"]) if m["xfr"] is not None else None
    return JobProgress(
        phase=phase,
        bytes_done=bytes_done,
        bytes_total=bytes_total,
        items_done=items_done,
        rate_bps=rate,
    )


def parse_stats_transferred_bytes(text: str) -> int | None:
    """``Total transferred file size`` from ``--stats`` output (works with -n)."""
    m = _STATS_TRANSFERRED.search(text)
    return int(m[1].replace(",", "")) if m else None


@dataclass(frozen=True)
class ItemizeEntry:
    change: Literal["added", "changed", "deleted"]
    path: str
    is_dir: bool


def parse_itemize_line(line: str) -> ItemizeEntry | None:
    """Parse one ``--itemize-changes`` line.

    Only file/dir entries in the transfer direction (``>`` or ``<``) and
    ``*deleting`` lines are entries; attribute-only chatter (``c``, ``h``,
    ``.`` leaders) and non-entry lines are ignored.
    """
    if line.startswith("*deleting"):
        path = line[len("*deleting") :].strip()
        return ItemizeEntry(change="deleted", path=path, is_dir=False)
    if len(line) < 12:
        return None
    flags, path = line[:11], line[12:]
    # file/dir transfer entries lead with '>' or '<'; directory *creation*
    # leads with 'c' ("cd+++++++++"); anything else is attribute chatter.
    if flags[1] not in ("f", "d"):
        return None
    if flags[0] == "c" and not (flags[1] == "d" and flags[2:] == "+++++++++"):
        return None
    if flags[0] not in (">", "<", "c"):
        return None
    if not path:
        return None
    is_dir = flags[1] == "d"
    added = flags[2:] == "+++++++++"
    return ItemizeEntry(change="added" if added else "changed", path=path, is_dir=is_dir)
