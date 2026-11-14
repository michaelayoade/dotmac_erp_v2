"""The startup gate runs before the kernel (and its import-time Settings) loads."""

import subprocess
import sys

import pytest

from erp_v2 import runtime


def test_importing_runtime_does_not_load_kernel_settings():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import erp_v2.runtime; assert 'dotmac_kernel.config' not in sys.modules"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dotenv_in_cwd_refuses_before_serving(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime.uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as exc:
        runtime.main([])
    assert exc.value.code == 2
    assert "refusing to serve" in capsys.readouterr().err
