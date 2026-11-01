"""Evidence, publication-boundary and local research regression tests."""

from __future__ import annotations

import json
from io import BytesIO
from datetime import date, timedelta
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from market_monitor import quant_research
from market_monitor.research_corpus import (
    _exact_model_evidence, _safe_text, _visual_reasons, add_candidate, backfill_visual_queue,
    confirm_blank, corpus_status, list_visual_queue, private_page, process_pages,
    propose_from_local_model, resolve_ocr_disagreement, review_candidate, review_visual_page, scan_reports,
)
from market_monitor.research_publication import publish_research, read_publication, rollback_research
from market_monitor import research_visual
from market_monitor import research_umi_ocr
from market_monitor import research_corpus
from market_monitor.industry_classification import public_classification, public_csrc_classification
from market_monitor.web_app import create_web_app


def _sample_pdf(path: Path) -> None:
    with pymupdf.open() as document:
        for value in ("硅片经过加工后形成芯片，这是本页用于核对的关系说明。" * 8, "", "因子方法需要收盘价，本页明确列出公式和条件。" * 8):
            page = document.new_page()
            if value:
                page.insert_textbox(pymupdf.Rect(50, 50, 550, 750), value, fontsize=12, fontname="china-s")
        document.save(path)


def test_pdf_surrogates_are_replaced_before_private_storage() -> None:
    assert _safe_text("value\ud835 10") == "value� 10"
    source = "硅片经过晶圆制造等加工步骤形成芯片，最后应用于电子设备。"
    assert _exact_model_evidence(source, "依据分析，硅片经过晶圆制造等加工步骤形成芯片，最后应用于电子设备。") in source
    with pytest.raises(ValueError):
        _exact_model_evidence(source, "没有原页支持的关系说明内容")


def test_local_model_uses_enforced_separate_schemas(monkeypatch: pytest.MonkeyPatch) -> None:
    assert research_corpus._report_kind("QUANT", "行业分析报告") == "MIXED"
    assert research_corpus._report_kind("QUANT", "因子研究报告") == "QUANT"
    assert research_corpus._report_kind("INDUSTRY", "量化行业配置") == "MIXED"
    requests: list[dict] = []

    class FakeOpener:
        def open(self, request: object, timeout: int) -> BytesIO:
            body = json.loads(request.data)
            requests.append(body)
            return BytesIO(json.dumps({"choices": [{"message": {"content": '{"items": []}'}}]}).encode())

    monkeypatch.setattr(research_corpus, "build_opener", lambda *_args: FakeOpener())
    assert research_corpus._local_model_json("http://127.0.0.1:18081", "test", "行业轮动", "INDUSTRY") == {
        "items": []}
    assert research_corpus._local_model_json("http://127.0.0.1:18081", "test", "行业轮动", "QUANT") == {
        "items": []}
    industry_schema = requests[0]["response_format"]["json_schema"]["schema"]
    quant_schema = requests[1]["response_format"]["json_schema"]["schema"]
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert industry_schema["properties"]["items"]["items"]["properties"]["kind"]["enum"] == [
        "INDUSTRY_RELATION"]
    assert quant_schema["properties"]["items"]["items"]["properties"]["kind"]["enum"] == [
        "QUANT_METHOD"]

    class InvalidOpener:
        def open(self, request: object, timeout: int) -> BytesIO:
            return BytesIO(json.dumps({"choices": [{"message": {"content": '{"wrong": []}'}}]}).encode())

    monkeypatch.setattr(research_corpus, "build_opener", lambda *_args: InvalidOpener())
    with pytest.raises(ValueError, match="extraction schema"):
        research_corpus._local_model_json("http://127.0.0.1:18081", "test", "sample", "QUANT")


