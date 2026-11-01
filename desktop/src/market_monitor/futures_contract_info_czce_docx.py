"""Read the current CZCE rapeseed futures contract from archived official DOCX bytes."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


_CONTRACT_FILE = "82a4a7b12cc141d893db0c333622e177.docx"
_OFFICIAL_PATH = "/cn/content_file/gyjys/jysdt/ggytz/2026/9/" + _CONTRACT_FILE
_LABELS = (
    "交易品种", "交易单位", "报价单位", "最小变动价位", "每日价格波动限制",
    "最低交易保证金", "合约交割月份", "交易时间", "最后交易日", "最后交割日",
    "交割品级", "交割地点", "交割方式", "交易代码", "上市交易所",
)
_WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def load_current_rs_contract(data_root: Path) -> dict[str, Any] | None:
    """Verify original bytes and exact contract rows before marking RS complete.

    The 2026 revision applies to RS2701 and later contracts.  Earlier RS
    contract months remain historical and must not be overwritten in raw facts.
    """
    root = Path(data_root) / "bronze" / "futures_contract_info" / "CZCE" / "RS"
    for metadata_path in root.glob("*.json") if root.is_dir() else ():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            digest = str(metadata["payloadSha256"])
            url = urlparse(str(metadata["sourceUrl"]))
            if (
                metadata.get("adapter") != "OFFICIAL_CZCE_2026_CONTRACT_DOCX"
                or metadata.get("productKey") != "CZCE.RS"
                or url.scheme != "https"
                or url.hostname != "www.czce.com.cn"
                or url.path != _OFFICIAL_PATH
                or metadata.get("publishedOn") != "2026-09-11"
                or metadata.get("effectiveFrom") != "2026-09-16"
                or metadata.get("appliesFromContract") != "RS2701"
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or metadata_path.stem != digest
            ):
                continue
            retrieved = str(metadata["retrievedAt"])
            datetime.fromisoformat(retrieved)
            payload_path = metadata_path.with_suffix(".docx")
            payload = payload_path.read_bytes()
            if sha256(payload).hexdigest() != digest:
                continue
            with ZipFile(payload_path) as archive:
                xml = archive.read("word/document.xml")
            document = ET.fromstring(xml)
            text = "".join(node.text or "" for node in document.findall(".//w:t", _WORD_NS))
            if "〔2026〕139号" not in text or "油菜籽期货2701" not in text:
                continue
            for table in document.findall(".//w:tbl", _WORD_NS):
                fields: dict[str, str] = {}
                for row in table.findall("./w:tr", _WORD_NS):
                    cells = [
                        "".join(node.text or "" for node in cell.findall(".//w:t", _WORD_NS)).strip()
                        for cell in row.findall("./w:tc", _WORD_NS)
                    ]
                    if len(cells) == 2 and cells[0] in _LABELS and cells[1]:
                        fields[cells[0]] = cells[1]
                if (
                    tuple(fields) != _LABELS
                    or fields["交易代码"] != "RS"
                    or fields["上市交易所"] != "郑州商品交易所"
                    or fields["合约交割月份"] != "1、3、6、7、9、11月"
                    or "次月10日" not in fields["最后交割日"]
                ):
                    continue
                return {
                    "url": str(metadata["sourceUrl"]), "sha": digest,
                    "retrievedAt": retrieved, "publishedOn": "2026-09-11",
                    "effectiveFrom": "2026-09-16", "appliesFromContract": "RS2701",
                    "fields": fields, "sourceKind": "OFFICIAL_CZCE_CONTRACT_DOCX",
                    "qualityStatus": "PASS",
                }
        except (OSError, ValueError, KeyError, TypeError, BadZipFile, ET.ParseError):
            continue
    return None


__all__ = ("load_current_rs_contract",)
