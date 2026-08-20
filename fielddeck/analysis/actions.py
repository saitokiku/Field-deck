"""The analysis toolbox, exposed to every client through the dispatcher.

Conversion, CRC, framing and protocol identification are pure computation
over bytes that were captured earlier, so every action here is PASSIVE and
every one stays available while an emergency stop is latched.  Working out
what the capture means is exactly what an engineer should be doing while the
bench is safe, and an analysis screen that greys itself out during an ESTOP
is an analysis screen nobody trusts.

The dangerous edge in this module is not the arithmetic, it is the file
paths.  These actions are reachable from a recipe and from Claude over the
restricted socket, and one of them reads files.  ``_resolve_path`` is
therefore the security boundary: a relative path is resolved inside one
session directory, an absolute path must already be inside the session store,
and ``Path.resolve()`` runs before the containment check so a symlink planted
inside a session cannot point at ``/etc/shadow``.  Anything else is refused
with a typed error that says no file was read.

Results are bounded on purpose.  A capture can be gigabytes; a client asking
for its hash gets told exactly which byte range was covered rather than a
digest of a silently truncated prefix, because a hash that is not the file's
hash is worse than no hash at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from fielddeck.analysis import autodetect, convert, crc, framing
from fielddeck.common.errors import CaptureError, InvalidRequest, SessionError
from fielddeck.common.models import PermissionLevel, StrictModel
from fielddeck.drivers.base import ActionContext, ActionSpec, NoParams, action, collect_actions

if TYPE_CHECKING:  # pragma: no cover
    from fielddeck.daemon.service import InstrumentDaemon

__all__ = ["build_action_specs"]

#: Most a single action will pull into memory.  A Pi 4 has 4 GB and is also
#: running a capture; an analysis request must never be the thing that ends a
#: session.
MAX_READ_BYTES = 64 * 1024 * 1024

#: Lines of a capture's timestamp sidecar that will be parsed.  Enough for
#: periodicity statistics, bounded so a huge index cannot stall the daemon.
MAX_INDEX_LINES = 50_000


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


class DataParams(StrictModel):
    """Where the bytes come from: inline, or a file inside the session store.

    Exactly one source must be given.  Accepting several and picking one
    silently is how a client ends up analysing something other than what it
    thought it sent.
    """

    hex: str | None = Field(default=None, description="Inline bytes as hex")
    text: str | None = Field(default=None, description="Inline bytes as UTF-8 text")
    base64: str | None = Field(default=None, description="Inline bytes as base64")
    path: str | None = Field(default=None, description="Artifact path inside the session")
    session_id: str | None = None
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=4 * 1024 * 1024, ge=1, le=MAX_READ_BYTES)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> DataParams:
        given = [name for name in ("hex", "text", "base64", "path") if getattr(self, name)]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of hex, text, base64 or path"
                + (f"; got {', '.join(given)}" if given else "")
            )
        return self


class ConvertParams(StrictModel):
    """One value, read every way it can plausibly be read."""

    value: str
    operation: Literal["interpret", "unit", "bitfield", "timestamp"] = "interpret"
    #: unit conversion
    from_unit: str | None = None
    to_unit: str | None = None
    #: bitfield extraction
    bit_offset: int = Field(default=0, ge=0, le=63)
    bit_count: int = Field(default=1, ge=1, le=64)
    total_width: int | None = Field(default=None, ge=1, le=64)
    #: timestamp conversion
    epoch_unit: Literal["s", "ms", "us", "ns"] = "s"


class CrcParams(DataParams):
    model: str | None = Field(default=None, description="CRC name; omit for the whole catalogue")
    expected: str | None = Field(
        default=None, description="Trailer bytes as hex; reports which models produce them"
    )


class CodecParams(DataParams):
    operation: Literal["encode", "decode"] = "decode"


class FramingParams(DataParams):
    #: A capture written by a driver has a sidecar index of arrival times.
    #: Using it turns "these frames repeat" into "these frames arrive every
    #: 100.0 ms", which is a measurement rather than an impression.
    use_timestamp_index: bool = True


class IdentifyParams(FramingParams):
    include_framing_report: bool = False
    limit: int = Field(default=6, ge=1, le=12)


class InspectFileParams(StrictModel):
    path: str
    session_id: str | None = None
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=MAX_READ_BYTES)
    hexdump_bytes: int = Field(default=256, ge=0, le=4096)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


class AnalysisActions:
    """Bound to the daemon so it can resolve session directories."""

    def __init__(self, daemon: InstrumentDaemon) -> None:
        self.daemon = daemon

    # -- path handling -----------------------------------------------------

    def _session_root(self, session_id: str | None) -> Path:
        chosen = session_id or self.daemon.sessions.current_id
        if chosen is None:
            raise SessionError(
                "no active session and no session_id given",
                details={"hint": "pass session_id, or start a session first"},
            )
        root = (self.daemon.sessions.sessions_dir / chosen).resolve()
        if not root.is_dir():
            raise SessionError(f"no session {chosen}", details={"session_id": chosen})
        return root

    def _resolve_path(self, raw: str, session_id: str | None) -> Path:
        """Resolve an artifact path, refusing anything outside the session store.

        Resolution happens before the containment test, so ``..`` segments and
        symlinks are both normalised away first.  The session store is the
        only readable root: these actions are reachable from MCP and recipes,
        and neither has any business reading the rest of the filesystem.
        """
        store = self.daemon.sessions.sessions_dir.resolve()
        root = self._session_root(session_id)
        candidate = Path(raw).expanduser()
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not (resolved.is_relative_to(root) or resolved.is_relative_to(store)):
            raise InvalidRequest(
                f"{raw!r} is outside the session store and will not be read",
                details={"session_dir": str(root), "sessions_dir": str(store)},
                preserved="no file was read",
            )
        if not resolved.is_file():
            raise CaptureError(
                f"no file at {raw}",
                details={"session_dir": str(root)},
                preserved="no file was read",
            )
        return resolved

    def _read_file(
        self, path: Path, *, offset: int, max_bytes: int
    ) -> tuple[bytes, dict[str, Any]]:
        size = path.stat().st_size
        if offset > size:
            raise InvalidRequest(
                f"offset {offset} is past the end of a {size}-byte file",
                details={"offset": offset, "size_bytes": size},
                preserved="no bytes were read",
            )
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(max_bytes)
        return data, {
            "size_bytes": size,
            "offset": offset,
            "bytes_read": len(data),
            # Says plainly whether a digest or an analysis covers everything.
            "complete": offset == 0 and len(data) == size,
        }

    def _load(self, params: DataParams) -> tuple[bytes, dict[str, Any]]:
        """The bytes an action was pointed at, and where they came from."""
        if params.path is not None:
            path = self._resolve_path(params.path, params.session_id)
            data, info = self._read_file(path, offset=params.offset, max_bytes=params.max_bytes)
            return data, {"kind": "file", "path": params.path, **info}
        if params.hex is not None:
            raw = convert.parse_hex_bytes(params.hex)
        elif params.base64 is not None:
            raw = convert.base64_decode(params.base64)
        else:
            raw = (params.text or "").encode("utf-8")
        data = convert.slice_bytes(raw, params.offset)[: params.max_bytes]
        return data, {
            "kind": "inline",
            "size_bytes": len(raw),
            "offset": params.offset,
            "bytes_read": len(data),
            "complete": params.offset == 0 and len(data) == len(raw),
        }

    def _timestamps(self, params: FramingParams, source: dict[str, Any]) -> list[int] | None:
        """Arrival times from a capture's sidecar index, when there is one.

        The index is advisory: a malformed or missing sidecar costs the timing
        analysis and nothing else.  Losing the whole action because a sidecar
        line was truncated by a power cut would be the wrong trade.
        """
        if not params.use_timestamp_index or source.get("kind") != "file" or not params.path:
            return None
        index_path = self._resolve_path(params.path, params.session_id).with_suffix(".idx.jsonl")
        if not index_path.is_file():
            return None
        start = int(source["offset"])
        end = start + int(source["bytes_read"])
        stamps: list[int] = []
        with index_path.open("r", encoding="ascii", errors="replace") as handle:
            for line_no, line in enumerate(handle):
                if line_no >= MAX_INDEX_LINES:
                    break
                try:
                    entry = json.loads(line)
                    offset = int(entry["offset"])
                    stamp = int(entry["monotonic_ns"])
                except (ValueError, KeyError, TypeError):
                    continue
                if start <= offset < end:
                    stamps.append(stamp)
        return stamps or None

    # -- conversion --------------------------------------------------------

    @action(
        "tools.convert",
        permission=PermissionLevel.PASSIVE,
        params=ConvertParams,
        state_changing=False,
        description="Read one value every way it can plausibly be read.",
        allowed_during_estop=True,
    )
    async def tools_convert(self, ctx: ActionContext, params: ConvertParams) -> dict[str, Any]:
        """Pure arithmetic. Nothing is opened, nothing is transmitted."""
        if params.operation == "interpret":
            return convert.interpret(params.value)
        if params.operation == "unit":
            if not params.from_unit or not params.to_unit:
                raise InvalidRequest(
                    "unit conversion needs from_unit and to_unit",
                    details={"units": convert.list_units()},
                )
            try:
                value = float(params.value)
            except ValueError:
                # Not decimal: a register value such as 0x1F is a fine input too.
                value = float(convert.parse_number(params.value))
            return convert.convert_unit(value, params.from_unit, params.to_unit)
        if params.operation == "bitfield":
            source = convert.parse_number(params.value)
            extracted = convert.bitfield(
                source, params.bit_offset, params.bit_count, total_width=params.total_width
            )
            return {
                "input": params.value,
                "value": source,
                "bit_offset": params.bit_offset,
                "bit_count": params.bit_count,
                "extracted": extracted,
                "hex": convert.to_base(extracted, 16),
                "binary": convert.to_base(extracted, 2, width=params.bit_count),
                "source_binary": convert.to_base(source, 2, width=params.total_width),
            }
        return _timestamp_result(params)

    # -- checksums ---------------------------------------------------------

    @action(
        "tools.crc",
        permission=PermissionLevel.PASSIVE,
        params=CrcParams,
        state_changing=False,
        description="Compute CRCs over bytes, or find which CRC produces a given trailer.",
        allowed_during_estop=True,
        timeout_s=60.0,
    )
    async def tools_crc(self, ctx: ActionContext, params: CrcParams) -> dict[str, Any]:
        """With ``expected``, answers the real question: which CRC is this?"""
        data, source = self._load(params)
        result: dict[str, Any] = {"source": source, "bytes": len(data)}
        if params.expected is not None:
            trailer = convert.parse_hex_bytes(params.expected)
            matches = crc.crc_candidates(data, trailer)
            result["expected"] = trailer.hex().upper()
            result["matches"] = matches
            result["match_count"] = len(matches)
            result["note"] = (
                "several models can produce the same short trailer; a match here is "
                "evidence, not proof"
                if len(matches) > 1
                else "no catalogue model produces that trailer over these bytes"
                if not matches
                else "one catalogue model produces that trailer"
            )
            return result
        if params.model is not None:
            model = crc.get_model(params.model)
            value = model.compute(data)
            result["model"] = model.name
            result["value"] = value
            result["hex"] = f"0x{value:0{model.byte_width * 2}X}"
            result["big_endian"] = model.to_bytes(value, byteorder="big").hex().upper()
            result["little_endian"] = model.to_bytes(value, byteorder="little").hex().upper()
            return result
        result["values"] = {
            name: f"0x{model.compute(data):0{model.byte_width * 2}X}"
            for name, model in crc.CATALOGUE.items()
        }
        return result

    @action(
        "tools.crc_list",
        permission=PermissionLevel.PASSIVE,
        params=NoParams,
        state_changing=False,
        description="The CRC catalogue with polynomial, init, reflection and check value.",
        allowed_during_estop=True,
    )
    async def tools_crc_list(self, ctx: ActionContext, params: NoParams) -> dict[str, Any]:
        models = crc.list_models()
        return {
            "models": models,
            "count": len(models),
            "note": "check is the CRC of b'123456789', verified by the test suite",
        }

    @action(
        "tools.hash",
        permission=PermissionLevel.PASSIVE,
        params=DataParams,
        state_changing=False,
        description="SHA-256, MD5 and CRC-32 over bytes or a session artifact.",
        allowed_during_estop=True,
        timeout_s=300.0,
    )
    async def tools_hash(self, ctx: ActionContext, params: DataParams) -> dict[str, Any]:
        """Says which byte range the digest covers; a partial hash is labelled."""

        def _work() -> dict[str, Any]:
            data, source = self._load(params)
            digests = convert.hash_bytes(data)
            covers = (
                "the whole file"
                if source.get("complete")
                else f"bytes {source['offset']}..{source['offset'] + source['bytes_read']} "
                f"of {source['size_bytes']}"
            )
            return {**digests, "source": source, "covers": covers}

        return await asyncio.to_thread(_work)

    # -- codecs ------------------------------------------------------------

    @action(
        "tools.cobs",
        permission=PermissionLevel.PASSIVE,
        params=CodecParams,
        state_changing=False,
        description="COBS encode or decode. The frame delimiter is not part of the block.",
        allowed_during_estop=True,
    )
    async def tools_cobs(self, ctx: ActionContext, params: CodecParams) -> dict[str, Any]:
        data, source = self._load(params)
        if params.operation == "encode":
            encoded = convert.cobs_encode(data)
            return {
                "source": source,
                "operation": "encode",
                "hex": encoded.hex().upper(),
                "bytes": len(encoded),
                "overhead_bytes": len(encoded) - len(data),
                "framed_hex": (encoded + b"\x00").hex().upper(),
                "note": "append 0x00 to put this block on the wire",
            }
        decoded = convert.cobs_decode(data)
        return {
            "source": source,
            "operation": "decode",
            "hex": decoded.hex().upper(),
            "bytes": len(decoded),
            "printable": convert.printable_text(decoded),
        }

    @action(
        "tools.slip",
        permission=PermissionLevel.PASSIVE,
        params=CodecParams,
        state_changing=False,
        description="SLIP (RFC 1055) encode or decode of one frame.",
        allowed_during_estop=True,
    )
    async def tools_slip(self, ctx: ActionContext, params: CodecParams) -> dict[str, Any]:
        data, source = self._load(params)
        if params.operation == "encode":
            encoded = convert.slip_encode(data)
            return {
                "source": source,
                "operation": "encode",
                "hex": encoded.hex().upper(),
                "bytes": len(encoded),
                "note": "END (0xC0) leads and trails the frame, per RFC 1055",
            }
        decoded = convert.slip_decode(data)
        return {
            "source": source,
            "operation": "decode",
            "hex": decoded.hex().upper(),
            "bytes": len(decoded),
            "printable": convert.printable_text(decoded),
        }

    # -- structure ---------------------------------------------------------

    @action(
        "tools.analyze_bytes",
        permission=PermissionLevel.PASSIVE,
        params=FramingParams,
        state_changing=False,
        description="Framing analysis of a captured stream: delimiters, fields, checksums.",
        allowed_during_estop=True,
        timeout_s=180.0,
    )
    async def tools_analyze_bytes(
        self, ctx: ActionContext, params: FramingParams
    ) -> dict[str, Any]:
        """Post-processing over bytes already recorded. Nothing reaches a DUT."""

        def _work() -> dict[str, Any]:
            data, source = self._load(params)
            stamps = self._timestamps(params, source)
            report = framing.analyze(data, timestamps_ns=stamps)
            return {**report, "source": source, "timestamps_used": len(stamps or ())}

        return await asyncio.to_thread(_work)

    @action(
        "tools.identify_protocol",
        permission=PermissionLevel.PASSIVE,
        params=IdentifyParams,
        state_changing=False,
        description="Evidence-based protocol hypotheses for a captured stream.",
        allowed_during_estop=True,
        timeout_s=180.0,
    )
    async def tools_identify_protocol(
        self, ctx: ActionContext, params: IdentifyParams
    ) -> dict[str, Any]:
        """Stage C of auto-detect: hypotheses with evidence, and a suggested test.

        The suggested test is never executed. It is returned with the
        permission it would require so the operator decides — which is the
        whole point of splitting observation from action.
        """

        def _work() -> dict[str, Any]:
            data, source = self._load(params)
            stamps = self._timestamps(params, source)
            result = autodetect.identify(
                data,
                timestamps_ns=stamps,
                limit=params.limit,
                include_report=params.include_framing_report,
            )
            return {**result, "source": source, "timestamps_used": len(stamps or ())}

        return await asyncio.to_thread(_work)

    # -- files -------------------------------------------------------------

    @action(
        "tools.inspect_file",
        permission=PermissionLevel.PASSIVE,
        params=InspectFileParams,
        state_changing=False,
        description="Identify a file in the session: format, hashes, entropy, extent.",
        allowed_during_estop=True,
        timeout_s=180.0,
    )
    async def tools_inspect_file(
        self, ctx: ActionContext, params: InspectFileParams
    ) -> dict[str, Any]:
        """Offline file analysis, restricted to the session store."""
        path = self._resolve_path(params.path, params.session_id)

        def _work() -> dict[str, Any]:
            data, source = self._read_file(path, offset=params.offset, max_bytes=params.max_bytes)
            result: dict[str, Any] = {
                "path": params.path,
                "filename": path.name,
                "source": source,
                **convert.hash_bytes(data),
                "entropy_bits_per_byte": framing.shannon_entropy(data),
                "printable_ratio": framing.printable_ratio(data),
                "format": _detect_format(data, path),
                "hexdump": convert.hexdump(
                    data, base_offset=params.offset, max_bytes=params.hexdump_bytes
                )
                if params.hexdump_bytes
                else None,
                "note": "file inspection only; nothing was read from or written to a device",
            }
            if not source["complete"]:
                result["warning"] = (
                    "digests and statistics cover only the requested byte range, not the whole file"
                )
            if result["format"] == "ihex":
                result["ihex"] = convert.parse_intel_hex(data.decode("ascii", "replace"))
            elif result["format"] == "elf":
                result["elf"] = convert.inspect_elf(data)
            return result

        return await asyncio.to_thread(_work)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _timestamp_result(params: ConvertParams) -> dict[str, Any]:
    """Epoch <-> ISO-8601, whichever direction the input implies."""
    text = params.value.strip()
    try:
        epoch = convert.parse_number(text)
    except InvalidRequest:
        value = convert.iso_to_epoch(text, unit=params.epoch_unit)
        return {
            "input": text,
            "direction": "iso -> epoch",
            "unit": params.epoch_unit,
            "value": value,
            "all_units": {
                unit: convert.iso_to_epoch(text, unit=unit) for unit in convert.EPOCH_UNITS
            },
        }
    return {
        "input": text,
        "direction": "epoch -> iso",
        "unit": params.epoch_unit,
        "utc": convert.epoch_to_iso(epoch, unit=params.epoch_unit),
        "all_units": {unit: convert.epoch_to_iso(epoch, unit=unit) for unit in convert.EPOCH_UNITS},
        "plausible_units": convert.guess_epoch_units(epoch),
    }


def _detect_format(data: bytes, path: Path) -> str:
    """Enough format identification to choose a parser, and no guessing beyond."""
    if data.startswith(b"\x7fELF"):
        return "elf"
    if data.startswith(b":") and path.suffix.lower() in {".hex", ".ihx", ".ihex", ""}:
        return "ihex"
    if data.startswith(b"S0") or data.startswith(b"S1"):
        return "srec"
    if framing.printable_ratio(data) > 0.95:
        return "text"
    return "binary"


def build_action_specs(daemon: InstrumentDaemon) -> dict[str, ActionSpec]:
    return collect_actions(AnalysisActions(daemon))
