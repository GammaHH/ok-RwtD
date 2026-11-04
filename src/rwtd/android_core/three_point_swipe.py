"""Continuous gesture using the owning task's official touch transport only."""
import time


def intersects(points, box):
    """Closed segment/rectangle intersection, including boundaries."""
    x, y, w, h = box
    for a, b in zip(points, points[1:]):
        low, high = 0.0, 1.0
        for origin, delta, lower, upper in [(a[0], b[0]-a[0], x, x+w), (a[1], b[1]-a[1], y, y+h)]:
            if delta == 0:
                if not lower <= origin <= upper:
                    high = -1
                    break
            else:
                enter, leave = sorted(((lower-origin)/delta, (upper-origin)/delta))
                low, high = max(low, enter), min(high, leave)
        if low <= high:
            return True
    return False


def perform(touch, points, duration_ms, clock=time.monotonic, sleep=time.sleep, *, bounds=(1080, 2400)):
    if len(points) != 3 or type(duration_ms) is not int or not 1 <= duration_ms <= 15000:
        raise ValueError("Require three points and duration 1..15000 ms")
    width, height = bounds
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ValueError('Invalid Android touch bounds')
    for point in points:
        if (len(point) != 2 or any(type(v) is not int for v in point)
                or not 0 <= point[0] < width or not 0 <= point[1] < height):
            raise ValueError("Three-point swipe point outside Android display")
    started, last = clock(), points[0]
    try:
        touch.down(*last)
        # Half the requested time for each leg; transport latency may extend it.
        for leg, samples in enumerate((8, 3)):
            a, b = points[leg], points[leg + 1]
            for sample in range(1, samples + 1):
                remaining = started + duration_ms / 1000 * (leg + sample / samples) / 2 - clock()
                if remaining > 0:
                    sleep(remaining)
                last = [round(a[i] + (b[i] - a[i]) * sample / samples) for i in range(2)]
                touch.move(*last)
    finally:
        touch.up(*last)  # Release on uncertain delivery; never retry automatically.
    return ""
