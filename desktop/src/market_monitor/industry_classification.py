"""Versioned, read-only GB/T 4754-2017 classification navigation."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def classification() -> dict[str, Any]:
    source = files("market_monitor").joinpath("config/nbs_gbt4754_2017.json")
    value = json.loads(source.read_text(encoding="utf-8"))
    if value.get("schema") != "marketlistener/gbt4754-classification/v1":
        raise ValueError("Unsupported NBS classification version")
    counts = {int(key): int(number) for key, number in value.get("counts", {}).items()}
    if counts != {1: 20, 2: 97, 3: 473, 4: 1382} or len(value.get("items", [])) != 1972:
        raise ValueError("Incomplete NBS classification catalogue")
    return value


def public_classification() -> dict[str, Any]:
    value = classification()
    return {"standard": value["standard"], "revision": value["revision"],
            "counts": value["counts"], "items": [
                {"code": item["code"], "name": item["name"], "parentCode": item["parentCode"],
                 "level": item["level"], "sector": item["sector"]}
                for item in value["items"]
            ]}


def valid_classification_code(code: str) -> bool:
    return any(item["code"] == code for item in classification()["items"])


@lru_cache(maxsize=1)
def csrc_classification() -> dict[str, Any]:
    source = files("market_monitor").joinpath("config/csrc_industry_2024.json")
    value = json.loads(source.read_text(encoding="utf-8"))
    groups = value.get("manufacturingSubcategories", [])
    if value.get("schema") != "marketlistener/csrc-industry-2024/v1" or len(groups) != 10:
        raise ValueError("Incomplete CSRC listed-company classification mapping")
    if sum(len(group.get("nbsDivisions", [])) for group in groups) != 31:
        raise ValueError("Incomplete CSRC manufacturing division mapping")
    return value


def public_csrc_classification() -> dict[str, Any]:
    value = csrc_classification()
    return {"standard": value["standard"], "nbsStandard": value["nbsStandard"],
            "manufacturingSubcategories": value["manufacturingSubcategories"],
            "companyAssignmentPolicy": value["companyAssignmentPolicy"]}
