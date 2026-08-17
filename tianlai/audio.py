from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from itertools import islice
import math
import os
from pathlib import Path
import stat
import struct
import wave
from typing import Any

from .instrument import StereoFrame
from .plain_file import PlainFileIdentity, revalidate_plain_file
from .render_lock import (
    PlainDirectoryIdentity,
    capture_plain_directory,
    revalidate_plain_directory,
)


_PCM24_SCALE = 8_388_607.0
_PCM24_NUMPY_CHUNK_FRAMES = 65_536
_PCM24_STEREO_WAV_HEADER_BYTES = 44
_PCM24_STEREO_BYTES_PER_FRAME = 6
_REPARSE_POINT = 0x400
_EVIDENCE_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class WavFileEvidence:
    """Digest and filesystem identity captured by one WAV writer descriptor.

    The digest is accepted by downstream metadata builders only after the
    pathname is reopened and proven to identify this same immutable snapshot.
    It is deliberately not a general path-stat cache.
    """

    sha256: str
    size_bytes: int
    identity: PlainFileIdentity
    change_token_kind: str
    change_token: int


@dataclass(frozen=True, slots=True)
class Pcm24WriteResult:
    """Frame count plus evidence for the exact WAV bytes just written."""

    frame_count: int
    evidence: WavFileEvidence | None


class _SequentialDigestWriter:
    """Hash final-order writes while delegating to one open file object.

    An evidenced WAV declares its frame count before the header is emitted, so
    the stdlib writer never needs to seek back and patch the RIFF header.  Any
    unexpected seek invalidates the evidence instead of producing a digest of
    write chronology rather than final file bytes.
    """

    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()
        self.byte_count = 0
        self.sequential = True

    def write(self, payload: Any) -> int:
        if self.raw.tell() != self.byte_count:
            self.sequential = False
        written = self.raw.write(payload)
        if written != len(payload):
            self.sequential = False
            raise OSError("WAV writer performed a short or invalid write")
        self.digest.update(payload)
        self.byte_count += written
        return written

    def tell(self) -> int:
        return int(self.raw.tell())

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self.sequential = False
        return int(self.raw.seek(offset, whence))

    def flush(self) -> None:
        self.raw.flush()


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)


def _require_evidence_file(value: os.stat_result) -> None:
    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_ISLNK(value.st_mode)
        or _is_reparse(value)
        or int(value.st_nlink) != 1
        or int(value.st_ino) == 0
    ):
        raise OSError("evidenced WAV must be a plain single-link file")


def _same_file_id(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
        and int(left.st_ino) != 0
    )


def _creation_time_ns(value: os.stat_result) -> int:
    return int(getattr(value, "st_birthtime_ns", value.st_ctime_ns))


def _windows_change_time_100ns(descriptor: int) -> int:
    """Read NT FileBasicInfo.ChangeTime from one already-open file handle."""

    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    handle = msvcrt.get_osfhandle(descriptor)
    if handle == -1:
        raise OSError("cannot obtain the Windows WAV file handle")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information.restype = wintypes.BOOL
    information = FILE_BASIC_INFO()
    # FILE_INFO_BY_HANDLE_CLASS.FileBasicInfo == 0.
    if not get_information(
        wintypes.HANDLE(handle),
        0,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, "GetFileInformationByHandleEx(FileBasicInfo) failed")
    token = int(information.ChangeTime)
    if token <= 0:
        raise OSError("Windows file ChangeTime is unavailable")
    return token


