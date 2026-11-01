"""Current-user Windows DPAPI storage for the user's DCE API accounts."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


_HEADER = b"MarketListener-DCE-DPAPI-v1\x00"
_ENTROPY = b"MarketListener/DCE/accounts/v1"


def credential_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if os.name != "nt" or not base:
        raise RuntimeError("DCE encrypted credentials require Windows current-user DPAPI")
    return Path(base) / "MarketListener" / "secrets" / "dce-accounts.dpapi"


def _validate(accounts: Any) -> list[dict[str, str]]:
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 2:
        raise ValueError("One or two DCE accounts are required")
    result = []
    for item in accounts:
        if not isinstance(item, dict):
            raise ValueError("Invalid DCE account configuration")
        key, secret = item.get("api_key"), item.get("secret")
        if not isinstance(key, str) or not key.strip() or not isinstance(secret, str) or not secret:
            raise ValueError("DCE account key and secret are required")
        result.append({"api_key": key.strip(), "secret": secret})
    if len({item["api_key"] for item in result}) != len(result):
        raise ValueError("DCE account keys must be distinct")
    return result


def save_accounts(accounts: list[dict[str, str]]) -> Path:
    import win32crypt

    values = _validate(accounts)
    target = credential_path()
    payload = json.dumps({"accounts": values}, ensure_ascii=False).encode("utf-8")
    protected = win32crypt.CryptProtectData(payload, "MarketListener DCE accounts", _ENTROPY, None, None, 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_HEADER + protected)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def load_accounts() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    target = credential_path()
    if not target.is_file():
        return []
    import win32crypt

    raw = target.read_bytes()
    if not raw.startswith(_HEADER) or len(raw) > 32_768:
        raise ValueError("Invalid encrypted DCE credential file")
    try:
        _, plain = win32crypt.CryptUnprotectData(raw[len(_HEADER):], _ENTROPY, None, None, 1)
        return _validate(json.loads(plain.decode("utf-8")).get("accounts"))
    except Exception:
        raise RuntimeError("DCE credentials cannot be decrypted by this Windows user") from None


def main() -> None:
    accounts = []
    for label in ("A", "B"):
        accounts.append({"api_key": getpass.getpass(f"DCE account {label} key: "),
                         "secret": getpass.getpass(f"DCE account {label} secret: ")})
    target = save_accounts(accounts)
    print(f"Stored {len(accounts)} DCE accounts with current-user DPAPI at {target}")


if __name__ == "__main__":
    main()
