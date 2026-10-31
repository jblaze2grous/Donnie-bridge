#!/usr/bin/env python3
"""Build dist/worker.js — the Donnie Bridge as ONE self-contained
Cloudflare Worker.

The generator reads a small, allowlisted set of sources from a workspace
on the owner's machine (a scrubbed notes corpus, allowlisted project
files, a rotation state file, staged-post listings) and injects them
into the Worker source as a JSON snapshot. The deployed Worker therefore
serves a dated snapshot, not live data — regenerate + redeploy to
refresh.

No credentials are ever embedded: the auth secrets live only in the
Worker's own settings (the BRIDGE_TOKEN and OAUTH_OWNER_KEY secrets,
which the owner sets in the Cloudflare dashboard — see DEPLOY.md). A
light secret-pattern scan runs over every embedded file as
defense-in-depth; flagged files are dropped, not embedded.

Usage:  python3 gen_worker.py     (writes dist/worker.js, prints a report)
"""

import base64
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ==========================================================================
# CONFIGURATION — make it yours.
# Every value below is a placeholder or a generic example. Replace them
# with your own before generating a Worker you actually deploy.
# ==========================================================================

BRIDGE_NAME = "Donnie Bridge"   # display name baked into the Worker
WORKER_NAME = "donnie-bridge"   # your Cloudflare Worker script name

# Deployment identifiers. These are consumed by deploy_preserve.py and
# the Cloudflare dashboard; they are listed here so every bridge setting
# lives in one place. They are NOT secrets, but they are yours — do not
# publish your real values in a public fork of this template.
CLOUDFLARE_ACCOUNT_ID = "YOUR_CLOUDFLARE_ACCOUNT_ID"
KV_NAMESPACE_ID = "YOUR_KV_NAMESPACE_ID"
KV_BINDING_NAME = "DONNIE_KV"   # binding name the Worker code reads

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"

# The workspace the snapshot is built from. Defaults to the bundled
# sample workspace (../examples) so the template runs out of the box.
# Point it at your real workspace (or set the BRIDGE_WORKSPACE env var)
# when you adopt the bridge.
WORKSPACE_ROOT = Path(
    os.environ.get("BRIDGE_WORKSPACE", HERE.parent / "examples")
).resolve()

# The ONLY notes file the snapshot embeds. This must be a trimmed,
# work-only corpus you prepare on purpose — the scrub-before-publish
# rule: personal details, credentials, and private-life notes never go
# in this file, because the generated Worker leaves your machine.
CORPUS_PATH = WORKSPACE_ROOT / "corpus.md"

PROJECTS_DIR = WORKSPACE_ROOT / "goals"   # <project>/files/** is allowlisted
NOTES_DIR = WORKSPACE_ROOT / "notes"      # second allowlisted tree (.md plans)
ROTATION_STATE = WORKSPACE_ROOT / "state" / "rotation_state.json"

# Directories whose file listings the list_staged_posts tool serves:
# (label shown to the connected AI, directory).
STAGED_LISTINGS = [
    ("Short-form video packages", WORKSPACE_ROOT / "staged" / "short-form"),
    ("Staged social posts", WORKSPACE_ROOT / "staged" / "social"),
]

# Projects whose files NEVER enter the cloud snapshot, even though they
# sit in an allowlisted location. Generic examples of why: a folder
# holding third-party contact details, or family/legal paperwork.
EXCLUDED_GOALS = {"private-demo"}

TIMEZONE_NAME = "UTC"  # e.g. "America/New_York" — used for snapshot dates

PER_FILE_CAP = 20000          # chars per embedded text file
SNAPSHOT_CAP = 700_000        # bytes of JSON for the TEXT part of the snapshot
TEXT_EXTS = {".md", ".txt", ".json"}

# Binary files (PDFs and images) under the SAME allowlist are embedded as
# base64 in a separate snapshot section and served by the
# read_project_file_bytes tool.
#
# WARNING: binaries inflate the generated Worker FAST — base64 adds ~33%
# on top of the raw bytes, and the finished worker.js must fit inside
# Cloudflare's Worker size limit for your plan. These caps are a budget,
# not a suggestion: keep the binary total small, and put anything large
# somewhere else (a file host, a drive link) instead of in the snapshot.
BINARY_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_BINARY_FILE_BYTES = 1_500_000   # a single PDF/image larger than this is skipped
MAX_BINARY_TOTAL_BYTES = 4_000_000  # binary budget for the whole snapshot
BINARY_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Crude tripwire for credential-shaped strings; a hit drops the file.
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|xox[bap]-|-----BEGIN|ghp_[A-Za-z0-9]{20,}"
    r"|AKIA[0-9A-Z]{16}|oauth:[a-z0-9]{20,}|Bearer\s+[A-Za-z0-9\-_]{25,})"
)