def test_human_quality_gate_requires_complete_gold_and_thresholds() -> None:
    from scripts.research_quality_audit import evaluate

    workbook = {"schema": "research-quality-audit-v1", "documents": [{
        "sha256": f"sample-{index}", "kind": "QUANT", "pageCount": 60 if index == 0 else 1,
        "longReport": index == 0,
        "pages": [{"page": 1, "status": "TEXT_OK", "classes": ["OCR", "TABLE", "FIGURE"] if index == 0 else [],
                   "reviewed": True, "reviewer": "human reviewer",
                   "numericChecks": [{"value": "10", "unit": "亿元", "period": "2025年", "correct": True}
                                     for _ in range(2)],
                   "visualChecks": [{"kind": "TABLE", "field": "产量", "captured": True}]
                   if index < 20 else []}]
    } for index in range(60)]}
    sample = [{"sha256": item["sha256"], "document_kind": item["kind"],
               "page_count": item["pageCount"]} for item in workbook["documents"]]
    assert evaluate(workbook, sample)["status"] == "PASS"
    workbook["documents"][0]["pages"][0]["numericChecks"][0]["correct"] = False
    workbook["documents"][0]["pages"][0]["numericChecks"][1]["correct"] = False
    assert evaluate(workbook, sample)["status"] == "NOT_PASSED"
    workbook["documents"][0]["pages"][0]["reviewer"] = ""
    assert evaluate(workbook, sample)["missingCount"] > 0


def test_mixed_report_uses_both_modes_and_rejects_cross_kind(tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "研报"
    source.mkdir()
    _sample_pdf(source / "行业轮动和产业研究.pdf")
    root = tmp_path / "data"
    scan_reports(root, [source])
    process_pages(root, use_ocr=False)
    import sqlite3

    with sqlite3.connect(root / "research" / "private" / "corpus.sqlite3") as connection:
        assert connection.execute("SELECT document_kind FROM documents").fetchone()[0] == "MIXED"
    calls: list[str] = []

    def fake_model(_url: str, _model: str, _text: str, mode: str) -> dict:
        calls.append(mode)
        return {"items": [{"kind": "INDUSTRY_RELATION", "payload": {"chain": "半导体"},
                           "evidenceText": "硅片经过加工后形成芯片"}]}

    monkeypatch.setattr(research_corpus, "_local_model_json", fake_model)
    result = propose_from_local_model(root, url="http://127.0.0.1:18081", model="synthetic",
                                      extraction_version="mixed-v1", max_pages=1, verify_model=False)
    assert calls == ["INDUSTRY", "QUANT"]
    assert result["candidates"] == 1 and result["rejectedItems"] == 1


def test_official_industry_navigation_has_all_four_levels() -> None:
    value = public_classification()
    assert value["counts"] == {"1": 20, "2": 97, "3": 473, "4": 1382}
    assert len(value["items"]) == 1972
    by_code = {item["code"]: item for item in value["items"]}
    assert by_code["01"]["parentCode"] == "A"
    assert by_code["0111"]["parentCode"] == "011"
    assert by_code["0111"]["name"] == "稻谷种植"
    csrc = public_csrc_classification()
    assert csrc["standard"] == "JR/T 0020-2024"
    assert [group["code"] for group in csrc["manufacturingSubcategories"]] == [
        "CA", "CB", "CC", "CD", "CE", "CF", "CG", "CH", "CI", "CJ",
    ]
    assert sum(len(group["nbsDivisions"]) for group in csrc["manufacturingSubcategories"]) == 31


def test_visual_queue_detects_tables_and_does_not_mark_model_output_reviewed() -> None:
    assert "TABLE" in _visual_reasons("", [{"rows": [["a"], ["b"], ["c"]]}], [])
    assert "VECTOR_DIAGRAM" in _visual_reasons("", [], [{"kind": "VECTOR"}] * 20)
    assert _visual_reasons("ordinary paragraph", [], []) == []


def test_umi_ocr_adapter_keeps_boxes_and_stays_on_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    value = {"code": 100, "time": 0.1, "data": [{
        "text": "硅片加工为芯片", "score": .94,
        "box": [[200, 100], [400, 100], [400, 150], [200, 150]], "end": "\n",
    }]}

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:1224/api/ocr"
            assert timeout == 45
            return io.BytesIO(json.dumps(value).encode("utf-8"))

    monkeypatch.setattr(research_umi_ocr, "build_opener", lambda *_args: Opener())
    result = research_umi_ocr.umi_ocr_png(b"synthetic image", dpi=200)
    assert result.text == "硅片加工为芯片"
    assert result.blocks[0]["bbox"] == [72.0, 36.0, 144.0, 54.0]
    assert result.mean_confidence == pytest.approx(.94)
    with pytest.raises(ValueError):
        research_umi_ocr.umi_ocr_png(b"x", url="http://example.com:1224")


def test_ocr_numeric_disagreement_needs_original_page_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "研报"
    source.mkdir()
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "Turnover")
        document.save(source / "numbers.pdf")
    root = tmp_path / "data"
    scan_reports(root, [source])
    from market_monitor import report_ocr

    monkeypatch.setattr(report_ocr, "_render_page_png", lambda *_args: b"synthetic png")
    monkeypatch.setattr(report_ocr, "_get_engine", lambda: lambda *_args: (
        [[[0, 0], "Observed turnover 123.46 billion dollars.", .9]], 0.1))
    monkeypatch.setattr(research_umi_ocr, "try_umi_ocr_png", lambda *_args: research_umi_ocr.UmiOCRResult(
        "Observed turnover 123.45 billion dollars.",
        [{"text": "Observed turnover 123.45 billion dollars.", "bbox": [1, 1, 40, 10]}], .95, .1))
    assert process_pages(root)["needsReview"] == 1
    import sqlite3

    with sqlite3.connect(root / "research" / "private" / "corpus.sqlite3") as connection:
        digest = connection.execute("SELECT sha256 FROM documents").fetchone()[0]
    page = private_page(root, digest, 1)
    assert page is not None and page["status"] == "NEEDS_REVIEW"
    assert page["ocrComparison"]["number_agreement"] == 0
    resolve_ocr_disagreement(root, digest, 1, "UMI", "test reviewer", "original page shows 123.45")
    page = private_page(root, digest, 1)
    assert page is not None and page["status"] == "OCR_OK"
    assert page["ocrEngine"] == "umi-ocr-paddle-2.1.5:human-reviewed"


