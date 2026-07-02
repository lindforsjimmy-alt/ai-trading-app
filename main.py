"""Tunn wrapper för att använda `trading`-modulen från CLI.

Denna fil behålls för kompatibilitet med tidigare CLI-anrop.
"""

import sys
from trading import handle


if __name__ == "__main__":
    # Exempel: python main.py "price|AAPL"
    print(handle(sys.argv[1]))
