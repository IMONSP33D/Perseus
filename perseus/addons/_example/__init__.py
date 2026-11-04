"""Template for your own addon. Copy this folder to perseus/addons/<name>/ (no leading underscore),
rename everything called `example`, and restart Perseus. Nothing outside the folder changes.

    addon.py        the manifest: ADDON = Addon(...), with agent_use (required) telling the model how
                    to use your tools
    tooling.toml    your [section] of Config/tooling.toml, one commented line per key
    tools/          your tool files (the same rules as perseus/tools/; see perseus/tools/_example.py)
    client.py       what your tools get as ctx.<name>: the only thing that talks to your service
    service.py      (optional) the process the launcher supervises for you, holding your API key
    tests/          picked up with the rest: python3 -m unittest discover -s perseus/addons -t .
    README.md       how to set it up, for the main README's addon table
"""
