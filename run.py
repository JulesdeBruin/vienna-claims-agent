#!/usr/bin/env python3
"""Entry point — seed sample data and launch the claims agent CLI."""

from claims_agent.seed_data import seed
from claims_agent.cli import main

if __name__ == "__main__":
    seed()
    main()
