"""Private helpers for audited event-free built-in render blocks.

The decorator in this module does not make an instrument trusted by itself.
The renderer still admits an exact, explicitly enumerated built-in type and
checks the original method identities plus factory provenance at every event
boundary.  This helper only gives those classes one uniform block contract.

Sounding spans deliberately remain frame-by-frame.  A class may opt into
zero-filled silent spans only when its audited ``render_frame`` has no
event-free state transition, effect tail, control smoothing or other work
while ``active_voice_count`` is zero.
"""

from __future__ import annotations

from typing import Any, TypeVar

from .instrument import (
    _EVENT_FREE_RENDER_BLOCK_CONTRACT,
    _render_frame_block,
)


_InstrumentType = TypeVar("_InstrumentType", bound=type[Any])


def _constant_empty_frame_block(
    render_frame: Any,
    frame_count: int,
    *,
    sample_dtype: Any,
) -> Any:
    """Repeat one audited, state-free empty-voice frame exactly."""

    if isinstance(frame_count, bool) or not isinstance(frame_count, int):
        raise ValueError("render block frame_count must be an integer")
    if frame_count < 0:
        raise ValueError("render block frame_count must not be negative")

    import numpy as np

    dtype = np.dtype(sample_dtype)
    if dtype not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise ValueError("render block sample_dtype must be float32 or float64")
    block = np.empty((frame_count, 2), dtype=dtype)
    if frame_count:
        left, right = render_frame()
        block[:, 0] = left
        block[:, 1] = right
    return block


def audited_event_free_blocks(
    *,
    silence_safe: bool,
) -> Any:
    """Declare the unchanged frame renderer as an audited block backend.

    The returned decorator runs while the built-in class is defined, before
    external code can replace any method.  It records exact method objects so
    the renderer can later reject class mutation, instance monkeypatching,
    local factories and subclasses.
    """

    if type(silence_safe) is not bool:
        raise TypeError("silence_safe must be a bool")

    def decorate(instrument_type: _InstrumentType) -> _InstrumentType:
        namespace = instrument_type.__dict__
        handle_event = namespace.get("handle_event")
        render_frame = namespace.get("render_frame")
        active_voice_count = namespace.get("active_voice_count")
        if (
            not callable(handle_event)
            or not callable(render_frame)
            or active_voice_count is None
            or "render_block" in namespace
        ):
            raise TypeError(
                "audited event-free classes must define handle_event, "
                "render_frame and active_voice_count exactly once"
            )

        def render_block(
            self: Any,
            frame_count: int,
            *,
            sample_dtype: Any = "float64",
        ) -> Any:
            """Render an event-free span without changing sounding DSP."""

            if silence_safe and self.active_voice_count == 0:
                return _constant_empty_frame_block(
                    lambda: self.render_frame(),
                    frame_count,
                    sample_dtype=sample_dtype,
                )
            return _render_frame_block(
                lambda: self.render_frame(),
                frame_count,
                sample_dtype=sample_dtype,
            )

        render_block.__name__ = "render_block"
        render_block.__qualname__ = (
            f"{instrument_type.__qualname__}.render_block"
        )
        setattr(instrument_type, "render_block", render_block)
        setattr(
            instrument_type,
            "_tianlai_render_block_contract",
            _EVENT_FREE_RENDER_BLOCK_CONTRACT,
        )
        setattr(
            instrument_type,
            "_tianlai_original_handle_event",
            handle_event,
        )
        setattr(
            instrument_type,
            "_tianlai_original_render_frame",
            render_frame,
        )
        setattr(
            instrument_type,
            "_tianlai_original_render_block",
            render_block,
        )
        setattr(
            instrument_type,
            "_tianlai_original_active_voice_count",
            active_voice_count,
        )
        return instrument_type

    return decorate


__all__ = ["audited_event_free_blocks"]
