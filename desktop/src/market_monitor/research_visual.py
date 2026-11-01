"""Local-only visual extraction of report tables, charts and process diagrams."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from .research_corpus import _connection, _digest, _now, list_visual_queue, private_page_png

_VISUAL_PROMPT = """只读取这张研报原页图像，不引入外部知识。
识别页内与产业流程、表格或数据图相关的内容。仅记录实际可辨认的标题、坐标轴、单位、期间、表头和带数值标签的数据；
曲线中未标数值的点不要估算。流程箭头必须方向明确。看不清时在 uncertainties 中说明，不补造数字。
返回 JSON {"items":[]}；每项必须有 kind、label、summary、rows、unit、period、uncertainties。
kind 仅能是 TABLE、CHART、FLOW。最多四项，每项 summary 不超过100字，rows 最多六行、每行最多六列。
长表仅记录可读的表头和前几行用于版面核验，不要抄完整表格；全量单元格另由 PDF 解析器保存。
图表也只列出实际可读的数据标签。严格压缩输出，确保完整 JSON。
"""
_VISUAL_SCHEMA: dict[str, Any] = {
    "type": "object", "properties": {"items": {"type": "array", "maxItems": 4,
        "items": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["TABLE", "CHART", "FLOW"]},
            "label": {"type": "string", "maxLength": 80},
            "summary": {"type": "string", "maxLength": 160},
            "rows": {"type": "array", "maxItems": 6,
                     "items": {"type": "array", "maxItems": 6,
                               "items": {"type": "string", "maxLength": 80}}},
            "unit": {"type": "string", "maxLength": 50},
            "period": {"type": "string", "maxLength": 50},
            "uncertainties": {"type": "array", "maxItems": 5,
                              "items": {"type": "string", "maxLength": 100}},
        }, "required": ["kind", "label", "summary", "rows", "unit", "period", "uncertainties"]}}},
    "required": ["items"],
}
_VISUAL_SHA = "66358cb18bb6b3b1b6675aa412c7a88ef01d228f481184d13668e5201c730a0a"
_PROJECTOR_SHA = "30ba2c7dd3127a4561b6cba9d13d0f711c91bdb38742e2f56d73c8cb596bd06d"


@lru_cache(maxsize=8)
def _model_digest(path: Path, size: int, modified_ns: int) -> str:
    return _digest(path)


def _endpoint(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Visual model must be served on this computer")
    return url.rstrip("/")


def _visual_lock(data_root: Path, url: str, model: str, label: str) -> str:
    endpoint = _endpoint(url)
    opener = build_opener(ProxyHandler({}))
    with opener.open(Request(endpoint + "/props"), timeout=5) as response:
        props = json.load(response)
    if props.get("model_alias") != model:
        raise ValueError("Visual model alias differs from requested model")
    path = Path(str(props.get("model_path") or "")).resolve()
    trusted_root = (Path(data_root) / "research" / "models").resolve()
    if not path.is_relative_to(trusted_root) or not path.is_file():
        raise ValueError("Visual model must be under the private research models directory")
    model_sha = _model_digest(path, path.stat().st_size, path.stat().st_mtime_ns)
    projector_sha = ""
    if model == "qwen-visual":
        projector = trusted_root / "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf"
        if model_sha != _VISUAL_SHA or not projector.is_file():
            raise ValueError("Pinned Qwen visual model or projector is missing")
        projector_sha = _model_digest(projector, projector.stat().st_size, projector.stat().st_mtime_ns)
        if projector_sha != _PROJECTOR_SHA:
            raise ValueError("Pinned Qwen visual projector digest mismatch")
    prompt_sha = hashlib.sha256((_VISUAL_PROMPT + json.dumps(_VISUAL_SCHEMA, sort_keys=True)).encode()).hexdigest()
    combined_sha = hashlib.sha256((model_sha + projector_sha).encode()).hexdigest()
    version = hashlib.sha256((model + combined_sha + str(props.get("build_info")) + prompt_sha + label).encode()).hexdigest()[:24]
    with _connection(data_root) as connection:
        connection.execute("INSERT OR IGNORE INTO model_locks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (version, label, model, combined_sha, str(props.get("build_info")),
                            prompt_sha, "visual-page-v1", _now()))
    return version


def _recognize(url: str, model: str, png: bytes) -> dict[str, Any]:
    endpoint = _endpoint(url)
    encoded = base64.b64encode(png).decode("ascii")
    body = {"model": model, "temperature": 0, "max_tokens": 2200,
            "response_format": {"type": "json_schema", "schema": _VISUAL_SCHEMA},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": _VISUAL_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
            ]}]}
    request = Request(endpoint + "/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=240) as response:
        value = json.load(response)
    content = value["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        raise ValueError("Visual model did not return text JSON")
    result = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE))
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise ValueError("Visual model returned no items array")
    return result


def propose_visual_page(data_root: Path, sha256: str, page: int, *, url: str,
                        model: str, label: str) -> dict[str, Any]:
    """Extract untrusted visual suggestions for one exact physical PDF page."""

    if not re.fullmatch(r"[0-9a-f]{64}", sha256) or page < 1:
        raise ValueError("Invalid report page")
    lock = _visual_lock(data_root, url, model, label)
    with _connection(data_root) as connection:
        known = connection.execute("SELECT status,observation_count FROM visual_runs "
                                   "WHERE sha256=? AND page=? AND model_lock=?", (sha256, page, lock)).fetchone()
        if known and known["status"] == "DONE":
            connection.execute("UPDATE visual_queue SET status=?,updated_at=? WHERE sha256=? AND page=?",
                               ("PROPOSED" if known["observation_count"] else "MODEL_EMPTY",
                                _now(), sha256, page))
            return {"status": "ALREADY_DONE", "observations": known["observation_count"], "modelLock": lock}
        source = connection.execute("SELECT status FROM pages WHERE sha256=? AND page=?",
                                    (sha256, page)).fetchone()
        if source is None or source["status"] == "ERROR":
            raise ValueError("Physical page must be extracted before visual review")
    png = private_page_png(data_root, sha256, page, dpi=120)
    image_sha = hashlib.sha256(png).hexdigest()
    try:
        response = _recognize(url, model, png)
    except (OSError, ValueError, KeyError, TypeError) as failure:
        with _connection(data_root) as connection:
            connection.execute("""
                INSERT INTO visual_runs VALUES (?, ?, ?, 'ERROR', 0, ?, ?)
                ON CONFLICT(sha256,page,model_lock) DO UPDATE SET
                  status='ERROR',observation_count=0,error=excluded.error,completed_at=excluded.completed_at
            """, (sha256, page, lock, f"{type(failure).__name__}: {failure}"[:500], _now()))
            connection.execute("UPDATE visual_queue SET status='ERROR',updated_at=? WHERE sha256=? AND page=?",
                               (_now(), sha256, page))
        raise
    saved = 0
    with _connection(data_root) as connection:
        for item in response["items"]:
            if not isinstance(item, dict) or item.get("kind") not in {"TABLE", "CHART", "FLOW"}:
                continue
            if not isinstance(item.get("label"), str) or not item["label"].strip():
                continue
            payload = json.dumps(item, ensure_ascii=True, sort_keys=True)
            identifier = hashlib.sha256((sha256 + f":{page}:" + lock + payload).encode()).hexdigest()[:32]
            connection.execute("""
                INSERT OR IGNORE INTO visual_observations VALUES
                (?, ?, ?, ?, ?, ?, ?, 'PENDING', NULL, NULL, ?, NULL)
            """, (identifier, sha256, page, item["kind"], payload, image_sha, lock, _now()))
            saved += 1
        connection.execute("""
            INSERT INTO visual_runs VALUES (?, ?, ?, 'DONE', ?, NULL, ?)
            ON CONFLICT(sha256,page,model_lock) DO UPDATE SET
              status=excluded.status,observation_count=excluded.observation_count,
              error=NULL,completed_at=excluded.completed_at
        """, (sha256, page, lock, saved, _now()))
        connection.execute("UPDATE visual_queue SET status=?,updated_at=? WHERE sha256=? AND page=?",
                           ("PROPOSED" if saved else "MODEL_EMPTY", _now(), sha256, page))
    return {"status": "PENDING_REVIEW", "observations": saved, "modelLock": lock}


def propose_visual_batch(data_root: Path, *, url: str, model: str, label: str,
                         max_pages: int = 10, on_progress: Any = None,
                         should_stop: Any = None) -> dict[str, int]:
    """Visit the queued key visual pages without auto-approving their content."""

    result = {"pages": 0, "observations": 0, "empty": 0, "errors": 0}
    for row in list_visual_queue(data_root, status="PENDING", limit=min(max_pages, 200))["items"]:
        if should_stop and should_stop():
            result["cancelled"] = 1
            break
        try:
            value = propose_visual_page(data_root, row["sha256"], row["page"],
                                        url=url, model=model, label=label)
        except (OSError, ValueError, RuntimeError):
            result["errors"] += 1
        else:
            result["observations"] += int(value["observations"])
            result["empty"] += int(value["observations"] == 0)
        result["pages"] += 1
        if on_progress:
            on_progress(dict(result))
    return result


def list_visual_observations(data_root: Path, *, status: str = "PENDING", limit: int = 100) -> dict[str, Any]:
    if status not in {"PENDING", "APPROVED", "REJECTED"}:
        raise ValueError("Unknown visual review status")
    with _connection(data_root) as connection:
        rows = connection.execute("""
            SELECT v.*, d.title FROM visual_observations v JOIN documents d USING (sha256)
            WHERE v.status=? ORDER BY v.created_at LIMIT ?
        """, (status, min(max(limit, 1), 200))).fetchall()
    return {"items": [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]}


def review_visual_observation(data_root: Path, identifier: str, status: str, reviewer: str, note: str) -> None:
    if status not in {"APPROVED", "REJECTED"} or not reviewer.strip() or not note.strip():
        raise ValueError("Visual review requires decision, reviewer and reason")
    with _connection(data_root) as connection:
        changed = connection.execute("""
            UPDATE visual_observations SET status=?,reviewer=?,review_note=?,reviewed_at=?
            WHERE id=? AND status='PENDING'
        """, (status, reviewer.strip(), note.strip(), _now(), identifier)).rowcount
        if changed != 1:
            raise ValueError("Visual observation is missing or already reviewed")
        connection.execute("""
            INSERT INTO visual_review_events(observation_id,previous_status,next_status,reviewer,note,occurred_at)
            VALUES (?, 'PENDING', ?, ?, ?, ?)
        """, (identifier, status, reviewer.strip(), note.strip(), _now()))
