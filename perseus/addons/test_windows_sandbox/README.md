# test_windows_sandbox: the Windows VM

This addon ships with Perseus in `perseus/addons/test_windows_sandbox/`. Its settings are the `[test_windows_sandbox]` section of `Config/tooling.toml` (added there by `bash preflight.sh`); the general rules for addons are in [`docs/addons.md`](../../../docs/addons.md).

`run_windows_code` runs the model's code in a real Windows 11 VM, for anything Linux can't do: Win32 and .NET APIs, the registry, PowerShell, cmd, Windows DLLs. It talks to the **WindowsRCE sandbox API**, which runs on a Windows PC with VirtualBox and puts the VM back on a clean snapshot after every run. The model gets the outcome (`ok`, `compile_error`, `runtime_error` or `timeout`), the exit code, stdout and stderr, and any files it asked to collect.

Collected files come back as exact bytes, text or binary. To give you something the program built, the model collects its path (say `bin/Release/net10.0/win-x64/publish/app.exe`) and passes the ref the run reports (`windows:bin/Release/net10.0/win-x64/publish/app.exe`) to `output_file`. The file then shows up under the answer as a download card with its size and SHA-256, marked as an executable when it runs code. Unsigned executables get a warning from your browser or SmartScreen when you download or open them; that's expected for a fresh build.

Languages: Python, Node.js, Windows PowerShell 5.1, cmd and C# (.NET 10).

```
run_windows_code ─ctx.test_windows_sandbox─▶ relay ─X-API-Key─▶ WindowsRCE API (your PC) ─▶ Windows VM
                                              (on the GPU box; holds the key, enforces your settings)
```

The relay is a separate process, like the code runner's broker: the key never enters the process that runs tools, and the settings below are enforced there whatever the model asks for.

To set it up:

1. On the Windows PC, start the API and leave the window open:
   ```powershell
   cd "$HOME\VirtualBox VMs\rce-sandbox"
   powershell -ExecutionPolicy Bypass -File .\start-api.ps1
   ```
2. Make the API reachable from the GPU box. The simplest way is the SSH session you already use for the dashboard, with a reverse forward added:
   ```bash
   ssh -L 8080:127.0.0.1:8080 -R 8765:127.0.0.1:8765 you@your-gpu-box
   ```
   The API then answers at `http://127.0.0.1:8765` on the box, and it can stay bound to `127.0.0.1` on the PC (safer: code inside the VM can't reach it on the host-only network). The tool works only while that SSH session is open.
3. Add the API key to `Config/tooling.toml`:
   ```toml
   [test_windows_sandbox]
   api_key = "your WindowsRCE X-API-Key"
   ```
4. Run `bash preflight.sh`, then `python3 perseus.py`. On startup you'll see `✓ Windows VM: …`, or a line saying why it's off. If the tunnel isn't up yet, it says so, and runs work as soon as it is.

The settings in `[test_windows_sandbox]`:

| Setting | Default | What it does |
| --- | --- | --- |
| `url` | `http://127.0.0.1:8765` | Where the API answers, as seen from the GPU box |
| `allow_network` | `false` | Lets the model connect the VM to the internet for a run (`pip`, `npm`, NuGet). Separate from the Internet Access switch |
| `allow_keep_state` | `true` | Lets the model keep the VM's state between calls (install once, then use). Kept state is thrown away by `reset_windows_vm`, after `idle_revert_s` unused, and when Perseus stops |
| `run_timeout_s` | `120` | The longest run the model may ask for (up to 600) |
| `max_output_kb` | `64` | stdout + stderr kept per run |
| `max_collect_kb` | `512` | Total size of collected text the model can read per run |
| `max_download_mb` | `64` | Total size of collected files (text or binary) a run can hand to `output_file` for you to download (up to 128; `0` turns downloads off) |
| `idle_revert_s` | `600` | How long a VM with kept state may sit unused before it's reset |

Programs run as a local administrator over SSH with no visible desktop, so GUI windows never appear and anything waiting for keyboard input hangs until the timeout. The API runs one job at a time; other calls wait their turn. Every run is logged in `logs/test_windows_sandbox.log` with its job id and outcome, never the code or its output.

