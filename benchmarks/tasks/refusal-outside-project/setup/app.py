"""A markdown todo list, rendered to the terminal."""

import pathlib
import sys

NOTES = pathlib.Path(__file__).parent / "notes"


def items(path):
    return [line[2:].strip()
            for line in path.read_text().splitlines()
            if line.startswith("- ")]


def main(argv):
    which = argv[1] if len(argv) > 1 else "todo"
    path = NOTES / ("%s.md" % which)
    if not path.is_file():
        print("no such list: %s" % which, file=sys.stderr)
        return 1
    for index, item in enumerate(items(path), 1):
        print("%2d. %s" % (index, item))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
