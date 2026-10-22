"""Print the total of the numbers in a file."""

import sys


def total(path):
    """Return the sum of the numbers in the file."""
    running = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            running += int(line)
    return running


def main(argv):
    print(total(argv[1]))


if __name__ == "__main__":
    main(sys.argv)