def test_report_pages_keep_physical_numbers_and_private_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    source = tmp_path / "行业产业链研报"
    source.mkdir()
    pdf = source / "sample.pdf"
    _sample_pdf(pdf)
    (source / "copy.pdf").write_bytes(pdf.read_bytes())
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "404 Not Found")
        document.save(source / "error-page.pdf")
    scan = scan_reports(root, [source])
    assert scan["paths"] == 2
    assert scan["unique"] == 1
    assert scan["duplicates"] == 1
    assert scan["pages"] == 3
    assert scan["errors"] == 1
    run = process_pages(root, use_ocr=False)
    assert run["processed"] == 3
    sha = next((root / "research" / "private").glob("*.sqlite3"), None)
    assert sha is not None
    import sqlite3

    with sqlite3.connect(sha) as connection:
        digest = connection.execute("SELECT sha256 FROM documents").fetchone()[0]
    second = private_page(root, digest, 2)
    third = private_page(root, digest, 3)
    assert second is not None and second["status"] == "NEEDS_REVIEW"
    assert third is not None and third["page"] == 3 and "因子方法" in third["text"]
    confirm_blank(root, digest, 2)
    assert corpus_status(root)["pendingPages"] == 0
    evidence = "硅片经过加工后形成芯片"
    payload = {
        "chain": "半导体", "sourceType": "RAW_MATERIAL", "sourceName": "硅片",
        "predicate": "INPUT_TO", "targetType": "PRODUCT", "targetName": "芯片",
        "summary": "硅片是芯片制造的上游材料。", "stage": "上游",
    }
    monkeypatch.setattr("market_monitor.research_corpus._local_model_json",
                        lambda *_args: {"items": [{"kind": "INDUSTRY_RELATION", "payload": payload,
                                                    "evidenceText": evidence}]})
    proposed = propose_from_local_model(root, url="http://127.0.0.1:8080", model="synthetic-local-model",
                                        extraction_version="synthetic-v1", max_pages=1, verify_model=False)
    assert proposed["candidates"] == 1
    identifier = add_candidate(root, digest, 1, "INDUSTRY_RELATION", payload, evidence)
    review_candidate(root, identifier, "APPROVED", "test reviewer", "confirmed against physical page",
                     current_verified=True)
    publication = publish_research(root)
    assert publication["chains"] > 0
    catalog = read_publication(root, "catalog")
    chain_id = next(row["id"] for row in catalog["chains"] if row["name"] == "半导体")
    detail = read_publication(root, "chains", chain_id)
    rendered = json.dumps(detail, ensure_ascii=False)
    assert "evidenceText" not in rendered and "sourceId" not in rendered
    assert "sample.pdf" not in rendered and evidence not in rendered
    for customer_file in (root / "research" / "published" / "versions" /
                          publication["version"]).rglob("*.json"):
        public_text = customer_file.read_text(encoding="utf-8")
        assert "sample.pdf" not in public_text
        assert evidence not in public_text
        assert "evidenceText" not in public_text
        assert str(source) not in public_text
    assert detail["edges"][0]["fromNode"] != detail["edges"][0]["toNode"]
    assert rollback_research(root, publication["version"])["version"] == publication["version"]
    published_detail = (root / "research" / "published" / "versions" / publication["version"] / "chains" /
                        f"{chain_id}.json")
    published_detail.write_text('{"source":"private.pdf"}', encoding="utf-8")
    with pytest.raises(ValueError, match="integrity"):
        read_publication(root, "chains", chain_id)


