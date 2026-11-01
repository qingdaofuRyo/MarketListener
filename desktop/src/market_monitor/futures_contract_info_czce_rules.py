"""Audited, historical CZCE business-rule excerpts for contract information.

These PDF rules contain several contract terms but are not the current complete
contract text.  Their observations must remain PARTIAL even when every checked
field parses.  Raw PDF bytes and SHA metadata are stored in ignored Bronze.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader


_ARTICLES = {
    "交易单位": ("第三", "第四", "交易单位为"),
    "报价单位": ("第四", "第五", "报价单位为"),
    "最小变动价位": ("第五", "第六", "最小变动价位为"),
    "合约交割月份": ("第六", "第七", "交割月份为"),
    "最后交易日": ("第九", "第十", "最后交易日为"),
    "交易代码": ("第十", "第十一", "交易代码为"),
}
_CONTRACT_LABELS = (
    "交易品种", "交易单位", "报价单位", "最小变动价位", "每日价格波动限制",
    "最低交易保证金", "合约交割月份", "交易时间", "最后交易日", "最后交割日",
    "交割品级", "交割地点", "交割方式", "交易代码", "上市交易所",
)


def _business_rule_fields(text: str, expected_code: str) -> dict[str, str]:
    compact = re.sub(r"\s+|—\d+—", "", text)
    if "郑州商品交易所" not in compact or "期货业务细则" not in compact:
        return {}
    fields: dict[str, str] = {}
    for label, (start, end, phrase) in _ARTICLES.items():
        match = re.search(rf"{start}条(.+?){end}条", compact)
        if match is None:
            continue
        value = re.search(re.escape(phrase) + r"(.+?)。", match.group(1))
        if value:
            fields[label] = value.group(1).strip()
    if fields.get("交易代码", "").upper() != expected_code:
        return {}
    title = re.search(r"郑州商品交易所(.+?)期货业务细则", compact[:160])
    if title:
        fields["交易品种"] = title.group(1)
    article_eight = re.search(r"第八条(.+?)第九条", compact)
    if article_eight:
        trading_hours = re.split(r"交易所暂停|交易所另行公告", article_eight.group(1), maxsplit=1)[0]
        if "交易时间" in trading_hours:
            fields["交易时间"] = trading_hours.rstrip("。")
    article_thirteen = re.search(r"第十三条(.+?)第十四条", compact)
    if article_thirteen and "最后交割日" in article_thirteen.group(1):
        fields["最后交割日"] = article_thirteen.group(1).rstrip("。")
    margin = re.search(r"最低交易保证金标准为(.+?)。", compact)
    if margin:
        fields["最低交易保证金"] = margin.group(1)
    limit = re.search(r"每日涨跌停板幅度为(.+?)。", compact)
    if limit:
        fields["每日价格波动限制"] = limit.group(1)
    return fields if len(fields) >= 6 else {}


def load_czce_business_rules(data_root: Path) -> dict[str, dict[str, Any]]:
    """Read only local PDFs whose original bytes and official URL validate."""
    root = Path(data_root) / "bronze" / "futures_contract_info" / "CZCE"
    observations: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return observations
    for code_dir in root.iterdir():
        if not code_dir.is_dir() or not re.fullmatch(r"[A-Z]{1,3}", code_dir.name):
            continue
        for metadata_path in code_dir.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                code = code_dir.name
                digest = str(metadata["payloadSha256"])
                url = str(metadata["sourceUrl"])
                if (metadata.get("productKey") != f"CZCE.{code}"
                        or metadata.get("adapter") != "OFFICIAL_BUSINESS_RULES_PDF"
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or metadata_path.stem != digest
                        or (urlparse(url).hostname or "").lower() != "www.czce.com.cn"
                        or not urlparse(url).path.startswith(("/cn/uploadfile/", "/cn/content_file/flfg/zcjywgz/pzxz/"))
                        or not urlparse(url).path.endswith(".pdf")):
                    continue
                retrieved = str(metadata["retrievedAt"])
                datetime.fromisoformat(retrieved)
                payload = metadata_path.with_suffix(".pdf").read_bytes()
                if not payload.startswith(b"%PDF") or sha256(payload).hexdigest() != digest:
                    continue
                reader = PdfReader(metadata_path.with_suffix(".pdf"))
                text = "".join(page.extract_text() or "" for page in reader.pages)
                fields = _business_rule_fields(text, code)
                if not fields:
                    continue
                compact = re.sub(r"\s+", "", text[:1500])
                releases = re.findall(
                    r"(\d{4})年(\d{1,2})月(\d{1,2})日〔\d{4}〕\d+号公告发布",
                    compact,
                )
                published = (
                    f"{int(releases[-1][0]):04d}-{int(releases[-1][1]):02d}-{int(releases[-1][2]):02d}"
                    if releases else None
                )
                observation = {
                    "url": url, "sha": digest, "retrievedAt": retrieved,
                    "publishedOn": published,
                    "fields": fields, "sourceKind": "OFFICIAL_CZCE_BUSINESS_RULES_PDF",
                    "qualityStatus": "PARTIAL",
                }
                prior = observations.get(f"CZCE.{code}")
                if prior is None or (prior.get("publishedOn") or "", prior["retrievedAt"]) < (published or "", retrieved):
                    observations[f"CZCE.{code}"] = observation
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
    return observations


def load_czce_historical_contract_pdfs(data_root: Path) -> dict[str, dict[str, Any]]:
    """Read a complete *historical* contract table without asserting current validity."""
    root = Path(data_root) / "bronze" / "futures_contract_info" / "CZCE"
    observations: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return observations
    for code_dir in root.iterdir():
        if not code_dir.is_dir() or not re.fullmatch(r"[A-Z]{1,3}", code_dir.name):
            continue
        for metadata_path in code_dir.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                code = code_dir.name
                digest = str(metadata["payloadSha256"])
                url = str(metadata["sourceUrl"])
                if (metadata.get("productKey") != f"CZCE.{code}"
                        or metadata.get("adapter") != "OFFICIAL_HISTORICAL_CONTRACT_PDF"
                        or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or metadata_path.stem != digest
                        or (urlparse(url).hostname or "").lower() != "www.czce.com.cn"
                        or not urlparse(url).path.startswith("/cn/rootfiles/")
                        or not urlparse(url).path.endswith(".pdf")):
                    continue
                retrieved = str(metadata["retrievedAt"])
                datetime.fromisoformat(retrieved)
                pdf_path = metadata_path.with_suffix(".pdf")
                payload = pdf_path.read_bytes()
                if not payload.startswith(b"%PDF") or sha256(payload).hexdigest() != digest:
                    continue
                reader = PdfReader(pdf_path)
                fields: dict[str, str] = {}
                page_index = metadata.get("contractPageIndex")
                if isinstance(page_index, int) and 0 <= page_index < len(reader.pages):
                    contract_pages = [reader.pages[page_index]]
                else:
                    contract_pages = reader.pages[:10]
                for page in contract_pages:
                    compact = re.sub(r"\s+", "", page.extract_text() or "")
                    if f"交易代码{code}上市交易所郑州商品交易所" not in compact:
                        continue
                    cursor = 0
                    candidate: dict[str, str] = {}
                    for index, label in enumerate(_CONTRACT_LABELS[:-1]):
                        start = compact.find(label, cursor)
                        following = compact.find(_CONTRACT_LABELS[index + 1], start + len(label))
                        if start < 0 or following <= start:
                            break
                        candidate[label] = compact[start + len(label):following]
                        cursor = following
                    if (len(candidate) == len(_CONTRACT_LABELS) - 1
                            and candidate.get("交易代码") == code
                            and "郑州商品交易所" in compact[cursor:cursor + 30]):
                        fields = candidate
                        fields["上市交易所"] = "郑州商品交易所"
                        break
                if not fields:
                    continue
                observation = {
                    "url": url, "sha": digest, "retrievedAt": retrieved,
                    "fields": fields,
                    "sourceKind": "OFFICIAL_CZCE_HISTORICAL_CONTRACT_PDF",
                    "qualityStatus": "PARTIAL",
                }
                prior = observations.get(f"CZCE.{code}")
                if prior is None or prior["retrievedAt"] < retrieved:
                    observations[f"CZCE.{code}"] = observation
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
    return observations


__all__ = ("load_czce_business_rules", "load_czce_historical_contract_pdfs")
