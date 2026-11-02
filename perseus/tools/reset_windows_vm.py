"""reset_windows_vm: put the RCE addon's Windows VM back on its clean snapshot (via ctx.windows).

The partner of run_windows_code: after calls with keep_vm_state=true, this throws the kept
state away. The relay also does it on its own after [windows_rce] idle_revert_s unused.
"""

from __future__ import annotations

from perseus.tools import ToolFailure, tool


@tool(description="Reset the Windows VM to its clean snapshot, throwing away everything earlier "
                  "run_windows_code calls kept with keep_vm_state=true. Call it when you are done with kept state.",
      parameters={"type": "object", "properties": {}},
      requires="windows_rce", timeout_s=1700)
def reset_windows_vm(ctx):
    if ctx.windows is None:
        raise ToolFailure("the Windows VM is off")
    ctx.windows.revert()
    return {"text": "The Windows VM is back on its clean snapshot.",
            "feed": {"pill": "VM reset", "tone": "", "summary": "Windows VM reverted to its clean snapshot"}}
