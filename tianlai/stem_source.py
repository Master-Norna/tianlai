"""Single-consumer ownership for verified raw stereo stems.

The ensemble renderer receives raw stems from more than one producer: a
managed child, a cache snapshot and a long in-process block render.
This module defines the small descriptor-backed primitive needed to consume a
managed result without materialising the complete track in coordinator memory.

``OwnedStemSource`` takes ownership only after construction succeeds.  A
successful consumer must exhaust and verify the source and then explicitly
close it.  That two-step boundary lets a producer keep a worker/scratch lease
until the last raw byte has actually been consumed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import hashlib
import os
import stat
import threading
from typing import Any, BinaryIO, Protocol, runtime_checkable

import numpy as np


_AUDIO_DTYPE = np.dtype("<f4")
_CHANNELS = 2
_BYTES_PER_FRAME = _AUDIO_DTYPE.itemsize * _CHANNELS
MAX_STEM_BLOCK_FRAMES = 65_536


class StemSourceError(RuntimeError):
    """An owned raw stem changed or became invalid during consumption."""


class _OwnedBlockIterator:
    """Make abandonment observable even before a generator's first next()."""

    __slots__ = ("_iterator", "_owner", "_closed")

    def __init__(self, owner: "OwnedStemSource", iterator: Iterator[np.ndarray]):
        self._owner: OwnedStemSource | None = owner
        self._iterator: Iterator[np.ndarray] | None = iterator
        self._closed = False

    def __iter__(self) -> "_OwnedBlockIterator":
        return self

    def __next__(self) -> np.ndarray:
        iterator = self._iterator
        if self._closed or iterator is None:
            raise StopIteration
        try:
            return next(iterator)
        except StopIteration:
            self._closed = True
            self._iterator = None
            self._owner = None
            raise
        except BaseException:
            self._closed = True
            self._iterator = None
            self._owner = None
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        iterator = self._iterator
        owner = self._owner
        self._iterator = None
        self._owner = None
        if iterator is not None:
            try:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            except BaseException:
                pass
        if owner is not None:
            owner._finish(False, suppress_errors=True)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


@runtime_checkable
class StemBlockSource(Protocol):
    """Structural interface shared by descriptor-backed stem sources.

    Implementations are deliberately single-consumer.  ``materialise`` and
    ``iter_blocks`` are two mutually exclusive ways to perform that one
    consumption.
    """

    @property
    def frame_count(self) -> int: ...

    @property
    def shape(self) -> tuple[int, int]: ...

    @property
    def audio_sha256(self) -> str: ...

    @property
    def closed(self) -> bool: ...

    def iter_blocks(
        self,
        block_frames: int = MAX_STEM_BLOCK_FRAMES,
    ) -> Iterator[np.ndarray]: ...

    def materialise(self) -> np.ndarray: ...

    def close(self) -> None: ...


