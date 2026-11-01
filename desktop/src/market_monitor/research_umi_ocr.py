"""Optional local Umi-OCR 2.1.5 Paddle HTTP adapter for PDF page images."""

from __future__ import annotations

import base64
import json
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_URL = "http://127.0.0.1:1224"
_unavailable_until = 0.0


@dataclass(frozen=True)
class UmiOCRResult:
    text: str
    blocks: list[dict[str, Any]]
    mean_confidence: float | None
    elapsed_seconds: float | None


def _local_endpoint(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Umi-OCR endpoint must be on this computer")
    if parsed.path not in {"", "/"} or parsed.username or parsed.password:
        raise ValueError("Umi-OCR endpoint must be a bare local HTTP origin")
    return url.rstrip("/")


def umi_ocr_png(png: bytes, *, url: str = DEFAULT_URL, dpi: int = 200) -> UmiOCRResult:
    """Use installed Umi-OCR's image endpoint; keep all text and boxes local."""

    endpoint = _local_endpoint(url)
    if not 72 <= dpi <= 300:
        raise ValueError("OCR image DPI is outside the supported range")
    body = json.dumps({"base64": base64.b64encode(png).decode("ascii"),
                       "options": {"ocr.language": "models/config_chinese.txt",
                                   "ocr.limit_side_len": 2880,
                                   "tbpu.parser": "multi_line", "data.format": "dict"}}).encode("utf-8")
    request = Request(endpoint + "/api/ocr", data=body,
                      headers={"Content-Type": "application/json"}, method="POST")
    with build_opener(ProxyHandler({})).open(request, timeout=45) as response:
        result = json.load(response)
    if result.get("code") == 101:
        return UmiOCRResult("", [], None, float(result.get("time") or 0))
    if result.get("code") != 100 or not isinstance(result.get("data"), list):
        raise ValueError(f"Umi-OCR returned failure code {result.get('code')}")
    scale = dpi / 72
    blocks: list[dict[str, Any]] = []
    confidences: list[float] = []
    fragments: list[str] = []
    for item in result["data"]:
        if not isinstance(item, dict):
            continue
        value = str(item.get("text") or "").encode("utf-8", "replace").decode("utf-8").strip()
        if not value:
            continue
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4:
            continue
        try:
            points = [(float(point[0]) / scale, float(point[1]) / scale) for point in box]
            confidence = float(item.get("score"))
        except (TypeError, ValueError, IndexError):
            continue
        if not all(math.isfinite(number) for point in points for number in point):
            continue
        bbox = [round(min(point[0] for point in points), 2), round(min(point[1] for point in points), 2),
                round(max(point[0] for point in points), 2), round(max(point[1] for point in points), 2)]
        blocks.append({"text": value, "bbox": bbox, "confidence": confidence, "engine": "umi-ocr-paddle-2.1.5"})
        confidences.append(confidence)
        fragments.append(value + str(item.get("end") or ""))
    return UmiOCRResult("".join(fragments).strip(), blocks,
                        sum(confidences) / len(confidences) if confidences else None,
                        float(result.get("time")) if isinstance(result.get("time"), (int, float)) else None)


def try_umi_ocr_png(png: bytes, *, url: str = DEFAULT_URL, dpi: int = 200) -> UmiOCRResult | None:
    """Return None during local service outage, allowing RapidOCR fallback."""

    global _unavailable_until
    if time.monotonic() < _unavailable_until:
        return None
    try:
        return umi_ocr_png(png, url=url, dpi=dpi)
    except (OSError, HTTPError, URLError, ValueError, TimeoutError):
        _unavailable_until = time.monotonic() + 60
        return None
