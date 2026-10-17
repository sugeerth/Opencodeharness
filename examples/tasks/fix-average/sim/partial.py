def average(xs):
    return sum(xs) / len(xs)


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]
