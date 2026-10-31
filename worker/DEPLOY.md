# Donnie Bridge — setup guide

This puts the Donnie Bridge on **your own free Cloudflare account** as a
Worker. An MCP client (ChatGPT, Claude) then connects to it: it can look
up your project data — notes, allowlisted project files, PDFs and
images, staged posts — and leave task notes for your agent. The agent
stays the actor: the client asks, the agent does. The bridge is **read +
handoff only**; it cannot change anything in your accounts.

**Do the dashboard steps on a computer browser, not a phone** — one step
pastes a big file into Cloudflare's code editor, and that editor is
genuinely painful on a phone screen.

**Time:** about 15 minutes, one time. All names/ids below in
`<angle brackets>` are placeholders — use your own.

---

## Step 0 — Configure and generate

On your own machine:

1. Edit the **CONFIGURATION** block at the top of `gen_worker.py`:
   - `BRIDGE_NAME` / `WORKER_NAME` — your names for the bridge and the
     Cloudflare Worker script.
   - `WORKSPACE_ROOT` — your workspace (defaults to the bundled fake
     `../examples` so you can try the whole flow first; or set the
     `BRIDGE_WORKSPACE` environment variable).
   - `EXCLUDED_GOALS` — projects that must never enter the snapshot.
   - `TIMEZONE_NAME` — your timezone, e.g. `America/New_York`.
2. Prepare your **scrubbed corpus**: the generator embeds
   `<workspace>/corpus.md` — a trimmed, work-only notes file you write
   on purpose. **Never point it at raw private notes.** Personal
   details, credentials, and private paperwork do not belong in a file
   that leaves your machine. The same rule covers anything you place in
   the allowlisted folders, binaries included — only put a PDF or image
   there if it is meant to cross the bridge.
3. **If you're trying the bundled examples first:** create the demo
   binaries (they're stored as base64 inside the script so the repo
   stays text-only):
   ```sh
   python3 examples/make_demo_files.py
   ```
4. Generate:
   ```sh
   cd worker
   python3 gen_worker.py
   ```
   This writes `dist/worker.js` and prints a report: what was embedded
   (text files, and each binary blob with its type and size), what was
   dropped by the secret-pattern tripwire, and what was **skipped**
   because it broke the binary size caps.
5. Optional but recommended: `node test_worker.mjs` runs the test
   harness against the Worker you just generated.

### About PDFs and images (size caps)

