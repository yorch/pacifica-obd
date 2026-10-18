"""Server-side downsampling for the chart/replay endpoints (design §4.4).

A 1 Hz watch for an hour is 3600 points per signal; several signals would stall a
naive chart. The series endpoint never returns raw high-rate rows: it buckets to
at most a few thousand points. Two algorithms are available:

* :func:`lttb` — largest-triangle-three-buckets, which preserves visual shape
  (peaks/troughs) far better than naive sampling. This is the default.
* :func:`min_max_avg` — a max/min/average envelope per bucket, useful when the
  caller wants to know the extremes rather than the drawn curve.

Both are pure functions over ``[(t, value), ...]`` and are unit-tested.
"""

from __future__ import annotations

Point = tuple[float, float]


def lttb(points: list[Point], threshold: int) -> list[Point]:
    """Largest-Triangle-Three-Buckets downsampling (Steinarsson, 2013).

    ``threshold`` is the maximum number of points returned; when it is >= the
    input length the input is returned unchanged. Always keeps the first and last
    points so the series' extent is exact.
    """
    n = len(points)
    if threshold >= n or threshold < 3:
        return list(points)

    sampled: list[Point] = [points[0]]
    every = (n - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        # Average of the next bucket (the "third" point of the triangle).
        start = int((i + 1) * every) + 1
        end = min(int((i + 2) * every) + 1, n)
        if start >= end:
            start, end = min(start, n - 1), min(end, n)
        if start == end:
            avg_x, avg_y = points[min(start, n - 1)]
        else:
            avg_x = avg_y = 0.0
            for index in range(start, end):
                avg_x += points[index][0]
                avg_y += points[index][1]
            span = end - start
            avg_x /= span
            avg_y /= span

        point_a = points[a]
        start_range = int(i * every) + 1
        end_range = int((i + 1) * every) + 1
        if start_range >= end_range:
            end_range = min(start_range + 1, n - 1)
        chosen = start_range
        max_area = -1.0
        for index in range(start_range, min(end_range, n - 1)):
            area = abs(
                (point_a[0] - avg_x) * (points[index][1] - point_a[1])
                - (point_a[0] - points[index][0]) * (avg_y - point_a[1])
            )
            if area > max_area:
                max_area = area
                chosen = index
        sampled.append(points[chosen])
        a = chosen

    sampled.append(points[-1])
    return sampled


def min_max_avg(points: list[Point], buckets: int) -> list[Point]:
    """A min/max/average envelope per bucket (3 points per bucket)."""
    n = len(points)
    if buckets < 1 or n <= buckets * 3:
        return list(points)
    size = n / buckets
    out: list[Point] = []
    for i in range(buckets):
        start = int(i * size)
        end = min(int((i + 1) * size), n)
        if start >= end:
            continue
        chunk = points[start:end]
        values = [value for _, value in chunk]
        middle = chunk[len(chunk) // 2][0]
        low = min(chunk, key=lambda item: item[1])
        high = max(chunk, key=lambda item: item[1])
        out.append((low[0], low[1]))
        out.append((middle, sum(values) / len(values)))
        out.append((high[0], high[1]))
    return out


def downsample(points: list[Point], max_points: int) -> tuple[list[Point], str | None]:
    """Downsample to ``max_points`` (or fewer); returns ``(points, method)``."""
    if max_points <= 0 or len(points) <= max_points:
        return list(points), None
    return lttb(points, max_points), "lttb"
