# Donnie Bridge

A small bridge that lets an MCP-capable AI chat client (ChatGPT, Claude)
**look up** a personal agent's project data and **leave it task notes** —
and nothing more. The agent ("Donnie") stays the actor: the connected AI
asks, Donnie does.

This repository is the **Cloudflare Worker edition**, published as a
template: it runs on your own free Cloudflare account, carries no one's
real data, and is meant to be renamed and reconfigured as yours.

**Scope: READ + HANDOFF only.** Nothing reachable through this bridge can
change anything in the owner's accounts. That is the design, not a
limitation to be patched later.

## What it does

| Tool | What it does |
| --- | --- |
| `donnie_status` | Today's date, the snapshot's date, the current rotation item, and an outline of the owner's project notes |
| `search_memory(query)` | Case-insensitive line search over the owner's **scrubbed** project-notes corpus |
| `read_project_file(path)` | Reads a text file (`.md` / `.txt` / `.json`), **only** from the allowlist (see below) |
| `read_project_file_bytes(path)` | Reads a binary file — PDF or image — from the **same** allowlist; images come back as image content, PDFs as an embedded resource blob |
| `list_staged_posts()` | Lists staged posts waiting to go out |
| `handoff_to_donnie(task)` | Leaves a task note for the agent (stored in KV when bound, always echoed back). It is **not** instant and not a live chat |

## Architecture

```
 your machine                          Cloudflare                AI client
┌───────────────────┐   generate    ┌──────────────────┐  MCP   ┌─────────┐
│ workspace          │ ───────────▶ │ Worker            │ ◀────▶ │ ChatGPT │
│  · corpus.md       │  gen_worker  │  · baked snapshot │  OAuth │ Claude  │
│  · goals/*/files/  │ ───────────▶ │  · OAuth door     │        └─────────┘
│  · notes/, staged/ │   deploy     │  · KV: handoffs,  │
└───────────────────┘              │    OAuth records  │
                                   └──────────────────┘
```

1. **Snapshot generator** (`worker/gen_worker.py`) reads an allowlisted
   slice of the owner's workspace and bakes it into a single
   self-contained `worker.js`. The deployed Worker serves this dated
   snapshot — never live files. Regenerate + redeploy to refresh.
2. **The Worker** answers MCP over HTTPS at `/mcp`, with two auth paths:
   a static bearer token the owner invents (`BRIDGE_TOKEN`), and a
   built-in **OAuth 2.1 door** (discovery metadata, dynamic client
   registration, PKCE S256, an owner-consent page gated by a second
   secret the owner invents, rotating refresh tokens with reuse
   detection). Tokens are stored only as SHA-256 hashes in KV.
3. **Handoffs** (`handoff_to_donnie`) drop notes into KV under
   `handoff-…` keys; the agent picks them up on its next sync, or the
   owner pastes the note into their chat with the agent.

### The allowlist

`read_project_file` and `read_project_file_bytes` can only reach:

- `goals/<project>/files/**` — text and binary files
- `notes/**` — markdown plans

…minus any project named in the generator's exclusion list, which never
enters the snapshot at all. Path traversal is rejected, and anything not
embedded in the snapshot simply doesn't exist as far as the bridge is
concerned.

### Files, PDFs, and pictures

Text files cross as text. Binary files — **`.pdf`, `.png`, `.jpg`,
`.jpeg`, `.webp`, `.gif`** — cross as base64 in a separate snapshot
section, served by `read_project_file_bytes`: images return as MCP
image content (the client can display them), PDFs and other binaries as
an embedded resource blob.

Size is the honest tradeoff of a baked snapshot: base64 inflates bytes
by ~33%, and the whole generated Worker must fit Cloudflare's size
limit for your plan. Two caps in the generator keep this sane:

- `MAX_BINARY_FILE_BYTES` — 1,500,000 bytes per file
- `MAX_BINARY_TOTAL_BYTES` — 4,000,000 bytes across the whole snapshot

Files over a cap are **skipped with a printed warning** during
generation — never silently dropped, never a crash. Very large files
don't belong in this model at all; link them from a note instead.

## Security model

- **Read + handoff only.** The tool set contains no action that writes
  to any account, file, or service of the owner's. The one write is the
  handoff note itself, stored in the bridge's own KV.
- **The connected AI can never act.** It can read the snapshot and leave
  notes. The agent decides what to do with a note, in the open, with
  the owner.
- **Scrub before you publish.** The snapshot embeds the owner's
  *prepared, work-only* notes corpus and allowlisted files into a file
  that leaves their machine. Personal details, credentials, and private
  paperwork never belong in it; excluded projects are skipped entirely,
  and a secret-pattern tripwire drops any text file that looks like it
  contains a credential. Binary files can't be pattern-scanned — only
  place binaries in the allowlist if they are meant to cross.
- **Fail-closed OAuth.** If the owner key secret is unset, no consent
  approval can ever succeed. If KV is unbound, every OAuth endpoint
  refuses.
- **Transparency rule.** No secret AI-to-AI talk: every exchange between
  the connected AI and the agent happens in the open, as a visible
  handoff note the owner can read. AIs are here to help humans, not
  each other. This sentence ships in the instructions the Worker serves
  to every connected client.

## Make it yours

Everything personal is a configuration placeholder at the top of
`worker/gen_worker.py` and `worker/deploy_preserve.py`:

- **Names** — `BRIDGE_NAME` and the tool prefix are the visible design;
  rename the bridge, and if you rename the agent, rename the tools
  (`donnie_status`, `handoff_to_donnie`, …) consistently in the
  generator's tool registry.
- **Your workspace** — point `WORKSPACE_ROOT` at your own notes/projects
  tree (or set `BRIDGE_WORKSPACE`), and write your own scrubbed
  `corpus.md`.
- **Your allowlist** — `PROJECTS_DIR`, `NOTES_DIR`, `STAGED_LISTINGS`,
  and the `EXCLUDED_GOALS` set define exactly what can cross. Start
  narrow.
- **Your ids** — `CLOUDFLARE_ACCOUNT_ID`, `KV_NAMESPACE_ID`
  (identifiers, not secrets — but a public fork shouldn't carry yours).

## Quick start

The template runs out of the box against the fake sample workspace in
`examples/`:

```sh
python3 examples/make_demo_files.py   # creates the demo PNG + PDF (kept as base64 in the script so the repo stays text-only)
cd worker
python3 gen_worker.py     # builds dist/worker.js from ../examples
node test_worker.mjs      # 60+ checks against the generated Worker
```

Then follow **`worker/DEPLOY.md`** to put it on your own Cloudflare
account and connect a client.

## Honest limits

- **It's a snapshot.** Data is exactly as fresh as the last time the
  owner regenerated and redeployed. `donnie_status` always states the
  snapshot date.
- **Handoffs aren't live.** A note waits until the agent's next sync or
  until the owner pastes it over.
- **Binaries are budgeted.** See the caps above; this is a notes bridge,
  not a file-transfer service.
- **Client support varies.** Custom MCP apps/connectors depend on each
  client's current flows and plan tiers; the bridge implements standard
  MCP + OAuth discovery so standards-following clients can connect.

## Files

- `worker/gen_worker.py` — snapshot generator (contains the Worker source template)
- `worker/deploy_preserve.py` — deploy helper that redeclares the KV binding so deploys never wipe it
- `worker/DEPLOY.md` — step-by-step setup guide
- `worker/test_worker.mjs` — Node test harness for the generated Worker
- `examples/` — a tiny fake workspace (demo corpus, project, staged posts, sample PNG + PDF) the generator runs against; `examples/make_demo_files.py` materializes the two demo binaries
