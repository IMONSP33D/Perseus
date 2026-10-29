"""How much network a sandboxed program gets: [code_runner] network.

    none      no network at all
    packages  package registries only (the default): pip, npm, cargo and Go module installs work,
              any other site is refused
    full      the open internet

This is separate from web search: the dashboard's Internet Access switch controls the model's
research (web_search, fetch_url), not what sandboxed programs can reach.

"packages" is enforced by the backend's firewall by host name (E2B: allow_out / deny_out). It is much
tighter than "full" but not sealed: DNS lookups still work, which is a slow side channel, and anything
published on these registries can be downloaded.
"""

from __future__ import annotations

LEVELS = ("none", "packages", "full")
DEFAULT = "packages"

PACKAGE_HOSTS = (
    "pypi.org", "files.pythonhosted.org",                        # pip
    "registry.npmjs.org",                                        # npm
    "crates.io", "index.crates.io", "static.crates.io",          # cargo
    "proxy.golang.org", "sum.golang.org",                        # go modules
    "repo.maven.apache.org", "repo1.maven.org",                  # maven
)

# what the model is told about the network, appended to run_code's description
NOTES = {
    "none": "Programs have no network access.",
    "packages": "Programs can install packages (pip, npm, cargo, go) but can't reach any other site.",
    "full": "Programs can reach the internet.",
}

# Start-up probe: real HTTPS requests (a firewall that filters by TLS host name can let a bare TCP
# connect through and drop the handshake, so a connect alone would misreport). Any HTTP response,
# even an error status, means the site was reachable.
PROBE_ARBITRARY = ("https://example.com/", "https://1.1.1.1/")   # must fail in "none" and "packages"
PROBE_PACKAGES = "https://pypi.org/simple/"                     # should work in "packages"

PROBE_PY = """\
import json, sys, urllib.error, urllib.request
out = {}
for url in json.loads(sys.argv[1]):
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=8).close()
        out[url] = True
    except urllib.error.HTTPError:
        out[url] = True
    except Exception:
        out[url] = False
print("PROBE " + json.dumps(out))
"""
