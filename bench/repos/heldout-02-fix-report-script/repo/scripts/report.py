"""Print the total of the numbers in a small CSV file."""

import sys


def total(path):
    """Return the sum of the second column of a CSV file."""
    running = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split(",")
            running += int(parts[1])
    return running


def main(argv):
    print(total(argv[1]))


if __name__ == "__main__":
    main(sys.argv)
