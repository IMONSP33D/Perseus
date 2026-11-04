"""RCE addon: run model-written code in a disposable Windows 11 VM.

    run_windows_code ──ctx.test_windows_sandbox──▶ client ──loopback + token──▶ relay process ──X-API-Key──▶ WindowsRCE API ──▶ VM
    (perseus/tools)                   (this process)               (supervised)                 (VirtualBox host)

  upstream.py   the WindowsRCE REST API (/execute, /status, /revert), as the relay calls it
  relay.py      the separate process that owns the API key and enforces the [test_windows_sandbox] policy
  client.py     what tools get as ctx.test_windows_sandbox
  launch.py     boot-time decisions: start the relay or not, offer the tool or not

Why a relay, when WindowsRCE is already an API: the same rule as the code runner's broker.
Perseus's socket guard lets the engine reach only its own loopback services, and no API
key ever sits in the process that runs tools. The relay is also where the policy lives
(network on or off, run time caps, how long a VM may stay dirty), so the model can only
ask for what tooling.toml allows, whatever arguments it sends.

The VM normally runs on another machine (a Windows PC with VirtualBox), not on the GPU
box. The simplest link is the SSH session you already use for the dashboard, with a
reverse forward:  ssh -L 8080:127.0.0.1:8080 -R 8765:127.0.0.1:8765 you@gpu-box
"""
