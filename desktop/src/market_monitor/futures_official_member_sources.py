"""Direct, source-isolated public exchange member-ranking adapters."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import re
from typing import Any, Mapping
import zipfile

import pandas as pd
import requests


_DATED_CONTRACT = re.compile(r"^[A-Z]{1,3}\d{3,4}$")
_CZCE_CONTRACT_TITLE = re.compile(r"合约[：:]\s*([A-Z]{1,3}\d{3,4})", re.I)
_RANK_URLS = {
    "SHFE": "https://www.shfe.com.cn/data/tradedata/future/dailydata/pm{day}.dat",
    "DCE": "http://www.dce.com.cn/dcereport/publicweb/dailystat/memberDealPosi/batchDownload",
    "CZCE": "http://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataHolding.xlsx",
    "GFEX": "http://www.gfex.com.cn/u/interfacesWebVariety/loadList",
    "CFFEX": "http://www.cffex.com.cn/sj/ccpm/{month}/{day}/{product}_1.csv",
}
_CFFEX_FUTURES_PRODUCTS = ("IF", "IH", "IC", "IM", "TS", "TF", "T", "TL")


class DirectOfficialRankApi:
    """Expose the old frame interface while fetching only exchange-owned URLs."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.evidence: dict[str, dict[str, Any]] = {}
        self.contract_evidence: dict[tuple[str, str], dict[str, Any]] = {}
        self.trade_rank_frames: dict[str, dict[str, pd.DataFrame]] = {}
        self.raw_artifacts: list[dict[str, Any]] = []

    def evidence_for(self, exchange: str, contract_code: str | None = None) -> dict[str, Any]:
        if contract_code is not None:
            contract_evidence = self.contract_evidence.get((exchange.upper(), contract_code.upper()))
            if contract_evidence is not None:
                return dict(contract_evidence)
        return dict(self.evidence.get(exchange, {
            "publisher": exchange, "source_url": None,
            "adapter": f"official-{exchange.lower()}-rank-v1",
            "retrieved_at": _now(), "payload_sha256": None,
            "quality_status": "FAILED", "legacy_adapter": None,
        }))

    def _request(
        self, exchange: str, url: str, *, method: str = "GET",
        data: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        content_type: str = "application/octet-stream",
    ) -> bytes:
        adapter = f"official-{exchange.lower()}-rank-v1"
        evidence = {
            "publisher": exchange, "source_url": url, "adapter": adapter,
            "retrieved_at": _now(), "payload_sha256": None,
            "quality_status": "FAILED", "legacy_adapter": None,
            "http_status": None, "response_content_type": None,
        }
        self.evidence[exchange] = evidence
        response = self.session.request(
            method, url, data=data, json=json_body, timeout=15,
        )
        raw = response.content
        evidence["retrieved_at"] = _now()
        evidence["http_status"] = int(response.status_code)
        response_type = str(response.headers.get("Content-Type") or "").lower()
        evidence["response_content_type"] = response_type or None
        evidence["payload_sha256"] = sha256(raw).hexdigest()
        if int(response.status_code) >= 400:
            response.raise_for_status()
        if not raw:
            raise ValueError(f"{exchange} official ranking response is empty")
        if content_type == "application/json" and "json" not in response_type:
            raise ValueError(f"{exchange} official ranking response is not JSON ({response_type or 'unknown type'})")
        if content_type == "application/zip" and not zipfile.is_zipfile(BytesIO(raw)):
            raise ValueError(f"{exchange} official ranking response is not a ZIP archive")
        if content_type.endswith("spreadsheetml.sheet") and not raw.startswith(b"PK"):
            raise ValueError(f"{exchange} official ranking response is not an XLSX file")
        if content_type == "text/csv" and "csv" not in response_type:
            raise ValueError(f"{exchange} official ranking response is not CSV ({response_type or 'unknown type'})")
        digest = str(evidence["payload_sha256"])
        # Valid source bytes are kept before the parser can reject a format.
        self.raw_artifacts.append({
            "provider": f"official_futures_member_{exchange.lower()}_{len(self.raw_artifacts):04d}",
            "response": {
                "publisher": exchange, "sourceUrl": url, "adapter": adapter,
                "retrievedAt": evidence["retrieved_at"], "payloadSha256": digest,
                "contentType": content_type,
                "contentBase64": base64.b64encode(raw).decode("ascii"),
            },
        })
        return raw

    def _passed(self, exchange: str, *, also: tuple[str, ...] = ()) -> None:
        evidence = self.evidence[exchange]
        evidence["quality_status"] = "PASS"
        for venue in also:
            self.evidence[venue] = {**evidence, "publisher": venue}

    def get_shfe_rank_table(self, *, date: str) -> dict[str, pd.DataFrame]:
        url = _RANK_URLS["SHFE"].format(day=date)
        raw = self._request("SHFE", url, content_type="application/json")
        frames = parse_shfe_rank_json(raw)
        if not frames:
            raise ValueError("SHFE official rank JSON contains no dated contract rows")
        self.trade_rank_frames["SHFE"] = frames
        self._passed("SHFE", also=("INE",))
        return frames

    def get_rank_table_czce(self, *, date: str) -> dict[str, pd.DataFrame]:
        url = _RANK_URLS["CZCE"].format(day=date, year=date[:4])
        raw = self._request("CZCE", url, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if not raw.startswith(b"PK"):
            raise ValueError("CZCE official rank response is not an XLSX file")
        frames = parse_czce_rank_xlsx(raw)
        if not frames:
            raise ValueError("CZCE official rank workbook contains no dated contract rows")
        self.trade_rank_frames["CZCE"] = frames
        self._passed("CZCE")
        return frames

    def futures_dce_position_rank(self, *, date: str) -> dict[str, pd.DataFrame]:
        url = _RANK_URLS["DCE"]
        raw = self._request(
            "DCE", url, method="POST", json_body={
                "tradeDate": date, "varietyId": "a", "contractId": f"a{date[2:4]}01",
                "tradeType": "1", "lang": "zh",
            }, content_type="application/zip",
        )
        if not zipfile.is_zipfile(BytesIO(raw)):
            raise ValueError("DCE official ranking response is not a ZIP archive")
        frames = parse_dce_rank_zip(raw, date)
        if not frames:
            raise ValueError("DCE official ZIP contains no usable dated contract rankings")
        self.trade_rank_frames["DCE"] = frames
        self._passed("DCE")
        return frames

    def get_cffex_rank_table(self, *, date: str) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        evidence_rows: list[dict[str, Any]] = []
        no_publication: list[str] = []
        errors: list[str] = []
        for product in _CFFEX_FUTURES_PRODUCTS:
            url = _RANK_URLS["CFFEX"].format(month=date[:6], day=date[6:], product=product)
            try:
                raw = self._request("CFFEX", url, content_type="text/csv")
                contract_frames = parse_cffex_rank_csv(raw)
            except Exception as error:
                evidence = self.evidence.get("CFFEX")
                response = getattr(error, "response", None)
                http_status = int(getattr(response, "status_code", 0)) if response is not None else 0
                if http_status == 404:
                    no_publication.append(product)
                else:
                    errors.append(f"{product}:{type(error).__name__}: {error}"[:240])
                if evidence is not None:
                    evidence_rows.append({**evidence, "product": product, "error": str(error)[:240]})
                if http_status in {403, 412, 429}:
                    break
                continue
            if not contract_frames:
                no_publication.append(product)
                evidence_rows.append({**self.evidence["CFFEX"], "product": product, "record_count": 0})
                continue
            evidence = dict(self.evidence["CFFEX"])
            evidence["quality_status"] = "PASS"
            for contract, frame in contract_frames.items():
                frames[contract] = frame
                self.contract_evidence[("CFFEX", contract)] = evidence
            evidence_rows.append({
                **evidence, "product": product,
                "record_count": sum(len(frame) for frame in contract_frames.values()),
            })
        self.trade_rank_frames["CFFEX"] = frames
        hashes = sorted({str(item["payload_sha256"]) for item in evidence_rows if item.get("payload_sha256")})
        aggregate = sha256("\n".join(hashes).encode("ascii")).hexdigest() if hashes else None
        self.evidence["CFFEX"] = {
            "publisher": "CFFEX", "source_url": "http://www.cffex.com.cn/cn/ccpm.html",
            "adapter": "official-cffex-rank-v1", "retrieved_at": _now(),
            "payload_sha256": aggregate,
            "quality_status": "PARTIAL" if errors else "NO_PUBLICATION" if not frames else "PASS",
            "legacy_adapter": None,
            "error": "；".join([
                *errors,
                *([f"未发现符合公布条件的合约：{','.join(no_publication)}"] if no_publication else []),
            ]) or None,
        }
        return frames

    def futures_gfex_position_rank(self, *, date: str) -> dict[str, pd.DataFrame]:
        raw = self._request("GFEX", _RANK_URLS["GFEX"], method="POST", content_type="application/json")
        varieties = _json_data(raw, "GFEX variety list")
        frames: dict[str, pd.DataFrame] = {}
        for variety in varieties:
            if not isinstance(variety, Mapping):
                continue
            code = str(variety.get("varietyId") or "").lower()
            if not code.isalpha():
                continue
            contract_url = "http://www.gfex.com.cn/u/interfacesWebTiMemberDealPosiQuotes/loadListContract_id"
            contracts = _json_data(
                self._request("GFEX", contract_url, method="POST", data={"variety": code, "trade_date": date}, content_type="application/json"),
                "GFEX contract list",
            )
            for item in contracts:
                value = (
                    item.get("contract_id") or item.get("contractId")
                    or next(iter(item.values()), "")
                    if isinstance(item, Mapping) else item
                )
                contract = str(value).upper()
                if not _DATED_CONTRACT.fullmatch(contract):
                    continue
                sides: dict[int, list[Mapping[str, Any]]] = {}
                for data_type in (1, 2, 3):
                    data_url = "http://www.gfex.com.cn/u/interfacesWebTiMemberDealPosiQuotes/loadList"
                    sides[data_type] = [
                        row for row in _json_data(
                            self._request(
                                "GFEX", data_url, method="POST",
                                data={"trade_date": date, "trade_type": "0", "variety": code,
                                      "contract_id": contract.lower(), "data_type": str(data_type)},
                                content_type="application/json",
                            ), "GFEX member list",
                        ) if isinstance(row, Mapping)
                    ]
                rows: list[dict[str, Any]] = []
                for index in range(max((len(values) for values in sides.values()), default=0)):
                    volume = sides[1][index] if index < len(sides[1]) else {}
                    long = sides[2][index] if index < len(sides[2]) else {}
                    short = sides[3][index] if index < len(sides[3]) else {}
                    rows.append({
                        "rank": index + 1,
                        "vol_party_name": volume.get("abbr"),
                        "vol": volume.get("todayQty"),
                        "vol_chg": volume.get("qtySub", volume.get("todayQtyChg")),
                        "long_party_name": long.get("abbr"),
                        "long_open_interest": long.get("todayQty"),
                        "long_open_interest_chg": long.get("qtySub", long.get("todayQtyChg")),
                        "short_party_name": short.get("abbr"),
                        "short_open_interest": short.get("todayQty"),
                        "short_open_interest_chg": short.get("qtySub", short.get("todayQtyChg")),
                    })
                if rows:
                    frames[contract] = pd.DataFrame(rows)
        if not frames:
            raise ValueError("GFEX official ranking API returned no dated contract rows")
        self.trade_rank_frames["GFEX"] = frames
        self._passed("GFEX")
        return frames


def parse_shfe_rank_json(raw: bytes) -> dict[str, pd.DataFrame]:
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("o_cursor"), list):
        raise ValueError("SHFE rank response has no o_cursor array")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in payload["o_cursor"]:
        if not isinstance(item, Mapping):
            continue
        contract = str(item.get("INSTRUMENTID") or "").upper()
        if not _DATED_CONTRACT.fullmatch(contract):
            continue
        try:
            rank = int(item.get("RANK") or 0)
        except (TypeError, ValueError):
            continue
        if rank <= 0:
            continue
        grouped.setdefault(contract, []).append({
            "rank": rank,
            "vol_party_name": item.get("PARTICIPANTABBR1"),
            "vol": item.get("CJ1"),
            "vol_chg": item.get("CJ1_CHG"),
            "long_party_name": item.get("PARTICIPANTABBR2"),
            "long_open_interest": item.get("CJ2"),
            "long_open_interest_chg": item.get("CJ2_CHG"),
            "short_party_name": item.get("PARTICIPANTABBR3"),
            "short_open_interest": item.get("CJ3"),
            "short_open_interest_chg": item.get("CJ3_CHG"),
        })
    return {contract: pd.DataFrame(rows) for contract, rows in grouped.items()}


