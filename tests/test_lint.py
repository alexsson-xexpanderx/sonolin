import os
from pathlib import Path
import subprocess
import sys

import pytest

from tools import lint


def test_regular_source_diagnostics(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("import os\nprint(missing)\n", encoding="utf-8")
    assert lint.check(str(source)) == [
        "undefined name 'missing' in top",
        "line 1: unused import 'os'",
    ]


@pytest.mark.parametrize("size", [31, 32, 33])
def test_source_byte_limit(tmp_path, monkeypatch, size):
    monkeypatch.setattr(lint, "MAX_SOURCE_BYTES", 32)
    source = tmp_path / "source.py"
    # Include multibyte text so the limit must count bytes, not characters.
    source.write_bytes(b"#\xc3\xa9" + b" " * (size - 3))
    if size > 32:
        with pytest.raises(ValueError, match="source exceeds 32 bytes"):
            lint.check(str(source))
    else:
        assert lint.check(str(source)) == []


def test_read_is_bounded_even_without_size_metadata(tmp_path, monkeypatch):
    source = tmp_path / "source.py"
    source.write_bytes(b"#" + b" " * lint.MAX_SOURCE_BYTES)
    fdopen = lint.os.fdopen
    reads = []

    def tracked_fdopen(*args, **kwargs):
        stream = fdopen(*args, **kwargs)
        read = stream.read

        def bounded_read(size=-1):
            reads.append(size)
            assert size == lint.MAX_SOURCE_BYTES + 1
            return read(size)

        stream.read = bounded_read
        return stream

    monkeypatch.setattr(lint.os, "fdopen", tracked_fdopen)
    with pytest.raises(ValueError, match="source exceeds"):
        lint.check(str(source))
    assert reads == [lint.MAX_SOURCE_BYTES + 1]


@pytest.mark.parametrize("kind", ["symlink", "device_link", "fifo", "directory", "device"])
def test_cli_rejects_unsafe_inputs_without_hanging(tmp_path, kind):
    source = tmp_path / "source.py"
    if kind == "symlink":
        target = tmp_path / "target.py"
        target.write_text("print('hello')\n", encoding="utf-8")
        source.symlink_to(target)
    elif kind == "device_link":
        source.symlink_to("/dev/zero")
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "directory":
        source.mkdir()
    else:
        source = Path("/dev/zero")

    result = subprocess.run(
        [sys.executable, lint.__file__, str(source)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert "Too many levels of symbolic links" in result.stderr or \
        "source must be a regular file" in result.stderr
