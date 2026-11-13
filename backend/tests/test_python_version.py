import pytest

from scripts import check_python_version


@pytest.mark.parametrize("version", [(3, 11, 0), (3, 12, 0)])
def test_supported_python_versions_are_accepted(monkeypatch, version):
    monkeypatch.setattr(check_python_version.sys, "version_info", version)

    assert check_python_version.require_supported_python() is True


@pytest.mark.parametrize("version", [(3, 10, 0), (3, 13, 0)])
def test_python_versions_outside_supported_range_are_rejected(monkeypatch, version):
    monkeypatch.setattr(check_python_version.sys, "version_info", version)

    with pytest.raises(SystemExit, match="UNSUPPORTED_PYTHON_VERSION"):
        check_python_version.require_supported_python()