CompletionCallback = Callable[[bool], None]


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _read_exact(source: BinaryIO, byte_count: int) -> bytes:
    """Read one bounded immutable payload, tolerating legal short reads."""

    first = source.read(byte_count)
    if first is None:
        raise StemSourceError("owned stem audio read made no progress")
    if len(first) == byte_count:
        return first
    chunks = [first]
    remaining = byte_count - len(first)
    while remaining:
        chunk = source.read(remaining)
        if not chunk:
            raise StemSourceError(
                "owned stem audio became truncated during consumption"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class OwnedStemSource:
    """Own and verify one float32-stereo span in an already-open file.

    The descriptor may contain a trusted protocol prefix and suffix, so the
    raw audio is identified by ``audio_offset`` plus ``frame_count`` rather
    than by requiring the whole file to be audio.  Construction captures the
    descriptor identity and total length.  Consumption then checks:

    * every byte in the exact audio span is present;
    * every decoded sample is finite;
    * the complete span has the expected SHA-256 digest; and
    * the same regular descriptor and total file length remain bound.

    Blocks are immutable little-endian float32 stereo arrays and never exceed
    65,536 frames.  The source can be consumed exactly once, either as blocks
    or through ``materialise()``.

    ``completion_callback`` is a one-shot resource/worker lease notification.
    It is called with ``True`` only when a complete verification is followed
    by ``close()``.  Early close, iterator abandonment and validation failure
    close the descriptor and notify ``False``.  A primary consumption error is
    never replaced by a cleanup or callback error.
    """

    __slots__ = (
        "_source",
        "_audio_offset",
        "_frame_count",
        "_expected_sha256",
        "_opened_status",
        "_opened_size",
        "_owner_pid",
        "_completion_callback",
        "_completion_notified",
        "_closed",
        "_consumed",
        "_iterator_active",
        "_verified",
        "_lock",
        "__weakref__",
    )

    def __init__(
        self,
        source: BinaryIO,
        *,
        audio_offset: int,
        frame_count: int,
        expected_sha256: str,
        completion_callback: CompletionCallback | None = None,
    ) -> None:
        if (
            isinstance(audio_offset, bool)
            or not isinstance(audio_offset, int)
            or audio_offset < 0
        ):
            raise ValueError("audio_offset must be a non-negative integer")
        if (
            isinstance(frame_count, bool)
            or not isinstance(frame_count, int)
            or frame_count < 0
        ):
            raise ValueError("frame_count must be a non-negative integer")
        if not _is_sha256(expected_sha256):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        if completion_callback is not None and not callable(completion_callback):
            raise TypeError("completion_callback must be callable")

        # Do all fallible preconditions before taking ownership.  If this
        # constructor raises, the caller still owns both the file and lease.
        try:
            opened_status = os.fstat(source.fileno())
        except (AttributeError, OSError, ValueError) as exc:
            raise ValueError("source must be an open regular binary file") from exc
        if not stat.S_ISREG(opened_status.st_mode):
            raise ValueError("source must be an open regular binary file")
        byte_count = frame_count * _BYTES_PER_FRAME
        if audio_offset + byte_count > opened_status.st_size:
            raise ValueError("owned stem audio span exceeds the source file")

        self._source: BinaryIO | None = source
        self._audio_offset = audio_offset
        self._frame_count = frame_count
        self._expected_sha256 = expected_sha256
        self._opened_status = opened_status
        self._opened_size = int(opened_status.st_size)
        self._owner_pid = os.getpid()
        self._completion_callback = completion_callback
        self._completion_notified = False
        self._closed = False
        self._consumed = False
        self._iterator_active = False
        self._verified = False
        self._lock = threading.Lock()

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def shape(self) -> tuple[int, int]:
        return (self._frame_count, _CHANNELS)

    @property
    def audio_sha256(self) -> str:
        """Expected digest that successful consumption independently verifies."""

        return self._expected_sha256

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def verified(self) -> bool:
        """Whether the one permitted consumption reached every final gate."""

        with self._lock:
            return self._verified

    def _begin_consumption(self) -> BinaryIO:
        if os.getpid() != self._owner_pid:
            raise StemSourceError("owned stem source belongs to another process")
        with self._lock:
            if self._closed:
                raise ValueError("owned stem source is closed")
            if self._consumed or self._iterator_active:
                raise ValueError("owned stem source can only be consumed once")
            source = self._source
            if source is None:
                raise ValueError("owned stem source is closed")
            self._consumed = True
            self._iterator_active = True
            return source

    def iter_blocks(
        self,
        block_frames: int = MAX_STEM_BLOCK_FRAMES,
    ) -> Iterator[np.ndarray]:
        """Return the source's sole iterator of immutable bounded blocks."""

        if (
            isinstance(block_frames, bool)
            or not isinstance(block_frames, int)
            or block_frames <= 0
            or block_frames > MAX_STEM_BLOCK_FRAMES
        ):
            raise ValueError(
                "block_frames must be between 1 and 65536"
            )
        source = self._begin_consumption()
        return _OwnedBlockIterator(
            self,
            self._iter_blocks(source, block_frames),
        )

    def _iter_blocks(
        self,
        source: BinaryIO,
        block_frames: int,
    ) -> Iterator[np.ndarray]:
        digest = hashlib.sha256()
        remaining = self._frame_count * _BYTES_PER_FRAME
        chunk_bytes = block_frames * _BYTES_PER_FRAME
        try:
            source.seek(self._audio_offset)
            while remaining:
                requested = min(remaining, chunk_bytes)
                payload = _read_exact(source, requested)
                digest.update(payload)
                block = np.frombuffer(payload, dtype=_AUDIO_DTYPE).reshape(-1, 2)
                if not bool(np.isfinite(block).all()):
                    raise StemSourceError(
                        "owned stem audio contains non-finite samples"
                    )
                remaining -= requested
                yield block

            if digest.hexdigest() != self._expected_sha256:
                raise StemSourceError(
                    "owned stem audio SHA-256 changed during consumption"
                )
            final_status = os.fstat(source.fileno())
            if not _same_file_identity(self._opened_status, final_status):
                raise StemSourceError(
                    "owned stem file identity changed during consumption"
                )
            if int(final_status.st_size) != self._opened_size:
                raise StemSourceError(
                    "owned stem file length changed during consumption"
                )
            with self._lock:
                if self._closed or self._source is not source:
                    raise StemSourceError(
                        "owned stem source closed during consumption"
                    )
                self._verified = True
        except BaseException:
            # GeneratorExit is an abandoned/partially consumed iterator too.
            # Release its lease immediately, but preserve the actual earlier
            # exception if file.close() or the callback is also faulty.
            self._finish(False, suppress_errors=True)
            raise
        finally:
            with self._lock:
                self._iterator_active = False

    def materialise(self) -> np.ndarray:
        """Return one owned writable ndarray through the same final gates."""

        blocks = self.iter_blocks()
        try:
            audio = np.empty(
                (self._frame_count, _CHANNELS),
                dtype=_AUDIO_DTYPE,
            )
            offset = 0
            for block in blocks:
                stop = offset + int(block.shape[0])
                audio[offset:stop] = block
                offset = stop
            if offset != self._frame_count:
                raise StemSourceError(
                    "owned stem audio frame count changed during materialisation"
                )
            return audio
        except BaseException:
            try:
                blocks.close()
            except BaseException:
                pass
            self._finish(False, suppress_errors=True)
            raise

    def _finish(self, success: bool, *, suppress_errors: bool) -> None:
        """Close owned state and deliver the completion notification once."""

        foreign_process = os.getpid() != self._owner_pid
        with self._lock:
            if self._closed and self._completion_notified:
                return
            source = self._source
            self._source = None
            self._closed = True
            if self._iterator_active and success:
                success = False
            success = success and self._verified
            callback: CompletionCallback | None = None
            if not self._completion_notified:
                self._completion_notified = True
                if not foreign_process:
                    callback = self._completion_callback
            self._completion_callback = None

        first_error: BaseException | None = None
        if source is not None:
            try:
                source.close()
            except BaseException as exc:
                first_error = exc
                success = False
        if callback is not None:
            try:
                callback(success)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None and not suppress_errors:
            raise first_error

    def close(self) -> None:
        """Close the descriptor and complete its lease exactly once."""

        with self._lock:
            success = self._verified and not self._iterator_active
        self._finish(success, suppress_errors=False)

    def __enter__(self) -> "OwnedStemSource":
        if self.closed:
            raise ValueError("owned stem source is closed")
        return self

    def __exit__(self, *exc_info: object) -> None:
        if exc_info and exc_info[0] is not None:
            self._finish(False, suppress_errors=True)
        else:
            self.close()

    def __del__(self) -> None:
        try:
            self._finish(False, suppress_errors=True)
        except BaseException:
            pass


__all__ = [
    "CompletionCallback",
    "MAX_STEM_BLOCK_FRAMES",
    "OwnedStemSource",
    "StemBlockSource",
    "StemSourceError",
]
