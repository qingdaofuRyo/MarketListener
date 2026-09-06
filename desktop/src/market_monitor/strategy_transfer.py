"""Safe desktop-only import/export for immutable Strategy Definition packages."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from market_monitor.contracts import ContractValidationError, validate_contract
from market_monitor.strategy_definition import StrategyDefinitionError, validate_strategy_definition
from market_monitor.strategy_function_registry import (
    StrategyFunctionRegistryError,
    build_builtin_strategy_function_registry,
)

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


MANIFEST_SCHEMA = "strategy-transfer-manifest.schema.json"
MAX_PACKAGE_BYTES = 5 * 1024 * 1024
_CONTENT_NAMES = (
    "definitions/strategy.json",
    "dependency-lock.json",
    "test-vectors.json",
    "schemas/strategy-definition.schema.json",
    "schemas/strategy-transfer-manifest.schema.json",
)
_MANIFEST_NAME = "manifest.json"
_SIGNATURE_NAME = "signature.ed25519"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class StrategyTransferError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _schema_bytes(name: str) -> bytes:
    path = _REPOSITORY_ROOT / "contracts" / name
    try:
        return path.read_bytes()
    except OSError as error:
        raise StrategyTransferError("MISSING_LOCAL_SCHEMA", f"local schema is unavailable: {name}") from error


def _load_private_key(value: bytes | Ed25519PrivateKey) -> Ed25519PrivateKey:
    if isinstance(value, Ed25519PrivateKey):
        return value
    try:
        key = serialization.load_pem_private_key(value, password=None)
    except (TypeError, ValueError) as error:
        raise StrategyTransferError("INVALID_SIGNING_KEY", "signing key is not a valid Ed25519 PEM") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise StrategyTransferError("INVALID_SIGNING_KEY", "signing key is not Ed25519")
    return key


def _signature_metadata(private_key: Ed25519PrivateKey) -> dict[str, str]:
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "algorithm": "ed25519",
        "file": _SIGNATURE_NAME,
        "key_id": hashlib.sha256(public_bytes).hexdigest(),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def dependency_lock(definition: Mapping[str, Any]) -> dict[str, Any]:
    """Derive only exact immutable resources; chart Indicators are prohibited."""

    try:
        evidence = validate_strategy_definition(definition)
    except StrategyDefinitionError as error:
        raise StrategyTransferError(error.code, error.message) from error
    registry = build_builtin_strategy_function_registry()
    functions: list[dict[str, Any]] = []
    for reference in evidence["function_references"]:
        try:
            function = registry.resolve(str(reference["id"]), int(reference["version"]))
        except StrategyFunctionRegistryError as error:
            raise StrategyTransferError("MISSING_FUNCTION_VERSION", error.message) from error
        functions.append({"id": function.function_id, "version": function.version, "definition_hash": function.definition_hash()})
    return {
        "strategy": {"id": definition["id"], "version": definition["version"], "definition_hash": _hash(definition)},
        "strategy_functions": functions,
        "indicators": [],
    }


def build_package(
    definition: Mapping[str, Any],
    *,
    created_at: str | None = None,
    signing_key: bytes | Ed25519PrivateKey | None = None,
) -> bytes:
    """Create a desktop-only package, optionally signing its manifest.

    A signature proves only integrity and key possession.  It intentionally
    does not make this Rule-AST package Android compatible: Android accepts
    only its separate declarative DSL package and trusted signing keys.
    """

    document = dict(definition)
    lock = dependency_lock(document)
    vectors = {
        "schema_version": 1,
        "vectors": [{"id": "definition-hash", "definition_hash": lock["strategy"]["definition_hash"], "dependency_lock_hash": _hash(lock)}],
    }
    contents = {
        "definitions/strategy.json": _canonical(document),
        "dependency-lock.json": _canonical(lock),
        "test-vectors.json": _canonical(vectors),
        "schemas/strategy-definition.schema.json": _schema_bytes("strategy-definition.schema.json"),
        "schemas/strategy-transfer-manifest.schema.json": _schema_bytes(MANIFEST_SCHEMA),
    }
    private_key = _load_private_key(signing_key) if signing_key is not None else None
    manifest = {
        "schema_version": 1,
        "package_id": f"strategy-transfer-{uuid4().hex[:16]}",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": "desktop",
        "strategy": lock["strategy"],
        "dependency_lock": lock,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()},
        "test_vectors_sha256": hashlib.sha256(contents["test-vectors.json"]).hexdigest(),
        "signature": _signature_metadata(private_key) if private_key is not None else None,
    }
    try:
        validate_contract(MANIFEST_SCHEMA, manifest)
    except ContractValidationError as error:
        raise StrategyTransferError("INVALID_MANIFEST", str(error)) from error
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in _CONTENT_NAMES:
            archive.writestr(name, contents[name])
        manifest_bytes = _canonical(manifest)
        archive.writestr(_MANIFEST_NAME, manifest_bytes)
        if private_key is not None:
            archive.writestr(_SIGNATURE_NAME, private_key.sign(manifest_bytes))
    return stream.getvalue()


def _verify_signature(manifest: Mapping[str, Any], files: Mapping[str, bytes]) -> tuple[str, bool]:
    signature = manifest.get("signature")
    if signature is None:
        return "UNSIGNED", False
    if not isinstance(signature, dict):
        raise StrategyTransferError("INVALID_SIGNATURE", "signature metadata is invalid")
    try:
        public_bytes = base64.b64decode(str(signature["public_key"]), validate=True)
        if hashlib.sha256(public_bytes).hexdigest() != signature["key_id"]:
            raise ValueError("key id mismatch")
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        public_key.verify(files[_SIGNATURE_NAME], files[_MANIFEST_NAME])
    except (KeyError, ValueError, TypeError, InvalidSignature) as error:
        raise StrategyTransferError("INVALID_SIGNATURE", "package signature does not verify") from error
    return "VALID", True


def inspect_package(payload: bytes) -> dict[str, Any]:
    """Read and validate a package without persisting anything."""

    if not payload or len(payload) > MAX_PACKAGE_BYTES:
        raise StrategyTransferError("PACKAGE_SIZE", "strategy package exceeds the 5 MiB limit")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            expected_names = set(_CONTENT_NAMES) | {_MANIFEST_NAME}
            allowed_names = expected_names | {_SIGNATURE_NAME}
            if (
                len(names) != len(set(names))
                or not set(names).issuperset(expected_names)
                or not set(names).issubset(allowed_names)
            ):
                raise StrategyTransferError("INVALID_PACKAGE_LAYOUT", "package contains unknown, missing, or duplicate files")
            infos = archive.infolist()
            if (
                sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES
                or any(
                    info.is_dir()
                    or info.file_size > MAX_PACKAGE_BYTES
                    or info.filename.startswith(("/", "\\"))
                    or "\\" in info.filename
                    or ".." in info.filename.split("/")
                    for info in infos
                )
            ):
                raise StrategyTransferError("INVALID_PACKAGE_LAYOUT", "package entry is unsafe or too large")
            manifest_candidate = archive.read(_MANIFEST_NAME)
            manifest_candidate_document = json.loads(manifest_candidate)
            signature = manifest_candidate_document.get("signature") if isinstance(manifest_candidate_document, dict) else None
            expected_names |= {_SIGNATURE_NAME} if signature is not None else set()
            if set(names) != expected_names:
                raise StrategyTransferError("INVALID_PACKAGE_LAYOUT", "package contains unknown, missing, or duplicate files")
            files = {name: archive.read(name) for name in expected_names}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyTransferError("INVALID_PACKAGE_JSON", "package manifest is invalid JSON") from error
    except zipfile.BadZipFile as error:
        raise StrategyTransferError("INVALID_PACKAGE", "package is not a ZIP archive") from error
    try:
        manifest = json.loads(files[_MANIFEST_NAME])
        document = json.loads(files["definitions/strategy.json"])
        lock = json.loads(files["dependency-lock.json"])
        vectors = json.loads(files["test-vectors.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StrategyTransferError("INVALID_PACKAGE_JSON", "package JSON is invalid") from error
    try:
        validate_contract(MANIFEST_SCHEMA, manifest)
    except ContractValidationError as error:
        raise StrategyTransferError("INVALID_MANIFEST", str(error)) from error
    for name, expected in manifest["files"].items():
        if name not in files or hashlib.sha256(files[name]).hexdigest() != expected:
            raise StrategyTransferError("PACKAGE_TAMPERED", f"hash mismatch: {name}")
    if manifest["test_vectors_sha256"] != hashlib.sha256(files["test-vectors.json"]).hexdigest():
        raise StrategyTransferError("PACKAGE_TAMPERED", "test vector hash mismatch")
    for name in ("strategy-definition.schema.json", MANIFEST_SCHEMA):
        packaged = files[f"schemas/{name}"]
        if packaged != _schema_bytes(name):
            raise StrategyTransferError("SCHEMA_MISMATCH", f"package schema differs from local allow-list: {name}")
    expected_lock = dependency_lock(document)
    if lock != expected_lock or manifest["dependency_lock"] != expected_lock or manifest["strategy"] != expected_lock["strategy"]:
        raise StrategyTransferError("DEPENDENCY_LOCK_MISMATCH", "package does not resolve the declared exact dependencies")
    expected_vectors = [{
        "id": "definition-hash",
        "definition_hash": expected_lock["strategy"]["definition_hash"],
        "dependency_lock_hash": _hash(expected_lock),
    }]
    if vectors.get("schema_version") != 1 or vectors.get("vectors") != expected_vectors:
        raise StrategyTransferError("INVALID_TEST_VECTORS", "package test vectors do not match the definition")
    signature_status, signature_verified = _verify_signature(manifest, files)
    return {
        "manifest": manifest,
        "definition": document,
        "dependency_lock": expected_lock,
        "signatureStatus": signature_status,
        "signatureVerified": signature_verified,
        "androidCompatible": False,
        "androidReason": "DECLARATIVE_ANDROID_DSL_REQUIRED",
    }


__all__ = ["MAX_PACKAGE_BYTES", "StrategyTransferError", "build_package", "dependency_lock", "inspect_package"]