def parse_czce_rank_xlsx(raw: bytes) -> dict[str, pd.DataFrame]:
    table = pd.read_excel(BytesIO(raw), header=None)
    grouped: dict[str, pd.DataFrame] = {}
    first_column = table.iloc[:, 0].tolist()
    for start, title in enumerate(first_column):
        match = _CZCE_CONTRACT_TITLE.search(str(title or ""))
        if match is None:
            continue
        contract = match.group(1).upper()
        rows: list[dict[str, Any]] = []
        for index in range(start + 2, len(table)):
            raw_rank = table.iat[index, 0]
            try:
                rank = int(raw_rank)
            except (TypeError, ValueError):
                break
            if rank <= 0:
                break
            item = table.iloc[index].tolist()
            if len(item) < 10:
                break
            rows.append({
                "rank": rank,
                "long_party_name": item[4], "long_open_interest": item[5],
                "long_open_interest_chg": item[6],
                "short_party_name": item[7], "short_open_interest": item[8],
                "short_open_interest_chg": item[9],
            })
        if rows:
            grouped[contract] = pd.DataFrame(rows)
    return grouped


def parse_cffex_rank_csv(raw: bytes) -> dict[str, pd.DataFrame]:
    """Parse the official per-product CSV, preserving distinct long/short ranks."""

    text = raw.decode("gb18030", errors="strict")
    table = pd.read_csv(BytesIO(text.encode("utf-8")), header=None, dtype=str)
    first_column = table.iloc[:, 0].fillna("").astype(str)
    header_rows = list(first_column[first_column.str.contains("交易日", regex=False)].index)
    if len(header_rows) > 2:
        table = table.iloc[header_rows[1]:, 0].str.split(",", expand=True).reset_index(drop=True)
    if table.empty:
        raise ValueError("CFFEX CSV has no rows")
    # Current official files contain a two-line heading; older files begin
    # with the column names. Validate the documented column count before use.
    header = [str(value or "").strip() for value in table.iloc[0].tolist()]
    if len(header) < 12 or "合约" not in header[1] or "排名" not in header[2]:
        raise ValueError("CFFEX CSV does not match the official futures ranking layout")
    records: dict[str, list[dict[str, Any]]] = {}
    for raw_row in table.iloc[2:].itertuples(index=False, name=None):
        values = [str(value or "").strip() for value in raw_row]
        if len(values) < 12:
            continue
        contract = values[1].upper()
        if not _DATED_CONTRACT.fullmatch(contract):
            continue
        try:
            rank = int(values[2].replace(",", ""))
        except ValueError:
            continue
        if rank <= 0:
            continue
        records.setdefault(contract, []).append({
            "rank": rank, "long_party_name": values[6] or None,
            "long_open_interest": values[7], "long_open_interest_chg": values[8],
            "short_party_name": values[9] or None,
            "short_open_interest": values[10], "short_open_interest_chg": values[11],
            "vol_party_name": values[3] or None, "vol": values[4], "vol_chg": values[5],
        })
    return {contract: pd.DataFrame(rows) for contract, rows in records.items()}


