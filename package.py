"""
package.py: Build the simplified rdn.exe Windows channel.

Per the SELECTIVE EXPANSION plan, the EXE is a honest "download and run"
channel for Windows users: CLI + local private node (8765) + system tray.
It deliberately does **not** try to embed the full Streamlit dashboard
(bundling proved difficult; the goal was simplicity).

What the resulting rdn.exe delivers:
- Double-click or `rdn.exe` (no args) starts the local node and tray
- Full CLI (remember with --tokens-used, status, xchange, start, etc.)
- Clear instructions to the reliable hero cross-platform path:
    pip install 'reason-rdn[full]' && rdn start
  (this gives the full metrics dashboard, real token accounting,
   suggestions, and first-run tour)

The EXE + zip are built with the current public on-ramp code for local memory
and optional broker participation.

Run:
    python package.py

Requirements:
    pip install pyinstaller pystray Pillow

Output:
    dist/rdn.exe
    dist/rdn-portable.zip

See README.md for architecture, quickstarts, and the public reason:// network.
"""

import subprocess
import sys
import os
import shutil

def build_one_stop_exe():
    print("Building the ONE-STOP rdn.exe (coherent agnostic harness)...\n")

    # Clean previous builds (important after seeing the collect warnings)
    for d in ["build", "dist", "rdn.spec"]:
        if os.path.exists(d):
            if os.path.isdir(d):
                shutil.rmtree(d)
            else:
                os.remove(d)
            print(f"Cleaned {d}")

    # The launcher is the single entry point for the EXE
    script_path = "rdn_launcher.py"

    if not os.path.exists(script_path):
        print(f"ERROR: {script_path} not found. Run from the project root.")
        sys.exit(1)

    # Discover the real on-disk locations of streamlit and plotly so we can force-add their data
    # This directly addresses the "skipping data collection ... as it is not a package" warnings
    import streamlit
    import plotly
    streamlit_dir = os.path.dirname(streamlit.__file__)
    plotly_dir = os.path.dirname(plotly.__file__)
    print(f"Streamlit dir: {streamlit_dir}")
    print(f"Plotly dir:    {plotly_dir}")

    # PyInstaller command for a nice one-file Windows EXE
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        # Use --console so CLI works when called with args from cmd.exe
        # Users who double-click will still get the launch behavior
        "--console",
        "--name", "rdn",
        "--add-data", f"rdn;rdn",
        "--add-data", f"bootstrap.py;.",
        # Find actual package dirs and add them explicitly: this bypasses the "not a package" skip
        # (we compute these paths a few lines above in the script)
        f"--add-data={streamlit_dir};streamlit",
        f"--add-data={plotly_dir};plotly",
        "--collect-all", "streamlit",
        "--collect-all", "plotly",
        "--collect-all", "rdn",
        "--collect-all", "pystray",
        "--collect-all", "PIL",
        # Many hidden imports needed for Streamlit to work inside the bundle
        "--hidden-import", "streamlit",
        "--hidden-import", "streamlit.web.cli",
        "--hidden-import", "streamlit.runtime",
        "--hidden-import", "streamlit.runtime.scriptrunner.script_runner",
        "--hidden-import", "streamlit.runtime.runtime",
        "--hidden-import", "streamlit.runtime.caching",
        "--hidden-import", "streamlit.runtime.state",
        "--hidden-import", "streamlit.web.server.server",
        "--hidden-import", "streamlit.web.server.routes",
        "--hidden-import", "plotly",
        "--hidden-import", "plotly.graph_objects",
        "--hidden-import", "plotly.subplots",
        "--hidden-import", "tornado",
        "--hidden-import", "tornado.web",
        "--hidden-import", "tornado.httpserver",
        "--hidden-import", "tornado.ioloop",
        "--hidden-import", "rdn.cli",
        "--hidden-import", "rdn.dash",
        "--hidden-import", "rdn.reason",
        "--hidden-import", "rdn.client",
        "--hidden-import", "rdn.mcp.server",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        # Exclude some heavy/unnecessary things to keep size reasonable
        "--exclude-module", "cryptography",
        "--exclude-module", "tkinter",
        "--paths", ".",
        script_path,
    ]

    print("Ensuring tray + dashboard extras are present for bundling...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pystray", "Pillow", "--quiet"])

    print("Running PyInstaller (this can take a minute or two)...")
    print("Command:", " ".join(cmd))
    print()

    try:
        subprocess.check_call(cmd)
        exe_path = os.path.join("dist", "rdn.exe")
        print("\n" + "=" * 70)
        print("✅ BUILD SUCCESSFUL: rdn.exe (simplified Windows channel) READY")
        print("=" * 70)
        print(f"\nExecutable: {exe_path}")
        print("\nWhat the EXE delivers (per selective plan: CLI + local node + tray only):")
        print("  • Double-click rdn.exe or `rdn.exe` (no args) starts the local node (8765 unless XCHANGE)")
        print("  • System tray for control (Open Dashboard, Start/Restart Node, Start/Stop MCP, Quit)")
        print("  • Full CLI: rdn.exe remember \"...\" --tags infra, rdn.exe --xchange status, rdn.exe start, etc.")
        print("  • Clear instructions printed for the hero cross-platform path:")
        print("      pip install 'reason-rdn[full]' && rdn start")
        print("    (This launches the full metrics dashboard + suggestions reliably.)")
        print("\nThe EXE is the honest Windows \"download and run\" channel for node + CLI + tray.")
        print("It does **not** embed the Streamlit dashboard (that was simplified because bundling the full")
        print("experience proved difficult and the goal was simplicity).")
        print("\nDistribute dist/rdn.exe (or the zip) for the Windows segment that wants a single file.")
        print("=" * 70)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Build failed: {e}")
        sys.exit(1)

    # Produce a portable zip alongside the single EXE (one stop distribution)
    try:
        import zipfile
        zip_path = os.path.join("dist", "rdn-portable.zip")
        exe_path = os.path.join("dist", "rdn.exe")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(exe_path, "rdn.exe")
            # Include a tiny readme for the zip
            readme = """rdn: Simplified Windows Channel (CLI + Node + Tray)

Double-click rdn.exe or run with no args:
- Auto-starts local private node (8765, unless XCHANGE mode)
- System tray for persistent control (Open Dashboard, Start/Restart Node, Start/Stop MCP, Quit)
- Full CLI (rdn.exe remember "...", rdn.exe --xchange status, rdn.exe start, etc.)

For the full metrics dashboard, suggestions, and harness experience:
  pip install 'reason-rdn[full]' && rdn start

This package is the local on-ramp plus bridge to the public WARF broker and
reason:// registry. Private scoring and promotion internals are not included.

See the root README for the current public architecture.
"""
            z.writestr("README.txt", readme)
        print(f"\n✓ Portable zip created: {zip_path}")
        print("   Distribute the zip or just the .exe: both are one-stop.")
    except Exception as e:
        print(f"Note: Could not create portable zip (non-fatal): {e}")

if __name__ == "__main__":
    build_one_stop_exe()
