"""Read verified contract tables extracted from user-opened CZCE Chrome tabs.

The archive stores structured DOM table values, not the original HTTP body.
Its SHA-256 certifies the captured extraction.  The exchange page URL and
complete futures-code match remain separate checks before it can be selected.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


_FILENAME = "chrome_dom_fields_20261006.json"
_LABELS = (
    "交易品种", "交易单位", "报价单位", "最小变动价位", "每日价格波动限制",
    "最低交易保证金", "合约交割月份", "交易时间", "最后交易日", "最后交割日",
    "交割品级", "交割地点", "交割方式", "交易代码", "上市交易所",
)
_CHANNEL_CODES = {
    "001": "WH", "002": "PM", "003": "CF", "004": "SR", "005": "TA",
    "006": "OI", "007": "RI", "008": "MA", "009": "FG", "010": "RS",
    "011": "RM", "012": "ZC", "013": "JR", "014": "LR", "017": "SF",
    "019": "CY", "020": "SM", "021": "AP", "022": "CJ", "023": "UR",
    "024": "SA", "025": "PF", "026": "PK", "027": "PX", "028": "SH",
    "029": "PR", "030": "PL",
}


def load_chrome_contract_tables(data_root: Path) -> dict[str, dict[str, Any]]:
    """Return source-audited complete term tables from the local Chrome capture."""
    path = Path(data_root) / "bronze" / "futures_contract_info" / "CZCE" / _FILENAME
    if not path.is_file():
        return {}
    raw = path.read_bytes()
    sidecar = path.with_suffix(".sha256")
    expected_hash = sidecar.read_text(encoding="ascii").strip()
    if sha256(raw).hexdigest() != expected_hash:
        raise ValueError("CZCE Chrome DOM archive SHA mismatch")
    document = json.loads(raw)
    if (document.get("schema") != "czce-chrome-dom-fields-v1"
            or document.get("captureKind") != "GOOGLE_CHROME_STRUCTURED_DOM_TABLE_EXTRACTION"
            or tuple(document.get("labels", [])) != _LABELS
            or not isinstance(document.get("observations"), list)):
        raise ValueError("CZCE Chrome DOM archive schema mismatch")
    retrieved = str(document["capturedAt"])
    if datetime.fromisoformat(retrieved).tzinfo is None:
        raise ValueError("CZCE Chrome DOM archive capture time lacks timezone")
    result: dict[str, dict[str, Any]] = {}
    for item in document["observations"]:
        if not isinstance(item, dict):
            raise ValueError("invalid CZCE Chrome DOM observation")
        code = str(item.get("code") or "")
        url = str(item.get("url") or "")
        values = item.get("values")
        parsed = urlparse(url)
        match = re.fullmatch(r"/cn/sspz/[^/]+/H077002(\d{3})index_1\.htm", parsed.path)
        if (parsed.scheme != "https" or parsed.hostname != "www.czce.com.cn"
                or match is None or _CHANNEL_CODES.get(match.group(1)) != code
                or not isinstance(values, list) or len(values) != len(_LABELS)
                or any(not isinstance(value, str) or not value.strip() for value in values)):
            raise ValueError(f"invalid CZCE Chrome DOM source for {code}")
        fields = dict(zip(_LABELS, values, strict=True))
        if fields["交易代码"] != code or fields["上市交易所"] != "郑州商品交易所":
            raise ValueError(f"CZCE Chrome DOM contract code mismatch for {code}")
        key = f"CZCE.{code}"
        if key in result:
            raise ValueError(f"duplicate CZCE Chrome DOM contract for {code}")
        canonical = json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        digest = sha256(canonical).hexdigest()
        row_path = path.parent / "chrome_tables" / code / f"{digest}.json"
        if not row_path.is_file() or row_path.read_bytes() != canonical:
            raise ValueError(f"CZCE Chrome DOM row SHA mismatch for {code}")
        result[key] = {
            "url": url,
            "sha": digest,
            "retrievedAt": retrieved,
            "fields": fields,
            "sourceKind": "OFFICIAL_CHROME_DOM_FIELDS",
            "qualityStatus": "PASS",
        }
    return result


__all__ = ("load_chrome_contract_tables",)
