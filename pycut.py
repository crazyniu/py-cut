#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified CLI/TUI entry point.

Use ./run.sh (Windows: run.cmd) to validate Python, dependencies, and media tools.
Configure material roots and main/backup gateways through .env or the local Web UI."""
import sys

from pycut.cli import main

if __name__ == "__main__":
    sys.exit(main())