def parse_dce_rank_zip(raw: bytes, trading_day: str) -> dict[str, pd.DataFrame]:
    grouped: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        for filename in archive.namelist():
            if not filename.startswith(trading_day):
                continue
            match = re.search(r"_([A-Za-z]{1,3}\d{3,4})_", filename)
            if match is None:
                continue
            contract = match.group(1).upper()
            text = archive.read(filename).decode("gb18030", errors="replace")
            sections: list[list[dict[str, Any]]] = []
            current: list[dict[str, Any]] | None = None
            for line in text.splitlines():
                cells = [cell.strip() for cell in line.split("\t")]
                if cells and cells[0].startswith("名次"):
                    current = []
                    sections.append(current)
                    continue
                if current is None or len(cells) < 3:
                    continue
                try:
                    rank = int(cells[0])
                except ValueError:
                    continue
                current.append({
                    "rank": rank, "member": cells[1], "position": cells[2],
                    "change": cells[3] if len(cells) > 3 else None,
                })
            if len(sections) < 3:
                continue
            rows: dict[int, dict[str, Any]] = {}
            for item in sections[0]:
                rows[item["rank"]] = {
                    "rank": item["rank"], "vol_party_name": item["member"],
                    "vol": item["position"], "vol_chg": item["change"],
                }
            for side, section in (("long", sections[1]), ("short", sections[2])):
                for item in section:
                    row = rows.setdefault(item["rank"], {"rank": item["rank"]})
                    row[f"{side}_party_name"] = item["member"]
                    row[f"{side}_open_interest"] = item["position"]
                    row[f"{side}_open_interest_chg"] = item["change"]
            if rows:
                grouped[contract] = pd.DataFrame([rows[key] for key in sorted(rows)])
    return grouped


def _json_data(raw: bytes, label: str) -> list[Any]:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON") from error
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise ValueError(f"{label} has no data array")
    return payload["data"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = (
    "DirectOfficialRankApi", "parse_shfe_rank_json", "parse_czce_rank_xlsx",
    "parse_dce_rank_zip",
)
