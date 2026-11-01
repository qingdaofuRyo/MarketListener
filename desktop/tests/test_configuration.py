from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from market_monitor.configuration import ConfigurationError, load_local_configuration


def test_configuration_uses_environment_without_implicitly_loading_repo_files(tmp_path: Path) -> None:
    configuration = load_local_configuration(
        environment={"JQDATA_USERNAME": "local-user", "JQDATA_PASSWORD": "local-password"},
        repo_root=tmp_path,
    )

    assert configuration.get("JQDATA_USERNAME") == "local-user"
    assert configuration.secret_values == ("local-user", "local-password")


def test_sensitive_configuration_names_align_with_redaction_registry(tmp_path: Path) -> None:
    configuration = load_local_configuration(
        environment={
            "PASSWD": "s1",
            "PWD": "s2",
            "SECRET_KEY": "s3",
            "ACCESS_KEY": "s4",
            "AWS_SECRET_ACCESS_KEY": "s5",
            "AWS_ACCESS_KEY_ID": "s6",
            "BEARER": "s7",
            "COOKIE": "s8",
            "REPORT_DIR": "not-a-secret",
            "LOG_LEVEL": "debug",
        },
        repo_root=tmp_path,
    )

    assert set(configuration.secret_values) == {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8"}


def test_explicit_configuration_must_be_outside_repository_and_does_not_echo_values(tmp_path: Path) -> None:
    inside = tmp_path / ".env"
    inside.write_text("JQDATA_PASSWORD=unsafe\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="outside the repository"):
        load_local_configuration(config_file=inside, repo_root=tmp_path)


def test_explicit_external_configuration_overrides_environment(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "market-monitor.env"
    external.write_text("JQDATA_USERNAME=external-user\nJQDATA_PASSWORD='external-password'\n", encoding="utf-8")  # Synthetic test credential or marker.

    configuration = load_local_configuration(
        config_file=external,
        environment={"JQDATA_USERNAME": "environment-user"},
        repo_root=repo,
    )

    assert configuration.get("JQDATA_USERNAME") == "external-user"
    assert configuration.get("JQDATA_PASSWORD") == "external-password"


def test_configuration_accepts_utf8_bom_and_rejects_case_normalized_duplicates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "market-monitor.env"
    external.write_bytes("\ufeffJQDATA_PASSWORD='quoted-value'\n".encode("utf-8"))  # Synthetic test credential or marker.

    assert load_local_configuration(config_file=external, repo_root=repo).get("JQDATA_PASSWORD") == "quoted-value"
    external.write_text("JQDATA_PASSWORD=first\njqdata_password=second\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate variable") as error:
        load_local_configuration(config_file=external, repo_root=repo)
    assert "first" not in str(error.value)
    assert "second" not in str(error.value)


def test_configuration_rejects_lexical_repo_paths_with_relative_and_parent_variants(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / ".env"
    inside.write_text("JQDATA_PASSWORD=value\n", encoding="utf-8")
    monkeypatch.chdir(repo)

    for path in (Path(".env"), repo / "nested" / ".." / ".env"):
        with pytest.raises(ConfigurationError, match="outside the repository"):
            load_local_configuration(config_file=path, repo_root=repo)


def test_configuration_rejects_link_paths_crossing_repository_boundary_when_supported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "credentials.env").write_text("JQDATA_PASSWORD=outside\n", encoding="utf-8")
    inside = repo / "credentials.env"
    inside.write_text("JQDATA_PASSWORD=inside\n", encoding="utf-8")
    outward_link = repo / "linked-outside"
    inward_link = tmp_path / "linked-inside"
    try:
        _create_directory_link(outward_link, external)
        _create_directory_link(inward_link, repo)
    except OSError:
        pytest.skip("this platform cannot create symlink/junction test fixtures")
    try:
        with pytest.raises(ConfigurationError, match="outside the repository"):
            load_local_configuration(config_file=outward_link / "credentials.env", repo_root=repo)
        with pytest.raises(ConfigurationError, match="outside the repository"):
            load_local_configuration(config_file=inward_link / "credentials.env", repo_root=repo)
    finally:
        _remove_directory_link(outward_link)
        _remove_directory_link(inward_link)


@pytest.mark.skipif(os.name != "nt", reason="Windows path comparison is case-insensitive")
def test_configuration_rejects_case_variant_of_repository_path_on_windows(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "credentials.env"
    inside.write_text("JQDATA_PASSWORD=value\n", encoding="utf-8")

    case_variant = Path(str(repo).upper()) / "CREDENTIALS.ENV"
    with pytest.raises(ConfigurationError, match="outside the repository"):
        load_local_configuration(config_file=case_variant, repo_root=repo)


def _create_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            raise symlink_error
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError("unable to create temporary directory link fixture")


def _remove_directory_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink(missing_ok=True)
    elif link.exists():
        os.rmdir(link)
