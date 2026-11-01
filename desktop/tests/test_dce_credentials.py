import os

import pytest

from market_monitor import dce_credentials as module


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_credentials_round_trip_is_encrypted_and_outside_dataset(tmp_path, monkeypatch):
    target = tmp_path / "dce-accounts.dpapi"
    monkeypatch.setattr(module, "credential_path", lambda: target)
    values = [{"api_key": "synthetic-test-key-A", "secret": "synthetic-test-secret-A"},
              {"api_key": "synthetic-test-key-B", "secret": "synthetic-test-secret-B"}]
    module.save_accounts(values)
    assert b"synthetic-test-key" not in target.read_bytes()
    assert b"synthetic-test-secret" not in target.read_bytes()
    assert module.load_accounts() == values
