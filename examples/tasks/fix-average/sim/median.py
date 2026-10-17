def average(xs):
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    mid = len(xs) // 2
    if len(xs) % 2 == 0:
        return (xs[mid - 1] + xs[mid]) / 2
    return xs[mid]
