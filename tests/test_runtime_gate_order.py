"""The startup gate runs before the kernel (and its import-time Settings) loads."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from erp_v2 import runtime


def test_importing_runtime_does_not_load_kernel_settings(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import erp_v2.runtime; assert 'dotmac_kernel.config' not in sys.modules"],
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path(runtime.__file__).resolve().parents[1])},
        text=True,
        timeout=120,
        check=False,
    )
    # Last line only: a full traceback could echo a local setting's value.
    assert result.returncode == 0, result.stderr.strip().splitlines()[-1:]


def test_dotenv_in_cwd_refuses_before_serving(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(runtime.uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as exc:
        runtime.main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "refusing to serve" in err
    assert ".env file present in the working directory" in err
