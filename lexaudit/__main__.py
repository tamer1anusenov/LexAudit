"""Enable `python -m lexaudit`."""
import sys

from lexaudit.cli import main

if __name__ == "__main__":
    sys.exit(main())
