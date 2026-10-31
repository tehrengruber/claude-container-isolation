#!/usr/bin/env python3
"""`python -m claude_isol`, for running from a checkout without installing.

claude_isol.py imports its backends relatively, so it cannot be executed as a
plain script path any more; this gives the same thing a module away.
"""
from .claude_isol import main

if __name__ == "__main__":
    main()
