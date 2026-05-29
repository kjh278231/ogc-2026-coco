#!/usr/bin/env python3
"""
Thin launcher for the off-the-shelf `mcp-server-sqlite` package, used so that
.mcp.json can stay independent of where pip installed the user-script shim.

Install once:
    py -3.12 -m pip install --user mcp-server-sqlite

Launch (what .mcp.json does):
    py -3.12 tools/run_sqlite_mcp.py --db-path tools/ogc2026_runs.db
"""
from mcp_server_sqlite import main

if __name__ == "__main__":
    main()