def _descriptor_change_token(
    descriptor: int,
    status: os.stat_result,
) -> tuple[str, int] | None:
    """Return a reliable mutation token, or ``None`` to force a full hash."""

    if os.name == "nt":
        try:
            return (
                "windows-file-basic-change-time-100ns",
                _windows_change_time_100ns(descriptor),
            )
        except (
            AttributeError,
            ImportError,
            OSError,
            OverflowError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            return None
    try:
        token = int(status.st_ctime_ns)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if token <= 0:
        return None
    return "posix-stat-ctime-ns", token


def _open_evidence_descriptor(path: Path) -> int:
    """Open one read handle that excludes Windows writers until close.

    POSIX ctime is part of every post-hash identity check and cannot be reset
    by an ordinary writer.  Windows exposes a separately queried ChangeTime,
    but relying on that timestamp alone leaves a narrow EOF-to-postcheck race.
    A CreateFile handle whose share mode permits reads only closes that race:
    existing writers make the open fail, and new writers or renames cannot
    enter until the verifier has completed its final descriptor/path checks.
    """

    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_flag_sequential_scan = 0x08000000
    handle = create_file(
        os.fspath(path),
        generic_read,
        file_share_read,
        None,
        open_existing,
        (
            file_attribute_normal
            | file_flag_open_reparse_point
            | file_flag_sequential_scan
        ),
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(wintypes.HANDLE(handle))
        raise


def _same_path_and_handle_snapshot(
    path_status: os.stat_result,
    handle_status: os.stat_result,
) -> bool:
    return (
        _same_file_id(path_status, handle_status)
        and int(path_status.st_size) == int(handle_status.st_size)
        and int(path_status.st_mtime_ns) == int(handle_status.st_mtime_ns)
        and _creation_time_ns(path_status) == _creation_time_ns(handle_status)
        and (
            os.name == "nt"
            or int(path_status.st_ctime_ns) == int(handle_status.st_ctime_ns)
        )
    )


def _capture_wav_evidence(
    path: Path,
    raw: Any,
    tracked: _SequentialDigestWriter,
    parent_identity: PlainDirectoryIdentity,
    expected_byte_count: int,
) -> WavFileEvidence | None:
    """Bind sequential writer bytes to the still-open output descriptor."""

    tracked.flush()
    revalidate_plain_directory(parent_identity)
    descriptor = raw.fileno()
    opened_status = os.fstat(descriptor)
    opened_path_status = os.lstat(path)
    _require_evidence_file(opened_status)
    _require_evidence_file(opened_path_status)
    if not _same_path_and_handle_snapshot(opened_path_status, opened_status):
        raise OSError("evidenced WAV pathname changed during writing")
    if (
        not tracked.sequential
        or tracked.byte_count != int(opened_status.st_size)
        or raw.tell() != int(opened_status.st_size)
        or int(opened_status.st_size) != expected_byte_count
    ):
        raise OSError(
            "evidenced WAV was not written once at its exact expected size"
        )

    opened_change = _descriptor_change_token(descriptor, opened_status)
    if opened_change is None:
        # Evidence is an optimization.  A platform/filesystem that cannot
        # expose a reliable mutation token must keep the established full-SHA
        # path available rather than making an otherwise valid render fail.
        return None

    identity = PlainFileIdentity(
        path=path,
        device=int(opened_status.st_dev),
        inode=int(opened_status.st_ino),
        size=int(opened_status.st_size),
        modified_ns=int(opened_status.st_mtime_ns),
        changed_ns=int(opened_status.st_ctime_ns),
        parent_identity=parent_identity,
    )
    revalidate_plain_file(identity)
    revalidate_plain_directory(parent_identity)
    finished_status = os.fstat(descriptor)
    finished_path_status = os.lstat(path)
    _require_evidence_file(finished_status)
    _require_evidence_file(finished_path_status)
    if (
        not _same_path_and_handle_snapshot(finished_path_status, finished_status)
        or not _same_path_and_handle_snapshot(opened_status, finished_status)
    ):
        raise OSError("evidenced WAV changed while its identity was captured")
    finished_change = _descriptor_change_token(descriptor, finished_status)
    if finished_change is None:
        return None
    if finished_change != opened_change:
        raise OSError("evidenced WAV changed while its digest was captured")
    return WavFileEvidence(
        sha256=tracked.digest.hexdigest(),
        size_bytes=int(finished_status.st_size),
        identity=identity,
        change_token_kind=finished_change[0],
        change_token=finished_change[1],
    )


def _sha256_open_descriptor(
    descriptor: int,
    *,
    expected_size: int,
) -> str:
    """Hash an already identity-bound descriptor from byte zero exactly once."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    observed = 0
    while chunk := os.read(descriptor, _EVIDENCE_HASH_CHUNK_BYTES):
        observed += len(chunk)
        if observed > expected_size:
            raise OSError("evidenced WAV grew during fallback hashing")
        digest.update(chunk)
    if observed != expected_size:
        raise OSError("evidenced WAV size changed during fallback hashing")
    return digest.hexdigest()


def _revalidate_wav_file_evidence(
    path: str | Path,
    evidence: WavFileEvidence,
    *,
    require_full_digest: bool,
) -> str:
    """Validate one captured WAV, optionally hashing its exact bytes again.

    A reliable unchanged mutation token takes the metadata-only fast path.  If
    that token is unavailable or changed for a metadata-only reason, this same
    bound descriptor is fully hashed.  Final provenance publication can also
    require that full digest unconditionally: mutation timestamps are useful
    performance evidence, but must not be the only content proof at a durable
    licence boundary.
    """

    if not isinstance(evidence, WavFileEvidence):
        raise TypeError("WavFileEvidence is required")
    if (
        not isinstance(evidence.change_token_kind, str)
        or not evidence.change_token_kind
        or isinstance(evidence.change_token, bool)
        or not isinstance(evidence.change_token, int)
        or evidence.change_token <= 0
    ):
        raise OSError("evidenced WAV mutation token is invalid")
    if evidence.size_bytes != evidence.identity.size:
        raise OSError("evidenced WAV byte count is internally inconsistent")
    digest = evidence.sha256
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise OSError("evidenced WAV digest is invalid")

    requested = Path(os.path.abspath(os.fspath(path)))
    captured = revalidate_plain_file(evidence.identity)
    try:
        same_name = requested.resolve(strict=True) == captured
    except OSError as error:
        raise OSError("evidenced WAV path cannot be resolved") from error
    if not same_name:
        raise OSError("evidenced WAV belongs to a different pathname")

    parent = revalidate_plain_directory(evidence.identity.parent_identity)
    if captured.parent != parent:
        raise OSError("evidenced WAV escaped its captured parent directory")
    before = os.lstat(captured)
    _require_evidence_file(before)
    descriptor = _open_evidence_descriptor(captured)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(captured)
        _require_evidence_file(opened)
        _require_evidence_file(after_open)
        if (
            not _same_path_and_handle_snapshot(before, opened)
            or not _same_path_and_handle_snapshot(opened, after_open)
            or int(opened.st_dev) != evidence.identity.device
            or int(opened.st_ino) != evidence.identity.inode
            or int(opened.st_size) != evidence.identity.size
            or int(opened.st_mtime_ns) != evidence.identity.modified_ns
            or int(opened.st_ctime_ns) != evidence.identity.changed_ns
        ):
            raise OSError("evidenced WAV identity changed after capture")

        opened_change = _descriptor_change_token(descriptor, opened)
        revalidate_plain_directory(evidence.identity.parent_identity)
        finished = os.fstat(descriptor)
        current = os.lstat(captured)
        _require_evidence_file(finished)
        _require_evidence_file(current)
        if (
            not _same_path_and_handle_snapshot(opened, finished)
            or not _same_path_and_handle_snapshot(finished, current)
            or int(finished.st_dev) != evidence.identity.device
            or int(finished.st_ino) != evidence.identity.inode
            or int(finished.st_size) != evidence.identity.size
            or int(finished.st_mtime_ns) != evidence.identity.modified_ns
            or int(finished.st_ctime_ns) != evidence.identity.changed_ns
        ):
            raise OSError("evidenced WAV identity changed during revalidation")
        finished_change = _descriptor_change_token(descriptor, finished)
        captured_change = (
            evidence.change_token_kind,
            evidence.change_token,
        )
        if require_full_digest or not (
            opened_change is not None
            and opened_change == finished_change == captured_change
        ):
            current_digest = _sha256_open_descriptor(
                descriptor,
                expected_size=evidence.size_bytes,
            )
            hashed = os.fstat(descriptor)
            hashed_path = os.lstat(captured)
            _require_evidence_file(hashed)
            _require_evidence_file(hashed_path)
            if (
                not _same_path_and_handle_snapshot(finished, hashed)
                or not _same_path_and_handle_snapshot(hashed, hashed_path)
                or int(hashed.st_dev) != evidence.identity.device
                or int(hashed.st_ino) != evidence.identity.inode
                or int(hashed.st_size) != evidence.identity.size
                or int(hashed.st_mtime_ns) != evidence.identity.modified_ns
                or int(hashed.st_ctime_ns) != evidence.identity.changed_ns
            ):
                raise OSError("evidenced WAV identity changed during fallback hash")
            if current_digest != digest:
                raise OSError("evidenced WAV bytes changed after digest capture")
        revalidate_plain_directory(evidence.identity.parent_identity)
        return digest
    finally:
        os.close(descriptor)


def revalidate_wav_file_evidence(
    path: str | Path,
    evidence: WavFileEvidence,
) -> str:
    """Return the writer digest when identity and mutation evidence match."""

    return _revalidate_wav_file_evidence(
        path,
        evidence,
        require_full_digest=False,
    )


def verify_wav_file_evidence_bytes(
    path: str | Path,
    evidence: WavFileEvidence,
) -> str:
    """Rehash an identity-bound WAV for durable provenance publication."""

    return _revalidate_wav_file_evidence(
        path,
        evidence,
        require_full_digest=True,
    )


def _pcm24(value: float) -> bytes:
    integer = round(max(-1.0, min(1.0, value)) * _PCM24_SCALE)
    if integer < 0:
        integer += 1 << 24
    return bytes((integer & 0xFF, (integer >> 8) & 0xFF, (integer >> 16) & 0xFF))


def _validate_pcm24_frame(
    left: object,
    right: object,
    frame_index: int,
) -> tuple[float, float]:
    left_value = float(left)
    right_value = float(right)
    if not math.isfinite(left_value) or not math.isfinite(right_value):
        raise ValueError(
            "单乐器渲染产生了非有限样本："
            f"第 {frame_index} 帧 left={left_value!r}, right={right_value!r}"
        )
    frame_peak = max(abs(left_value), abs(right_value))
    if frame_peak > 1.0:
        excess_db = 20.0 * math.log10(frame_peak)
        raise ValueError(
            "单乐器渲染过载："
            f"第 {frame_index} 帧量化前峰值 {frame_peak:.6f}"
            f"（超出 {excess_db:+.2f} dB）。"
            "写盘会被静默削平，因此拒绝输出；"
            f"请将乐器或演奏增益降低至少 {excess_db:.2f} dB"
        )
    return left_value, right_value


def _validate_pcm24_samples(samples: Any, frame_offset: int) -> None:
    """Reject the first non-finite or clipping stereo frame in one batch."""

    import numpy as np

    finite_frames = np.isfinite(samples).all(axis=1)
    overloaded = (
        (samples[:, 0] > 1.0)
        | (samples[:, 0] < -1.0)
        | (samples[:, 1] > 1.0)
        | (samples[:, 1] < -1.0)
    )
    nonfinite_indices = np.flatnonzero(~finite_frames)
    overload_indices = np.flatnonzero(overloaded)
    first_nonfinite = (
        None if nonfinite_indices.size == 0 else int(nonfinite_indices[0])
    )
    first_overload = (
        None if overload_indices.size == 0 else int(overload_indices[0])
    )
    if first_nonfinite is not None and (
        first_overload is None or first_nonfinite <= first_overload
    ):
        local_index = first_nonfinite
        _validate_pcm24_frame(
            samples[local_index, 0],
            samples[local_index, 1],
            frame_offset + local_index,
        )
    if first_overload is not None:
        local_index = first_overload
        _validate_pcm24_frame(
            samples[local_index, 0],
            samples[local_index, 1],
            frame_offset + local_index,
        )


def _write_numpy_pcm24(
    output: Any,
    frames: object,
    *,
    reject_out_of_range: bool = False,
    frame_offset: int = 0,
) -> int | None:
    """Write a numeric ``(frames, 2)`` ndarray in bounded vector chunks.

    Return ``None`` when the value is not eligible, allowing the public writer
    to retain its generic iterable behaviour.  Conversion intentionally keeps
    the scalar writer's clipping, bankers-rounding and NaN/Infinity semantics.
    """

    import numpy as np

    if (
        not isinstance(frames, np.ndarray)
        or frames.ndim != 2
        or frames.shape[1] != 2
        or not (
            np.issubdtype(frames.dtype, np.integer)
            or (
                np.issubdtype(frames.dtype, np.floating)
                and frames.dtype.itemsize <= 8
            )
        )
    ):
        return None
    frame_count = int(frames.shape[0])
    for start in range(0, frame_count, _PCM24_NUMPY_CHUNK_FRAMES):
        samples = np.array(
            frames[start : start + _PCM24_NUMPY_CHUNK_FRAMES],
            dtype=np.float64,
            order="C",
            copy=True,
        )
        if reject_out_of_range:
            _validate_pcm24_samples(samples, frame_offset + start)
        np.nan_to_num(
            samples,
            copy=False,
            nan=1.0,
            posinf=1.0,
            neginf=-1.0,
        )
        np.clip(samples, -1.0, 1.0, out=samples)
        samples *= _PCM24_SCALE
        np.rint(samples, out=samples)
        integers = samples.astype("<i4")
        packed = integers.view(np.uint8).reshape(-1, 4)[:, :3]
        output.writeframesraw(packed.tobytes(order="C"))
    return frame_count


def _write_iterable_pcm24(
    output: Any,
    frames: Iterable[StereoFrame],
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Encode a generic frame iterator in bounded, vectorizable batches.

    Numeric stereo batches take the same NumPy encoder as ndarray callers.
    Unusual iterable values retain the original scalar unpacking and encoding
    behaviour instead of becoming a narrower API as a side effect of the fast
    path.
    """

    import numpy as np

    iterator = iter(frames)
    frame_count = 0
    scalar_buffer = bytearray()
    while True:
        batch = list(islice(iterator, _PCM24_NUMPY_CHUNK_FRAMES))
        if not batch:
            break
        try:
            numeric_batch: object = np.asarray(batch)
        except (TypeError, ValueError, OverflowError):
            numeric_batch = None
        written = _write_numpy_pcm24(
            output,
            numeric_batch,
            reject_out_of_range=reject_out_of_range,
            frame_offset=frame_count,
        )
        if written is not None:
            frame_count += written
            continue
        for left, right in batch:
            if reject_out_of_range:
                left, right = _validate_pcm24_frame(left, right, frame_count)
            scalar_buffer.extend(_pcm24(left))
            scalar_buffer.extend(_pcm24(right))
            frame_count += 1
            if len(scalar_buffer) >= 24_576:
                output.writeframesraw(scalar_buffer)
                scalar_buffer.clear()
    if scalar_buffer:
        output.writeframesraw(scalar_buffer)
    return frame_count


def _write_wav_pcm24_file(
    raw_output: Any,
    frames: Iterable[StereoFrame],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Write PCM-24 through an already-open, identity-bound binary file."""

    with wave.open(raw_output, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        numpy_frame_count = _write_numpy_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )
        if numpy_frame_count is not None:
            return numpy_frame_count
        return _write_iterable_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )


def write_wav_pcm24(
    path: str | Path,
    frames: Iterable[StereoFrame],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w+b") as raw_output:
        return _write_wav_pcm24_file(
            raw_output,
            frames,
            sample_rate,
            reject_out_of_range=reject_out_of_range,
        )


def _write_wav_pcm24_blocks_file(
    raw_output: Any,
    blocks: Iterable[Any],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Write bounded stereo blocks to an identity-bound binary file.

    Numeric arrays go directly to the existing bit-compatible vector encoder.
    Generic blocks retain the scalar fallback, which keeps custom instrument
    behaviour no narrower than :func:`write_wav_pcm24`.
    """

    import numpy as np

    with wave.open(raw_output, "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        frame_count = 0
        scalar_buffer = bytearray()
        for block in blocks:
            if isinstance(block, np.ndarray):
                batch: Any = block
            else:
                batch = block if isinstance(block, (list, tuple)) else list(block)
                try:
                    batch = np.asarray(batch)
                except (TypeError, ValueError, OverflowError):
                    pass
            # The numeric encoder writes immediately.  Flush any preceding
            # scalar fallback first so heterogeneous block streams cannot be
            # reordered on disk.
            if scalar_buffer:
                output.writeframesraw(scalar_buffer)
                scalar_buffer.clear()
            written = _write_numpy_pcm24(
                output,
                batch,
                reject_out_of_range=reject_out_of_range,
                frame_offset=frame_count,
            )
            if written is not None:
                frame_count += written
                continue

            for left, right in batch:
                if reject_out_of_range:
                    left, right = _validate_pcm24_frame(
                        left,
                        right,
                        frame_count,
                    )
                scalar_buffer.extend(_pcm24(left))
                scalar_buffer.extend(_pcm24(right))
                frame_count += 1
                if len(scalar_buffer) >= 24_576:
                    output.writeframesraw(scalar_buffer)
                    scalar_buffer.clear()
        if scalar_buffer:
            output.writeframesraw(scalar_buffer)
        return frame_count


def write_wav_pcm24_blocks(
    path: str | Path,
    blocks: Iterable[Any],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Write bounded stereo blocks without flattening them into frame tuples."""

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w+b") as raw_output:
        return _write_wav_pcm24_blocks_file(
            raw_output,
            blocks,
            sample_rate,
            reject_out_of_range=reject_out_of_range,
        )


def _write_evidenced_wav_pcm24(
    path: str | Path,
    sample_rate: int,
    expected_frame_count: int,
    write_payload: Any,
) -> Pcm24WriteResult:
    """Write one final-order WAV stream and capture its descriptor evidence."""

    if (
        isinstance(expected_frame_count, bool)
        or not isinstance(expected_frame_count, int)
        or expected_frame_count < 0
    ):
        raise ValueError("expected_frame_count must be a non-negative integer")
    requested = Path(os.path.abspath(os.fspath(path)))
    requested.parent.mkdir(parents=True, exist_ok=True)
    parent_identity = capture_plain_directory(requested.parent)
    parent = revalidate_plain_directory(parent_identity)
    output_path = parent / requested.name

    with output_path.open("w+b") as raw:
        opened_status = os.fstat(raw.fileno())
        path_status = os.lstat(output_path)
        _require_evidence_file(opened_status)
        _require_evidence_file(path_status)
        if not _same_path_and_handle_snapshot(path_status, opened_status):
            raise OSError("evidenced WAV pathname changed while opening")
        revalidate_plain_directory(parent_identity)

        tracked = _SequentialDigestWriter(raw)
        output = wave.open(tracked, "wb")
        try:
            output.setnchannels(2)
            output.setsampwidth(3)
            output.setframerate(sample_rate)
            # Declaring the exact size up front makes the RIFF header final on
            # its first write.  This is what permits a streaming SHA-256 over
            # final byte order without reading the completed WAV again.
            output.setnframes(expected_frame_count)
            frame_count = int(write_payload(output))
            if frame_count != expected_frame_count:
                raise ValueError(
                    "evidenced WAV frame count mismatch: "
                    f"expected {expected_frame_count}, got {frame_count}"
                )
        finally:
            output.close()
        evidence = _capture_wav_evidence(
            output_path,
            raw,
            tracked,
            parent_identity,
            _PCM24_STEREO_WAV_HEADER_BYTES
            + expected_frame_count * _PCM24_STEREO_BYTES_PER_FRAME,
        )
    return Pcm24WriteResult(frame_count=frame_count, evidence=evidence)


def write_wav_pcm24_with_evidence(
    path: str | Path,
    frames: Iterable[StereoFrame],
    sample_rate: int,
    *,
    expected_frame_count: int,
    reject_out_of_range: bool = False,
) -> Pcm24WriteResult:
    """Write PCM-24 and return a digest captured without reopening the path.

    The established :func:`write_wav_pcm24` API remains unchanged.  This
    variant is for trusted render transactions that already know their exact
    frame count and need to bind several metadata documents to the same bytes.
    """

    def write_payload(output: Any) -> int:
        numpy_frame_count = _write_numpy_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )
        if numpy_frame_count is not None:
            return numpy_frame_count
        return _write_iterable_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )

    return _write_evidenced_wav_pcm24(
        path,
        sample_rate,
        expected_frame_count,
        write_payload,
    )


def write_wav_pcm24_blocks_with_evidence(
    path: str | Path,
    blocks: Iterable[Any],
    sample_rate: int,
    *,
    expected_frame_count: int,
    reject_out_of_range: bool = False,
) -> Pcm24WriteResult:
    """Block-stream counterpart to :func:`write_wav_pcm24_with_evidence`."""

    def write_payload(output: Any) -> int:
        import numpy as np

        frame_count = 0
        scalar_buffer = bytearray()
        for block in blocks:
            if isinstance(block, np.ndarray):
                batch: Any = block
            else:
                batch = block if isinstance(block, (list, tuple)) else list(block)
                try:
                    batch = np.asarray(batch)
                except (TypeError, ValueError, OverflowError):
                    pass
            if scalar_buffer:
                output.writeframesraw(scalar_buffer)
                scalar_buffer.clear()
            written = _write_numpy_pcm24(
                output,
                batch,
                reject_out_of_range=reject_out_of_range,
                frame_offset=frame_count,
            )
            if written is not None:
                frame_count += written
                continue
            for left, right in batch:
                if reject_out_of_range:
                    left, right = _validate_pcm24_frame(
                        left,
                        right,
                        frame_count,
                    )
                scalar_buffer.extend(_pcm24(left))
                scalar_buffer.extend(_pcm24(right))
                frame_count += 1
                if len(scalar_buffer) >= 24_576:
                    output.writeframesraw(scalar_buffer)
                    scalar_buffer.clear()
        if scalar_buffer:
            output.writeframesraw(scalar_buffer)
        return frame_count

    return _write_evidenced_wav_pcm24(
        path,
        sample_rate,
        expected_frame_count,
        write_payload,
    )


def audio_file_info(path: str | Path) -> tuple[int, int, int]:
    """Return sample rate, frame count and channels without decoding the file."""

    audio_path = Path(path)
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        if info.channels not in (1, 2):
            raise ValueError(f"unsupported audio channel count: {info.channels}")
        return int(info.samplerate), int(info.frames), int(info.channels)
    except ImportError:
        if audio_path.suffix.lower() != ".wav":
            raise ValueError("FLAC samples require the soundfile dependency") from None
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            if channels not in (1, 2):
                raise ValueError(f"unsupported WAV channel count: {channels}")
            return source.getframerate(), source.getnframes(), channels


def wav_loop_points(path: str | Path) -> tuple[int, int] | None:
    """Read the first forward loop from a WAV `smpl` chunk, if present."""

    audio_path = Path(path)
    if audio_path.suffix.lower() != ".wav":
        return None
    with audio_path.open("rb") as source:
        if source.read(4) != b"RIFF":
            return None
        source.seek(4, 1)
        if source.read(4) != b"WAVE":
            return None
        while True:
            chunk_id = source.read(4)
            if len(chunk_id) < 4:
                return None
            raw_size = source.read(4)
            if len(raw_size) < 4:
                return None
            chunk_size = struct.unpack("<I", raw_size)[0]
            if chunk_id == b"smpl" and chunk_size >= 60:
                # Only the fixed header and first loop record are needed.
                # Do not trust an unbounded chunk length from a third-party
                # sample, and check the bounded read before unpacking it.
                payload = source.read(60)
                if len(payload) < 60:
                    return None
                loop_count = struct.unpack_from("<I", payload, 28)[0]
                if loop_count < 1:
                    return None
                loop_type, start, end = struct.unpack_from("<III", payload, 40)[0:3]
                if loop_type != 0 or end < start:
                    return None
                return int(start), int(end + 1)
            source.seek(chunk_size + (chunk_size & 1), 1)


def read_audio_float(path: str | Path) -> tuple[int, Any]:
    """Decode WAV/FLAC into memory-efficient normalized stereo frames."""

    audio_path = Path(path)
    try:
        import soundfile as sf

        frames, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if frames.shape[1] == 1:
            import numpy as np

            frames = np.repeat(frames, 2, axis=1)
        elif frames.shape[1] != 2:
            raise ValueError(f"unsupported audio channel count: {frames.shape[1]}")
        return int(sample_rate), frames
    except ImportError:
        if audio_path.suffix.lower() != ".wav":
            raise ValueError("FLAC samples require the soundfile dependency") from None

    return read_wav_float(audio_path)


def read_wav_float(path: str | Path) -> tuple[int, tuple[tuple[float, float], ...]]:
    """Standard-library PCM WAV fallback."""

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        sample_rate = source.getframerate()
        frame_count = source.getnframes()
        if channels not in (1, 2):
            raise ValueError(f"unsupported WAV channel count: {channels}")
        if width not in (1, 2, 3, 4):
            raise ValueError(f"unsupported WAV sample width: {width}")
        raw = source.readframes(frame_count)

    def decode(offset: int) -> float:
        if width == 1:
            return (raw[offset] - 128) / 128.0
        if width == 2:
            return struct.unpack_from("<h", raw, offset)[0] / 32768.0
        if width == 3:
            value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            return value / 8_388_608.0
        return struct.unpack_from("<i", raw, offset)[0] / 2_147_483_648.0

    stride = channels * width
    frames: list[tuple[float, float]] = []
    for frame_index in range(frame_count):
        offset = frame_index * stride
        left = decode(offset)
        right = left if channels == 1 else decode(offset + width)
        frames.append((left, right))
    return sample_rate, tuple(frames)
