# ReasonRDN

**A local memory store for AI agents.** Save what your agents learn, find it again
in the next session, and give the answers worth keeping a fixed address that every
agent can look up.

Today, useful work disappears when an agent session ends. The next agent has to
rediscover what was decided and why. ReasonRDN keeps that knowledge on your
computer, in a small local database, so each run starts from what the last one
already figured out. When you are ready, the same answers can be shared with your
team or the wider community, one deliberate step at a time.

- **Remember and recall.** Save a note with `rdn remember`, find it later with `rdn recall`.
- **Fixed addresses.** Keep a reusable answer at an address such as
  `reason://ops/deployment/rollback-plan`, so any agent can look it up by name.
- **Local first.** Nothing leaves your computer unless you choose to share it.
- **Works where your agents work.** Command line, Python, a local web service, a
  dashboard, and a plug-in for AI coding tools.

## Quick start

### 1. Install

ReasonRDN needs Python 3.9 or newer. It is published on PyPI (the Python package
index) as `reason-rdn`.

**Linux and Mac**

```bash
python3 -m venv ~/.venvs/reason-rdn
source ~/.venvs/reason-rdn/bin/activate
pip install reason-rdn
```

**Windows (PowerShell)**

```powershell
py -m venv $HOME\.venvs\reason-rdn
& $HOME\.venvs\reason-rdn\Scripts\Activate.ps1
pip install reason-rdn
```

