"""Security and compatibility tests for desktop-only Strategy transfer packages."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from market_monitor.builtin_strategies import build_builtin_strategy_definitions
from market_monitor.strategy_transfer import MAX_PACKAGE_BYTES, StrategyTransferError, build_package, inspect_package
from market_monitor.web_app import create_web_app


def custom_definition() -> dict[str, object]:
    definition = deepcopy(build_builtin_strategy_definitions()[0])
    definition.update(
        {
            "id": "strategy.transfer_demo",
            "display_name": "导入导出演示",
            "origin": "custom",
            "created_at": "2026-09-05T00:00:00+00:00",
            "updated_at": "2026-09-05T00:00:00+00:00",
        }
    )
    return definition


def rewrite_entry(payload: bytes, name: str, replacement: bytes) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in source.infolist():
                archive.writestr(item.filename, replacement if item.filename == name else source.read(item.filename))
    return target.getvalue()


def unsafe_layout_package() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../outside.json", b"{}")
    return stream.getvalue()


def oversized_zip_layout_package() -> bytes:
    stream = io.BytesIO()
    names = (
        "definitions/strategy.json",
        "dependency-lock.json",
        "test-vectors.json",
        "schemas/strategy-definition.schema.json",
        "schemas/strategy-transfer-manifest.schema.json",
        "manifest.json",
    )
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, b"0" * (MAX_PACKAGE_BYTES // 2))
    return stream.getvalue()


def test_transfer_round_trip_has_exact_dependencies_schemas_and_android_boundary() -> None:
    definition = custom_definition()

    package = inspect_package(build_package(definition, created_at="2026-09-05T00:00:00+00:00"))

    assert package["definition"] == definition
    assert package["dependency_lock"]["strategy"]["id"] == "strategy.transfer_demo"
    assert set(package["manifest"]["files"]) == {
        "definitions/strategy.json",
        "dependency-lock.json",
        "test-vectors.json",
        "schemas/strategy-definition.schema.json",
        "schemas/strategy-transfer-manifest.schema.json",
    }
    assert package["signatureStatus"] == "UNSIGNED"
    assert package["signatureVerified"] is False
    assert package["androidCompatible"] is False
    assert package["androidReason"] == "DECLARATIVE_ANDROID_DSL_REQUIRED"


def test_transfer_signature_validates_and_tampering_fails_closed() -> None:
    signed = build_package(custom_definition(), signing_key=Ed25519PrivateKey.generate())

    inspected = inspect_package(signed)
    assert inspected["signatureStatus"] == "VALID"
    assert inspected["signatureVerified"] is True

    tampered = rewrite_entry(signed, "signature.ed25519", b"not-a-signature")
    with pytest.raises(StrategyTransferError, match="signature") as caught:
        inspect_package(tampered)
    assert caught.value.code == "INVALID_SIGNATURE"


def test_transfer_rejects_oversize_package() -> None:
    with pytest.raises(StrategyTransferError) as caught:
        inspect_package(b"x" * (MAX_PACKAGE_BYTES + 1))
    assert caught.value.code == "PACKAGE_SIZE"


def test_transfer_rejects_unsafe_layout() -> None:
    with pytest.raises(StrategyTransferError) as caught:
        inspect_package(unsafe_layout_package())
    assert caught.value.code == "INVALID_PACKAGE_LAYOUT"


def test_transfer_rejects_compressed_package_with_oversized_total_before_reading_manifest() -> None:
    with pytest.raises(StrategyTransferError) as caught:
        inspect_package(oversized_zip_layout_package())
    assert caught.value.code == "INVALID_PACKAGE_LAYOUT"


def test_transfer_rejects_tampering_unknown_capability_and_missing_dependency() -> None:
    package = build_package(custom_definition())
    tampered = rewrite_entry(package, "definitions/strategy.json", b"{}")
    with pytest.raises(StrategyTransferError) as caught:
        inspect_package(tampered)
    assert caught.value.code == "PACKAGE_TAMPERED"

    unsafe = custom_definition()
    unsafe["script"] = "import os"
    with pytest.raises(StrategyTransferError) as caught:
        build_package(unsafe)
    assert caught.value.code == "INVALID_DEFINITION"

    missing = custom_definition()
    missing["entry_rules"]["left"]["version"] = 999  # type: ignore[index]
    with pytest.raises(StrategyTransferError) as caught:
        build_package(missing)
    assert caught.value.code == "MISSING_FUNCTION_VERSION"


def test_transfer_api_previews_then_imports_with_explicit_conflict_resolution(tmp_path) -> None:
    source = TestClient(create_web_app(tmp_path / "source"), client=("127.0.0.1", 50000))
    definition = custom_definition()
    assert source.post("/api/strategy/definition-resources", json=definition).status_code == 201
    exported = source.get("/api/strategy/definition-resources/strategy.transfer_demo/export?version=1")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/zip")
    body = {"packageBase64": base64.b64encode(exported.content).decode("ascii")}

    target = TestClient(create_web_app(tmp_path / "target"), client=("127.0.0.1", 50000))
    preview = target.post("/api/strategy/packages/preview", json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["definition"]["id"] == "strategy.transfer_demo"
    imported = target.post("/api/strategy/packages/import", json=body)
    assert imported.status_code == 201, imported.text
    assert imported.json()["importedUnchanged"] is True
    persisted = tmp_path / "target" / "strategies" / "resources" / "strategy.transfer_demo@1.json"
    assert json.loads(persisted.read_text(encoding="utf-8")) == definition

    conflict = target.post("/api/strategy/packages/import", json=body)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IMPORT_CONFLICT"
    versioned = target.post("/api/strategy/packages/import", json={**body, "conflict": "new_version"})
    assert versioned.status_code == 201, versioned.text
    assert versioned.json()["definition"]["version"] == 2
    assert versioned.json()["mutation"] == "new_version"

    renamed = target.post(
        "/api/strategy/packages/import",
        json={**body, "conflict": "rename", "newStrategyId": "strategy.transfer_renamed"},
    )
    assert renamed.status_code == 201, renamed.text
    assert renamed.json()["definition"]["id"] == "strategy.transfer_renamed"
    assert renamed.json()["definition"]["version"] == 1


def test_transfer_api_rejects_invalid_base64_and_builtin_export(tmp_path) -> None:
    client = TestClient(create_web_app(tmp_path / "data"), client=("127.0.0.1", 50000))
    invalid = client.post("/api/strategy/packages/preview", json={"packageBase64": "not base64!"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "INVALID_PACKAGE_ENCODING"
    builtin = client.get("/api/strategy/definition-resources/strategy.ma_crossover/export?version=1")
    assert builtin.status_code == 403
    assert builtin.json()["detail"]["code"] == "BUILTIN_NOT_EXPORTABLE"
