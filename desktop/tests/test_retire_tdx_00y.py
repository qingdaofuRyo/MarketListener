from pathlib import Path
import runpy
import struct

import duckdb
import pytest

from market_monitor.futures_bulk import _tdx_rows


def test_retirement_preserves_main_and_backups(tmp_path):
    module = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'scripts/retire_tdx_00y.py'))
    root = tmp_path / 'data'
    folder = root / 'silver/market=GLOBAL/asset_type=FUTURE'
    folder.mkdir(parents=True)
    path = folder / 'sample.parquet'
    c = duckdb.connect(str(root / 'catalog.duckdb'))
    c.execute('create table bars(instrument_id varchar,bar_open_time varchar)')
    c.executemany('insert into bars values (?,?)', [
        ('GLOBAL.COMEX.FUTURE.GC00Y.TDX_LOCAL', '2026-09-08'),
        ('GLOBAL.COMEX.FUTURE.GC00W.TDX_LOCAL', '2026-09-08'),
    ])
    c.execute('copy bars to ? (format parquet)', [str(path)])
    c.execute('create table partitions(partition_id varchar,file_path varchar,row_count bigint,data_cutoff varchar,sha256 varchar,updated_at varchar,status varchar)')
    c.execute('insert into partitions values (?,?,?,?,?,?,?)', ['sample', str(path.relative_to(root)), 2, '2026-09-08', 'synthetic', '', 'COMPLETE'])
    c.close()
    terminal = tmp_path / 'terminal'
    raw = terminal / 'vipdoc/ds/lday'
    raw.mkdir(parents=True)
    (raw / '16#GC00Y.day').write_bytes(bytes(32))
    (raw / '16#GC00W.day').write_bytes(bytes(32))
    assert module['run'](root, terminal, False)['rows'] == 1
    result = module['run'](root, terminal, True)
    assert result['rows'] == 1
    assert not (raw / '16#GC00Y.day').exists()
    assert (raw / '16#GC00W.day').exists()
    backup = Path(result['backup'])
    assert (backup / 'terminal/vipdoc/ds/lday/16#GC00Y.day').exists()
    assert (backup / path.relative_to(root)).is_file()
    c = duckdb.connect()
    assert c.execute('select instrument_id from read_parquet(?)', [str(path)]).fetchall() == [('GLOBAL.COMEX.FUTURE.GC00W.TDX_LOCAL',)]
    c.close()
    with pytest.raises(ValueError):
        module['inside'](root, tmp_path / 'outside')


def test_futures_date_bounds_keep_incomplete_day_out(tmp_path):
    folder = tmp_path / 'vipdoc/ds/lday'
    folder.mkdir(parents=True)
    (folder / '30#RBL8.day').write_bytes(b''.join(
        struct.pack('<IffffIIf', day, 100, 101, 99, 100, 10, 20, 100)
        for day in (20260831, 20260908, 20260909)
    ))
    rows = []
    def emit(batch):
        rows.extend(batch)
        return len(batch)
    _, _, errors = _tdx_rows(tmp_path, {}, full_rescan=False, emit=emit,
                             start_date='2026-09-01', end_date='2026-09-08')
    assert not errors
    assert len(rows) == 1
    assert rows[0]['trading_date'] == '2026-09-08'