The first two lines create and switch on a virtual environment (a private folder
for this tool's Python packages). On Debian or Ubuntu, if `python3 -m venv` says
it is missing, run `sudo apt install python3-venv` first. Open a new terminal
later? Run the `source` (or `Activate.ps1`) line again before using `rdn`.

### 2. Save a note and find it again

```bash
rdn remember "Fixed the deployment race" --tags infra,deploy
rdn recall "deployment race"
```

`remember` prints the note's address. `recall` searches your notes by words and
prints the best matches. Both answer in JSON (structured text that other programs
can read), so agents can use them too.

### 3. The same thing from Python

Save this as `quickstart.py` and run `python quickstart.py`:

```python
import rdn

saved = rdn.remember("Use blue-green deploys for the web app", tags=["deploy"])
print("Saved at", saved["address"])

for hit in rdn.RDNClient().recall("blue-green deploys"):
    print("Found:", hit["content"])
```

### 4. Check what is set up

```bash
rdn status
```

This shows where your notes are stored and confirms that everything is local.

## Give an answer a fixed address

Notes from `remember` get an automatic address. When an answer is worth reusing,
give it a name you choose and look it up by that name:

```bash
rdn contribute "Pause writes, restore, verify, then switch traffic." \
  --uri reason://ops/deployment/rollback-plan
rdn resolve reason://ops/deployment/rollback-plan
```

From Python:

```python
import rdn

address = "reason://ops/deployment/rollback-plan"
artifact = rdn.resolve(address)
if artifact is None:
    rdn.contribute("Pause writes, restore, verify, then switch traffic.", reason_address=address)
    artifact = rdn.resolve(address)
print(artifact.content)
```

Addresses have exactly three lowercase parts: `reason://team/topic/task`.

This works fully offline in ReasonRDN 0.6.2 and newer. Until 0.6.2 is on PyPI,
install the newest code with
`pip install "git+https://github.com/Astrognosy-Ai/ReasonRDN.git@public"`.

## Sharing with a team (optional)

ReasonRDN looks things up in up to three layers, called scopes:

| Scope | Where it lives | Set up needed |
|---|---|---|
| `local` (default) | This computer | None |
| `organization` | Your team's Reason server | Point ReasonRDN at the server |
| `shared` | The public Reason Registry run by Astrognosy | Only used when you choose it |

To use a team server, tell ReasonRDN where it is:

```bash
export REASON_ORGANIZATION_RESOLVER=https://reason.example.org
export REASON_ORGANIZATION_API_KEY=your-team-key     # only if the server needs one
```

(On Windows PowerShell: `$env:REASON_ORGANIZATION_RESOLVER = 'https://reason.example.org'`.)

Then contribute to the team and look things up through every layer in order,
local first:

```bash
rdn contribute "Pause writes, restore, verify, then switch traffic." \
  --uri reason://ops/deployment/rollback-plan --scope organization
rdn resolve reason://ops/deployment/rollback-plan --source chain --scope organization
rdn queue            # see what is waiting to be sent
rdn queue --flush    # send it now
```

Team contributions are saved on your computer first and sent in the background,
with retries. If no organization server is set up, ReasonRDN says so plainly,
keeps the item on your computer (you can still `rdn resolve` it), and sends it
once you add a server. Nothing is ever sent to a layer you did not choose.

## Ways to use it

| Way | How to start it | Notes |
|---|---|---|
| Command line | `rdn --help` | `remember`, `recall`, `contribute`, `resolve`, `list`, `queue`, `status` |
| Python | `import rdn` | `rdn.remember`, `rdn.recall`, `rdn.contribute`, `rdn.resolve`, or `rdn.RDNClient()` |
| Local web service | `rdn-node` | Listens on `http://127.0.0.1:8765`. The command line and Python find it automatically. |
| Dashboard | `pip install "reason-rdn[dash]"`, then `rdn start` | Opens on `http://127.0.0.1:8501`. See below. |
| AI coding tool plug-in | `pip install "reason-rdn[mcp]"`, then `rdn-mcp` | See below. |
| Docker | `docker compose up` | Runs the web service and dashboard, reachable from this computer only. |

### Local web service

`rdn-node` runs a small web service that answers on this computer only. It offers:

- `GET /api/health`: is the service running
- `POST /api/remember` with JSON `{"content": "...", "tags": [...], "project": "..."}`: save a note
- `GET /api/recall?query=deployment`: search notes
- `GET /api/resolve?address=reason://team/topic/task`: look up one address

Use `rdn-node --port 9000` for another port. Set `REASON_RDN_TOKEN` on both the
service and its clients to require a password on every request.

### Dashboard

```bash
pip install "reason-rdn[dash]"
rdn start
```

The dashboard shows which services are actually running (it checks them, and
shows offline when they are not), what is stored on this computer, and forms to
save and look up notes. It only listens on this computer. Use
`rdn start --network` to let other computers on your network open it, and
`rdn start --port 8502` to pick another port. Online services are only contacted
when you tick "Check online services" in the sidebar or pass `--check-services`.

### Plug-in for AI coding tools

ReasonRDN speaks MCP (Model Context Protocol), the standard way AI coding tools
such as Claude Desktop, Cursor, and Codex call outside tools. It offers six
tools: `remember`, `recall`, `resolve`, `contribute`, `arbitrate`, and `status`.

```bash
pip install "reason-rdn[mcp]"
which rdn-mcp        # Windows: where.exe rdn-mcp
```

Use the full path that prints in place of `rdn-mcp` below if your tool cannot
find it.

Claude Desktop (`claude_desktop_config.json`) or Cursor (`.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "reason-rdn": { "command": "rdn-mcp" }
  }
}
```

Codex:

```bash
codex mcp add reason-rdn -- rdn-mcp
```

## Where your data lives

Everything is stored in `~/.reason-rdn` (on Windows, `.reason-rdn` in your user
folder). Set `REASON_RDN_HOME` to use another folder, for example one per project:

```bash
export REASON_RDN_HOME=~/projects/my-app/.reason-rdn
```

Optional settings can also go in `~/.reason-ecosystem.cfg` (a JSON file), or in
the file named by `REASON_RDN_CONFIG`.

## Developing ReasonRDN

```bash
git clone https://github.com/Astrognosy-Ai/ReasonRDN.git
cd ReasonRDN
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

The tests use a temporary folder for all data, so they never touch your real
notes. Add `mcp`, `dash`, or `doclang` to the brackets (for example
`".[dev,mcp,dash]"`) to work on those parts.

### Windows without Python on your PATH

If `py` and `pip` are not on your PATH, call Python by its full path instead:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
& $python -m pip install -e '.[dev,mcp]'
& $python -m pytest
& $python -m rdn.cli status
$server = Join-Path (Split-Path $python -Parent) 'Scripts\rdn-mcp.exe'
codex mcp add reason-rdn -- $server
```

## Going further

### Exact documents

Contributions keep the exact bytes and the file type you give them. Nothing is
reformatted:

```bash
rdn contribute --file ./artifact.xml --media-type application/xml \
  --uri reason://agents/knowledge/artifact
```

ReasonRDN also has an optional bridge for DocLang, a structured document format.
Install `".[doclang]"` to validate DocLang files, or `".[documents]"` to convert
other document files to DocLang with the Docling converter. Neither is
needed for normal use. See
[`docs/DOCLANG-INTEROPERABILITY.md`](docs/DOCLANG-INTEROPERABILITY.md) and the
runnable example, which includes a sample file:

```bash
python examples/resolve_doclang_contribute.py examples/runbook.dclg \
  --uri reason://ops/runbook/rollback
```

### Choosing between competing answers

When several agents propose different answers, WARF (Astrognosy's arbitration
service) compares them under a fixed scoring profile and returns a record of the
decision that anyone can check. It is a separate, explicit call:

```bash
rdn arbitrate "Best rollback plan?" \
  --package agent-a:"Pause writes, then restore" \
  --package agent-b:"Restore, then pause writes"
```

This contacts the WARF service online, so it only runs when you ask for it.

### How lookups work

- `rdn resolve ADDRESS` (or `source="local"`) reads only this computer.
- `--source registry` reads only the public Reason Registry.
- `--source chain --scope organization` tries local, then your team server, and
  stops at the first answer. `--scope shared` adds the public registry at the end.
- `--version sha256:...` asks for one exact version, at every layer.

### The protocol

`rdn/protocol-lock.json` is the single description of the formats ReasonRDN
reads and writes: the shape of a stored answer, the contribution format, and the
plug-in tools. The Python library, command line, and plug-in all follow it.

The `reason://` address format and the WARF arbitration protocol are submitted
as Internet-Drafts to the IETF (Internet Engineering Task Force, the group that
publishes internet standards):

- [reason:// draft](https://datatracker.ietf.org/doc/draft-westerbeck-reason-protocol/)
- [WARF draft](https://datatracker.ietf.org/doc/draft-westerbeck-warf-protocol/)

### Code map

```text
rdn/cli.py               command line
rdn/reason.py            high-level Python functions (rdn.remember, rdn.recall, ...)
rdn/client.py            local storage, lookups through each layer, send queue
rdn/artifact.py          stored answer format and version checks
rdn/contribution.py      contribution format
rdn/mcp/server.py        plug-in for AI coding tools
rdn/node/server.py       local web service
rdn/dash.py              dashboard
rdn/doclang_adapter.py   optional DocLang bridge
rdn/handoff/             session handoff helpers
```

More background: [`docs/CANONICAL.md`](docs/CANONICAL.md) (what this package
does and does not do) and
[`docs/AAIF-INTEROPERABILITY.md`](docs/AAIF-INTEROPERABILITY.md) (how it fits
with other open agent tools).

## License

Apache-2.0. See [LICENSE](LICENSE).
