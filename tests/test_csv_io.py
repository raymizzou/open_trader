from pathlib import Path

from open_trader.csv_io import write_rows


def test_write_rows_creates_parent_and_writes_header(tmp_path: Path):
    output = tmp_path / "nested" / "rows.csv"

    write_rows(output, ["symbol", "quantity"], [{"symbol": "NVDA", "quantity": "10"}])

    assert output.read_text(encoding="utf-8") == "symbol,quantity\nNVDA,10\n"


def test_write_rows_writes_header_for_empty_rows(tmp_path: Path):
    output = tmp_path / "empty.csv"

    write_rows(output, ["symbol", "quantity"], [])

    assert output.read_text(encoding="utf-8") == "symbol,quantity\n"
