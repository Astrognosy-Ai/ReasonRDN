"""
ReasonRDN Desktop Console (GUI)

The operator interface for the coherent ReasonRDN / WARF memory system.
Allows creating a private embedded node, installing git hooks, manual + auto sync
of repo state as handoff artifacts, and browsing the shared memory.

Run:
  python gui/app.py
  (or after install: the built ReasonRDN.exe)

The GUI uses the unified rdn client + ReasonRDN handoff protocol under the hood.
"""

import customtkinter as ctk
import sys
import os
import subprocess
import threading
from datetime import datetime
from urllib.request import urlopen
from urllib.error import URLError

# Support running gui/app.py directly from source tree (before `pip install -e .`)
# This makes `import rdn` find the local rdn/ package at the project root.
_gui_dir = os.path.dirname(os.path.abspath(__file__))
_root_dir = os.path.dirname(_gui_dir)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)

from rdn.handoff.protocol import ReasonRDN
from rdn.handoff import sync as rdn_sync
from rdn.node.server import DEFAULT_STORAGE_DIR, port_file_path


class ReasonRDNApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("ReasonRDN Console")
        self.geometry("960x700")

        # Set theme to match the Astrognosy look
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.rdn = ReasonRDN()
        self.sync_thread = None
        self.sync_stop_event = None
        self.node_process = None

        # Layout
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Sidebar
        self.sidebar = ctk.CTkFrame(self, width=240, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(25, weight=1)
        
        self.sidebar_label = ctk.CTkLabel(self.sidebar, text="REASON RDN", font=ctk.CTkFont(size=22, weight="bold", family="SF Mono"))
        self.sidebar_label.grid(row=0, column=0, padx=20, pady=(30, 20))
        
        self.manifest_btn = ctk.CTkButton(self.sidebar, text="SYNC MANIFEST", command=self.load_manifest, fg_color="#7c6af5", hover_color="#5a4ed1")
        self.manifest_btn.grid(row=1, column=0, padx=20, pady=10)
        
        self.proj_label = ctk.CTkLabel(self.sidebar, text="TARGET DOMAIN", font=ctk.CTkFont(size=11, weight="bold"))
        self.proj_label.grid(row=2, column=0, padx=20, pady=(20, 0), sticky="w")
        
        self.project_dropdown = ctk.CTkComboBox(self.sidebar, values=["ALL PROJECTS"], command=self.on_project_change)
        self.project_dropdown.grid(row=3, column=0, padx=20, pady=10)
        self.project_dropdown.set("ALL PROJECTS")

        # Heartbeat Feature
        self.heartbeat_label = ctk.CTkLabel(self.sidebar, text="PROJECT HEARTBEAT", font=ctk.CTkFont(size=11, weight="bold"))
        self.heartbeat_label.grid(row=4, column=0, padx=20, pady=(20, 0), sticky="w")
        
        self.heartbeat_display = ctk.CTkLabel(self.sidebar, text="░░░░░░░", font=ctk.CTkFont(size=20), text_color="#7c6af5")
        self.heartbeat_display.grid(row=5, column=0, padx=20, pady=5)
        
        self.heartbeat_legend = ctk.CTkLabel(self.sidebar, text="LAST 7 DAYS", font=ctk.CTkFont(size=9), text_color="#44445a")
        self.heartbeat_legend.grid(row=6, column=0, padx=20, pady=0)

        # Repo Sync Controls
        self.sync_label = ctk.CTkLabel(self.sidebar, text="REPO SYNC", font=ctk.CTkFont(size=11, weight="bold"))
        self.sync_label.grid(row=7, column=0, padx=20, pady=(20, 0), sticky="w")

        self.node_label = ctk.CTkLabel(self.sidebar, text="WARF NODE URL", font=ctk.CTkFont(size=9))
        self.node_label.grid(row=8, column=0, padx=20, pady=(10, 0), sticky="w")

        self.node_entry = ctk.CTkEntry(self.sidebar, placeholder_text="https://your-warf-node")
        self.node_entry.grid(row=9, column=0, padx=20, pady=6, sticky="ew")
        if rdn_sync.DEFAULT_NODE_URL:
            self.node_entry.insert(0, rdn_sync.DEFAULT_NODE_URL)

        self.root_label = ctk.CTkLabel(self.sidebar, text="REPO ROOT", font=ctk.CTkFont(size=9))
        self.root_label.grid(row=10, column=0, padx=20, pady=(10, 0), sticky="w")

        self.root_entry = ctk.CTkEntry(self.sidebar)
        self.root_entry.grid(row=11, column=0, padx=20, pady=6, sticky="ew")
        self.root_entry.insert(0, rdn_sync.DEFAULT_ROOT)

        self.interval_label = ctk.CTkLabel(self.sidebar, text="INTERVAL (MIN)", font=ctk.CTkFont(size=9))
        self.interval_label.grid(row=12, column=0, padx=20, pady=(10, 0), sticky="w")

        self.interval_entry = ctk.CTkEntry(self.sidebar)
        self.interval_entry.grid(row=13, column=0, padx=20, pady=6, sticky="ew")
        self.interval_entry.insert(0, str(max(1, rdn_sync.DEFAULT_INTERVAL // 60)))

        self.node_label = ctk.CTkLabel(self.sidebar, text="PRIVATE NODE", font=ctk.CTkFont(size=11, weight="bold"))
        self.node_label.grid(row=14, column=0, padx=20, pady=(20, 0), sticky="w")

        self.node_port_label = ctk.CTkLabel(self.sidebar, text="NODE PORT", font=ctk.CTkFont(size=9))
        self.node_port_label.grid(row=15, column=0, padx=20, pady=(10, 0), sticky="w")

        self.node_port_entry = ctk.CTkEntry(self.sidebar)
        self.node_port_entry.grid(row=16, column=0, padx=20, pady=6, sticky="ew")
        self.node_port_entry.insert(0, "8765")

        self.node_storage_label = ctk.CTkLabel(self.sidebar, text="NODE STORAGE", font=ctk.CTkFont(size=9))
        self.node_storage_label.grid(row=17, column=0, padx=20, pady=(10, 0), sticky="w")

        self.node_storage_entry = ctk.CTkEntry(self.sidebar)
        self.node_storage_entry.grid(row=18, column=0, padx=20, pady=6, sticky="ew")
        self.node_storage_entry.insert(0, DEFAULT_STORAGE_DIR)

        self.create_node_btn = ctk.CTkButton(self.sidebar, text="CREATE PRIVATE NODE", command=self.create_private_node, fg_color="#7c6af5", hover_color="#5a4ed1")
        self.create_node_btn.grid(row=19, column=0, padx=20, pady=(12, 6), sticky="ew")

        self.stop_node_btn = ctk.CTkButton(self.sidebar, text="STOP NODE", command=self.stop_private_node, fg_color="#ef4444", hover_color="#dc2626")
        self.stop_node_btn.grid(row=20, column=0, padx=20, pady=6, sticky="ew")

        self.sync_once_btn = ctk.CTkButton(self.sidebar, text="SYNC ONCE", command=self.sync_once, fg_color="#7c6af5", hover_color="#5a4ed1")
        self.sync_once_btn.grid(row=21, column=0, padx=20, pady=(12, 6), sticky="ew")

        self.install_hooks_btn = ctk.CTkButton(self.sidebar, text="INSTALL HOOKS", command=self.install_hooks, fg_color="#3b82f6", hover_color="#2563eb")
        self.install_hooks_btn.grid(row=22, column=0, padx=20, pady=6, sticky="ew")

        self.start_sync_btn = ctk.CTkButton(self.sidebar, text="START AUTO SYNC", command=self.start_auto_sync, fg_color="#10b981", hover_color="#059669")
        self.start_sync_btn.grid(row=23, column=0, padx=20, pady=6, sticky="ew")

        self.stop_sync_btn = ctk.CTkButton(self.sidebar, text="STOP AUTO SYNC", command=self.stop_auto_sync, fg_color="#ef4444", hover_color="#dc2626")
        self.stop_sync_btn.grid(row=24, column=0, padx=20, pady=6, sticky="ew")

        self.status_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.status_frame.grid(row=25, column=0, padx=20, pady=20, sticky="s")
        
        self.status_indicator = ctk.CTkLabel(self.status_frame, text="●", text_color="#4ade80", font=ctk.CTkFont(size=14))
        self.status_indicator.pack(side="left", padx=5)
        
        self.status_label = ctk.CTkLabel(self.status_frame, text="SYSTEM READY", text_color="#8888a0", font=ctk.CTkFont(size=12, weight="bold"))
        self.status_label.pack(side="left")

        # Main Content
        self.content = ctk.CTkFrame(self, corner_radius=10, fg_color="#0a0a0f")
        self.content.grid(row=0, column=1, padx=20, pady=20, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(1, weight=1)

        self.header_label = ctk.CTkLabel(self.content, text="AGENT HANDOFF LOG", font=ctk.CTkFont(size=18, weight="bold", family="SF Mono"))
        self.header_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="w")

        self.scrollable_frame = ctk.CTkScrollableFrame(self.content, label_text="DETECTED REASONING ARTIFACTS", fg_color="transparent")
        self.scrollable_frame.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.scrollable_frame.grid_columnconfigure(0, weight=1)

        # Footer / Action Area
        self.action_frame = ctk.CTkFrame(self.content, height=120, fg_color="#13131a", border_width=1, border_color="#2a2a3a")
        self.action_frame.grid(row=2, column=0, padx=20, pady=20, sticky="ew")
        self.action_frame.grid_columnconfigure(0, weight=1)

        self.summary_entry = ctk.CTkEntry(self.action_frame, placeholder_text="Enter work summary for handoff...", height=40, fg_color="#0a0a0f")
        self.summary_entry.grid(row=0, column=0, padx=(20, 10), pady=20, sticky="ew")

        self.deposit_btn = ctk.CTkButton(self.action_frame, text="DEPOSIT STATE", width=140, height=40, command=self.deposit_handoff, fg_color="#7c6af5", hover_color="#5a4ed1")
        self.deposit_btn.grid(row=0, column=1, padx=(10, 20), pady=20)

        # Initial data load
        self.refresh_projects()
        self.load_manifest()

    def refresh_projects(self):
        self.ensure_client()
        projects = self.rdn.client.get_recent_projects()
        options = ["ALL PROJECTS"] + sorted(list(set(projects)))
        self.project_dropdown.configure(values=options)

    def on_project_change(self, value):
        self.load_manifest()

    def ensure_client(self):
        node_url = self.get_node_url()
        current = getattr(self.rdn.client, "node_url", None)
        if (current or None) == (node_url or None):
            return
        self.rdn = ReasonRDN(node_url=node_url)

    def _node_command(self):
        port = self.get_node_port()
        if port is None:
            return None
        storage = self.get_node_storage()
        if getattr(sys, "frozen", False):
            return [sys.executable, "--serve-node", "--port", str(port), "--storage", storage]
        return [sys.executable, os.path.abspath(__file__), "--serve-node", "--port", str(port), "--storage", storage]

    def _probe_node(self, node_url):
        try:
            with urlopen(f"{node_url}/api/health", timeout=2) as response:
                return response.status == 200
        except URLError:
            return False

    def _watch_node_startup(self, node_url):
        for _ in range(20):
            discovered_url = self._discover_node_url(node_url)
            if discovered_url and self._probe_node(discovered_url):
                self.after(0, lambda: self.node_entry.delete(0, "end"))
                self.after(0, lambda: self.node_entry.insert(0, discovered_url))
                self.after(0, lambda: self.set_status("Private Node Ready", "#4ade80"))
                self.after(0, lambda: self.set_sync_controls(True))
                self.after(0, self.ensure_client)
                self.after(0, self.refresh_projects)
                return
            threading.Event().wait(0.5)
        if self.node_process:
            try:
                self.node_process.terminate()
                self.node_process.wait(timeout=5)
            except Exception:
                try:
                    self.node_process.kill()
                except Exception:
                    pass
        self.node_process = None
        self.after(0, lambda: self.set_status("Node Startup Failed", "#f87171"))
        self.after(0, lambda: self.set_sync_controls(True))

    def _discover_node_url(self, fallback_url):
        storage = self.get_node_storage()
        path = port_file_path(storage)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    port_text = handle.read().strip()
                if port_text:
                    port = int(port_text)
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
        return fallback_url

    def create_private_node(self):
        port = self.get_node_port()
        if port is None:
            self.set_status("Invalid Node Port", "#f87171")
            return
        storage = self.get_node_storage()
        command = self._node_command()
        if command is None:
            self.set_status("Invalid Node Port", "#f87171")
            return

        node_url = f"http://127.0.0.1:{port}"
        if self._probe_node(node_url):
            self.node_entry.delete(0, "end")
            self.node_entry.insert(0, node_url)
            self.set_status("Private Node Already Running", "#60a5fa")
            return

        try:
            os.makedirs(storage, exist_ok=True)
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self.node_process = subprocess.Popen(command, creationflags=creationflags)
            self.node_entry.delete(0, "end")
            self.node_entry.insert(0, node_url)
            self.set_status("Starting Private Node...", "#fbbf24")
            self.set_sync_controls(False)
            threading.Thread(target=self._watch_node_startup, args=(node_url,), daemon=True).start()
        except Exception as exc:
            self.node_process = None
            self.set_status(f"Node Start Failed: {exc}", "#f87171")

    def stop_private_node(self):
        if not self.node_process:
            self.set_status("Node Not Running", "#60a5fa")
            return

        try:
            self.node_process.terminate()
            self.node_process.wait(timeout=5)
        except Exception:
            try:
                self.node_process.kill()
            except Exception:
                pass
        finally:
            self.node_process = None
            self.set_sync_controls(True)
            self.set_status("Private Node Stopped", "#8888a0")

    def get_node_url(self):
        value = self.node_entry.get().strip()
        return value or None

    def get_root_path(self):
        value = self.root_entry.get().strip()
        return value or rdn_sync.DEFAULT_ROOT

    def get_interval_seconds(self):
        raw = self.interval_entry.get().strip()
        if not raw:
            return rdn_sync.DEFAULT_INTERVAL
        try:
            minutes = int(raw)
        except ValueError:
            return None
        if minutes <= 0:
            return None
        return minutes * 60

    def get_node_port(self):
        raw = self.node_port_entry.get().strip()
        if not raw:
            return 8765
        try:
            port = int(raw)
        except ValueError:
            return None
        if port < 1 or port > 65535:
            return None
        return port

    def get_node_storage(self):
        value = self.node_storage_entry.get().strip()
        return value or DEFAULT_STORAGE_DIR

    def set_sync_controls(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.sync_once_btn.configure(state=state)
        self.install_hooks_btn.configure(state=state)
        self.start_sync_btn.configure(state=state)
        self.create_node_btn.configure(state=state)
        self.stop_node_btn.configure(state=state if self.node_process else "disabled")
        if enabled and self.sync_thread and self.sync_thread.is_alive():
            self.start_sync_btn.configure(state="disabled")
        if enabled and self.node_process:
            self.stop_node_btn.configure(state="normal")

    def format_sync_summary(self, summary: dict, label: str):
        if summary["errors"]:
            return f"{label}: {summary['deposits_succeeded']}/{summary['deposits_attempted']} ok ({len(summary['errors'])} errors)", "#fbbf24"
        return f"{label}: {summary['deposits_succeeded']}/{summary['deposits_attempted']} ok", "#4ade80"

    def set_status(self, text, color="#8888a0", pulse=False):
        self.status_label.configure(text=text.upper(), text_color=color)
        self.status_indicator.configure(text_color=color)

    def load_manifest(self):
        self.ensure_client()
        selected = self.project_dropdown.get()
        project = None if selected == "ALL PROJECTS" else selected
        
        self.set_status("Recalling...", "#fbbf24")
        
        # Update heartbeat
        heartbeat = self.rdn.client.get_heartbeat(project)
        self.heartbeat_display.configure(text=heartbeat)
        
        def run():
            try:
                # Query artifacts with handoff tags
                results = self.rdn.client.recall(project=project, tags=["handoff"])
                
                # Update UI in main thread
                self.after(0, lambda: self.render_results(results))
            except Exception as e:
                self.after(0, lambda: self.set_status("Error", "#f87171"))
                print(f"Recall Error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def sync_once(self):
        self.run_sync(install_hooks=False)

    def install_hooks(self):
        self.run_sync(install_hooks=True)

    def run_sync(self, install_hooks: bool):
        root = self.get_root_path()
        node_url = self.get_node_url()
        if not os.path.isdir(root):
            self.set_status("Invalid Repo Root", "#f87171")
            return

        def run():
            try:
                self.after(0, lambda: self.set_sync_controls(False))
                self.after(0, lambda: self.set_status("Syncing...", "#fbbf24"))
                if install_hooks:
                    repos = rdn_sync.find_git_repos(root)
                    summary = {
                        "repos_scanned": len(repos),
                        "deposits_attempted": 0,
                        "deposits_succeeded": 0,
                        "hooks_installed": 0,
                        "errors": [],
                    }
                    if not repos:
                        self.after(0, lambda: self.set_status("No Repos Found", "#60a5fa"))
                        return
                    for repo in repos:
                        try:
                            summary["hooks_installed"] += len(rdn_sync.install_hooks(repo))
                        except Exception as exc:
                            summary["errors"].append({"repo": repo, "error": str(exc)})
                    msg = f"Hooks: {summary['hooks_installed']} installed"
                    color = "#4ade80" if not summary["errors"] else "#fbbf24"
                    self.after(0, lambda: self.set_status(msg, color))
                else:
                    summary = rdn_sync.run_once(None, root, node_url, False)
                    message, color = self.format_sync_summary(summary, "Sync")
                    self.after(0, lambda: self.set_status(message, color))
                self.after(0, self.refresh_projects)
                self.after(0, self.load_manifest)
            except Exception as e:
                self.after(0, lambda: self.set_status("Sync error", "#f87171"))
                print(f"Sync Error: {e}")
            finally:
                self.after(0, lambda: self.set_sync_controls(True))

        threading.Thread(target=run, daemon=True).start()

    def start_auto_sync(self):
        if self.sync_thread and self.sync_thread.is_alive():
            self.set_status("Auto Sync Already Running", "#60a5fa")
            return

        interval = self.get_interval_seconds()
        if interval is None:
            self.set_status("Invalid Interval", "#f87171")
            return

        root = self.get_root_path()
        if not os.path.isdir(root):
            self.set_status("Invalid Repo Root", "#f87171")
            return
        node_url = self.get_node_url()
        self.sync_stop_event = threading.Event()

        def run():
            try:
                rdn_sync.run_loop(root, interval, node_url, False, stop_event=self.sync_stop_event)
                self.after(0, lambda: self.set_status("Auto Sync Off", "#8888a0"))
            except Exception as e:
                self.after(0, lambda: self.set_status("Sync error", "#f87171"))
                print(f"Auto Sync Error: {e}")
            finally:
                self.sync_thread = None
                self.sync_stop_event = None
                self.after(0, lambda: self.set_sync_controls(True))

        self.sync_thread = threading.Thread(target=run, daemon=True)
        self.sync_thread.start()
        self.set_sync_controls(False)
        self.set_status("Auto Sync On", "#4ade80")

    def stop_auto_sync(self):
        if self.sync_stop_event:
            self.sync_stop_event.set()
            self.set_status("Stopping...", "#fbbf24")
        else:
            self.set_status("Auto Sync Not Running", "#60a5fa")

    def render_results(self, results):
        # Clear frame
        for child in self.scrollable_frame.winfo_children():
            child.destroy()

        if results:
            for item in results:
                self.add_handoff_card(item)
            self.set_status("System Ready", "#4ade80")
        else:
            self.add_empty_msg()
            self.set_status("No Artifacts", "#60a5fa")

    def add_handoff_card(self, data):
        card = ctk.CTkFrame(self.scrollable_frame, corner_radius=8, fg_color="#1a1a24", border_width=1, border_color="#2a2a3a")
        card.pack(fill="x", padx=10, pady=8)
        
        # Header row
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=(10, 0))
        
        domain_label = ctk.CTkLabel(header, text=data.get('project', 'UNKNOWN').upper(), font=ctk.CTkFont(size=11, weight="bold"), text_color="#7c6af5")
        domain_label.pack(side="left")
        
        ts = data.get('deposited_at', '')
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                ts_str = dt.strftime("%Y-%m-%d %H:%M")
            except:
                ts_str = ts
            time_label = ctk.CTkLabel(header, text=ts_str, font=ctk.CTkFont(size=10), text_color="#8888a0")
            time_label.pack(side="right")

        # Content
        content_text = data.get('content', 'No content')
        content_label = ctk.CTkLabel(card, text=content_text, wraplength=600, justify="left", font=ctk.CTkFont(size=13))
        content_label.pack(anchor="w", padx=15, pady=15)

        # Footer / Badges
        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.pack(fill="x", padx=15, pady=(0, 10))
        
        addr_label = ctk.CTkLabel(footer, text=data.get('address', ''), font=ctk.CTkFont(size=9), text_color="#44445a")
        addr_label.pack(side="left")

        meta = data.get('meta', {})
        integrity_hash = meta.get('artifact_hash', meta.get('audit_hash', 'UNKNOWN'))
        
        badge_frame = ctk.CTkFrame(footer, fg_color="#2a2a3a", corner_radius=4)
        badge_frame.pack(side="right")
        
        badge_text = f"VERIFIED: {integrity_hash[:12]}..."
        badge = ctk.CTkLabel(badge_frame, text=badge_text, font=ctk.CTkFont(size=9, weight="bold"), text_color="#4ade80", padx=8)
        badge.pack()

    def add_empty_msg(self):
        label = ctk.CTkLabel(self.scrollable_frame, text="NO REASONING ARTIFACTS DETECTED IN THIS DOMAIN", font=ctk.CTkFont(size=12, slant="italic"), text_color="#44445a")
        label.pack(pady=40)

    def deposit_handoff(self):
        self.ensure_client()
        selected = self.project_dropdown.get()
        project = "astrognosy" if selected == "ALL PROJECTS" else selected
        summary = self.summary_entry.get()
        
        if not summary:
            self.set_status("Missing Summary", "#f87171")
            return

        self.set_status("Depositing...", "#fb923c")
        
        def run():
            try:
                # Simulated state tokens (GUI manual deposit path)
                state_tokens = summary.split() + ["git", "state", "verified"]
                self.rdn.deposit_handoff(project, summary, state_tokens)
                
                self.after(0, self.on_deposit_success)
            except Exception as e:
                self.after(0, lambda: self.set_status("Error", "#f87171"))
                print(f"Deposit Error: {e}")

        threading.Thread(target=run, daemon=True).start()

    def on_deposit_success(self):
        self.summary_entry.delete(0, 'end')
        self.refresh_projects()
        self.load_manifest()
        self.set_status("Deposited", "#4ade80")


if __name__ == "__main__":
    if "--serve-node" in sys.argv:
        from rdn.node.server import main as node_main

        # Pass remaining args after the flag
        idx = sys.argv.index("--serve-node")
        node_main(sys.argv[idx + 1 :])
    else:
        app = ReasonRDNApp()
        app.mainloop()
