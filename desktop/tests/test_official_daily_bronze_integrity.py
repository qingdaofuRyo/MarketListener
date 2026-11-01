from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile

from market_monitor.futures_official_backfill import _find_cffex_archive
from market_monitor.futures_official_daily_sources import official_daily_request


def test_cffex_zip_requires_matching_manifest_digest_and_source(tmp_path):
    day = "2023-01-03"
    archive = BytesIO()
    with ZipFile(archive, "w") as output:
        output.writestr("20230103_1.csv", "合约代码,今收盘,今结算,成交量,持仓量,成交金额\nIF2302,4000,4000,1,1,120\n")
    raw = archive.getvalue()
    directory = tmp_path / "bronze" / "official_futures_cffex_archives"
    directory.mkdir(parents=True)
    path = directory / f"202301-{sha256(raw).hexdigest()[:12]}.zip"
    path.write_bytes(raw)
    proof = {"sourceUrl": official_daily_request("CFFEX", day)["url"],
             "payloadSha256": sha256(raw).hexdigest(), "retrievedAt": "2026-10-03T00:00:00+00:00"}
    manifest = path.with_suffix(".json")
    manifest.write_text(json.dumps(proof), encoding="utf-8")
    assert _find_cffex_archive(tmp_path, day)[0] == raw
    proof["payloadSha256"] = "0" * 64
    manifest.write_text(json.dumps(proof), encoding="utf-8")
    assert _find_cffex_archive(tmp_path, day) is None
    proof["payloadSha256"] = sha256(raw).hexdigest()
    proof["sourceUrl"] = "https://wrong.invalid/fake.zip"
    manifest.write_text(json.dumps(proof), encoding="utf-8")
    assert _find_cffex_archive(tmp_path, day) is None
    manifest.unlink()
    assert _find_cffex_archive(tmp_path, day) is None
