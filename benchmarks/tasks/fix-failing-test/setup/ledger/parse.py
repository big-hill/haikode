"""Parsing of the tiny ledger file format: `YYYY-MM-DD  description  amount`."""


class LedgerError(ValueError):
    pass


def parse_line(line):
    parts = line.strip().split()
    if len(parts) < 3:
        raise LedgerError("not enough fields in %r" % line)
    date, amount = parts[0], parts[-1]
    description = " ".join(parts[1:-1])
    try:
        value = float(amount)
    except ValueError as e:
        raise LedgerError("bad amount %r in %r" % (amount, line)) from e
    return {"date": date, "description": description, "amount": value}


def parse(text):
    entries = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entries.append(parse_line(line))
    return entries


def amounts(entries):
    return [entry["amount"] for entry in entries]
