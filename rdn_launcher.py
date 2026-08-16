"""
rdn_launcher.py - Entry point for the one-stop rdn.exe

This script is the target for PyInstaller to create a single self-contained
rdn.exe that provides the full "start using rdn" experience.

Behavior:
- rdn.exe (double-click or no args) starts a local private node, launches the dashboard, and shows a system tray icon with controls for Dashboard, Node, MCP, and Quit.
- rdn.exe remember "foo"   provides full CLI functionality.
- rdn.exe start            is the same as no args.
- rdn.exe --help           shows CLI help.

This is the "one stop .exe" for Windows users.

Tray icon provides persistent control even after browser is closed.
Auto-starts the local node on first launch for the best "just works" experience.
"""

import os
import sys
import webbrowser
import time
import subprocess

# Tray support (will be collected by PyInstaller when building with --collect-all pystray PIL)
try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = None
    ImageDraw = None

# When frozen by PyInstaller, resources are in sys._MEIPASS
if getattr(sys, "frozen", False):
    BUNDLE_DIR = sys._MEIPASS
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))

# Global process handles
_node_proc = None
_mcp_proc = None
_tray_icon = None

def is_frozen():
    return getattr(sys, "frozen", False)

def get_streamlit_command():
    if is_frozen():
        python = sys.executable
        dash_script = os.path.join(BUNDLE_DIR, "rdn", "dash.py")
        return [python, "-m", "streamlit", "run", dash_script, "--server.port", "8501", "--server.headless", "true", "--browser.gatherUsageStats", "false"]
    else:
        return [sys.executable, "-m", "streamlit", "run", "rdn/dash.py", "--server.port", "8501"]