Binary files (`.pdf`, `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) inside
the allowlist are embedded as base64 and served by the
`read_project_file_bytes` tool. Two caps in `gen_worker.py` bound them:

- `MAX_BINARY_FILE_BYTES` — default 1,500,000 bytes per file
- `MAX_BINARY_TOTAL_BYTES` — default 4,000,000 bytes total

Base64 inflates bytes by ~33%, and the finished `worker.js` must fit
Cloudflare's Worker size limit for your plan — so binaries eat your
budget fast. Over-cap files are skipped with a printed warning, never
silently. Anything genuinely large belongs outside the snapshot model:
host it elsewhere and link it from a note.

## Step 1 — Create the KV namespace

Handoffs and OAuth records live in KV. Without it the bridge still
answers reads, but handoff notes can't be stored and the OAuth door
refuses to run.

1. In the Cloudflare dashboard: **Storage & Databases → KV → Create
   namespace**. Name it e.g. `donnie-bridge-kv`.
2. Note its **namespace ID** — `deploy_preserve.py` needs it later
   (`BRIDGE_KV_NAMESPACE_ID`), and you'll pick the namespace from a list
   when binding it in Step 3.

## Step 2 — Create the Worker

1. **Workers & Pages → Create → Start with Hello World!**
2. Name it exactly what you set as `WORKER_NAME` (default
   `donnie-bridge`).
3. Click **Deploy**. Cloudflare puts up a starter Worker — you're about
   to replace its code.

## Step 3 — Put the bridge code in

**Option A — paste (simplest):**

1. On the Worker's page, click **Edit code**, select **everything**
   (Ctrl+A / Cmd+A), delete it.
2. Paste the full contents of your generated `dist/worker.js`.
3. Click **Save and Deploy**.
4. Then bind KV: **Settings → Bindings → Add → KV namespace** —
   variable name `DONNIE_KV` (this exact name; the code reads it),
   namespace = the one from Step 1. Save / Deploy if prompted.

**Option B — script deploy (better for refreshes):**

`deploy_preserve.py` uploads the script **and redeclares the KV
binding**, so a redeploy never wipes it (a naive upload that omits
bindings drops them). Secrets are untouched either way.

```sh
export CLOUDFLARE_API_TOKEN=<token with "Workers Scripts: Edit">
export BRIDGE_KV_NAMESPACE_ID=<namespace id from Step 1>
python3 deploy_preserve.py upload <your-account-id> <worker-name> dist/worker.js
```

Use Option A for the very first deploy so the Worker exists, then
Option B for refreshes.

## Step 4 — Set your secrets

Secrets are values **you invent**; they go into the Worker settings and
nowhere else — never into this repo, never into the generated code.

On the Worker's page: **Settings → Variables and Secrets → Add →
Secret**, twice:

1. Name `BRIDGE_TOKEN` — a long random string (30+ characters). This is
   the static bearer token for clients that support a custom
   Authorization header.
2. Name `OAUTH_OWNER_KEY` — a different long random string. This is the
   key **you** type on the bridge's consent page to approve a new
   client connection. Until it is set, the OAuth door stays locked —
   every approval is denied, by design.

Save, and click **Deploy** if Cloudflare offers it (secrets take effect
on deploy).

## Step 5 — Connect a client

Your Worker URL looks like
`https://<worker-name>.<your-subdomain>.workers.dev`, and the MCP
endpoint is that plus `/mcp`.

**ChatGPT** (computer browser): chatgpt.com → **Plugins / Settings →
Add custom MCP server** → name it after your bridge, server URL = your
`/mcp` URL, authentication **OAuth**. ChatGPT registers itself with the
bridge and sends you to its consent page; type your `OAUTH_OWNER_KEY`
and approve. No token is ever pasted into ChatGPT.

**Claude** (computer browser; custom connectors need a paid Claude
plan): claude.ai → **Settings → Connectors → Add custom connector** →
name + the same `/mcp` URL. **Leave the advanced OAuth client fields
empty** — Claude registers itself through the bridge's open
registration. Approve on the consent page with the same owner key.

> ⚠️ **Stop condition:** if a client's connect flow offers **no
> authentication at all** — no OAuth sign-in and nowhere to put a
> header — do not connect it. An unlocked bridge would show your
> snapshot to anyone who found the URL.

## Step 6 — Try it

Ask the connected client things like:

- "Use the bridge: what's the current rotation item?" (`donnie_status`)
- "Search the project notes for `lighthouse`." (`search_memory`)
- "Read the demo project plan." (`read_project_file`)
- "Show me the demo image / the demo PDF." (`read_project_file_bytes`)
- "Hand the agent a note: remind me about the demo timer."
  (`handoff_to_donnie`)

If the client reports the tools are unreachable or unauthorized, the
usual suspects are: a secret that wasn't deployed (Step 4), or a URL
missing `/mcp`.

---

## Day-to-day

- **Refresh the data:** rerun `python3 gen_worker.py`, then redeploy
  (Option B, or re-paste). The token, owner key, and KV binding survive
  refreshes — you never redo Steps 1–5.
- **Rotate the token:** invent a new string, replace the `BRIDGE_TOKEN`
  secret, deploy, and update any client that uses header auth. OAuth
  clients are unaffected; to cut off an OAuth client, delete its
  `oauth:…` records from the KV namespace in the dashboard.
- **Change who can approve connections:** replace `OAUTH_OWNER_KEY`.
- **Take it down:** Cloudflare dashboard → the Worker → Settings →
  Delete. Everything lives in your account, under your control.

## Good to know

- **The data is a snapshot.** The Worker serves your project data as of
  the date baked into it (`donnie_status` reports it). It does not read
  live files.
- **Handoffs are not live.** A note waits in KV (or in the client's
  reply, without KV) until the agent's next sync or until you paste it
  over. Nothing happens the second a client sends it.
- **Binaries are budgeted, not unlimited.** See the caps in Step 0. If
  the generator reports skipped binaries, shrink them or host them
  elsewhere — don't just raise the caps without checking your plan's
  Worker size limit.
