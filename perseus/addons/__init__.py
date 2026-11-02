"""Addons: optional integrations that plug extra machinery into Perseus.

An addon is everything a tool needs that a tool file isn't allowed to hold: a
service it talks to, the key for that service, and the process that owns both.
The tool itself still lives in perseus/tools/ (so it shows up under Addon Tools
on the Agent Tools page) and reaches its addon only through `ctx`.

    windows_rce/   runs code in a disposable Windows VM (the WindowsRCE sandbox API)
"""
