from __future__ import annotations

import io
import sys

import pytest

from tianlai._console_encoding import configure_utf8_standard_streams
from tianlai.cli import main as cli_main


def test_reconfigures_real_text_streams_without_replacing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    configure_utf8_standard_streams()

    assert sys.stdout is stdout
    assert sys.stderr is stderr
    assert stdout.encoding.lower().replace("-", "") == "utf8"
    assert stderr.encoding.lower().replace("-", "") == "utf8"
    assert stdout.errors == "strict"
    assert stderr.errors == "strict"
    print("候选", file=stdout, flush=True)
    print("乐器", file=stderr, flush=True)
    assert stdout_bytes.getvalue().decode("utf-8").splitlines() == ["候选"]
    assert stderr_bytes.getvalue().decode("utf-8").splitlines() == ["乐器"]


def test_cli_configures_utf8_before_argparse_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    with pytest.raises(SystemExit) as caught:
        cli_main(["project-render-v2", "--help"])

    stdout.flush()
    assert caught.value.code == 0
    decoded = stdout_bytes.getvalue().decode("utf-8", errors="strict")
    assert "--execution-profile" in decoded
    assert "TIANLAI_OUTPUT_DIR/候选" in decoded
    assert "TIANLAI_HOME/乐器" in decoded
