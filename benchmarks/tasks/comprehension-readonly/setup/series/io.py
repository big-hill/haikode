"""Reading and writing the one-number-per-line series format."""


def loads(text):
    values = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        values.append(float(line))
    return values


def dumps(values):
    return "\n".join("%g" % value for value in values) + "\n"