def read_capped(path: Path) -> str | None:
    """Read a text file, truncated at PER_FILE_CAP."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None
    if len(text) > PER_FILE_CAP:
        return text[:PER_FILE_CAP] + f"\n... [truncated at {PER_FILE_CAP} of {len(text)} chars]"
    return text


def collect_files() -> tuple[dict[str, str], dict[str, dict], list[str], list[str]]:
    """Collect the snapshot's file sections from the allowlist.

    Sources (the allowlist shared by read_project_file and
    read_project_file_bytes):
      - under <workspace>/goals/*/files/ : text (.md/.txt/.json) and
        binary (.pdf/.png/.jpg/.jpeg/.webp/.gif) files
      - markdown under <workspace>/notes/ (text only)
    Projects named in EXCLUDED_GOALS are skipped entirely — binaries get
    NO wider access than text.

    Returns (text_files, blobs, dropped, skipped_binaries):
      text_files       rel path -> text content
      blobs            rel path -> {"mime", "bytes", "data" (base64)}
      dropped          text files dropped by the secret-pattern tripwire
      skipped_binaries binaries skipped by the size caps (always reported
                       by the generator, never silently dropped)
    """
    files: dict[str, str] = {}
    blobs: dict[str, dict] = {}
    dropped: list[str] = []
    skipped: list[str] = []
    text_candidates: list[Path] = []
    binary_candidates: list[Path] = []
    if PROJECTS_DIR.is_dir():
        for path in sorted(PROJECTS_DIR.glob("*/files/**/*")):
            if path.relative_to(PROJECTS_DIR).parts[0] in EXCLUDED_GOALS:
                continue
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix in TEXT_EXTS:
                text_candidates.append(path)
            elif suffix in BINARY_EXTS:
                binary_candidates.append(path)
    if NOTES_DIR.is_dir():
        for path in sorted(NOTES_DIR.rglob("*.md")):
            if path.is_file():
                text_candidates.append(path)

    for path in text_candidates:
        rel = path.relative_to(WORKSPACE_ROOT).as_posix()
        if rel in files:
            continue
        text = read_capped(path)
        if text is None:
            continue
        if SECRET_RE.search(text):
            dropped.append(rel + " (secret-pattern tripwire)")
            continue
        files[rel] = text

    binary_total = 0
    for path in binary_candidates:
        rel = path.relative_to(WORKSPACE_ROOT).as_posix()
        if rel in blobs:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            skipped.append(rel + " (unreadable)")
            continue
        if len(raw) > MAX_BINARY_FILE_BYTES:
            skipped.append(
                f"{rel} ({len(raw)} bytes > per-file cap {MAX_BINARY_FILE_BYTES})")
            continue
        if binary_total + len(raw) > MAX_BINARY_TOTAL_BYTES:
            skipped.append(
                f"{rel} ({len(raw)} bytes would exceed total binary cap "
                f"{MAX_BINARY_TOTAL_BYTES})")
            continue
        binary_total += len(raw)
        blobs[rel] = {
            "mime": BINARY_MIME[path.suffix.lower()],
            "bytes": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
        }
    return files, blobs, dropped, skipped


def listing(directory: Path, tz: ZoneInfo) -> list[dict[str, str]]:
    """Staged-post listing entries: file name + modified stamp."""
    out = []
    if not directory.is_dir():
        return out
    for path in sorted(p for p in directory.iterdir() if p.is_file()):
        when = datetime.fromtimestamp(path.stat().st_mtime, tz).strftime("%Y-%m-%d %H:%M")
        out.append({"name": path.name, "modified": when})
    return out


def build_snapshot() -> tuple[dict, dict]:
    tz = ZoneInfo(TIMEZONE_NAME)
    now = datetime.now(tz)

    rotation: dict = {"last_index": None, "current_item": None,
                      "next_item": None, "item_count": 0}
    try:
        state = json.loads(ROTATION_STATE.read_text(encoding="utf-8"))
        items = state.get("tracks") or []
        idx = state.get("last_index")
        rotation["item_count"] = len(items)
        if isinstance(idx, int) and 0 <= idx < len(items):
            rotation["last_index"] = idx
            rotation["current_item"] = items[idx]
            rotation["next_item"] = items[(idx + 1) % len(items)]
    except Exception as exc:  # missing/corrupt — snapshot carries the error
        rotation["error"] = str(exc)

    files, blobs, dropped, skipped_binaries = collect_files()

    # Scrub-before-publish rule: the snapshot embeds ONLY the prepared
    # corpus file. There is deliberately no fallback to any raw notes
    # file — if the corpus is missing, the snapshot ships without notes
    # and the report says so.
    corpus_warning = None
    if CORPUS_PATH.exists():
        memory_text = CORPUS_PATH.read_text(encoding="utf-8")
    else:
        memory_text = ""
        corpus_warning = f"corpus file not found at {CORPUS_PATH} — snapshot has no notes"

    staged = []
    for label, directory in STAGED_LISTINGS:
        try:
            dir_label = "~/" + directory.relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            dir_label = str(directory)
        staged.append({
            "label": label,
            "dir": dir_label,
            "entries": listing(directory, tz),
        })

    snapshot = {
        "generated_at": now.strftime("%A, %B %d, %Y %I:%M %p %Z"),
        "generated_at_iso": now.isoformat(timespec="seconds"),
        "timezone": TIMEZONE_NAME,
        "rotation": rotation,
        "memory_text": memory_text,
        "files": files,
        "blobs": blobs,
        "staged": staged,
    }

    # Enforce the total snapshot budget: drop largest files first if needed.
    trimmed: list[str] = []
    while True:
        size = len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8"))
        if size <= SNAPSHOT_CAP or not snapshot["files"]:
            break
        biggest = max(snapshot["files"], key=lambda k: len(snapshot["files"][k]))
        trimmed.append(biggest + f" ({len(snapshot['files'][biggest])} chars)")
        del snapshot["files"][biggest]

    stats = {
        "files_embedded": len(snapshot["files"]),
        "files_dropped": dropped,
        "files_trimmed_for_size": trimmed,
        "blobs_embedded": {
            rel: {"mime": b["mime"], "bytes": b["bytes"]}
            for rel, b in snapshot["blobs"].items()
        },
        "blobs_total_bytes": sum(b["bytes"] for b in snapshot["blobs"].values()),
        "binaries_skipped": skipped_binaries,
        "corpus_warning": corpus_warning,
        "memory_chars": len(memory_text),
        "snapshot_bytes": len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")),
    }
    return snapshot, stats


