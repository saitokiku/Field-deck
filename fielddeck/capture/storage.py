"""Session storage primitives.

Raw capture data is immutable.  Once bytes are written to a capture file they
are never rewritten, re-ordered or "cleaned up" — a decoder that disagrees
with the raw file is a decoder bug, and you can only find that out if the raw
file is still exactly what came off the wire.

Append-only logs are compressed with zstd when it is available and gzip
otherwise, so a minimal install still works.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

from fielddeck.common.errors import CaptureError

__all__ = [
    "AppendLog",
    "SessionLayout",
    "compression_available",
    "free_space_mb",
    "sha256_file",
]

_zstd: Any | None
try:  # pragma: no cover - presence depends on the install extra
    import zstandard as _zstd
except ImportError:  # pragma: no cover
    _zstd = None


def compression_available() -> str:
    """``"zstd"``, ``"gzip"`` — whichever this install can actually use."""
    return "zstd" if _zstd is not None else "gzip"


def free_space_mb(path: Path) -> float:
    target = path if path.exists() else path.parent
    usage = shutil.disk_usage(target)
    return usage.free / (1024 * 1024)


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    """Content hash for artifact integrity.  Streamed, so big captures are fine."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class SessionLayout:
    """The on-disk shape of one session directory."""

    SUBDIRS = (
        "can",
        "serial",
        "logic",
        "scope",
        "bench",
        "camera",
        "firmware",
        "reports",
        "notes",
        "modbus",
    )

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, sessions_dir: Path, session_id: str) -> SessionLayout:
        root = sessions_dir / session_id
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise CaptureError(
                f"session directory {root} already exists",
                details={"path": str(root)},
            ) from exc
        layout = cls(root)
        for name in cls.SUBDIRS:
            (root / name).mkdir(exist_ok=True)
        return layout

    @property
    def session_json(self) -> Path:
        return self.root / "session.json"

    @property
    def timeline_db(self) -> Path:
        return self.root / "timeline.sqlite"

    @property
    def events_log(self) -> Path:
        suffix = ".zst" if compression_available() == "zstd" else ".gz"
        return self.root / f"events.jsonl{suffix}"

    @property
    def audit_log(self) -> Path:
        suffix = ".zst" if compression_available() == "zstd" else ".gz"
        return self.root / f"audit.jsonl{suffix}"

    def path_for(self, kind: str, filename: str) -> Path:
        """Resolve a capture path, refusing anything that escapes the session.

        Filenames reach this function from recipes and from MCP callers, so
        ``../../etc/something`` has to be impossible rather than unlikely.
        """
        if kind not in self.SUBDIRS:
            raise CaptureError(
                f"unknown capture kind {kind!r}",
                details={"kind": kind, "known": list(self.SUBDIRS)},
            )
        candidate = (self.root / kind / filename).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise CaptureError(
                "capture filename escapes the session directory",
                details={"kind": kind, "filename": filename},
            )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate

    def relative(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.root.resolve()))

    def next_filename(self, kind: str, stem: str, suffix: str) -> Path:
        """``can/can0-0001.log`` — never overwrites an existing capture.

        Storage failures are translated here rather than escaping as a bare
        OSError: sessions routinely live on an external SSD, and "the drive
        you were recording to is gone" deserves to say so, and to say what is
        still on disk.
        """
        directory = self.root / kind
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CaptureError(
                f"cannot write to {directory}: {exc}. If the session store is on "
                "removable media, check that it is still mounted.",
                details={"path": str(directory), "errno": exc.errno},
                preserved="everything already written to this session is untouched",
            ) from exc
        index = 1
        while True:
            candidate = directory / f"{stem}-{index:04d}{suffix}"
            try:
                if not candidate.exists():
                    return candidate
            except OSError as exc:  # pragma: no cover - media vanished mid-scan
                raise CaptureError(
                    f"cannot inspect {directory}: {exc}",
                    details={"path": str(directory), "errno": exc.errno},
                    preserved="everything already written to this session is untouched",
                ) from exc
            index += 1


class AppendLog:
    """A compressed, append-only JSON-lines writer.

    Flushes are explicit.  On a field device that can lose power at any moment,
    "we buffered it and the process died" is data loss, so audit-grade records
    call :meth:`flush` immediately.
    """

    def __init__(self, path: Path, *, compress: bool = True) -> None:
        self.path = path
        self._compress = compress
        self._raw: BinaryIO | None = None
        self._writer: Any = None
        self._closed = False
        self.lines_written = 0

    def open(self) -> AppendLog:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._compress:
            self._raw = self.path.open("ab")
            self._writer = self._raw
        elif _zstd is not None:
            self._raw = self.path.open("ab")
            self._writer = _zstd.ZstdCompressor(level=6).stream_writer(self._raw)
        else:
            self._raw = None
            self._writer = gzip.open(self.path, "ab")  # noqa: SIM115 - long-lived handle
        return self

    def write(self, record: dict[str, Any]) -> None:
        if self._closed:
            raise CaptureError(f"append log {self.path} is closed")
        if self._writer is None:
            self.open()
        assert self._writer is not None
        line = json.dumps(record, default=str, separators=(",", ":")) + "\n"
        self._writer.write(line.encode("utf-8"))
        self.lines_written += 1

    def flush(self) -> None:
        if self._writer is None or self._closed:
            return
        flush = getattr(self._writer, "flush", None)
        if flush is not None:
            try:
                flush()
            except (ValueError, OSError):  # pragma: no cover - closed underneath us
                return
        if self._raw is not None and self._raw is not self._writer:
            self._raw.flush()
            os.fsync(self._raw.fileno())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            with contextlib.suppress(ValueError, OSError):
                self._writer.close()
        if self._raw is not None and self._raw is not self._writer:
            with contextlib.suppress(ValueError, OSError):
                self._raw.close()

    def __enter__(self) -> AppendLog:
        return self.open()

    def __exit__(self, *_exc: object) -> None:
        self.close()


def read_append_log(path: Path) -> Iterator[dict[str, Any]]:
    """Read back a compressed JSON-lines log, whichever codec wrote it."""
    if path.suffix == ".zst":
        if _zstd is None:
            raise CaptureError(
                f"{path} is zstd-compressed but zstandard is not installed "
                "(pip install 'fielddeck[compress]')",
                details={"path": str(path)},
            )
        with path.open("rb") as raw, _zstd.ZstdDecompressor().stream_reader(raw) as stream:
            buffer = b""
            while chunk := stream.read(1 << 16):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if line.strip():
                        yield json.loads(line)
            if buffer.strip():
                yield json.loads(buffer)
    elif path.suffix == ".gz":
        with gzip.open(path, "rb") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
    else:
        with path.open("rb") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)
