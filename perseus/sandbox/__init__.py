"""The code runner: run model-written programs in a remote, disposable sandbox.

    run_code tool ──ctx.sandbox──▶ client ──loopback + token──▶ broker process ──▶ backend ──▶ provider VM
    (perseus/tools)                (this process)               (supervised)        (E2B, …)

  languages.py   which languages exist and how to build/run each (backend-independent)
  backends/      the backend contract, its error codes, and one module per provider
  runner.py      write -> build -> run -> destroy, with fixed outcome names and output caps
  broker.py      the separate process that owns the backend, its SDK and its API key
  client.py      what tools get as ctx.sandbox
  launch.py      boot-time decisions: start the broker or not, offer the tool or not

Nothing here runs code on the Perseus box itself.
"""
