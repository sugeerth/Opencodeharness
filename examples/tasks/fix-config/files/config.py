def load_config(path):
    out = {}
    for line in open(path):
        key, value = line.split("=")
        out[key] = value
    return out
