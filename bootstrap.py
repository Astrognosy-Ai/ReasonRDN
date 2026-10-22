#!/usr/bin/env python3
"""
Tiny bootstrap for ReasonRDN.

Run this one-liner (no prior install needed):

    python -c "$(curl -fsSL https://raw.githubusercontent.com/Astrognosy-Ai/ReasonRDN/public/bootstrap.py)"

Or save it and run:
    python bootstrap.py

This will:
1. Ensure the package is installed with the full experience (dash + mcp + cli)
2. Print the "start using rdn" one-liner
3. Optionally launch the harness dashboard immediately

It solves the "I want persistent memory + metrics now" problem for any agent or human.

After this, agents get:
- One import: import rdn as reason
- Auto Xchange federation (when you want it)
- Local usage counts (notes saved, lookups, token savings your agents report)
- The dashboard that makes the value visible

This is the on-ramp to the warf reasoning substrate. High-quality handoffs you create here can eventually become callable deterministic artifacts in the network.

The long-term vision: high-quality, addressable reasoning artifacts make inference feel like resolution.
"""

import os
import subprocess
import sys
import tempfile
import urllib.request

def main():
    print("\n" + "=" * 72)
    print("BOOTSTRAPPING REASONRDN")
    print("=" * 72)
    print("""
This tiny script gets you the full coherent experience:
• Persistent cross-agent memory (local + warf Xchange)
• Local artifact metadata + real metrics
• Local usage counts: notes saved, lookups, and token savings your agents report
• Workflow suggestions that improve how you ship
• One dashboard that shows local memory and metrics

The one-liner:
    pip install 'reason-rdn[full]' && rdn start
""")

    # Minimal dependency bootstrap - install the full experience
    print("Installing reason-rdn[full] (this gives you the dash, MCP, and coherent API)...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", "--upgrade",
            "reason-rdn[full]"
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✓ Installed.")
    except Exception as e:
        print(f"Install step had an issue (you may need to run pip manually): {e}")
        print("You can still do: pip install 'reason-rdn[full]'")

    print("\n" + "-" * 72)
    print("✅ You're ready.")
    print("\nThe easy 'start using rdn' one-liner:")
    print("    pip install 'reason-rdn[full]' && rdn start")
    print("\nThis launches the harness dashboard with metrics, suggestions, and Xchange controls.")
    print("\nOr run now:")
    print("    rdn start")
    print("\nIn code:")
    print("    import rdn as reason")
    print("    reason.remember(\"Fixed the critical thing...\", tags=[\"infra\"])")
    print("    print(reason.harness_metrics())   # real token savings + suggestions")
    print("\nWhen you're ready to use the public broker:")
    print("    REASON_USE_XCHANGE=1 rdn start")
    print("\nEvery high-quality artifact you create here helps turn")
    print(" inference into resolution across the reason:// network.")
    print("=" * 72 + "\n")

    # Optional auto-launch the dashboard.
    if "--no-launch" not in sys.argv:
        try:
            print("Launching the harness dashboard so you see the value immediately...")
            subprocess.run([sys.executable, "-m", "rdn.dash"], check=False)
        except Exception:
            print("Run `rdn start` manually to see the full visual experience.")

if __name__ == "__main__":
    main()
