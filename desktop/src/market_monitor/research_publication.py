"""Safe, immutable customer projections of privately reviewed research claims."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chain_taxonomy import canonical_chain, get_canonical_parents
from .research_corpus import approved_candidates

SCHEMA = "marketlistener/research-publication/v1"
NODE_TYPES = {"RAW_MATERIAL", "PRODUCT", "PROCESS", "EQUIPMENT", "APPLICATION", "BRAND", "COMPANY", "SERVICE"}
EDGE_TYPES = {"INPUT_TO", "PROCESSES_TO", "PRODUCES", "USED_IN", "BRANDS", "SUPPLIES", "EQUIPS"}
METHOD_KINDS = {"工具", "方法", "策略"}
METHOD_CATEGORIES = (
    "数据预处理", "统计与风险", "因子选股", "行业轮动与择时", "指数增强与组合优化",
    "固收与可转债", "期货CTA与套利", "期权、波动率与对冲", "宏观跨资产",
    "机器学习与另类数据", "回测评价与归因",
)

# GB/T 4754-2017 top-level navigation. Product chains can cross categories;
# classification is a navigation aid, not a claim of supply relationship.
INDUSTRY_SECTORS = (
    ("A", "农、林、牧、渔业"), ("B", "采矿业"), ("C", "制造业"),
    ("D", "电力、热力、燃气及水生产和供应业"), ("E", "建筑业"),
    ("F", "批发和零售业"), ("G", "交通运输、仓储和邮政业"),
    ("H", "住宿和餐饮业"), ("I", "信息传输、软件和信息技术服务业"),
    ("J", "金融业"), ("K", "房地产业"), ("L", "租赁和商务服务业"),
    ("M", "科学研究和技术服务业"), ("N", "水利、环境和公共设施管理业"),
    ("O", "居民服务、修理和其他服务业"), ("P", "教育"),
    ("Q", "卫生和社会工作"), ("R", "文化、体育和娱乐业"),
    ("S", "公共管理、社会保障和社会组织"), ("T", "国际组织"),
    ("X", "跨行业主题（非国标行业）"),
)
SECTOR_BY_CHAIN = {
    "农业": "A", "煤炭": "B", "石油天然气": "B", "电力": "D", "储能": "D",
    "建筑建材": "E", "零售": "F", "交通运输": "G", "旅游酒店": "H",
    "计算机": "I", "通信": "I", "云计算": "I", "数据中心": "I", "软件": "I",
    "金融": "J", "房地产": "K", "教育": "P", "医疗服务": "Q",
    "传媒": "R", "环保": "N", "体育": "R", "养老": "Q",
    "人工智能": "I", "数据要素": "I", "工业软件": "I", "网络安全": "I",
    "机械设备": "C", "半导体": "C", "锂电池": "C", "光伏": "C",
    "医药生物": "C", "白酒": "C", "钢铁": "C", "有色金属": "C",
    "化工": "C", "建材": "C", "消费电子": "C", "食品饮料": "C",
}
FORBIDDEN_PUBLIC_KEYS = {"source", "sourceId", "reportId", "report_id", "evidence", "evidenceId",
                         "evidenceText", "page", "pageNumber", "pdf", "path", "filename", "title", "sha256"}
_SENSITIVE_TEXT = re.compile(r"\.pdf\b|[A-Za-z]:[/\\]|(?:第\s*\d+\s*页)|(?:报告|研报)(?:标题|原文|出处|来源)", re.IGNORECASE)


def _identifier(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def _clean(value: Any, *, max_length: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value.encode("utf-8", "replace").decode("utf-8")).strip()[:max_length]
    if _SENSITIVE_TEXT.search(text):
        raise ValueError("Public research text contains source-like content")
    return text


def _reject_title_copy(value: str, source_title: str) -> None:
    normalized_value = re.sub(r"[^\w\u4e00-\u9fff]", "", value).casefold()
    normalized_title = re.sub(r"[^\w\u4e00-\u9fff]", "", source_title).casefold()
    if len(normalized_title) >= 8 and normalized_title in normalized_value:
        raise ValueError("Customer text resembles the original report title")


def _reject_report_copy(value: str, evidence: str, source_title: str) -> None:
    _reject_title_copy(value, source_title)
    if not evidence or evidence.startswith("VISUAL:"):
        return
    public = re.sub(r"\s+", "", value)
    original = re.sub(r"\s+", "", evidence)
    if len(public) < 24 or len(original) < 24:
        return
    overlap = SequenceMatcher(None, public, original, autojunk=False).find_longest_match(
        0, len(public), 0, len(original)).size
    if overlap >= 24:
        raise ValueError("Customer text contains a verbatim report passage")


def _assert_public(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"Private research key in customer projection: {key}")
            _assert_public(item)
    elif isinstance(value, list):
        for item in value:
            _assert_public(item)
    elif isinstance(value, str) and _SENSITIVE_TEXT.search(value):
        raise ValueError("Private research text in customer projection")


def _root(data_root: Path) -> Path:
    return Path(data_root) / "research" / "published"


def _industry_claim(payload: dict[str, Any], evidence: str, source_title: str = "") -> dict[str, str]:
    chain = canonical_chain(_clean(payload.get("chain"), max_length=100))
    source_type = str(payload.get("sourceType") or "")
    target_type = str(payload.get("targetType") or "")
    predicate = str(payload.get("predicate") or "")
    if not chain or source_type not in NODE_TYPES or target_type not in NODE_TYPES or predicate not in EDGE_TYPES:
        raise ValueError("Industry relation has incomplete typed endpoints")
    source_name = _clean(payload.get("sourceName"), max_length=120)
    target_name = _clean(payload.get("targetName"), max_length=120)
    summary = _clean(payload.get("summary"))
    if not source_name or not target_name or source_name == target_name or not summary:
        raise ValueError("Industry relation has incomplete content")
    for value in (chain, source_name, target_name, summary, _clean(payload.get("stage"), max_length=30)):
        _reject_report_copy(value, evidence, source_title)
    return {"chain": chain, "sourceType": source_type, "sourceName": source_name,
            "predicate": predicate, "targetType": target_type, "targetName": target_name,
            "summary": summary, "stage": _clean(payload.get("stage"), max_length=30) or "产业关系"}


def _method_claim(payload: dict[str, Any], evidence: str, source_title: str = "") -> dict[str, Any]:
    name = _clean(payload.get("name"), max_length=140)
    summary = _clean(payload.get("summary"))
    kind = _clean(payload.get("kind"), max_length=10)
    category = _clean(payload.get("category"), max_length=30)
    if not name or not summary or kind not in METHOD_KINDS or category not in METHOD_CATEGORIES:
        raise ValueError("Quant method has no supported name, kind or category")
    def strings(key: str) -> list[str]:
        raw = payload.get(key)
        return [_clean(item, max_length=120) for item in raw[:20] if _clean(item, max_length=120)] if isinstance(raw, list) else []

    formula = _clean(payload.get("formula"), max_length=400)
    blockers = strings("blockReasons")
    if not formula:
        blockers.insert(0, "报告未给出完整、可核对的公式")
    if not strings("requiredData"):
        blockers.append("所需数据字段尚未确认")
    required = strings("requiredData")
    assumptions = strings("assumptions")
    limitations = strings("limitations")
    for value in (name, summary, formula, *required, *assumptions, *limitations, *blockers):
        _reject_report_copy(value, evidence, source_title)
    method_id = _identifier("method", name, category, formula,
                            json.dumps(required, ensure_ascii=False, sort_keys=True),
                            json.dumps(assumptions, ensure_ascii=False, sort_keys=True),
                            json.dumps(limitations, ensure_ascii=False, sort_keys=True))
    return {"id": method_id, "version": 1,
            "name": name, "kind": kind, "category": category, "summary": summary,
            "formula": formula or None, "requiredData": required,
            "assumptions": assumptions, "limitations": limitations,
            "blockReasons": list(dict.fromkeys(blockers)), "implementationStatus": "EXPLAINED"}


def _projection(candidates: list[dict[str, Any]], company_refs: dict[str, str] | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    chains: dict[str, dict[str, Any]] = {}
    methods: dict[str, dict[str, Any]] = {}
    company_refs = company_refs or {}
    rejected = 0
    for item in candidates:
        try:
            payload = item["payload"]
            if item["kind"] == "INDUSTRY_RELATION":
                claim = _industry_claim(payload, item["evidenceText"], item.get("sourceTitle", ""))
                name = claim["chain"]
                chain_id = _identifier("chain", name)
                chain = chains.setdefault(chain_id, {"id": chain_id, "name": name,
                    "sector": SECTOR_BY_CHAIN.get(name, "X"), "nodes": {}, "edges": [],
                    "sections": {"产品定位": [], "原材料": [], "加工": [], "品牌": [], "企业": [], "应用": []},
                    "_reports": set()})
                chain["_reports"].add(item.get("sha256") or item["id"])
                source_id = _identifier(chain_id, claim["sourceType"], claim["sourceName"])
                target_id = _identifier(chain_id, claim["targetType"], claim["targetName"])
                for node_id, node_type, node_name in ((source_id, claim["sourceType"], claim["sourceName"]),
                                                      (target_id, claim["targetType"], claim["targetName"])):
                    node = {"id": node_id, "type": node_type, "name": node_name}
                    if node_type == "COMPANY" and node_name in company_refs:
                        node["instrumentKey"] = company_refs[node_name]
                    chain["nodes"][node_id] = node
                chain["edges"].append({"id": _identifier(source_id, claim["predicate"], target_id),
                                       "fromNode": source_id, "toNode": target_id, "type": claim["predicate"],
                                       "summary": claim["summary"], "stage": claim["stage"]})
            elif item["kind"] == "QUANT_METHOD":
                method = _method_claim(payload, item["evidenceText"], item.get("sourceTitle", ""))
                methods.setdefault(method["id"], method)
        except (KeyError, TypeError, ValueError):
            rejected += 1
    for name in get_canonical_parents():
        if name in {"宏观", "策略", "量化", "商品期货"}:
            continue
        chain_id = _identifier("chain", name)
        chains.setdefault(chain_id, {"id": chain_id, "name": name, "sector": SECTOR_BY_CHAIN.get(name, "X"),
                                     "nodes": {}, "edges": [], "sections": {}, "_reports": set()})
    details: dict[str, dict[str, Any]] = {}
    catalog_items: list[dict[str, Any]] = []
    conflict_count = 0
    for chain in chains.values():
        edges_by_id: dict[str, dict[str, str]] = {}
        conflicting: set[str] = set()
        for edge in chain["edges"]:
            previous = edges_by_id.get(edge["id"])
            if previous is not None and (previous["summary"], previous["stage"]) != (edge["summary"], edge["stage"]):
                conflicting.add(edge["id"])
            else:
                edges_by_id[edge["id"]] = edge
        chain["edges"] = [edge for key, edge in edges_by_id.items() if key not in conflicting]
        conflict_count += len(conflicting)
        used_nodes = {value for edge in chain["edges"] for value in (edge["fromNode"], edge["toNode"])}
        chain["nodes"] = {key: value for key, value in chain["nodes"].items() if key in used_nodes}
        for edge in chain["edges"]:
            target_type = chain["nodes"][edge["toNode"]]["type"]
            section = {"RAW_MATERIAL": "原材料", "PROCESS": "加工", "BRAND": "品牌",
                       "COMPANY": "企业", "APPLICATION": "应用"}.get(target_type, "产品定位")
            chain["sections"][section].append(edge["summary"])
        detail = {key: value for key, value in chain.items() if key != "_reports"}
        detail["nodes"] = list(chain["nodes"].values())
        for key, values in detail["sections"].items():
            detail["sections"][key] = list(dict.fromkeys(values))
        detail["status"] = ("部分已核验" if conflicting and detail["edges"] else
                            "已核验" if detail["edges"] else "资料待核")
        details[chain["id"]] = detail
        catalog_items.append({"id": chain["id"], "name": chain["name"], "sector": chain["sector"],
                              "status": detail["status"], "relationCount": len(detail["edges"]),
                              "_sortHeat": len(chain["_reports"])})
    catalog_items.sort(key=lambda row: (-row["_sortHeat"], row["name"]))
    for item in catalog_items:
        del item["_sortHeat"]
    catalog = {"schema": SCHEMA, "sectors": [{"code": code, "name": name} for code, name in INDUSTRY_SECTORS],
               "chains": catalog_items}
    quant = {"schema": SCHEMA, "categories": list(METHOD_CATEGORIES),
             "methods": sorted(methods.values(), key=lambda row: (row["category"], row["name"]))}
    for value in (catalog, *details.values(), quant):
        _assert_public(value)
    return catalog, details, quant | {"skippedInvalidCandidates": rejected,
                                      "conflictedRelations": conflict_count}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def publish_research(data_root: Path) -> dict[str, Any]:
    from .industry_graph.f10 import CompanyRepository

    repository = CompanyRepository(data_root)
    by_name: dict[str, str | None] = {}
    first = 1
    while True:
        page = repository.list_companies(page=first, page_size=500)
        for company in page.items:
            name = company.name.strip()
            key = company.instrument_key
            by_name[name] = key if name not in by_name or by_name[name] == key else None
        if first * 500 >= page.total:
            break
        first += 1
    catalog, details, methods = _projection(approved_candidates(data_root),
                                            {name: key for name, key in by_name.items() if key})
    public_methods = {key: value for key, value in methods.items()
                      if key not in {"skippedInvalidCandidates", "conflictedRelations"}}
    company_index: dict[str, list[dict[str, str]]] = {}
    for detail in details.values():
        for node in detail["nodes"]:
            if node.get("instrumentKey"):
                company_index.setdefault(node["instrumentKey"], []).append({
                    "chainId": detail["id"], "chain": detail["name"], "node": node["name"],
                })
    published = _root(data_root)
    published.mkdir(parents=True, exist_ok=True)
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + _identifier(
        "publication", json.dumps(catalog, ensure_ascii=False, sort_keys=True),
        json.dumps(public_methods, ensure_ascii=False, sort_keys=True),
        json.dumps(details, ensure_ascii=False, sort_keys=True),
        json.dumps(company_index, ensure_ascii=False, sort_keys=True))[:8]
    target = published / "versions" / version
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix="research-", dir=published))
        try:
            _write_json(staging / "catalog.json", catalog)
            _write_json(staging / "methods.json", public_methods)
            _write_json(staging / "companies.json", {"schema": SCHEMA, "items": company_index})
            for chain_id, detail in details.items():
                _write_json(staging / "chains" / f"{chain_id}.json", detail)
            files = {str(path.relative_to(staging)).replace("\\", "/"):
                     hashlib.sha256(path.read_bytes()).hexdigest() for path in staging.rglob("*.json")}
            _write_json(staging / "manifest.json", {"schema": SCHEMA, "files": files})
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    pointer = published / "current.json"
    temporary = published / "current.tmp"
    manifest_hash = hashlib.sha256((target / "manifest.json").read_bytes()).hexdigest()
    _write_json(temporary, {"schema": SCHEMA, "version": version, "manifestSha256": manifest_hash})
    os.replace(temporary, pointer)
    return {"version": version, "chains": len(details), "methods": len(methods["methods"]),
            "skippedInvalidCandidates": methods["skippedInvalidCandidates"],
            "conflictedRelations": methods["conflictedRelations"]}


def publication_versions(data_root: Path) -> list[str]:
    directory = _root(data_root) / "versions"
    return sorted((path.name for path in directory.iterdir() if path.is_dir()), reverse=True) if directory.is_dir() else []


def rollback_research(data_root: Path, version: str) -> dict[str, str]:
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", version):
        raise ValueError("Invalid publication version")
    if not (_root(data_root) / "versions" / version / "catalog.json").is_file():
        raise FileNotFoundError(version)
    manifest = _root(data_root) / "versions" / version / "manifest.json"
    if not manifest.is_file():
        raise ValueError("Publication manifest is missing")
    temporary = _root(data_root) / "current.tmp"
    _write_json(temporary, {"schema": SCHEMA, "version": version,
                            "manifestSha256": hashlib.sha256(manifest.read_bytes()).hexdigest()})
    os.replace(temporary, _root(data_root) / "current.json")
    return {"version": version}


def read_publication(data_root: Path, name: str, identifier: str | None = None) -> dict[str, Any]:
    if name not in {"catalog", "methods", "chains", "companies"}:
        raise ValueError("Unknown research projection")
    pointer = _root(data_root) / "current.json"
    if not pointer.is_file():
        if name == "companies":
            return {"schema": SCHEMA, "status": "NOT_PUBLISHED", "items": {}}
        return {"schema": SCHEMA, "status": "NOT_PUBLISHED", "sectors": [{"code": code, "name": title}
                for code, title in INDUSTRY_SECTORS], "chains": [], "categories": list(METHOD_CATEGORIES),
                "methods": []} if name != "chains" else {}
    pointer_value = json.loads(pointer.read_text(encoding="utf-8"))
    version = pointer_value["version"]
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", version):
        raise ValueError("Corrupt research publication pointer")
    if name == "chains":
        if identifier is None or not re.fullmatch(r"[0-9a-f]{16}", identifier):
            raise ValueError("Invalid chain identifier")
        path = _root(data_root) / "versions" / version / "chains" / f"{identifier}.json"
    else:
        path = _root(data_root) / "versions" / version / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(path.name)
    manifest_path = _root(data_root) / "versions" / version / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != pointer_value.get("manifestSha256"):
        raise ValueError("Research publication manifest does not match its pointer")
    manifest = json.loads(manifest_bytes)
    relative = str(path.relative_to(manifest_path.parent)).replace("\\", "/")
    if manifest.get("files", {}).get(relative) != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError("Research publication payload failed integrity check")
    value = json.loads(path.read_text(encoding="utf-8"))
    _assert_public(value)
    return {**value, "version": version}