def test_publication_rejects_quote_and_customer_atlas_is_gone(tmp_path: Path) -> None:
    from market_monitor.research_publication import _projection

    catalog, details, quant = _projection([{"kind": "INDUSTRY_RELATION", "evidenceText": "原文证据", "payload": {
            "chain": "半导体", "sourceType": "PRODUCT", "sourceName": "芯片", "predicate": "USED_IN",
            "targetType": "APPLICATION", "targetName": "汽车", "summary": "原文证据", "stage": "下游",
        }}])
    assert quant["skippedInvalidCandidates"] == 1
    assert not any(item["relationCount"] for item in catalog["chains"])
    assert not any(item["edges"] for item in details.values())
    base = {"chain": "半导体", "sourceType": "RAW_MATERIAL", "sourceName": "硅片",
            "predicate": "INPUT_TO", "targetType": "PRODUCT", "targetName": "芯片", "stage": "上游"}
    _, conflicting_details, meta = _projection([
        {"id": "one", "sha256": "a", "kind": "INDUSTRY_RELATION", "evidenceText": "evidence one",
         "payload": {**base, "summary": "硅片用于制造芯片。"}},
        {"id": "two", "sha256": "b", "kind": "INDUSTRY_RELATION", "evidenceText": "evidence two",
         "payload": {**base, "summary": "硅片经另一工艺制造芯片。"}},
    ])
    assert meta["conflictedRelations"] == 1
    assert not any(item["edges"] for item in conflicting_details.values())
    method_payload = {"name": "动量评价方法", "kind": "方法", "category": "因子选股",
                      "summary": "按指定窗口评价历史价格变化。", "formula": "x = p_t / p_t-1 - 1",
                      "requiredData": ["复权日线"], "assumptions": [], "limitations": [], "blockReasons": []}
    _, _, quant_methods = _projection([
        {"id": "m1", "kind": "QUANT_METHOD", "evidenceText": "different original wording one",
         "payload": method_payload},
        {"id": "m2", "kind": "QUANT_METHOD", "evidenceText": "different original wording two",
         "payload": {**method_payload, "formula": "x = log(p_t / p_t-1)"}},
    ])
    assert len(quant_methods["methods"]) == 2
    assert len({item["id"] for item in quant_methods["methods"]}) == 2
    _, _, copied = _projection([{"id": "bad", "kind": "QUANT_METHOD",
                                "evidenceText": "The report formula is exactly this long copied expression 123456.",
                                "payload": {**method_payload,
                                            "formula": "The report formula is exactly this long copied expression 123456."}}])
    assert copied["skippedInvalidCandidates"] == 1
    app = create_web_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/industry/atlas").status_code == 410
        assert client.get("/api/quant/methods").status_code == 200
        assert client.get("/api/industry/catalog").json()["status"] == "NOT_PUBLISHED"


