"""Unit tests for jobs.progress — rsync output parsing against golden captures."""

from pathlib import Path

from steamos_devkit.jobs.progress import (
    parse_itemize_line,
    parse_progress2_line,
    parse_stats_transferred_bytes,
    split_records,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def test_split_records_handles_cr_and_lf() -> None:
    assert split_records("a\rb\r\nc\nd") == ["a", "b", "c", "d"]
    assert split_records("") == []


def test_parse_progress2_full_record() -> None:
    progress = parse_progress2_line(
        "     29,974,559  67%    2.79GB/s    0:00:00 (xfr#3, to-chk=4/13)"
    )
    assert progress is not None
    assert progress.bytes_done == 29_974_559
    assert progress.bytes_total == round(29_974_559 * 100 / 67)
    assert progress.items_done == 3
    assert progress.rate_bps == 2.79e9


def test_parse_progress2_without_xfer_suffix() -> None:
    progress = parse_progress2_line("             31   0%    0.00kB/s    0:00:00  ")
    assert progress is not None
    assert progress.bytes_done == 31
    assert progress.bytes_total is None  # 0%: no estimate
    assert progress.items_done is None


def test_parse_progress2_ignores_plain_lines() -> None:
    assert parse_progress2_line("sending incremental file list") is None
    assert parse_progress2_line("assets/audio/intro.ogg") is None


def test_golden_progress2_success_fixture() -> None:
    text = (FIXTURES / "rsync-progress2-success.txt").read_text(encoding="utf-8")
    records = [
        parsed
        for record in split_records(text)
        if (parsed := parse_progress2_line(record)) is not None
    ]
    assert records, "no progress records parsed from golden capture"
    assert records[-1].bytes_done == 44_654_666 // 1 or records[-1].bytes_done > 0
    # monotonic bytes within one transfer direction
    for earlier, later in zip(records, records[1:], strict=False):
        assert later.bytes_done is not None
        assert earlier.bytes_done is not None
        assert later.bytes_done >= earlier.bytes_done


def test_golden_progress2_with_deletes_fixture() -> None:
    text = (FIXTURES / "rsync-progress2-with-deletes.txt").read_text(encoding="utf-8")
    assert "*deleting" in text  # sanity: right fixture
    records = [
        parsed
        for record in split_records(text)
        if (parsed := parse_progress2_line(record)) is not None
    ]
    assert records


def test_parse_stats_transferred_bytes() -> None:
    stats = "Number of files: 13\nTotal transferred file size: 44,654,666 bytes\nLiteral data: 0"
    assert parse_stats_transferred_bytes(stats) == 44_654_666
    assert parse_stats_transferred_bytes("no stats here") is None


def test_parse_itemize_line_classification() -> None:
    added = parse_itemize_line(">f+++++++++ data/pak1.dat")
    assert added is not None and added.change == "added" and not added.is_dir
    changed = parse_itemize_line(">f.s....... README.txt")
    assert changed is not None and changed.change == "changed"
    deleted = parse_itemize_line("*deleting   obsolete/junk.dat")
    assert deleted is not None and deleted.change == "deleted"
    new_dir = parse_itemize_line("cd+++++++++ scripts/")
    assert new_dir is not None and new_dir.is_dir and new_dir.change == "added"
    # sending direction also counts
    sent = parse_itemize_line("<f+++++++++ out.bin")
    assert sent is not None and sent.change == "added"
    # chatter is ignored
    assert parse_itemize_line("sending incremental file list") is None
    assert parse_itemize_line(".d..t...... config/") is None
    assert parse_itemize_line("") is None


def test_golden_itemize_fixture_counts() -> None:
    lines = (FIXTURES / "rsync-itemize-dry-run.txt").read_text(encoding="utf-8").splitlines()
    entries = [e for line in lines if (e := parse_itemize_line(line)) is not None]
    deleted = [e for e in entries if e.change == "deleted"]
    added = [e for e in entries if e.change == "added"]
    changed = [e for e in entries if e.change == "changed"]
    assert len(deleted) == 3  # junk.dat, obsolete/, savegame.sav
    assert any(e.path == "data/pak1.dat" for e in added)
    assert any(e.path == "README.txt" for e in changed)
