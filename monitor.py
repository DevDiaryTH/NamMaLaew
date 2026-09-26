#!/usr/bin/env python3
"""Backwards-compatible entry point — delegates to wlm.runner.

The launchd plist (com.user.water-level-monitor.plist) still invokes this
file directly; it now simply calls the new package main.
"""
from wlm.runner import main

if __name__ == "__main__":
    main()
