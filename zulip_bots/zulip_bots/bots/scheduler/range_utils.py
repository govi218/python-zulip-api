"""Pure time-range math — no I/O, no datetime, no timezone."""

from typing import List, Optional, Tuple

TimeRange = Tuple[int, int]  # (start_minutes, end_minutes)


def merge_ranges(ranges: List[TimeRange]) -> List[TimeRange]:
    """Sort and merge overlapping/adjacent time ranges."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges)
    merged = [sorted_ranges[0]]
    for start, end in sorted_ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def intersect_ranges(ranges_list: List[List[TimeRange]]) -> List[TimeRange]:
    """Intersect multiple lists of (start, end) time ranges."""
    if not ranges_list:
        return []
    result = ranges_list[0]
    for ranges in ranges_list[1:]:
        new_result: List[TimeRange] = []
        for r1 in result:
            for r2 in ranges:
                start = max(r1[0], r2[0])
                end = min(r1[1], r2[1])
                if start < end:
                    new_result.append((start, end))
        result = merge_ranges(new_result)
    return result


def subtract_ranges(
    available: List[TimeRange], busy: List[TimeRange]
) -> List[TimeRange]:
    """Subtract *busy* ranges from *available* ranges. Returns remaining free time."""
    busy = merge_ranges(busy)
    free: List[TimeRange] = []
    for avail_start, avail_end in available:
        cursor = avail_start
        for busy_start, busy_end in busy:
            if busy_end <= cursor or busy_start >= avail_end:
                continue
            if busy_start > cursor:
                free.append((cursor, min(busy_start, avail_end)))
            cursor = max(cursor, busy_end)
            if cursor >= avail_end:
                break
        if cursor < avail_end:
            free.append((cursor, avail_end))
    return free


def find_earliest_slot(
    ranges: List[TimeRange], duration_minutes: int
) -> Optional[TimeRange]:
    """Find the earliest (start, end) slot of at least *duration_minutes*."""
    for start, end in sorted(ranges):
        if end - start >= duration_minutes:
            return (start, start + duration_minutes)
    return None


def snap_to_granularity(
    ranges: List[TimeRange], granularity: int
) -> List[TimeRange]:
    """
    Snap range starts up to the next granularity boundary.

    For example, with granularity=30, a range starting at 15:01
    snaps to 15:30. A range starting at 15:00 stays at 15:00.
    Ranges that become empty after snapping are dropped.
    """
    snapped: List[TimeRange] = []
    for start, end in ranges:
        remainder = start % granularity
        if remainder == 0:
            snapped_start = start
        else:
            snapped_start = start + (granularity - remainder)
        if snapped_start < end:
            snapped.append((snapped_start, end))
    return snapped