# --------------------------------------------------------------------------
# Worker source template. __SNAPSHOT_JSON__ is replaced with the snapshot
# and __BRIDGE_NAME__ with the configured display name.
# Plain dependency-free ES module JavaScript (export default { fetch }).
# --------------------------------------------------------------------------
TEMPLATE = r'''// ============================================================================
// Donnie Bridge — Cloudflare Worker edition (GENERATED FILE — do not edit)
// Generated by gen_worker.py. Regenerate and redeploy to refresh the data.
// Everything served here was true as of SNAPSHOT.generated_at.
// Auth: every /mcp request must carry "Authorization: Bearer <token>". The
// token is either the Worker's BRIDGE_TOKEN secret or an OAuth access token
// issued by this Worker's own OAuth door (see below). No secret value and
// no issued token is ever present in this code — tokens live only as
// SHA-256 hashes in the KV binding.
// ============================================================================

const SNAPSHOT = __SNAPSHOT_JSON__;
const BRIDGE_NAME = __BRIDGE_NAME__;
const SNAPSHOT_TZ = SNAPSHOT.timezone || "UTC";

const PROTOCOL_VERSION = "2025-03-26";
const INSTRUCTIONS =
  "You are connected to Donnie, the owner's personal agent, through the " +
  "Donnie Bridge (Cloudflare Worker edition). These tools are read-only, " +
  "except handoff_to_donnie, which leaves Donnie a task note: he picks " +
  "it up on sync, or when the owner pastes the note into their chat with " +
  "Donnie. A handoff is not an instant action and not a live chat. " +
  "read_project_file_bytes returns embedded PDF and image files from " +
  "the same allowlist as base64 content. All " +
  "data comes from a snapshot generated " + SNAPSHOT.generated_at +
  " and may be stale. Donnie stays the actor — this bridge cannot change " +
  "anything in the owner's accounts. Transparency rule: no secret " +
  "AI-to-AI talk — Donnie never converses with another AI about the owner " +
  "or their work where the owner cannot see it; every AI-to-AI exchange " +
  "happens in the open, as a visible handoff note the owner can read. " +
  "AIs are here to help humans, not each other.";

function jsonResponse(body, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: { "Content-Type": "application/json" },
  });
}

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id: id, result: result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id: id, error: { code: code, message: message } };
}

function textResult(text, isError) {
  const r = { content: [{ type: "text", text: String(text) }] };
  if (isError) r.isError = true;
  return r;
}

function tokenOk(request, env) {
  const expected = env && env.BRIDGE_TOKEN ? String(env.BRIDGE_TOKEN) : "";
  if (!expected) return false;
  const provided = request.headers.get("Authorization") || "";
  const want = "Bearer " + expected;
  if (provided.length !== want.length) return false;
  let diff = 0;
  for (let i = 0; i < want.length; i++) {
    diff |= provided.charCodeAt(i) ^ want.charCodeAt(i);
  }
  return diff === 0;
}

// ============================================================================
// OAuth 2.1 authorization server
//
// The bridge is its own OAuth authorization server so MCP clients that only
// support OAuth sign-in (no custom header field — e.g. ChatGPT's current
// "Add custom MCP server" flow, and Claude's custom connectors, which
// register via DCR and use the claude.ai callback) can connect. Shape:
// RFC 9728 protected-resource metadata, RFC 8414 authorization-server
// metadata, RFC 7591 dynamic client registration (public clients, PKCE
// S256 only), an owner-consent page gated by the OAUTH_OWNER_KEY secret,
// and authorization-code + refresh-token grants with rotation and reuse
// detection.
//
// Fail closed throughout: if OAUTH_OWNER_KEY is not set, no approval can
// ever succeed; if the KV binding is missing, every OAuth endpoint
// refuses. Authorization codes and tokens are stored ONLY as SHA-256
// hashes in KV. The static BRIDGE_TOKEN path above stays untouched.
// ============================================================================

const ACCESS_TTL_MS = 60 * 24 * 3600 * 1000;   // access tokens: ~60 days
const REFRESH_TTL_MS = 180 * 24 * 3600 * 1000; // refresh tokens: ~180 days
const CODE_TTL_MS = 10 * 60 * 1000;            // authorization codes: 10 min

function ctEqual(a, b) {
  a = String(a);
  b = String(b);
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

function b64urlOfBytes(bytes) {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomB64url(nbytes) {
  const bytes = new Uint8Array(nbytes);
  crypto.getRandomValues(bytes);
  return b64urlOfBytes(bytes);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(text)
  );
  return Array.from(new Uint8Array(digest), function (b) {
    return b.toString(16).padStart(2, "0");
  }).join("");
}

async function pkceS256(verifier) {
  const digest = await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(verifier)
  );
  return b64urlOfBytes(new Uint8Array(digest));
}

function htmlEscape(s) {
  return String(s).replace(/[&<>"']/g, function (c) {
    return {
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c];
  });
}

function oauthJson(body, status) {
  return new Response(JSON.stringify(body), {
    status: status || 200,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

function oauthError(status, error, description) {
  const body = { error: error };
  if (description) body.error_description = description;
  return oauthJson(body, status);
}

function oauthStore(env) {
  const s = env && env.DONNIE_KV;
  return s && typeof s.get === "function" && typeof s.put === "function"
    ? s
    : null;
}

async function kvJson(store, key) {
  try {
    const raw = await store.get(key);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    return null;
  }
}

async function issueTokens(store, clientId, chainId) {
  const now = Date.now();
  if (!chainId) chainId = randomB64url(18);
  await store.put(
    "oauth:chain:" + chainId,
    JSON.stringify({ revoked: false, client_id: clientId }),
    { expirationTtl: Math.floor(REFRESH_TTL_MS / 1000) }
  );
  const accessToken = randomB64url(32);
  const refreshToken = randomB64url(32);
  await store.put(
    "oauth:access:" + (await sha256Hex(accessToken)),
    JSON.stringify({ client_id: clientId, chain: chainId, exp: now + ACCESS_TTL_MS }),
    { expirationTtl: Math.floor(ACCESS_TTL_MS / 1000) }
  );
  await store.put(
    "oauth:refresh:" + (await sha256Hex(refreshToken)),
    JSON.stringify({
      client_id: clientId, chain: chainId, exp: now + REFRESH_TTL_MS, used: false,
    }),
    { expirationTtl: Math.floor(REFRESH_TTL_MS / 1000) }
  );
  return {
    access_token: accessToken,
    token_type: "Bearer",
    expires_in: Math.floor(ACCESS_TTL_MS / 1000),
    refresh_token: refreshToken,
  };
}

function protectedResourceMetadata(request) {
  const origin = new URL(request.url).origin;
  return oauthJson({
    resource: origin + "/mcp",
    authorization_servers: [origin],
    bearer_methods_supported: ["header"],
    resource_name: BRIDGE_NAME,
  }, 200);
}

function authorizationServerMetadata(request) {
  const origin = new URL(request.url).origin;
  return oauthJson({
    issuer: origin,
    authorization_endpoint: origin + "/authorize",
    token_endpoint: origin + "/token",
    registration_endpoint: origin + "/register",
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["none"],
    authorization_response_iss_parameter_supported: true,
    scopes_supported: [],
  }, 200);
}

async function handleRegister(request, env) {
  const store = oauthStore(env);
  if (!store) {
    return oauthError(500, "server_error",
      "OAuth storage is not available on this bridge.");
  }
  let meta;
  try {
    meta = await request.json();
  } catch (e) {
    return oauthError(400, "invalid_client_metadata",
      "Request body must be JSON.");
  }
  const uris = meta && Array.isArray(meta.redirect_uris) ? meta.redirect_uris : [];
  const valid = uris.filter(function (u) {
    if (typeof u !== "string") return false;
    try {
      const parsed = new URL(u);
      if (parsed.hash) return false;
      return parsed.protocol === "https:" ||
        (parsed.protocol === "http:" &&
          (parsed.hostname === "localhost" || parsed.hostname === "127.0.0.1"));
    } catch (e) {
      return false;
    }
  });
  if (!uris.length || valid.length !== uris.length) {
    return oauthError(400, "invalid_redirect_uri",
      "redirect_uris must be absolute https URLs (http only for localhost), without fragments.");
  }
  const clientId = "db_" + randomB64url(18);
  const record = {
    client_id: clientId,
    client_name: String(meta.client_name || "Unnamed client").slice(0, 120),
    redirect_uris: valid,
    created_at: new Date().toISOString(),
  };
  await store.put("oauth:client:" + clientId, JSON.stringify(record));
  return oauthJson({
    client_id: clientId,
    client_name: record.client_name,
    redirect_uris: valid,
    token_endpoint_auth_method: "none",
    grant_types: ["authorization_code", "refresh_token"],
    response_types: ["code"],
    client_id_issued_at: Math.floor(Date.now() / 1000),
  }, 201);
}

function htmlResponse(body, status) {
  return new Response(body, {
    status: status || 200,
    headers: {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    },
  });
}

function oauthErrorPage(status, message) {
  return htmlResponse(
    "<!doctype html><html><head><meta charset=\"utf-8\">" +
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" +
    "<title>" + htmlEscape(BRIDGE_NAME) + " — sign-in</title></head>" +
    "<body style=\"font-family: system-ui, sans-serif; max-width: 34rem; " +
    "margin: 3rem auto; padding: 0 1rem;\">" +
    "<h1>" + htmlEscape(BRIDGE_NAME) + "</h1><p>" + htmlEscape(message) +
    "</p></body></html>",
    status
  );
}

function consentPage(v, errorMsg) {
  const hidden = [
    ["response_type", "code"],
    ["client_id", v.client.client_id],
    ["redirect_uri", v.redirectUri],
    ["state", v.state],
    ["code_challenge", v.challenge],
    ["code_challenge_method", "S256"],
  ].map(function (pair) {
    return '<input type="hidden" name="' + pair[0] + '" value="' +
      htmlEscape(pair[1]) + '">';
  }).join("");
  return "<!doctype html><html><head><meta charset=\"utf-8\">" +
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" +
    "<title>Connect to " + htmlEscape(BRIDGE_NAME) + "</title></head>" +
    "<body style=\"font-family: system-ui, sans-serif; max-width: 34rem; " +
    "margin: 3rem auto; padding: 0 1rem;\">" +
    "<h1>Connect to " + htmlEscape(BRIDGE_NAME) + "</h1>" +
    "<p><strong>" + htmlEscape(v.client.client_name) + "</strong> is asking " +
    "to connect to your bridge.</p>" +
    "<p>If you approve, it can use the bridge tools to:</p>" +
    "<ul><li>Read your project-data snapshot — status, project-note search, " +
    "allowlisted project files, staged-post listings</li>" +
    "<li>Leave task notes for Donnie (handoff)</li></ul>" +
    "<p>Nothing else. It cannot change anything in your accounts — the " +
    "bridge is read + handoff only. Approving requires your owner key. " +
    "Only continue if you started this connection yourself.</p>" +
    (errorMsg
      ? "<p role=\"alert\" style=\"color:#b00020;\">" + htmlEscape(errorMsg) + "</p>"
      : "") +
    "<form method=\"POST\" action=\"/authorize\">" + hidden +
    "<label>Owner key " +
    "<input type=\"password\" name=\"owner_key\" autocomplete=\"off\" " +
    "required style=\"display:block; width:100%; padding:0.5rem; " +
    "margin:0.5rem 0;\"></label>" +
    "<button type=\"submit\" style=\"padding:0.6rem 1.2rem;\">" +
    "Approve connection</button></form></body></html>";
}

async function validateAuthorizeParams(store, params) {
  if (params.get("response_type") !== "code") {
    return { error: "Only response_type=code is supported." };
  }
  const client = await kvJson(
    store, "oauth:client:" + (params.get("client_id") || "")
  );
  if (!client || !Array.isArray(client.redirect_uris)) {
    return { error: "Unknown client_id." };
  }
  const redirectUri = params.get("redirect_uri") || "";
  if (client.redirect_uris.indexOf(redirectUri) === -1) {
    return { error: "redirect_uri does not match this client's registration." };
  }
  const challenge = params.get("code_challenge") || "";
  if (!challenge || params.get("code_challenge_method") !== "S256") {
    return { error: "PKCE is required: code_challenge with code_challenge_method=S256." };
  }
  return {
    client: client,
    redirectUri: redirectUri,
    state: params.get("state") || "",
    challenge: challenge,
  };
}

async function handleAuthorizeGet(request, env) {
  const store = oauthStore(env);
  if (!store) {
    return oauthErrorPage(500,
      "Sign-in is unavailable: this bridge has no storage bound.");
  }
  const v = await validateAuthorizeParams(
    store, new URL(request.url).searchParams
  );
  if (v.error) return oauthErrorPage(400, v.error);
  return htmlResponse(consentPage(v, ""), 200);
}

async function handleAuthorizePost(request, env) {
  const store = oauthStore(env);
  if (!store) {
    return oauthErrorPage(500,
      "Sign-in is unavailable: this bridge has no storage bound.");
  }
  let form;
  try {
    form = await request.formData();
  } catch (e) {
    return oauthErrorPage(400, "Expected a form submission.");
  }
  const v = await validateAuthorizeParams(store, form);
  if (v.error) return oauthErrorPage(400, v.error);
  const ownerKey = env && env.OAUTH_OWNER_KEY ? String(env.OAUTH_OWNER_KEY) : "";
  const provided = String(form.get("owner_key") || "");
  if (!ownerKey || !ctEqual(provided, ownerKey)) {
    // Fail closed: an unset key approves nothing, and a wrong key gets the
    // same generic denial — the page never hints how close a guess was.
    return htmlResponse(
      consentPage(v, "Access denied — that key was not accepted."), 403
    );
  }
  const code = randomB64url(32);
  await store.put(
    "oauth:code:" + (await sha256Hex(code)),
    JSON.stringify({
      client_id: v.client.client_id,
      redirect_uri: v.redirectUri,
      code_challenge: v.challenge,
      exp: Date.now() + CODE_TTL_MS,
    }),
    { expirationTtl: Math.floor(CODE_TTL_MS / 1000) }
  );
  const target = new URL(v.redirectUri);
  target.searchParams.set("code", code);
  if (v.state) target.searchParams.set("state", v.state);
  target.searchParams.set("iss", new URL(request.url).origin);
  return new Response(null, {
    status: 302,
    headers: { Location: target.toString() },
  });
}

async function handleToken(request, env) {
  const store = oauthStore(env);
  if (!store) {
    return oauthError(500, "server_error",
      "OAuth storage is not available on this bridge.");
  }
  let form;
  try {
    form = await request.formData();
  } catch (e) {
    return oauthError(400, "invalid_request", "Expected a form-encoded body.");
  }
  const grant = String(form.get("grant_type") || "");

  if (grant === "authorization_code") {
    const code = String(form.get("code") || "");
    if (!code) return oauthError(400, "invalid_request", "Missing code.");
    const key = "oauth:code:" + (await sha256Hex(code));
    const rec = await kvJson(store, key);
    if (!rec) {
      return oauthError(400, "invalid_grant", "Unknown authorization code.");
    }
    await store.delete(key); // single-use: consumed whatever happens next
    if (rec.exp < Date.now()) {
      return oauthError(400, "invalid_grant", "Authorization code expired.");
    }
    const clientId = String(form.get("client_id") || "");
    if (clientId && clientId !== rec.client_id) {
      return oauthError(400, "invalid_grant",
        "client_id does not match the authorization code.");
    }
    if (String(form.get("redirect_uri") || "") !== rec.redirect_uri) {
      return oauthError(400, "invalid_grant",
        "redirect_uri does not match the authorization code.");
    }
    const verifier = String(form.get("code_verifier") || "");
    if (!verifier || (await pkceS256(verifier)) !== rec.code_challenge) {
      return oauthError(400, "invalid_grant", "PKCE verification failed.");
    }
    return oauthJson(await issueTokens(store, rec.client_id, null), 200);
  }

  if (grant === "refresh_token") {
    const presented = String(form.get("refresh_token") || "");
    if (!presented) {
      return oauthError(400, "invalid_request", "Missing refresh_token.");
    }
    const key = "oauth:refresh:" + (await sha256Hex(presented));
    const rec = await kvJson(store, key);
    if (!rec) {
      return oauthError(400, "invalid_grant", "Unknown refresh token.");
    }
    if (rec.used) {
      // A rotated-out refresh token was presented again: assume theft and
      // revoke the whole chain (all access + refresh tokens in it).
      await store.put(
        "oauth:chain:" + rec.chain,
        JSON.stringify({ revoked: true, client_id: rec.client_id }),
        { expirationTtl: Math.floor(REFRESH_TTL_MS / 1000) }
      );
      return oauthError(400, "invalid_grant",
        "Refresh token reuse detected; the token chain has been revoked.");
    }
    const chain = await kvJson(store, "oauth:chain:" + rec.chain);
    if (!chain || chain.revoked) {
      return oauthError(400, "invalid_grant", "Token chain has been revoked.");
    }
    if (rec.exp < Date.now()) {
      return oauthError(400, "invalid_grant", "Refresh token expired.");
    }
    const clientId = String(form.get("client_id") || "");
    if (clientId && clientId !== rec.client_id) {
      return oauthError(400, "invalid_grant",
        "client_id does not match the refresh token.");
    }
    const remaining = Math.max(60, Math.floor((rec.exp - Date.now()) / 1000));
    await store.put(key, JSON.stringify({
      client_id: rec.client_id, chain: rec.chain, exp: rec.exp, used: true,
    }), { expirationTtl: remaining });
    return oauthJson(await issueTokens(store, rec.client_id, rec.chain), 200);
  }

  return oauthError(400, "unsupported_grant_type",
    "Only authorization_code and refresh_token are supported.");
}

async function mcpAuthorized(request, env) {
  if (tokenOk(request, env)) return true; // static BRIDGE_TOKEN path
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Bearer ")) return false;
  const store = oauthStore(env);
  if (!store) return false;
  const presented = header.slice("Bearer ".length);
  if (!presented) return false;
  const rec = await kvJson(
    store, "oauth:access:" + (await sha256Hex(presented))
  );
  if (!rec || rec.exp < Date.now()) return false;
  const chain = await kvJson(store, "oauth:chain:" + rec.chain);
  if (!chain || chain.revoked) return false;
  return true;
}

function fmtDateLine(d) {
  const date = new Intl.DateTimeFormat("en-US", {
    timeZone: SNAPSHOT_TZ,
    weekday: "long", month: "long", day: "numeric", year: "numeric",
  }).format(d);
  const time = new Intl.DateTimeFormat("en-US", {
    timeZone: SNAPSHOT_TZ,
    hour: "2-digit", minute: "2-digit", hour12: true, timeZoneName: "short",
  }).format(d);
  return date + " (" + time + ")";
}

function fmtStamp(d) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: SNAPSHOT_TZ,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: true, timeZoneName: "short",
  }).formatToParts(d);
  const g = function (t) {
    for (const p of parts) if (p.type === t) return p.value;
    return "";
  };
  return g("year") + "-" + g("month") + "-" + g("day") + " " +
    g("hour") + ":" + g("minute") + " " + g("dayPeriod") + " " + g("timeZoneName");
}

// --- Tool 1: status digest -------------------------------------------------
function toolDonnieStatus() {
  const parts = [];
  parts.push("Date: " + fmtDateLine(new Date()));
  parts.push(
    "Data snapshot: generated " + SNAPSHOT.generated_at + " — this Worker " +
    "serves a snapshot, not live data. Donnie refreshes it when he " +
    "regenerates the Worker."
  );
  const p = SNAPSHOT.rotation || {};
  if (p.current_item) {
    parts.push(
      "Rotation: last_index=" + p.last_index +
      ", current item = '" + p.current_item + "'"
    );
    if (p.next_item) parts.push("Next item in rotation: '" + p.next_item + "'");
  } else {
    parts.push("Rotation: unavailable in snapshot" + (p.error ? " (" + p.error + ")" : ""));
  }
  parts.push("");
  parts.push("Project notes outline:");
  const lines = String(SNAPSHOT.memory_text || "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].startsWith("#")) {
      parts.push(lines[i].trim());
      for (let j = i + 1; j < lines.length; j++) {
        if (lines[j].trim()) {
          parts.push("  " + lines[j].trim().slice(0, 160));
          break;
        }
      }
    }
  }
  return textResult(parts.join("\n").slice(0, 6000));
}

// --- Tool 2: notes search ----------------------------------------------------
function toolSearchMemory(args) {
  const raw = String((args && args.query) || "");
  const q = raw.trim().toLowerCase();
  if (!q) return textResult("Give me a non-empty search query.");
  const matches = [];
  const lines = String(SNAPSHOT.memory_text || "").split("\n");
  for (const line of lines) {
    if (line.toLowerCase().indexOf(q) !== -1) {
      matches.push("[project notes] " + line.trim().slice(0, 300));
    }
  }
  if (!matches.length) return textResult("No matches for '" + raw.trim() + "'.");
  const out = matches.slice(0, 40);
  if (matches.length > 40) {
    out.push("... and " + (matches.length - 40) + " more matches (showing first 40).");
  }
  return textResult(out.join("\n"));
}

// --- Tool 3: snapshot file reads ----------------------------------------------
function normalizePath(raw) {
  let p = String(raw || "").trim().replace(/\\/g, "/");
  if (!p) return "";
  if (p.startsWith("~/")) p = p.slice(2);
  p = p.replace(/^\/+/, "");
  if (p.startsWith("workspace/")) p = p.slice("workspace/".length);
  const segs = [];
  for (const s of p.split("/")) {
    if (s === "" || s === ".") continue;
    if (s === "..") return null; // traversal attempt
    segs.push(s);
  }
  return segs.join("/");
}

function inAllowlist(rel) {
  if (!rel) return false;
  if (rel.startsWith("notes/")) return true;
  const parts = rel.split("/");
  return parts.length >= 4 && parts[0] === "goals" && parts[2] === "files";
}

function toolReadProjectFile(args) {
  const raw = String((args && args.path) || "").trim();
  if (!raw) return textResult("Give me a file path.");
  const rel = normalizePath(raw);
  if (rel === null || !inAllowlist(rel)) {
    return textResult(
      "Rejected: that path is outside the allowlist " +
      "(only goals/*/files/ and notes/ inside the bridge workspace are readable).",
      true
    );
  }
  const files = SNAPSHOT.files || {};
  if (Object.prototype.hasOwnProperty.call(files, rel)) {
    return textResult(files[rel]);
  }
  return textResult(
    "Not in the snapshot: '" + rel + "' was not embedded when this Worker " +
    "was generated (" + SNAPSHOT.generated_at + "). It may exist on " +
    "Donnie's machine but was not part of the embedded set (binary, added " +
    "later, excluded, or a file type that is not embedded). Ask Donnie to " +
    "regenerate the snapshot if you need it.",
    true
  );
}

// --- Tool 3b: binary file reads (PDFs and images) ------------------------------
function toolReadProjectFileBytes(args) {
  const raw = String((args && args.path) || "").trim();
  if (!raw) return textResult("Give me a file path.");
  const rel = normalizePath(raw);
  if (rel === null || !inAllowlist(rel)) {
    return textResult(
      "Rejected: that path is outside the allowlist " +
      "(only goals/*/files/ and notes/ inside the bridge workspace are readable).",
      true
    );
  }
  const blobs = SNAPSHOT.blobs || {};
  if (Object.prototype.hasOwnProperty.call(blobs, rel)) {
    const b = blobs[rel];
    if (b.mime.startsWith("image/")) {
      // MCP image content block: the client can show the picture directly.
      return { content: [{ type: "image", data: b.data, mimeType: b.mime }] };
    }
    // MCP embedded-resource block carrying the blob as base64.
    return {
      content: [{
        type: "resource",
        resource: { uri: "snapshot://" + rel, mimeType: b.mime, blob: b.data },
      }],
    };
  }
  const files = SNAPSHOT.files || {};
  if (Object.prototype.hasOwnProperty.call(files, rel)) {
    return textResult(
      "'" + rel + "' is a text file in the snapshot — use read_project_file for it.",
      true
    );
  }
  return textResult(
    "Not in the snapshot: '" + rel + "' was not embedded when this Worker " +
    "was generated (" + SNAPSHOT.generated_at + "). Binary files cross only " +
    "if they sit in the allowlist, have a supported type (.pdf, .png, .jpg, " +
    ".jpeg, .webp, .gif), and fit the generator's size caps. Ask Donnie to " +
    "regenerate the snapshot if you need it.",
    true
  );
}

// --- Tool 4: staged post listings ----------------------------------------------
function toolListStagedPosts() {
  const groups = SNAPSHOT.staged || [];
  const lines = [];
  for (const g of groups) {
    lines.push(g.label + " (" + g.dir + "):");
    if (!g.entries || !g.entries.length) {
      lines.push("  (empty)");
    } else {
      for (const e of g.entries) lines.push("  " + e.name + " — modified " + e.modified);
    }
  }
  lines.push("");
  lines.push("(Listing comes from the data snapshot generated " + SNAPSHOT.generated_at + ".)");
  return textResult(lines.join("\n"));
}

// --- Tool 5: handoff -------------------------------------------------------------
async function toolHandoff(args, env) {
  const task = String((args && args.task) || "").trim();
  if (!task) return textResult("Give me a task to hand off.");
  const now = new Date();
  const stamp = fmtStamp(now);
  const note = "## " + stamp + "\n" + task;
  let stored = false;
  let key = null;
  if (env && env.DONNIE_KV && typeof env.DONNIE_KV.put === "function") {
    key = "handoff-" + now.toISOString();
    try {
      await env.DONNIE_KV.put(key, note);
      stored = true;
    } catch (e) {
      stored = false;
    }
  }
  const lines = [];
  if (stored) {
    lines.push(
      "Handoff saved at " + stamp + " (bridge KV storage, key " + key + "). " +
      "Donnie picks handoffs up on sync — or paste the note below into your " +
      "chat with Donnie and he will act on it there. Nothing happens " +
      "instantly, and this is not a live chat with Donnie."
    );
  } else {
    lines.push(
      "Handoff written at " + stamp + ", but this bridge has no KV storage " +
      "bound, so the note was NOT saved anywhere. Copy the note below and " +
      "paste it into your chat with Donnie — that is the delivery path. " +
      "Nothing happens instantly, and this is not a live chat with Donnie."
    );
  }
  lines.push("");
  lines.push("Note text:");
  lines.push(note);
  return textResult(lines.join("\n"));
}

// --- Tool registry ---------------------------------------------------------------
const TOOLS = [
  {
    name: "donnie_status",
    description:
      "Concise status: today's date, the data snapshot's date, the " +
      "current rotation item, and the project-notes outline.",
    inputSchema: { type: "object", properties: {} },
    annotations: { title: "Donnie status", readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    run: function () { return toolDonnieStatus(); },
  },
  {
    name: "search_memory",
    description:
      "Case-insensitive line search over the owner's project notes " +
      "(the embedded scrubbed snapshot — personal details are never included).",
    inputSchema: {
      type: "object",
      properties: { query: { type: "string", description: "Word or phrase to search for" } },
      required: ["query"],
    },
    annotations: { title: "Search project notes", readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    run: function (args) { return toolSearchMemory(args); },
  },
  {
    name: "read_project_file",
    description:
      "Read a text file from the allowlist: goals/*/files/ or notes/ " +
      "inside the bridge workspace. Worker edition: only files embedded " +
      "in the snapshot can be returned; anything else is rejected with " +
      "an explanation.",
    inputSchema: {
      type: "object",
      properties: { path: { type: "string", description: "Workspace-relative or ~/ path of the file" } },
      required: ["path"],
    },
    annotations: { title: "Read project file", readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    run: function (args) { return toolReadProjectFile(args); },
  },
  {
    name: "read_project_file_bytes",
    description:
      "Read a binary file (PDF or image: .pdf, .png, .jpg, .jpeg, .webp, " +
      ".gif) from the same allowlist as read_project_file. Images return " +
      "as image content; PDFs and other binaries return as an embedded " +
      "resource blob (base64). Text files are rejected — use " +
      "read_project_file for those.",
    inputSchema: {
      type: "object",
      properties: { path: { type: "string", description: "Workspace-relative or ~/ path of the file" } },
      required: ["path"],
    },
    annotations: { title: "Read project file (binary)", readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    run: function (args) { return toolReadProjectFileBytes(args); },
  },
  {
    name: "list_staged_posts",
    description: "List staged posts waiting to go out (from the snapshot's staged listings).",
    inputSchema: { type: "object", properties: {} },
    annotations: { title: "List staged posts", readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    run: function () { return toolListStagedPosts(); },
  },
  {
    name: "handoff_to_donnie",
    description:
      "Leave a task for Donnie. The note is stored in KV when bound and " +
      "is always returned in the result; Donnie picks it up on sync or " +
      "when the owner pastes it into chat — this is NOT instant and NOT " +
      "a live conversation with Donnie.",
    inputSchema: {
      type: "object",
      properties: { task: { type: "string", description: "The task or note for Donnie" } },
      required: ["task"],
    },
    // Writes a note to KV (or returns it for pasting) — not read-only,
    // but not destructive: it changes nothing in any of the owner's accounts.
    annotations: { title: "Hand off to Donnie", readOnlyHint: false, destructiveHint: false, openWorldHint: false },
    run: function (args, env) { return toolHandoff(args, env); },
  },
];

// --- JSON-RPC / MCP handling -------------------------------------------------------
async function handleMessage(msg, env) {
  if (!msg || typeof msg !== "object") return rpcError(null, -32600, "Invalid request");
  const hasId = Object.prototype.hasOwnProperty.call(msg, "id");
  const method = msg.method;
  if (typeof method !== "string") {
    return hasId ? rpcError(msg.id, -32600, "Invalid request: missing method") : null;
  }
  if (!hasId) return null; // notification — nothing to answer
  const id = msg.id;
  try {
    if (method === "initialize") {
      // Protocol negotiation: the Worker is version-agnostic (plain
      // JSON-RPC over HTTP, no version-gated features), so it answers in
      // the client's requested version when that version is a known MCP
      // version, and falls back to 2025-03-26 otherwise. Some clients
      // reject a mismatched version under the spec's negotiation rules,
      // so echo what was asked for.
      const requested = msg.params && msg.params.protocolVersion;
      const known = ["2024-11-05", "2025-03-26", "2025-06-18"];
      const negotiated = known.indexOf(requested) !== -1
        ? requested : PROTOCOL_VERSION;
      return rpcResult(id, {
        protocolVersion: negotiated,
        capabilities: { tools: {} },
        serverInfo: { name: BRIDGE_NAME, version: "1.0.0" },
        instructions: INSTRUCTIONS,
      });
    }
    if (method === "ping") return rpcResult(id, {});
    if (method === "tools/list") {
      return rpcResult(id, {
        tools: TOOLS.map(function (t) {
          return { name: t.name, description: t.description, inputSchema: t.inputSchema, annotations: t.annotations };
        }),
      });
    }
    if (method === "tools/call") {
      const params = msg.params || {};
      let tool = null;
      for (const t of TOOLS) if (t.name === params.name) tool = t;
      if (!tool) {
        return rpcResult(id, textResult(
          "Unknown tool: " + String(params.name) + ". Available tools: " +
          TOOLS.map(function (t) { return t.name; }).join(", "),
          true
        ));
      }
      const result = await tool.run(params.arguments || {}, env);
      return rpcResult(id, result);
    }
    return rpcError(id, -32601, "Method not found: " + method);
  } catch (e) {
    return rpcResult(id, textResult(
      "Tool error: " + (e && e.message ? e.message : String(e)), true
    ));
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;
    // --- OAuth 2.1 door (discovery, registration, consent, token) ---
    if (path === "/.well-known/oauth-protected-resource" ||
        path.startsWith("/.well-known/oauth-protected-resource/")) {
      if (request.method !== "GET") {
        return jsonResponse({ error: "method not allowed" }, 405);
      }
      return protectedResourceMetadata(request);
    }
    if (path === "/.well-known/oauth-authorization-server" ||
        path === "/.well-known/openid-configuration") {
      if (request.method !== "GET") {
        return jsonResponse({ error: "method not allowed" }, 405);
      }
      return authorizationServerMetadata(request);
    }
    if (path === "/register") {
      if (request.method !== "POST") {
        return jsonResponse({ error: "method not allowed" }, 405);
      }
      return handleRegister(request, env);
    }
    if (path === "/authorize") {
      if (request.method === "GET") return handleAuthorizeGet(request, env);
      if (request.method === "POST") return handleAuthorizePost(request, env);
      return jsonResponse({ error: "method not allowed" }, 405);
    }
    if (path === "/token") {
      if (request.method !== "POST") {
        return jsonResponse({ error: "method not allowed" }, 405);
      }
      return handleToken(request, env);
    }
    // --- MCP endpoint ---
    if (path !== "/mcp") {
      return jsonResponse({ error: "not found" }, 404);
    }
    if (!(await mcpAuthorized(request, env))) {
      return new Response(JSON.stringify({ error: "unauthorized" }), {
        status: 401,
        headers: {
          "Content-Type": "application/json",
          "WWW-Authenticate":
            'Bearer resource_metadata="' + url.origin +
            '/.well-known/oauth-protected-resource"',
        },
      });
    }
    if (request.method !== "POST") {
      return jsonResponse({ error: "method not allowed" }, 405);
    }
    let body;
    try {
      body = await request.json();
    } catch (e) {
      return jsonResponse(rpcError(null, -32700, "Parse error"), 400);
    }
    if (Array.isArray(body)) {
      const responses = [];
      for (const m of body) {
        const r = await handleMessage(m, env);
        if (r) responses.push(r);
      }
      if (!responses.length) return new Response(null, { status: 202 });
      return jsonResponse(responses, 200);
    }
    const r = await handleMessage(body, env);
    if (!r) return new Response(null, { status: 202 });
    return jsonResponse(r, 200);
  },
};
'''