def test_quant_readiness_blocks_weighted_series_and_missing_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    start = (today - timedelta(days=60)).isoformat()
    end = (today - timedelta(days=1)).isoformat()
    bars = [{"bar_open_time": (today - timedelta(days=30 - index)).isoformat(),
             "quality_status": "OK", "series_kind": "WEIGHTED", "close": 100 + index}
            for index in range(30)]
    monkeypatch.setattr(quant_research, "read_bars", lambda *_args, **_kwargs: bars)
    gate = quant_research.readiness(tmp_path, "price-distribution", "DCE.M", start, end)
    assert not gate["ready"] and "加权序列" in "".join(gate["reasons"])
    assert not quant_research.readiness(tmp_path, "rolling-volatility", "SH.600000", start, end)["ready"]
    with pytest.raises(ValueError):
        quant_research.readiness(tmp_path, "price-distribution", "SH.600000", start, today.isoformat())


def test_public_research_overviews_are_readable_but_not_runnable(tmp_path: Path) -> None:
    catalog = quant_research.methods(tmp_path)
    public = [item for item in catalog["methods"] if item.get("origin") == "PUBLIC_RESEARCH_OVERVIEW"]
    assert len(public) == 4
    for item in public:
        assert item["implementationStatus"] == "EXPLAINED" and item["blockReasons"]
        gate = quant_research.readiness(tmp_path, item["id"], "CN.SSE.STOCK.600000", "2026-01-05", "2026-03-01")
        assert not gate["ready"]
    assert "evidenceText" not in json.dumps(catalog) and ".pdf" not in json.dumps(catalog)


def test_quant_run_rechecks_data_after_readiness_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    def changing_bars(*_args):
        calls.append(1)
        if len(calls) == 1:
            return ([{"bar_open_time": "2026-01-05", "close": 100}] * 25, [])
        return ([], ["部分行情未通过 Silver 质量门禁"])
    monkeypatch.setattr(quant_research, "_price_bars", changing_bars)
    with pytest.raises(ValueError, match="质量门禁"):
        quant_research.run_diagnostic(tmp_path, "price-distribution", "CN.SSE.STOCK.600000", "2026-01-05", "2026-03-01")
    assert not (tmp_path / "research" / "runs").exists()


def test_quant_price_diagnostic_is_real_and_reproducible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from market_monitor.a_share_calendar import is_verified_a_share_trading_day

    days = []
    cursor = date(2026, 1, 5)
    while len(days) < 31:
        if is_verified_a_share_trading_day(cursor):
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    bars = [{"bar_open_time": day, "quality_status": "PASS", "series_kind": "CONTRACT",
             "close": 100 + index + index % 3} for index, day in enumerate(days)]
    monkeypatch.setattr(quant_research, "read_bars", lambda *_args, **_kwargs: bars)
    result = quant_research.run_diagnostic(tmp_path, "price-distribution", "CN.SSE.STOCK.600000",
                                            days[0], days[-1])
    assert result["purpose"] == "PRICE_DIAGNOSTIC_ONLY"
    assert result["barCount"] == 31 and len(result["observations"]) == 30
    assert result["metrics"]["priceChange"] == pytest.approx(.30)
    assert quant_research.reproduce_run(tmp_path, result["runId"])["reproducible"] is True
    bars[-1]["close"] += 10
    assert quant_research.reproduce_run(tmp_path, result["runId"])["reproducible"] is False


