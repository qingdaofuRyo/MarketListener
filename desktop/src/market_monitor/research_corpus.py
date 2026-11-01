"""Private, resumable page-level research report ingestion.

No PDF bytes, extracted text, filenames or evidence from this module belong in
the customer read model.  Source PDFs are opened read-only and never moved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from difflib import SequenceMatcher
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PARSER_VERSION = "research-pages-v1"
EVIDENCE_MATCH_VERSION = "verbatim-span-v2"
FINAL_PAGE_STATES = {"TEXT_OK", "OCR_OK", "CONFIRMED_BLANK"}
MODEL_CANDIDATE_KINDS = {"INDUSTRY_RELATION", "QUANT_METHOD"}
_SCHEMA_LOCK = threading.RLock()
_INITIALIZED_DATABASES: set[Path] = set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_text(value: Any) -> str:
    """PDF fonts can decode to lone UTF-16 surrogates, invalid in SQLite UTF-8."""

    return re.sub(r"[\ud800-\udfff]", "\ufffd", str(value))


def _looks_corrupt(text: str) -> bool:
    visible = re.sub(r"\s+", "", text)
    return bool(visible) and visible.count("\ufffd") / len(visible) >= .01


def _connection(data_root: Path) -> sqlite3.Connection:
    location = (Path(data_root) / "research" / "private" / "corpus.sqlite3").resolve()
    location.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(location, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    with _SCHEMA_LOCK:
        if location in _INITIALIZED_DATABASES:
            return connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
          sha256 TEXT PRIMARY KEY, title TEXT NOT NULL, document_kind TEXT NOT NULL,
          bytes INTEGER NOT NULL, page_count INTEGER NOT NULL, parser_version TEXT NOT NULL,
          scanned_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_paths (
          path TEXT PRIMARY KEY, sha256 TEXT NOT NULL REFERENCES documents(sha256),
          seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS source_errors (
          path TEXT NOT NULL, stage TEXT NOT NULL, reason TEXT NOT NULL,
          recorded_at TEXT NOT NULL, PRIMARY KEY (path, stage)
        );
        CREATE TABLE IF NOT EXISTS pages (
          sha256 TEXT NOT NULL REFERENCES documents(sha256), page INTEGER NOT NULL,
          status TEXT NOT NULL, text TEXT NOT NULL, blocks_json TEXT NOT NULL,
          tables_json TEXT NOT NULL, figures_json TEXT NOT NULL, error TEXT,
          parser_version TEXT NOT NULL, processed_at TEXT NOT NULL,
          PRIMARY KEY (sha256, page)
        );
        CREATE TABLE IF NOT EXISTS page_ocr (
          sha256 TEXT NOT NULL, page INTEGER NOT NULL, engine TEXT NOT NULL,
          mean_confidence REAL, processed_at TEXT NOT NULL,
          PRIMARY KEY (sha256,page),
          FOREIGN KEY (sha256,page) REFERENCES pages(sha256,page)
        );
        CREATE TABLE IF NOT EXISTS ocr_comparisons (
          sha256 TEXT NOT NULL, page INTEGER NOT NULL,
          umi_text TEXT NOT NULL, umi_blocks_json TEXT NOT NULL,
          rapid_text TEXT NOT NULL, number_agreement REAL,
          compared_at TEXT NOT NULL, PRIMARY KEY (sha256,page),
          FOREIGN KEY (sha256,page) REFERENCES pages(sha256,page)
        );
        CREATE TABLE IF NOT EXISTS ocr_resolution_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          sha256 TEXT NOT NULL, page INTEGER NOT NULL, engine TEXT NOT NULL,
          reviewer TEXT NOT NULL, note TEXT NOT NULL, resolved_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
          id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, page INTEGER NOT NULL,
          kind TEXT NOT NULL, payload_json TEXT NOT NULL, evidence_text TEXT NOT NULL,
          status TEXT NOT NULL, extraction_version TEXT NOT NULL,
          reviewer TEXT, review_note TEXT, created_at TEXT NOT NULL, reviewed_at TEXT,
          FOREIGN KEY (sha256, page) REFERENCES pages(sha256, page)
        );
        CREATE INDEX IF NOT EXISTS candidates_review_idx ON candidates(status, kind);
        CREATE TABLE IF NOT EXISTS candidate_runs (
          sha256 TEXT NOT NULL, page INTEGER NOT NULL, extraction_version TEXT NOT NULL,
          status TEXT NOT NULL, candidate_count INTEGER NOT NULL, error TEXT,
          completed_at TEXT NOT NULL,
          PRIMARY KEY (sha256, page, extraction_version),
          FOREIGN KEY (sha256, page) REFERENCES pages(sha256, page)
        );
        CREATE TABLE IF NOT EXISTS review_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id TEXT NOT NULL,
          previous_status TEXT NOT NULL, next_status TEXT NOT NULL,
          reviewer TEXT NOT NULL, note TEXT NOT NULL, occurred_at TEXT NOT NULL,
          FOREIGN KEY (candidate_id) REFERENCES candidates(id)
        );
        CREATE TABLE IF NOT EXISTS model_locks (
          extraction_version TEXT PRIMARY KEY, label TEXT NOT NULL,
          model_alias TEXT NOT NULL, model_sha256 TEXT NOT NULL,
          runtime_build TEXT NOT NULL, prompt_sha256 TEXT NOT NULL,
          parser_version TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proposal_rejections (
          id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, page INTEGER NOT NULL,
          extraction_version TEXT NOT NULL, item_json TEXT NOT NULL,
          reason TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visual_observations (
          id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, page INTEGER NOT NULL,
          kind TEXT NOT NULL, payload_json TEXT NOT NULL,
          image_sha256 TEXT NOT NULL, model_lock TEXT NOT NULL,
          status TEXT NOT NULL, reviewer TEXT, review_note TEXT,
          created_at TEXT NOT NULL, reviewed_at TEXT,
          FOREIGN KEY (sha256,page) REFERENCES pages(sha256,page)
        );
        CREATE TABLE IF NOT EXISTS visual_runs (
          sha256 TEXT NOT NULL, page INTEGER NOT NULL, model_lock TEXT NOT NULL,
          status TEXT NOT NULL, observation_count INTEGER NOT NULL,
          error TEXT, completed_at TEXT NOT NULL,
          PRIMARY KEY (sha256,page,model_lock)
        );
        CREATE TABLE IF NOT EXISTS visual_review_events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          observation_id TEXT NOT NULL, previous_status TEXT NOT NULL,
          next_status TEXT NOT NULL, reviewer TEXT NOT NULL,
          note TEXT NOT NULL, occurred_at TEXT NOT NULL,
          FOREIGN KEY (observation_id) REFERENCES visual_observations(id)
        );
        CREATE TABLE IF NOT EXISTS visual_queue (
          sha256 TEXT NOT NULL, page INTEGER NOT NULL,
          reasons_json TEXT NOT NULL, priority INTEGER NOT NULL,
          status TEXT NOT NULL, reviewer TEXT, review_note TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (sha256,page),
          FOREIGN KEY (sha256,page) REFERENCES pages(sha256,page)
        );
        CREATE TABLE IF NOT EXISTS document_priority (
          sha256 TEXT PRIMARY KEY, topic TEXT, report_heat INTEGER NOT NULL,
          FOREIGN KEY (sha256) REFERENCES documents(sha256)
        );
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(review_events)")}
        if "current_verified_at" not in columns:
            connection.execute("ALTER TABLE review_events ADD COLUMN current_verified_at TEXT")
        connection.commit()
        _INITIALIZED_DATABASES.add(location)
    return connection


def backfill_ocr_provenance(data_root: Path) -> int:
    """One-time label for OCR pages written before engine provenance existed."""

    with _connection(data_root) as connection:
        return connection.execute("""
            INSERT OR IGNORE INTO page_ocr(sha256,page,engine,mean_confidence,processed_at)
            SELECT sha256,page,'rapidocr-legacy',NULL,processed_at FROM pages WHERE status='OCR_OK'
        """).rowcount


def _pdf_files(roots: Iterable[Path]) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for raw_root in roots:
        root = Path(raw_root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(root)
        kind = "INDUSTRY" if "行业" in root.name or "产业链" in root.name else "QUANT"
        files.extend((path, _report_kind(kind, path.stem)) for path in root.rglob("*")
                     if path.is_file() and path.suffix.lower() == ".pdf")
    return sorted(files, key=lambda item: str(item[0]).casefold())


def _report_kind(folder_kind: str, title: str) -> str:
    """Mark cross-topic reports for both specialist passes; the folder is only a hint."""

    industry_terms = ("行业", "产业链", "供应链", "原材料", "制造工艺")
    quant_terms = ("金融工程", "金工", "量化", "因子", "择时", "CTA", "套利", "回测")
    if folder_kind == "QUANT" and any(term in title for term in industry_terms):
        return "MIXED"
    if folder_kind == "INDUSTRY" and any(term in title for term in quant_terms):
        return "MIXED"
    return folder_kind


def _verified_pdf_path(connection: sqlite3.Connection, sha256: str) -> Path:
    rows = connection.execute("SELECT path FROM document_paths WHERE sha256=? ORDER BY path",
                              (sha256,)).fetchall()
    for row in rows:
        path = Path(row["path"])
        try:
            if path.is_file() and _digest(path) == sha256:
                return path
        except OSError:
            continue
    raise FileNotFoundError("No unchanged source PDF remains for this document hash")


def scan_reports(data_root: Path, roots: Iterable[Path], *, limit: int | None = None,
                 on_progress: Callable[[dict[str, int]], None] | None = None,
                 should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    """Hash every path, count physical pages and keep same-content aliases."""

    import pymupdf

    roots = tuple(roots)
    files = _pdf_files(roots)
    source_roots = [Path(root).resolve() for root in roots]
    seen_paths: set[str] = set()
    result = {"paths": 0, "unique": 0, "duplicates": 0, "pages": 0, "errors": 0, "totalPaths": len(files)}
    with _connection(data_root) as connection:
        for path, kind in files[:limit]:
            if should_stop and should_stop():
                result["cancelled"] = 1
                break
            try:
                sha256 = _digest(path)
                with pymupdf.open(path) as document:
                    page_count = len(document)
                    if page_count <= 2:
                        first_page = document[0].get_text("text") if page_count else ""
                        if len(first_page) < 1000 and re.search(
                            r"(?:404\s*(?:not found|error|页面不存在)|(?:not found|页面不存在)\s*404)",
                            first_page, re.IGNORECASE,
                        ):
                            raise ValueError("HTTP 404 response was saved as a PDF")
                if page_count < 1:
                    raise ValueError("PDF has no pages")
                known = connection.execute("SELECT page_count,document_kind FROM documents WHERE sha256=?",
                                           (sha256,)).fetchone()
                if known is None:
                    connection.execute(
                        "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (sha256, path.stem, kind, path.stat().st_size, page_count, PARSER_VERSION, _now()),
                    )
                    result["unique"] += 1
                    result["pages"] += page_count
                else:
                    result["duplicates"] += 1
                    revised_kind = ("MIXED" if "MIXED" in (known["document_kind"], kind) or
                                    known["document_kind"] != kind else kind)
                    if revised_kind != known["document_kind"]:
                        connection.execute("UPDATE documents SET document_kind=? WHERE sha256=?",
                                           (revised_kind, sha256))
                connection.execute(
                    "INSERT INTO document_paths(path, sha256, seen_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, seen_at=excluded.seen_at",
                    (str(path), sha256, _now()),
                )
                connection.execute("DELETE FROM source_errors WHERE path=? AND stage='SCAN'", (str(path),))
                seen_paths.add(str(path))
                result["paths"] += 1
            except (OSError, ValueError, RuntimeError) as failure:
                result["errors"] += 1
                connection.execute("""
                    INSERT INTO source_errors VALUES (?, 'SCAN', ?, ?)
                    ON CONFLICT(path,stage) DO UPDATE SET reason=excluded.reason,recorded_at=excluded.recorded_at
                """, (str(path), f"{type(failure).__name__}: {failure}"[:500], _now()))
            if (result["paths"] + result["errors"]) % 25 == 0:
                connection.commit()
                if on_progress:
                    on_progress(dict(result))
        connection.commit()
        if limit is None and not result.get("cancelled"):
            for row in connection.execute("SELECT path FROM document_paths").fetchall():
                path = Path(row["path"])
                if any(path.is_relative_to(root) for root in source_roots) and row["path"] not in seen_paths:
                    connection.execute("DELETE FROM document_paths WHERE path=?", (row["path"],))
            connection.commit()
        _rebuild_priorities(connection)
    if on_progress:
        on_progress(dict(result))
    return result


def _rebuild_priorities(connection: sqlite3.Connection) -> None:
    from .chain_taxonomy import CANONICAL_CHAIN_MAP

    records = connection.execute("SELECT sha256,title,document_kind FROM documents").fetchall()
    quant_topics = ("因子", "CTA", "期权", "波动率", "行业轮动", "择时", "机器学习", "可转债",
                    "固收", "组合", "风险", "指数增强", "高频", "套利", "资产配置")
    assignments: list[tuple[str, str | None]] = []
    counts: dict[str, int] = {}
    labels = sorted(CANONICAL_CHAIN_MAP, key=len, reverse=True)
    for row in records:
        title = row["title"]
        if row["document_kind"] == "INDUSTRY":
            topic = next((CANONICAL_CHAIN_MAP[label] for label in labels
                          if label not in {"宏观", "策略", "量化", "商品期货"} and label in title), None)
        else:
            topic = next((label for label in quant_topics if label.casefold() in title.casefold()), None)
        assignments.append((row["sha256"], topic))
        if topic:
            counts[topic] = counts.get(topic, 0) + 1
    connection.executemany("""
        INSERT INTO document_priority VALUES (?, ?, ?)
        ON CONFLICT(sha256) DO UPDATE SET topic=excluded.topic,report_heat=excluded.report_heat
    """, [(sha, topic, counts.get(topic, 0)) for sha, topic in assignments])
    connection.commit()


def rebuild_report_priority(data_root: Path) -> dict[str, int]:
    with _connection(data_root) as connection:
        _rebuild_priorities(connection)
        populated = connection.execute("SELECT count(*) FROM document_priority WHERE report_heat>0").fetchone()[0]
    return {"prioritizedDocuments": populated}


def _page_payload(page: Any, *, use_ocr: bool) -> dict[str, Any]:
    document = page.get_text("dict")
    blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    for block in document.get("blocks", []):
        bbox = [round(float(value), 2) for value in block.get("bbox", ())]
        if block.get("type") == 1:
            figures.append({"kind": "IMAGE", "bbox": bbox})
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                value = _safe_text(span.get("text") or "").strip()
                if value:
                    blocks.append({"text": value, "bbox": [round(float(x), 2) for x in span.get("bbox", ())]})
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None:
            figures.append({"kind": "VECTOR", "bbox": [round(float(x), 2) for x in rect]})
    tables: list[dict[str, Any]] = []
    try:
        for table in page.find_tables():
            tables.append({
                "bbox": [round(float(x), 2) for x in table.bbox],
                "rows": [[_safe_text(cell) if cell is not None else None for cell in row]
                         for row in table.extract()],
            })
    except (AttributeError, ValueError, RuntimeError):
        pass
    text = "\n".join(item["text"] for item in blocks)
    status = "TEXT_OK"
    ocr_engine = None
    ocr_confidence = None
    comparison = None
    corrupt = _looks_corrupt(text)
    if (len(re.sub(r"\s+", "", text)) < 120 or corrupt) and use_ocr:
        from .report_ocr import _get_engine, _render_page_png
        from .research_umi_ocr import try_umi_ocr_png

        png = _render_page_png(page)
        umi = try_umi_ocr_png(png)
        try:
            result, _elapsed = _get_engine()(png)
        except (ImportError, RuntimeError):
            result = []
        rapid_text = "\n".join(_safe_text(item[1]).strip() for item in result or []
                               if _safe_text(item[1]).strip())
        if umi and umi.text and (umi.mean_confidence or 0) >= .65 and (corrupt or len(umi.text) > len(text)):
            text, blocks, status = umi.text, umi.blocks, "OCR_OK"
            ocr_engine, ocr_confidence = "umi-ocr-paddle-2.1.5", umi.mean_confidence
        if rapid_text and status != "OCR_OK" and (corrupt or len(rapid_text) > len(text)):
            text, blocks, status = rapid_text, [], "OCR_OK"
            ocr_engine, ocr_confidence = "rapidocr-onnxruntime", None
        if umi is not None:
            umi_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?", umi.text))
            rapid_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?", rapid_text))
            agreement = len(umi_numbers & rapid_numbers) / max(1, len(umi_numbers | rapid_numbers))
            comparison = {"umiText": umi.text, "umiBlocks": umi.blocks, "rapidText": rapid_text,
                          "numberAgreement": agreement}
            if umi.text and rapid_text and agreement < 1:
                status = "NEEDS_REVIEW"
    if not text.strip():
        status = "NEEDS_REVIEW"
    return {"status": status, "text": text, "blocks": blocks,
            "tables": tables, "figures": figures,
            "ocrEngine": ocr_engine, "ocrConfidence": ocr_confidence,
            "ocrComparison": comparison}


def _visual_reasons(text: str, tables: list[dict[str, Any]],
                    figures: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    if any(len(table.get("rows") or []) >= 3 for table in tables):
        reasons.append("TABLE")
    if any(figure.get("kind") == "IMAGE" and len(figure.get("bbox") or []) == 4 and
           (figure["bbox"][2] - figure["bbox"][0]) *
           (figure["bbox"][3] - figure["bbox"][1]) >= 20_000 for figure in figures):
        reasons.append("LARGE_IMAGE")
    if sum(figure.get("kind") == "VECTOR" for figure in figures) >= 20:
        reasons.append("VECTOR_DIAGRAM")
    if re.search(r"(?:图|表)\s*[0-9一二三四五六七八九十]+|流程图|产业链图", text[:8000]):
        reasons.append("CAPTION")
    return reasons


def backfill_visual_queue(data_root: Path) -> dict[str, int]:
    """Discover visual pages from prior extraction without rerunning OCR."""

    result = {"scanned": 0, "queued": 0}
    with _connection(data_root) as connection:
        rows = connection.execute("SELECT sha256,page,text,tables_json,figures_json FROM pages "
                                  "WHERE parser_version=? AND status IN ('TEXT_OK','OCR_OK','NEEDS_REVIEW')",
                                  (PARSER_VERSION,)).fetchall()
        for row in rows:
            reasons = _visual_reasons(row["text"], json.loads(row["tables_json"]),
                                      json.loads(row["figures_json"]))
            result["scanned"] += 1
            if not reasons:
                continue
            changed = connection.execute("""
                INSERT OR IGNORE INTO visual_queue VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?)
            """, (row["sha256"], row["page"], json.dumps(reasons), len(reasons), _now())).rowcount
            result["queued"] += changed
    return result


def list_visual_queue(data_root: Path, *, status: str = "PENDING", limit: int = 100) -> dict[str, Any]:
    if status not in {"PENDING", "PROPOSED", "MODEL_EMPTY", "ERROR", "REVIEWED"}:
        raise ValueError("Unknown visual queue status")
    with _connection(data_root) as connection:
        rows = connection.execute("""
            SELECT q.sha256,q.page,q.reasons_json,q.priority,q.status,q.updated_at,d.title,
                   coalesce(p.report_heat,0) AS report_heat
            FROM visual_queue q JOIN documents d ON d.sha256=q.sha256
            LEFT JOIN document_priority p ON p.sha256=q.sha256
            WHERE q.status=? ORDER BY q.priority DESC, report_heat DESC, q.sha256,q.page LIMIT ?
        """, (status, min(max(limit, 1), 200))).fetchall()
    return {"items": [{**dict(row), "reasons": json.loads(row["reasons_json"])} for row in rows]}


def review_visual_page(data_root: Path, sha256: str, page: int, reviewer: str, note: str) -> None:
    if not reviewer.strip() or not note.strip():
        raise ValueError("Visual page review requires reviewer and reason")
    with _connection(data_root) as connection:
        changed = connection.execute("""
            UPDATE visual_queue SET status='REVIEWED',reviewer=?,review_note=?,updated_at=?
            WHERE sha256=? AND page=? AND status IN ('PROPOSED','MODEL_EMPTY')
        """, (reviewer.strip(), note.strip(), _now(), sha256, page)).rowcount
        if changed != 1:
            raise ValueError("Visual page has no completed proposal to review")


def process_pages(data_root: Path, *, max_pages: int = 500, use_ocr: bool = True,
                  retry_problem_pages: bool = False,
                  only_documents: set[str] | None = None,
                  on_progress: Callable[[dict[str, int]], None] | None = None,
                  should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    """Resume at unfinished pages; each page is committed independently."""

    import pymupdf

    outcome = {"processed": 0, "text": 0, "ocr": 0, "needsReview": 0, "errors": 0}
    with _connection(data_root) as connection:
        documents = connection.execute("""
            SELECT d.sha256,d.page_count FROM documents d
            LEFT JOIN document_priority p USING (sha256)
            WHERE EXISTS (SELECT 1 FROM document_paths paths WHERE paths.sha256=d.sha256)
            ORDER BY coalesce(p.report_heat,0) DESC,d.document_kind,d.sha256
        """).fetchall()
        for document in documents:
            if outcome["processed"] >= max_pages:
                break
            if only_documents is not None and document["sha256"] not in only_documents:
                continue
            existing = {int(row["page"]): row["status"] for row in connection.execute(
                "SELECT page, status FROM pages WHERE sha256=? AND parser_version=?",
                (document["sha256"], PARSER_VERSION),
            )}
            pending = [number for number in range(1, int(document["page_count"]) + 1)
                       if number not in existing or (retry_problem_pages and existing[number] == "ERROR")]
            if not pending:
                continue
            try:
                source = _verified_pdf_path(connection, document["sha256"])
            except FileNotFoundError:
                connection.execute("""
                    INSERT INTO source_errors VALUES (?, 'PROCESS', 'No matching read-only PDF path', ?)
                    ON CONFLICT(path,stage) DO UPDATE SET reason=excluded.reason,recorded_at=excluded.recorded_at
                """, (document["sha256"], _now()))
                connection.commit()
                continue
            with pymupdf.open(source) as pdf:
                connection.execute("DELETE FROM source_errors WHERE path=? AND stage='PROCESS'",
                                   (document["sha256"],))
                for number in pending:
                    if outcome["processed"] >= max_pages:
                        break
                    if should_stop and should_stop():
                        outcome["cancelled"] = 1
                        return outcome
                    try:
                        value = _page_payload(pdf[number - 1], use_ocr=use_ocr)
                        error = None
                    except Exception as failure:  # noqa: BLE001 - persist isolated page errors
                        value = {"status": "ERROR", "text": "", "blocks": [], "tables": [], "figures": []}
                        error = f"{type(failure).__name__}: {failure}"[:500]
                    connection.execute("""
                        INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(sha256, page) DO UPDATE SET
                          status=excluded.status, text=excluded.text, blocks_json=excluded.blocks_json,
                          tables_json=excluded.tables_json, figures_json=excluded.figures_json,
                          error=excluded.error, parser_version=excluded.parser_version,
                          processed_at=excluded.processed_at
                    """, (document["sha256"], number, value["status"], value["text"],
                          json.dumps(value["blocks"], ensure_ascii=False),
                          json.dumps(value["tables"], ensure_ascii=False),
                          json.dumps(value["figures"], ensure_ascii=False), error,
                          PARSER_VERSION, _now()))
                    if value["status"] != "ERROR":
                        reasons = _visual_reasons(value["text"], value["tables"], value["figures"])
                        if reasons:
                            connection.execute("""
                                INSERT OR IGNORE INTO visual_queue VALUES (?, ?, ?, ?, 'PENDING', NULL, NULL, ?)
                            """, (document["sha256"], number, json.dumps(reasons), len(reasons), _now()))
                    if value["status"] == "OCR_OK" and value.get("ocrEngine"):
                        connection.execute("""
                            INSERT INTO page_ocr VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(sha256,page) DO UPDATE SET
                            engine=excluded.engine,mean_confidence=excluded.mean_confidence,
                            processed_at=excluded.processed_at
                        """, (document["sha256"], number, value["ocrEngine"],
                              value.get("ocrConfidence"), _now()))
                    comparison = value.get("ocrComparison")
                    if comparison is not None:
                        connection.execute("""
                            INSERT INTO ocr_comparisons VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(sha256,page) DO UPDATE SET
                              umi_text=excluded.umi_text,umi_blocks_json=excluded.umi_blocks_json,
                              rapid_text=excluded.rapid_text,number_agreement=excluded.number_agreement,
                              compared_at=excluded.compared_at
                        """, (document["sha256"], number, comparison["umiText"],
                              json.dumps(comparison["umiBlocks"], ensure_ascii=False),
                              comparison["rapidText"], comparison["numberAgreement"], _now()))
                    connection.commit()
                    outcome["processed"] += 1
                    bucket = {"TEXT_OK": "text", "OCR_OK": "ocr", "NEEDS_REVIEW": "needsReview",
                              "ERROR": "errors"}[value["status"]]
                    outcome[bucket] += 1
                    if on_progress and outcome["processed"] % 25 == 0:
                        on_progress(dict(outcome))
    if on_progress:
        on_progress(dict(outcome))
    return outcome


def corpus_status(data_root: Path) -> dict[str, Any]:
    with _connection(data_root) as connection:
        documents, total_pages = connection.execute(
            "SELECT count(*), coalesce(sum(page_count),0) FROM documents d "
            "WHERE EXISTS (SELECT 1 FROM document_paths paths WHERE paths.sha256=d.sha256)"
        ).fetchone()
        paths = connection.execute("SELECT count(*) FROM document_paths").fetchone()[0]
        states = {row[0]: row[1] for row in connection.execute("""
            SELECT p.status,count(*) FROM pages p
            WHERE p.parser_version=? AND EXISTS
              (SELECT 1 FROM document_paths paths WHERE paths.sha256=p.sha256)
            GROUP BY p.status
        """, (PARSER_VERSION,))}
        reviews = {row[0]: row[1] for row in connection.execute("SELECT status,count(*) FROM candidates GROUP BY status")}
        source_errors = connection.execute("SELECT count(*) FROM source_errors").fetchone()[0]
        ocr_engines = {row[0]: row[1] for row in connection.execute(
            "SELECT o.engine,count(*) FROM page_ocr o JOIN pages p "
            "ON p.sha256=o.sha256 AND p.page=o.page WHERE p.parser_version=? GROUP BY o.engine",
            (PARSER_VERSION,))}
        visual_states = {row[0]: row[1] for row in connection.execute(
            "SELECT status,count(*) FROM visual_observations GROUP BY status")}
        visual_queue_states = {row[0]: row[1] for row in connection.execute(
            "SELECT status,count(*) FROM visual_queue GROUP BY status")}
    completed = sum(states.get(key, 0) for key in FINAL_PAGE_STATES)
    return {"documents": documents, "paths": paths, "pages": total_pages,
            "parserVersion": PARSER_VERSION, "completedPages": completed,
            "pendingPages": max(0, total_pages - completed), "pageStates": states,
            "candidateStates": reviews, "visualStates": visual_states,
            "visualQueueStates": visual_queue_states,
            "sourceErrors": source_errors, "ocrEngines": ocr_engines}


def list_source_errors(data_root: Path, *, limit: int = 100) -> dict[str, Any]:
    with _connection(data_root) as connection:
        rows = connection.execute("SELECT path,stage,reason,recorded_at FROM source_errors "
                                  "ORDER BY recorded_at DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
    return {"items": [dict(row) for row in rows]}


def list_documents(data_root: Path, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    with _connection(data_root) as connection:
        rows = connection.execute("""
            SELECT d.sha256, d.title, d.document_kind, d.page_count, d.scanned_at,
                   count(pg.page) AS processed_pages,
                   sum(CASE WHEN pg.status IN ('TEXT_OK','OCR_OK','CONFIRMED_BLANK') THEN 1 ELSE 0 END) AS completed_pages
            FROM documents d LEFT JOIN pages pg ON pg.sha256=d.sha256
            GROUP BY d.sha256 ORDER BY d.scanned_at DESC, d.title LIMIT ? OFFSET ?
        """, (min(max(limit, 1), 200), max(offset, 0))).fetchall()
    return {"items": [dict(row) for row in rows]}


def private_page(data_root: Path, sha256: str, page: int) -> dict[str, Any] | None:
    with _connection(data_root) as connection:
        row = connection.execute("SELECT * FROM pages WHERE sha256=? AND page=?", (sha256, page)).fetchone()
        ocr = connection.execute("SELECT engine,mean_confidence FROM page_ocr WHERE sha256=? AND page=?",
                                 (sha256, page)).fetchone()
        comparison = connection.execute("SELECT umi_text,rapid_text,number_agreement FROM ocr_comparisons "
                                        "WHERE sha256=? AND page=?", (sha256, page)).fetchone()
    if row is None:
        return None
    return {**dict(row), "ocrEngine": ocr["engine"] if ocr else None,
            "ocrConfidence": ocr["mean_confidence"] if ocr else None,
            "ocrComparison": dict(comparison) if comparison else None,
            "blocks": json.loads(row["blocks_json"]),
            "tables": json.loads(row["tables_json"]), "figures": json.loads(row["figures_json"])}


def private_page_png(data_root: Path, sha256: str, page: int, *, dpi: int = 110) -> bytes:
    import pymupdf

    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or page < 1 or dpi < 72 or dpi > 180:
        raise ValueError("Invalid private PDF page request")
    with _connection(data_root) as connection:
        path = _verified_pdf_path(connection, sha256)
    with pymupdf.open(path) as document:
        if page > len(document):
            raise ValueError("Page is outside the PDF")
        pixmap = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False)
        return pixmap.tobytes("png")


def confirm_blank(data_root: Path, sha256: str, page: int) -> None:
    with _connection(data_root) as connection:
        row = connection.execute("SELECT status, text FROM pages WHERE sha256=? AND page=?", (sha256, page)).fetchone()
        if row is None or row["status"] != "NEEDS_REVIEW" or row["text"].strip():
            raise ValueError("Only an extracted empty page can be confirmed blank")
        connection.execute("UPDATE pages SET status='CONFIRMED_BLANK', processed_at=? "
                           "WHERE sha256=? AND page=?", (_now(), sha256, page))


def resolve_ocr_disagreement(data_root: Path, sha256: str, page: int, engine: str,
                             reviewer: str, note: str) -> None:
    if engine not in {"UMI", "RAPID"} or not reviewer.strip() or not note.strip():
        raise ValueError("OCR resolution needs an engine, reviewer and reason")
    with _connection(data_root) as connection:
        current = connection.execute("SELECT status FROM pages WHERE sha256=? AND page=?",
                                     (sha256, page)).fetchone()
        comparison = connection.execute("SELECT * FROM ocr_comparisons WHERE sha256=? AND page=?",
                                        (sha256, page)).fetchone()
        if current is None or current["status"] != "NEEDS_REVIEW" or comparison is None:
            raise ValueError("Page has no unresolved OCR comparison")
        text = comparison["umi_text"] if engine == "UMI" else comparison["rapid_text"]
        if not text.strip():
            raise ValueError("Selected OCR engine has no readable text")
        blocks = comparison["umi_blocks_json"] if engine == "UMI" else "[]"
        selected = "umi-ocr-paddle-2.1.5" if engine == "UMI" else "rapidocr-onnxruntime"
        connection.execute("UPDATE pages SET text=?,blocks_json=?,status='OCR_OK',processed_at=? "
                           "WHERE sha256=? AND page=?", (text, blocks, _now(), sha256, page))
        connection.execute("""
            INSERT INTO page_ocr VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(sha256,page) DO UPDATE SET
              engine=excluded.engine,mean_confidence=NULL,processed_at=excluded.processed_at
        """, (sha256, page, selected + ":human-reviewed", _now()))
        connection.execute("INSERT INTO ocr_resolution_events(sha256,page,engine,reviewer,note,resolved_at) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (sha256, page, engine, reviewer.strip(), note.strip(), _now()))


def add_candidate(data_root: Path, sha256: str, page: int, kind: str,
                  payload: dict[str, Any], evidence_text: str,
                  *, extraction_version: str = "manual-v1") -> str:
    if kind not in MODEL_CANDIDATE_KINDS:
        raise ValueError("Unknown candidate kind")
    value = _safe_text(evidence_text).strip()
    if len(value) < 8:
        raise ValueError("Evidence span is too short")
    with _connection(data_root) as connection:
        source = connection.execute("SELECT text, status FROM pages WHERE sha256=? AND page=?",
                                    (sha256, page)).fetchone()
        if source is None or source["status"] not in FINAL_PAGE_STATES or value not in source["text"]:
            raise ValueError("Evidence must be an exact span from a completed physical page")
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        identifier = hashlib.sha256((sha256 + f":{page}:" + kind + ":" +
                                     payload_json + value).encode()).hexdigest()[:32]
        connection.execute("""
            INSERT OR IGNORE INTO candidates VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, ?, NULL)
        """, (identifier, sha256, page, kind, payload_json, value,
              extraction_version, _now()))
    return identifier


def add_visual_candidate(data_root: Path, observation_id: str, kind: str,
                         payload: dict[str, Any]) -> str:
    """Link an independently reviewed image observation to a pending claim."""

    if kind not in MODEL_CANDIDATE_KINDS:
        raise ValueError("Unknown candidate kind")
    with _connection(data_root) as connection:
        row = connection.execute("SELECT sha256,page,status FROM visual_observations WHERE id=?",
                                 (observation_id,)).fetchone()
        if row is None or row["status"] != "APPROVED":
            raise ValueError("Visual observation has not been reviewed")
        from .research_publication import _industry_claim, _method_claim

        if kind == "INDUSTRY_RELATION":
            _industry_claim(payload, "")
        else:
            _method_claim(payload, "")
        marker = "VISUAL:" + observation_id
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        identifier = hashlib.sha256((row["sha256"] + f":{row['page']}:" + kind + ":" +
                                     payload_json + marker).encode()).hexdigest()[:32]
        connection.execute("""
            INSERT OR IGNORE INTO candidates VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL, NULL, ?, NULL)
        """, (identifier, row["sha256"], row["page"], kind, payload_json, marker,
              "reviewed-visual-v1", _now()))
    return identifier


def _exact_model_evidence(page_text: str, proposed: str) -> str:
    proposed = _safe_text(proposed).strip()
    if len(proposed) < 8:
        raise ValueError("Evidence span is too short")
    if proposed in page_text:
        return proposed
    match = SequenceMatcher(None, proposed, page_text, autojunk=False).find_longest_match(
        0, len(proposed), 0, len(page_text))
    if match.size < 24:
        raise ValueError("No sufficiently specific verbatim page span")
    return page_text[match.b:match.b + match.size]


def list_candidates(data_root: Path, *, status: str = "PENDING", limit: int = 100) -> dict[str, Any]:
    if status not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("Unknown review status")
    with _connection(data_root) as connection:
        rows = connection.execute("""
            SELECT c.*, d.title FROM candidates c JOIN documents d USING (sha256)
            WHERE c.status=? ORDER BY c.created_at LIMIT ?
        """, (status, min(max(limit, 1), 200))).fetchall()
    return {"items": [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]}


def review_candidate(data_root: Path, identifier: str, status: str, reviewer: str, note: str,
                     *, current_verified: bool = False) -> None:
    if status not in {"APPROVED", "REJECTED"} or not reviewer.strip() or not note.strip():
        raise ValueError("Review requires a decision, reviewer and reason")
    with _connection(data_root) as connection:
        current = connection.execute("""SELECT c.kind,c.payload_json,c.evidence_text,c.status,d.title
                                       FROM candidates c JOIN documents d ON d.sha256=c.sha256 WHERE c.id=?""",
                                     (identifier,)).fetchone()
        if current is None or current["status"] != "PENDING":
            raise ValueError("Candidate is missing or already reviewed")
        if status == "APPROVED" and current["kind"] == "INDUSTRY_RELATION" and not current_verified:
            raise ValueError("当前产业关系必须确认已核对现行官方或企业披露")
        if status == "APPROVED":
            from .research_publication import _industry_claim, _method_claim

            payload = json.loads(current["payload_json"])
            if current["kind"] == "INDUSTRY_RELATION":
                _industry_claim(payload, current["evidence_text"], current["title"])
            else:
                _method_claim(payload, current["evidence_text"], current["title"])
            if current["evidence_text"].startswith("VISUAL:"):
                visual_id = current["evidence_text"].removeprefix("VISUAL:")
                visual = connection.execute("SELECT status FROM visual_observations WHERE id=?",
                                            (visual_id,)).fetchone()
                if visual is None or visual["status"] != "APPROVED":
                    raise ValueError("Linked visual observation is not approved")
        changed = connection.execute("""
            UPDATE candidates SET status=?, reviewer=?, review_note=?, reviewed_at=?
            WHERE id=? AND status='PENDING'
        """, (status, reviewer.strip(), note.strip(), _now(), identifier)).rowcount
        if changed != 1:
            raise ValueError("Candidate is missing or already reviewed")
        connection.execute("""
            INSERT INTO review_events(candidate_id,previous_status,next_status,reviewer,note,occurred_at,current_verified_at)
            VALUES (?, 'PENDING', ?, ?, ?, ?, ?)
        """, (identifier, status, reviewer.strip(), note.strip(), _now(),
              _now() if status == "APPROVED" and current_verified else None))


def approved_candidates(data_root: Path) -> list[dict[str, Any]]:
    with _connection(data_root) as connection:
        rows = connection.execute("""SELECT c.id,c.sha256,c.page,c.kind,c.payload_json,c.evidence_text,d.title,
                                            p.status AS page_status,p.text AS page_text,p.parser_version,
                                            r.current_verified_at
                                     FROM candidates c JOIN documents d ON d.sha256=c.sha256
                                     LEFT JOIN pages p ON p.sha256=c.sha256 AND p.page=c.page
                                     LEFT JOIN review_events r ON r.candidate_id=c.id AND r.next_status='APPROVED'
                                     WHERE c.status='APPROVED' ORDER BY c.id""").fetchall()
        verified_documents: set[str] = set()
        for row in rows:
            if row["page_status"] not in FINAL_PAGE_STATES or row["parser_version"] != PARSER_VERSION:
                raise ValueError("Approved candidate points to an unfinished or outdated physical page")
            if row["sha256"] not in verified_documents:
                _verified_pdf_path(connection, row["sha256"])
                verified_documents.add(row["sha256"])
            evidence = row["evidence_text"]
            if evidence.startswith("VISUAL:"):
                visual_id = evidence.removeprefix("VISUAL:")
                visual = connection.execute("""SELECT status,image_sha256,sha256,page
                                               FROM visual_observations WHERE id=?""",
                                            (visual_id,)).fetchone()
                if (visual is None or visual["status"] != "APPROVED" or
                        (visual["sha256"], visual["page"]) != (row["sha256"], row["page"])):
                    raise ValueError("Approved candidate lost its reviewed visual observation")
                if hashlib.sha256(private_page_png(data_root, row["sha256"], row["page"], dpi=120)).hexdigest() != visual["image_sha256"]:
                    raise ValueError("Reviewed visual page changed since approval")
            elif evidence not in row["page_text"]:
                raise ValueError("Approved candidate evidence no longer occurs on its physical page")
            if row["kind"] == "INDUSTRY_RELATION":
                checked = row["current_verified_at"]
                if not checked:
                    raise ValueError("Current industry relation lacks an independent current check")
                age = datetime.now(timezone.utc) - datetime.fromisoformat(checked)
                if age.total_seconds() < 0 or age.days > 365:
                    raise ValueError("Current industry relation check is stale")
    return [{"id": row["id"], "sha256": row["sha256"], "sourceTitle": row["title"],
             "kind": row["kind"], "payload": json.loads(row["payload_json"]),
             "evidenceText": row["evidence_text"]} for row in rows]


_EXTRACTION_PROMPT = """你是本机研究资料结构化助手。只依据给定页面文字提取候选，不补充常识或推测。
每项 evidenceText 必须是本页连续出现的至少8个字的原句片段；找不到就不要生成。
候选说明 summary 要用自己的中文重写，不复制报告句子。绝不生成报告出处、页码、作者或文件名。
"""


def _extraction_spec(document_kind: str) -> tuple[str, dict[str, Any]]:
    from .research_publication import METHOD_CATEGORIES

    if document_kind == "INDUSTRY":
        fields = {"chain": {"type": "string"}, "sourceType": {"type": "string", "enum": [
            "RAW_MATERIAL", "PRODUCT", "PROCESS", "EQUIPMENT", "APPLICATION", "BRAND", "COMPANY", "SERVICE"]},
            "sourceName": {"type": "string"}, "predicate": {"type": "string", "enum": [
                "INPUT_TO", "PROCESSES_TO", "PRODUCES", "USED_IN", "BRANDS", "SUPPLIES", "EQUIPS"]},
            "targetType": {"type": "string", "enum": [
                "RAW_MATERIAL", "PRODUCT", "PROCESS", "EQUIPMENT", "APPLICATION", "BRAND", "COMPANY", "SERVICE"]},
            "targetName": {"type": "string"}, "summary": {"type": "string"}, "stage": {"type": "string"}}
        kind = "INDUSTRY_RELATION"
        instruction = ("只提取真实产品、材料、工艺、设备、应用、品牌、企业间有明确方向的产业关系。"
                       "行业轮动、因子、资产配置等金融研究不是产业链，遇到它们请返回空 items。"
                       "chain 必须是一个中文产业链名称；sourceType/targetType/predicate 严格使用给定枚举。")
    elif document_kind == "QUANT":
        fields = {"name": {"type": "string"}, "kind": {"type": "string", "enum": ["工具", "方法", "策略"]},
                  "category": {"type": "string", "enum": list(METHOD_CATEGORIES)},
                  "summary": {"type": "string"}, "formula": {"type": "string"},
                  "requiredData": {"type": "array", "items": {"type": "string"}},
                  "assumptions": {"type": "array", "items": {"type": "string"}},
                  "limitations": {"type": "array", "items": {"type": "string"}},
                  "blockReasons": {"type": "array", "items": {"type": "string"}}}
        kind = "QUANT_METHOD"
        instruction = ("只提取可辨认的金工工具、方法或策略，不生成产业链关系。"
                       "kind 只能是 工具/方法/策略，category 只能从枚举选择。"
                       "原文没有完整公式时 formula 设空字符串，缺参数、数据或执行假设写入 blockReasons，不能猜测。")
    else:
        raise ValueError("Unknown report kind")
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"items": {"type": "array", "maxItems": 6,
        "items": {"type": "object", "additionalProperties": False, "properties": {
            "kind": {"type": "string", "enum": [kind]},
            "payload": {"type": "object", "additionalProperties": False,
                        "properties": fields, "required": list(fields)},
            "evidenceText": {"type": "string", "minLength": 8},
        }, "required": ["kind", "payload", "evidenceText"]}}}, "required": ["items"]}
    return _EXTRACTION_PROMPT + instruction, schema


def _model_lock(data_root: Path, url: str, model: str, label: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Research model must be a local HTTP server")
    request = Request(url.rstrip("/") + "/props", method="GET")
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        props = json.load(response)
    if props.get("model_alias") != model:
        raise ValueError("Local model alias differs from the requested model")
    model_path = Path(str(props.get("model_path") or "")).resolve()
    trusted_root = (Path(data_root) / "research" / "models").resolve()
    if not model_path.is_relative_to(trusted_root) or not model_path.is_file():
        raise ValueError("Local model file must be under the private research models directory")
    model_sha = _digest(model_path)
    if model == "qwen-local" and model_sha != "ae916ede1c010a26955ee8ae2e908bf8815a3f135ec860439ab924701c69d5f1":
        raise ValueError("Pinned Qwen text model digest mismatch")
    build = str(props.get("build_info") or "")
    prompt_sha = hashlib.sha256("".join(
        prompt + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        for prompt, schema in (_extraction_spec("INDUSTRY"), _extraction_spec("QUANT"))
    ).encode("utf-8")).hexdigest()
    version = hashlib.sha256((model + model_sha + build + prompt_sha + PARSER_VERSION +
                              EVIDENCE_MATCH_VERSION + label).encode()).hexdigest()[:24]
    with _connection(data_root) as connection:
        connection.execute("INSERT OR IGNORE INTO model_locks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (version, label, model, model_sha, build, prompt_sha,
                            PARSER_VERSION + "/" + EVIDENCE_MATCH_VERSION, _now()))
    return version


def _local_model_json(url: str, model: str, text: str, document_kind: str = "QUANT") -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Research model must be a local HTTP server")
    prompt, schema = _extraction_spec(document_kind)
    request = Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps({"model": model, "temperature": 0, "max_tokens": 1200,
                         "response_format": {"type": "json_schema", "json_schema": {
                             "name": "research_candidates", "strict": True, "schema": schema}},
                         "messages": [{"role": "system", "content": prompt},
                                      {"role": "user", "content": text}]}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
        body = json.load(response)
    answer = body["choices"][0]["message"]["content"]
    if not isinstance(answer, str):
        raise ValueError("Local model returned a non-text response")
    answer = re.sub(r"^```(?:json)?\s*|\s*```$", "", answer.strip(), flags=re.IGNORECASE)
    result = json.loads(answer)
    from jsonschema import ValidationError, validate

    try:
        validate(result, schema)
    except ValidationError as error:
        raise ValueError(f"Local model result violates extraction schema: {error.message}") from error
    return result


def propose_from_local_model(data_root: Path, *, url: str, model: str,
                             extraction_version: str, max_pages: int = 20,
                             on_progress: Callable[[dict[str, int]], None] | None = None,
                             should_stop: Callable[[], bool] | None = None,
                             verify_model: bool = True) -> dict[str, int]:
    """Create private pending candidates; model output never self-publishes."""

    if not extraction_version.strip() or not model.strip():
        raise ValueError("A pinned model and extraction version are required")
    if verify_model:
        extraction_version = _model_lock(data_root, url, model, extraction_version)
    result = {"pages": 0, "rawItems": 0, "candidates": 0, "rejectedItems": 0, "errors": 0}
    with _connection(data_root) as connection:
        rows = connection.execute("""
            SELECT p.sha256,p.page,p.text,p.tables_json,d.document_kind FROM pages p
            JOIN documents d ON d.sha256=p.sha256
            LEFT JOIN candidate_runs r ON r.sha256=p.sha256 AND r.page=p.page AND r.extraction_version=?
            LEFT JOIN document_priority priority ON priority.sha256=p.sha256
            WHERE p.status IN ('TEXT_OK','OCR_OK') AND (r.sha256 IS NULL OR r.status!='DONE')
            ORDER BY coalesce(priority.report_heat,0) DESC,p.sha256,p.page LIMIT ?
        """, (extraction_version, min(max(max_pages, 1), 1000))).fetchall()
        for row in rows:
            if should_stop and should_stop():
                result["cancelled"] = 1
                break
            page_text = row["text"]
            # A physical page can contain a very long appendix. Chunk without
            # changing the physical page number or accepting evidence outside it.
            chunks = [page_text[start:start + 8000] for start in range(0, len(page_text), 7600)] or [""]
            count = 0
            error: str | None = None
            try:
                pass_kinds = (("INDUSTRY", "QUANT") if row["document_kind"] == "MIXED"
                              else (row["document_kind"],))
                for chunk in chunks:
                    for pass_kind in pass_kinds:
                        response = _local_model_json(url, model, chunk, pass_kind)
                        for item in response["items"]:
                            result["rawItems"] += 1
                            try:
                                if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                                    raise ValueError("Candidate has no structured payload")
                                expected_kind = ("INDUSTRY_RELATION" if pass_kind == "INDUSTRY"
                                                 else "QUANT_METHOD")
                                if item.get("kind") != expected_kind:
                                    raise ValueError("Candidate kind disagrees with the report classification")
                                exact_span = _exact_model_evidence(page_text, str(item.get("evidenceText") or ""))
                                add_candidate(data_root, row["sha256"], row["page"], str(item.get("kind")),
                                              item["payload"], exact_span,
                                              extraction_version=extraction_version)
                            except ValueError as failure:
                                result["rejectedItems"] += 1
                                connection.execute("""
                                    INSERT INTO proposal_rejections(sha256,page,extraction_version,item_json,reason,recorded_at)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                """, (row["sha256"], row["page"], extraction_version,
                                      json.dumps(item, ensure_ascii=False)[:4000], str(failure)[:500], _now()))
                                connection.commit()
                                continue
                            count += 1
            except (OSError, ValueError, KeyError, TypeError) as failure:
                error = f"{type(failure).__name__}: {failure}"[:500]
            connection.execute("""
                INSERT INTO candidate_runs VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sha256,page,extraction_version) DO UPDATE SET
                status=excluded.status,candidate_count=excluded.candidate_count,
                error=excluded.error,completed_at=excluded.completed_at
            """, (row["sha256"], row["page"], extraction_version,
                  "ERROR" if error else "DONE", count, error, _now()))
            connection.commit()
            result["pages"] += 1
            result["candidates"] += count
            result["errors"] += int(error is not None)
            if on_progress:
                on_progress(dict(result))
    return result
