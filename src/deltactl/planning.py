"""Tag planning: tag construction, read coalescing, and PDU chunking.

``plan_reads`` coalesces overlapping and near-adjacent tags into read groups
(FC03 for D, FC02 for X, FC01 for M/Y) and ``plan_chunks`` splits a bulk
value range into PDU-sized chunks that never divide a single value across a
Delta memory-block boundary (D4095|D4096 or M1535|M1536).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .addressing import (
    Address,
    TagAddress,
    View,
    block_for,
    parse_tag,
    validate_span,
    wire_for,
)
from .errors import BlockBoundaryError, DeltaValueError
from .packing import DType
from .profiles import DEFAULT_PROFILE, SeriesProfile

__all__ = [
    "Chunk",
    "ReadGroup",
    "Tag",
    "make_tag",
    "make_tags",
    "plan_chunks",
    "plan_reads",
]

_PARTITION_ORDER = (3, 2, 1)


@dataclass(frozen=True)
class Tag:
    """One planned tag: display name, resolved address, dtype, register width."""

    name: str
    address: Address
    dtype: DType
    width: int
    deadband: float | None = None


@dataclass(frozen=True)
class ReadGroup:
    """One coalesced Modbus read: function code, wire start, count, and tags."""

    function: int
    start: int
    count: int
    tags: tuple[Tag, ...]


@dataclass(frozen=True)
class Chunk:
    """One PDU-sized chunk of a bulk range.

    ``first`` is the 0-based index of the chunk's first value, ``values`` the
    number of values, ``count`` registers (D) or bits (M/X/Y), and ``start``
    the wire address of the chunk's first value.
    """

    start: int
    count: int
    first: int
    values: int


def make_tag(
    text: str,
    default_dtype: DType | None = None,
    *,
    dtype: DType | None = None,
    profile: SeriesProfile | None = None,
) -> Tag:
    """Parse *text* into a validated :class:`Tag`.

    Bit tags (``D100.3``) and the M/X/Y devices are always ``DType.BIT``; an
    explicit non-BIT ``default_dtype`` for them is a :class:`DeltaValueError`.
    A plain D tag without an inline dtype resolves ``None`` to ``U16``. The
    inline dtype wins when present. The span is validated with
    ``validate_span`` so range and boundary errors surface before any I/O.
    """
    prof = profile or DEFAULT_PROFILE
    resolved_dtype = dtype if dtype is not None else default_dtype
    parsed = parse_tag(text, profile=prof)
    address = parsed.address
    bit_like = address.bit is not None or address.view is View.BIT
    inline = DType(parsed.dtype) if parsed.dtype is not None else None
    if bit_like:
        if resolved_dtype is not None and resolved_dtype is not DType.BIT:
            raise DeltaValueError(
                f"{parsed.display} is a bit tag: only DType.BIT is valid, got {resolved_dtype.value}"
            )
        target_dtype = DType.BIT
    else:
        if inline is not None:
            target_dtype = inline
        elif resolved_dtype is not None:
            target_dtype = resolved_dtype
        else:
            target_dtype = DType.U16
    width = target_dtype.width
    validate_span(address, width, write=False, profile=prof)
    return Tag(name=parsed.display, address=address, dtype=target_dtype, width=width)


def make_tags(
    texts: Sequence[str],
    default_dtype: DType | None = None,
    *,
    dtype: DType | None = None,
    profile: SeriesProfile | None = None,
) -> list[Tag]:
    """Build validated :class:`Tag` objects for every tag text in *texts*."""
    return [make_tag(text, default_dtype, dtype=dtype, profile=profile) for text in texts]


def plan_reads(
    tags: Sequence[Tag],
    *,
    max_registers: int = 100,
    max_bits: int = 256,
    max_gap: int = 10,
) -> list[ReadGroup]:
    """Coalesce *tags* into sorted, gap- and cap-aware read groups.

    Tags are partitioned by read function (FC03 for D, FC02 for X, FC01 for
    M/Y; partitions returned in that order) and sorted by wire start. A group
    grows while the next tag starts within ``max_gap`` of the group's span
    end, sits in the same split block, and stays within the PDU cap
    (``max_registers`` for FC03, ``max_bits`` for FC01/FC02). Overlaps are
    allowed and extend the span. Each group's ``tags`` tuple preserves the
    original input order.
    """
    # (wire start, wire end, input order, tag) per function code.
    intervals: dict[int, list[tuple[int, int, int, Tag]]] = {}
    for order, tag in enumerate(tags):
        function = tag.address.function
        start = tag.address.wire
        intervals.setdefault(function, []).append((start, start + tag.width, order, tag))

    groups: list[ReadGroup] = []
    for function in _PARTITION_ORDER:
        items = sorted(
            intervals.get(function, []),
            key=lambda item: (item[0], item[1]),
        )
        if not items:
            continue
        first_tag_addr = items[0][3].address
        cap = max_registers if first_tag_addr.view is View.WORD else max_bits

        chunk: list[tuple[int, int, int, Tag]] = []
        group_start = 0
        span_end = 0
        group_block = -1
        for start, end, order, tag in items:
            tag_block = tag.address.block
            if (
                chunk
                and tag_block == group_block
                and start <= span_end + max_gap
                and max(span_end, end) - group_start <= cap
            ):
                span_end = max(span_end, end)
                chunk.append((start, end, order, tag))
                continue
            if chunk:
                _append_group(groups, function, group_start, span_end, chunk)
            chunk = [(start, end, order, tag)]
            group_start, span_end, group_block = start, end, tag_block
        if chunk:
            _append_group(groups, function, group_start, span_end, chunk)
    return groups


def _append_group(
    groups: list[ReadGroup],
    function: int,
    start: int,
    end: int,
    chunk: list[tuple[int, int, int, Tag]],
) -> None:
    """Finalize one group, restoring the original input order of its tags."""
    ordered = sorted(chunk, key=lambda item: item[2])
    groups.append(
        ReadGroup(
            function=function,
            start=start,
            count=end - start,
            tags=tuple(item[3] for item in ordered),
        )
    )


def plan_chunks(
    address: TagAddress | Tag | None = None,
    value_width: int = 1,
    value_count: int = 1,
    max_registers: int = 100,
    max_bits: int = 256,
    profile: SeriesProfile | None = None,
    *,
    start: TagAddress | Tag | None = None,
) -> list[Chunk]:
    """Split a bulk value range into PDU-sized :class:`Chunk` objects.

    The whole span is range-checked first (``allow_block_crossing=True`` so
    bulk 16-bit reads may cross the split), then values are walked one by
    one: a single value straddling D4095|D4096 or M1535|M1536 raises
    :class:`BlockBoundaryError`, and a chunk ends when the next value would
    change block or exceed the PDU cap (``max_registers`` for D,
    ``max_bits`` for M/X/Y). This is the single place that guarantees a
    32-bit value is never divided across PDUs.
    """
    target = address if address is not None else start
    if target is None:
        raise ValueError("address or start must be specified")
    if isinstance(target, Address):
        addr = target
    elif hasattr(target, "address") and isinstance(target.address, Address):
        addr = target.address
    else:
        raise TypeError(f"expected Address, ParsedTag, or Tag, got {type(target)!r}")

    if value_count < 1:
        raise DeltaValueError(f"value_count must be at least 1, got {value_count}")
    if value_width < 1:
        raise DeltaValueError(f"value_width must be at least 1, got {value_width}")

    prof = profile or DEFAULT_PROFILE
    device = addr.device
    total = value_count * value_width if addr.view is View.WORD else value_count
    validate_span(addr, total, write=False, allow_block_crossing=True, profile=prof)
    cap = max_registers if addr.view is View.WORD else max_bits

    chunks: list[Chunk] = []
    first = 0
    values = 0
    first_block = -1
    for i in range(value_count):
        low = addr.index + i * value_width
        high = low + value_width - 1
        value_block = block_for(device, low, profile=prof)
        if value_block != block_for(device, high, profile=prof):
            split = next(
                idx
                for idx in range(low + 1, high + 1)
                if block_for(device, idx, profile=prof) != value_block
            )
            raise BlockBoundaryError(
                f"value {i} at {device.value}{low} spanning {value_width} "
                f"crosses the {device.value}{split - 1}|{device.value}{split} "
                f"block boundary"
            )
        if values and ((values + 1) * value_width > cap or value_block != first_block):
            chunks.append(
                Chunk(
                    start=wire_for(device, addr.index + first * value_width, profile=prof),
                    count=values * value_width,
                    first=first,
                    values=values,
                )
            )
            first, values = i, 0
        if not values:
            first_block = value_block
        values += 1
    if values:
        chunks.append(
            Chunk(
                start=wire_for(device, addr.index + first * value_width, profile=prof),
                count=values * value_width,
                first=first,
                values=values,
            )
        )
    return chunks