def start_local_node(port=8765):
    """Auto-start the embedded private node on first launch (one-stop behavior)."""
    global _node_proc
    if _node_proc and _node_proc.poll() is None:
        return _node_proc

    storage = os.path.join(os.path.expanduser("~"), ".reason-rdn", "private-node")
    os.makedirs(storage, exist_ok=True)

    cmd = [
        sys.executable, "-m", "rdn.node.server",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--storage", storage,
    ]

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        _node_proc = subprocess.Popen(
            cmd,
            cwd=BUNDLE_DIR if is_frozen() else os.getcwd(),
            creationflags=creationflags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[rdn] Auto-started local private node on port {port}")
        print(f"[rdn] Node (reason db / memory API) available at: http://127.0.0.1:{port}")
        time.sleep(1.2)  # wait for port file
        return _node_proc
    except Exception as e:
        print(f"[rdn] Could not auto-start local node: {e}")
        return None

def start_mcp_server():
    global _mcp_proc
    if _mcp_proc and _mcp_proc.poll() is None:
        print("[rdn] MCP server already running.")
        return
    cmd = [sys.executable, "-m", "rdn.mcp.server"]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        _mcp_proc = subprocess.Popen(cmd, cwd=BUNDLE_DIR if is_frozen() else os.getcwd(), creationflags=creationflags)
        print(f"[rdn] Started MCP server (PID {_mcp_proc.pid}). Configure your agents to use it.")
    except Exception as e:
        print(f"[rdn] Failed to start MCP: {e}")

def stop_mcp_server():
    global _mcp_proc
    if _mcp_proc:
        try:
            _mcp_proc.terminate()
            _mcp_proc.wait(timeout=3)
        except Exception:
            pass
        _mcp_proc = None
        print("[rdn] MCP server stopped.")

def open_dashboard(dash_port=8501, node_port=8765):
    """Launch Streamlit dashboard and reliably open the browser.
    
    Note on ports (this is by design):
      - Node (memory API / "reason db"): localhost:{node_port}
      - Dashboard (harness UI with metrics and explicit WARF actions): localhost:{dash_port}
    """
    print(f"\n[rdn] Starting the ReasonRDN dashboard (Streamlit on :{dash_port})...")
    print(f"[rdn] Local private node (memory backend) is on :{node_port}")
    print("[rdn] This is your one-stop view: live metrics (token savings, velocity, ship rate, positive signals),")
    print("[rdn] workflow suggestions, WARF arbitration, reason:// explorer, visual fingerprints, etc.")
    
    cmd = get_streamlit_command()
    
    env = os.environ.copy()
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    
    dashboard_url = f"http://localhost:{dash_port}"
    
    # Tell the dashboard what node port to expect (improves the status display in the UI)
    env["RDN_NODE_PORT"] = str(node_port)
    
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BUNDLE_DIR if is_frozen() else os.getcwd(),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        
        # Give Streamlit time
        time.sleep(5)
        
        # Check if the streamlit process died quickly — this is the usual cause of "localhost refused to connect"
        if proc.poll() is not None:
            out, err = proc.communicate()
            print("[rdn] Streamlit process exited early (this is almost always a missing-data bundling problem).")
            if err:
                print("=== streamlit stderr (first 2000 chars) ===")
                print(err.decode(errors="replace")[:2000])
            print(f"\nTry manually opening {dashboard_url} in a few seconds, or rebuild with the improved package.py.")
        else:
            print(f"[rdn] Dashboard server started. UI URL: {dashboard_url}")
            print(f"[rdn] Node (reason db): http://127.0.0.1:{node_port}")

            # Reliable open, especially from onefile console EXE on Windows
            try:
                if os.name == "nt":
                    os.startfile(dashboard_url)
                else:
                    webbrowser.open_new_tab(dashboard_url)
                print("[rdn] ✓ Attempted to open the dashboard in your browser.")
            except Exception as be:
                print(f"[rdn] Browser auto-open failed: {be}")
                print(f"[rdn] Please open manually: {dashboard_url}")

            print("\n[rdn] System tray (if shown) keeps control available.")
        
        print(f"[rdn] Dashboard server started. UI URL: {dashboard_url}")
        print(f"[rdn] Actual memory node (reason db / API) is at: http://127.0.0.1:{node_port}")
        
        # Reliable browser launch, especially important from a PyInstaller console .exe on Windows
        # We try hard because many users reported "no dash opening" from the EXE.
        opened = False
        try:
            if os.name == "nt":
                os.startfile(dashboard_url)  # Best method from console exes on Windows
                opened = True
            else:
                if webbrowser.open_new_tab(dashboard_url):
                    opened = True
        except Exception as browser_err:
            print(f"[rdn] Browser auto-open had an issue: {browser_err}")
        
        if opened:
            print("[rdn] ✓ Opened the dashboard in your default browser.")
        else:
            print(f"[rdn] Please manually visit: {dashboard_url}")
        
        print("\n[rdn] System tray icon (if present) gives persistent controls even after you close the browser tab.")
        
    except Exception as e:
        print(f"[rdn] Failed to launch dashboard: {e}")
        print(f"[rdn] Manual fallback: open {dashboard_url} after running the Streamlit command yourself if needed.")

def create_tray_icon():
    if not pystray or not Image:
        return None

    # Simple nice icon
    size = 64
    img = Image.new('RGB', (size, size), (10, 10, 15))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, size-6, size-6], fill=(124, 106, 245))
    draw.text((size//2-7, size//2-9), "R", fill="white")

    def menu_open(icon, item):
        open_dashboard()

    def menu_node(icon, item):
        start_local_node()

    def menu_mcp(icon, item):
        start_mcp_server()

    def menu_stop_mcp(icon, item):
        stop_mcp_server()

    def menu_quit(icon, item):
        global _node_proc, _mcp_proc
        if _node_proc:
            try:
                _node_proc.terminate()
            except Exception:
                pass
        if _mcp_proc:
            try:
                _mcp_proc.terminate()
            except Exception:
                pass
        icon.stop()
        sys.exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", menu_open, default=True),
        pystray.MenuItem("Start/Restart Local Node", menu_node),
        pystray.MenuItem("Start MCP Server (for agents)", menu_mcp),
        pystray.MenuItem("Stop MCP Server", menu_stop_mcp),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit rdn", menu_quit),
    )

    icon = pystray.Icon("rdn", img, "rdn - ReasonRDN", menu)
    return icon

def main():
    no_args = len(sys.argv) <= 1
    is_start = len(sys.argv) > 1 and sys.argv[1] in ("start", "dash", "--start")

    if no_args or is_start:
        print("\n" + "="*70)
        print("rdn - ReasonRDN one-stop launcher")
        print("="*70)
        print("""
Double-click (or run with no args / "start") does this automatically:
• Starts the local private node (memory API / "reason db") on port 8765
• Launches the ReasonRDN dashboard (metrics, token savings, velocity,
  ship rate, positive signals, workflow suggestions, WARF network controls, etc.)
• Tries very hard to open the dashboard in your browser
• Shows a system tray icon for ongoing control (re-open dash, restart node, start/stop MCP, quit)

Important ports (this exactly matches your report):
  • Node (the actual "reason db" / memory backend API):  http://127.0.0.1:8765
  • Dashboard (the UI/harness with metrics): http://localhost:8501

You saw the dashboard at 8501, which is the harness UI.
When you manually went to 8765 you saw the local node, which is also correct.
"No dash opening" = the auto browser launch was flaky from the console .exe (often because streamlit data wasn't bundled due to the warnings you saw).
The updated package.py below has much stronger --collect and --hidden-import flags so the next build should properly bundle streamlit + plotly and the dash should launch + auto-open reliably from the .exe.
""")

        # Local memory remains available in every network configuration.
        start_local_node()

        # Launch the visual dashboard.
        open_dashboard(dash_port=8501, node_port=8765)

        # Persistent system tray so the user has control even after closing the browser
        global _tray_icon
        if pystray:
            _tray_icon = create_tray_icon()
            if _tray_icon:
                print("[rdn] System tray icon is now active in your taskbar.")
                print("[rdn] Right-click it anytime for: Open Dashboard | Node | MCP | Quit")
                _tray_icon.run()  # This keeps the process alive
        else:
            print("[rdn] (Tray not available in this build. The dashboard should still be running.)")
            try:
                input("\n[rdn] Press Enter when you're done (the dashboard keeps running in the browser)...")
            except Exception:
                pass
        return

    # CLI dispatch
    try:
        from rdn.cli import main as cli_main
        sys.argv = ["rdn"] + sys.argv[1:]
        cli_main()
    except Exception as e:
        print(f"[rdn] CLI error: {e}")
        print("Try running the .exe with no arguments for the full dashboard experience.")
        try:
            input("Press Enter to exit...")
        except Exception:
            pass

if __name__ == "__main__":
    main()