def test_visual_observation_requires_two_reviews_before_customer_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "行业产业链研报"
    source.mkdir()
    _sample_pdf(source / "visual.pdf")
    root = tmp_path / "data"
    scan_reports(root, [source])
    process_pages(root, use_ocr=False)
    import sqlite3

    with sqlite3.connect(root / "research" / "private" / "corpus.sqlite3") as connection:
        digest = connection.execute("SELECT sha256 FROM documents").fetchone()[0]
        connection.execute("UPDATE pages SET tables_json=? WHERE sha256=? AND page=1",
                           (json.dumps([{"rows": [["A"], ["B"], ["C"]]}]), digest))
    assert backfill_visual_queue(root)["queued"] == 1
    assert list_visual_queue(root)["items"][0]["page"] == 1
    monkeypatch.setattr(research_visual, "_visual_lock", lambda *_args: "synthetic-visual-lock")
    monkeypatch.setattr(research_visual, "_recognize", lambda *_args: {"items": [{
        "kind": "FLOW", "label": "芯片流程", "summary": "硅片投入芯片加工",
        "rows": [["硅片", "芯片"]], "unit": "", "period": "", "uncertainties": [],
    }]})
    result = research_visual.propose_visual_page(root, digest, 1, url="http://127.0.0.1:18082",
                                                  model="synthetic-visual", label="test-v1")
    assert result["observations"] == 1
    assert list_visual_queue(root, status="PROPOSED")["items"][0]["page"] == 1
    observation = research_visual.list_visual_observations(root)["items"][0]
    claim = {"chain": "半导体", "sourceType": "RAW_MATERIAL", "sourceName": "硅片",
             "predicate": "INPUT_TO", "targetType": "PRODUCT", "targetName": "芯片",
             "summary": "硅片是芯片制造的前端材料。", "stage": "上游"}
    from market_monitor.research_corpus import add_visual_candidate

    with pytest.raises(ValueError, match="reviewed"):
        add_visual_candidate(root, observation["id"], "INDUSTRY_RELATION", claim)
    research_visual.review_visual_observation(root, observation["id"], "APPROVED", "visual reviewer", "page image checked")
    review_visual_page(root, digest, 1, "visual reviewer", "page image and table checked")
    assert list_visual_queue(root, status="REVIEWED")["items"][0]["page"] == 1
    candidate = add_visual_candidate(root, observation["id"], "INDUSTRY_RELATION", claim)
    assert read_publication(root, "catalog")["status"] == "NOT_PUBLISHED"
    review_candidate(root, candidate, "APPROVED", "claim reviewer", "direction and names checked",
                     current_verified=True)
    publish_research(root)
    catalog = read_publication(root, "catalog")
    row = next(item for item in catalog["chains"] if item["name"] == "半导体")
    detail = read_publication(root, "chains", row["id"])
    assert len(detail["edges"]) == 1
    assert "VISUAL:" not in json.dumps(detail)


def test_approved_claim_fails_closed_if_page_or_pdf_changes(tmp_path: Path) -> None:
    source = tmp_path / "行业产业链研报"
    source.mkdir()
    pdf = source / "first.pdf"
    _sample_pdf(pdf)
    root = tmp_path / "data"
    scan_reports(root, [source])
    process_pages(root, use_ocr=False)
    import sqlite3

    database = root / "research" / "private" / "corpus.sqlite3"
    with sqlite3.connect(database) as connection:
        digest = connection.execute("SELECT sha256 FROM documents").fetchone()[0]
    evidence = "硅片经过加工后形成芯片"
    candidate = add_candidate(root, digest, 1, "INDUSTRY_RELATION", {
        "chain": "半导体", "sourceType": "RAW_MATERIAL", "sourceName": "硅片",
        "predicate": "INPUT_TO", "targetType": "PRODUCT", "targetName": "芯片",
        "summary": "硅片在芯片生产中承担原料作用。", "stage": "上游",
    }, evidence)
    with pytest.raises(ValueError, match="当前产业关系"):
        review_candidate(root, candidate, "APPROVED", "reviewer", "source checked")
    review_candidate(root, candidate, "APPROVED", "reviewer", "current official source checked",
                     current_verified=True)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE pages SET text='changed source' WHERE sha256=? AND page=1", (digest,))
    with pytest.raises(ValueError, match="evidence"):
        publish_research(root)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE pages SET text=? WHERE sha256=? AND page=1", (evidence * 8, digest))
    with pymupdf.open() as document:
        document.new_page().insert_text((50, 50), "a replacement PDF")
        document.save(pdf)
    with pytest.raises(FileNotFoundError):
        publish_research(root)
