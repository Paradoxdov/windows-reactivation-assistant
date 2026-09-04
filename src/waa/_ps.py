"""Minimal helper to run read-only PowerShell/CIM queries and get JSON back."""

import json
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000


def _powershell_path():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    # 32-bit process on 64-bit Windows: reach the native PowerShell.
    candidate = os.path.join(root, "Sysnative", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    return "powershell.exe"


def run_json(script, timeout=60):
    """Run a PowerShell snippet that emits JSON on stdout; return parsed data."""
    command = [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-OutputFormat", "Text",
        "-Command", script,
    ]
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if not out:
        raise RuntimeError("PowerShell returned no data (rc=%s): %s" % (proc.returncode, err[:400]))
    try:
        return json.loads(out)
    except ValueError as exc:
        raise RuntimeError("PowerShell returned non-JSON output: %s" % out[:400]) from exc