def main() -> None:
    snapshot, stats = build_snapshot()
    snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    # U+2028/U+2029 are legal in modern JS strings but escape them anyway.
    snapshot_json = snapshot_json.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    assert TEMPLATE.count("__SNAPSHOT_JSON__") == 1, "snapshot placeholder missing/duplicated"
    assert TEMPLATE.count("__BRIDGE_NAME__") >= 1, "bridge-name placeholder missing"
    worker_js = TEMPLATE.replace("__SNAPSHOT_JSON__", snapshot_json)
    worker_js = worker_js.replace("__BRIDGE_NAME__", json.dumps(BRIDGE_NAME))

    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / "worker.js"
    out.write_text(worker_js, encoding="utf-8")

    rotation = snapshot["rotation"]
    print(f"Wrote {out} ({out.stat().st_size} bytes)")
    print(f"Snapshot generated_at : {snapshot['generated_at']}")
    print(f"Workspace             : {WORKSPACE_ROOT}")
    print(f"Rotation              : last_index={rotation['last_index']} "
          f"current={rotation['current_item']!r} next={rotation['next_item']!r}")
    print(f"Notes corpus          : {stats['memory_chars']} chars")
    if stats["corpus_warning"]:
        print(f"WARNING               : {stats['corpus_warning']}")
    print(f"Files embedded        : {stats['files_embedded']}")
    print(f"Blobs embedded        : {len(stats['blobs_embedded'])} "
          f"({stats['blobs_total_bytes']} raw bytes, cap {MAX_BINARY_TOTAL_BYTES})")
    for rel, info in stats["blobs_embedded"].items():
        print(f"  blob: {rel} — {info['mime']}, {info['bytes']} bytes")
    if stats["binaries_skipped"]:
        print("SKIPPED (binary caps) : " + "; ".join(stats["binaries_skipped"]))
    print(f"Snapshot JSON         : {stats['snapshot_bytes']} bytes "
          f"(text cap {SNAPSHOT_CAP}; binaries add base64 on top)")
    if stats["files_dropped"]:
        print("DROPPED (tripwire)    : " + "; ".join(stats["files_dropped"]))
    if stats["files_trimmed_for_size"]:
        print("TRIMMED (size cap)    : " + "; ".join(stats["files_trimmed_for_size"]))
    staged = snapshot["staged"]
    print("Staged listings       : " + ", ".join(
        f"{g['label']}={len(g['entries'])}" for g in staged))


if __name__ == "__main__":
    main()
