// Test harness for dist/worker.js — imports the generated Worker with a fake
// env and drives it with simulated Fetch Request objects.
// Run: node test_worker.mjs   (after `python3 gen_worker.py`)
//
// The harness is wired to the bundled sample workspace in ../examples, so
// it passes against the template exactly as shipped. When you adopt the
// bridge, repoint the expectations below at your own sample data.
import { readFileSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { fileURLToPath, pathToFileURL } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const workerPath = join(here, "dist", "worker.js");

// Node loads .js as CommonJS without a package.json type field, so copy the
// exact bytes to a .mjs sibling purely so the ESM loader accepts it.
const tmpPath = join(here, "dist", ".worker-under-test.mjs");
writeFileSync(tmpPath, readFileSync(workerPath));
const worker = (await import(pathToFileURL(tmpPath).href)).default;

const TOKEN = "test-token-123";
const env = { BRIDGE_TOKEN: TOKEN };
const URL_MCP = "https://bridge.example.workers.dev/mcp";

let idc = 0;
async function rpc(method, params, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (opts.token !== null) headers["Authorization"] = "Bearer " + (opts.token ?? TOKEN);
  const body = { jsonrpc: "2.0", method };
  if (!opts.notification) body.id = ++idc;
  if (params !== undefined) body.params = params;
  const res = await worker.fetch(
    new Request(URL_MCP, { method: "POST", headers, body: JSON.stringify(body) }),
    opts.env ?? env
  );
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* empty body */ }
  return { status: res.status, text, json };
}

async function callTool(name, args, opts = {}) {
  const r = await rpc("tools/call", { name, arguments: args ?? {} }, opts);
  return { ...r, result: r.json?.result, toolText: r.json?.result?.content?.[0]?.text ?? "" };
}

const results = [];
function check(name, cond, extra = "") {
  results.push([name, !!cond]);
  console.log((cond ? "PASS" : "FAIL") + "  " + name + (extra ? "  — " + extra : ""));
}

// Expected rotation item, computed from the sample workspace state file.
const rotationState = JSON.parse(readFileSync(
  join(here, "..", "examples", "state", "rotation_state.json"), "utf-8"));
const expectedItem = rotationState.tracks[rotationState.last_index];

// 1. initialize
{
  const r = await rpc("initialize", { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "harness", version: "0" } });
  check("initialize 200 + protocolVersion", r.status === 200 && r.json?.result?.protocolVersion === "2025-03-26");
  check("initialize serverInfo", r.json?.result?.serverInfo?.name === "Donnie Bridge");
  check("initialize instructions mention snapshot", /snapshot/i.test(r.json?.result?.instructions ?? ""));
}

// 2. tools/list
{
  const r = await rpc("tools/list");
  const names = (r.json?.result?.tools ?? []).map((t) => t.name).sort();
  const want = ["donnie_status", "handoff_to_donnie", "list_staged_posts", "read_project_file", "read_project_file_bytes", "search_memory"];
  check("tools/list shows all 6 tools", JSON.stringify(names) === JSON.stringify(want), names.join(","));
}

// 3. ping
{
  const r = await rpc("ping");
  check("ping -> {}", r.status === 200 && JSON.stringify(r.json?.result) === "{}");
}

// 4. donnie_status
{
  const r = await callTool("donnie_status");
  check("donnie_status has sample rotation item", r.toolText.includes(expectedItem), expectedItem);
  check("donnie_status has notes outline", r.toolText.includes("Project notes outline:"));
}

// 5. search_memory (the sample corpus mentions a lighthouse series)
{
  const r = await callTool("search_memory", { query: "lighthouse" });
  check("search_memory 'lighthouse' finds matches", r.toolText.includes("lighthouse") && r.toolText.includes("[project notes]"));
  const none = await callTool("search_memory", { query: "zzq-no-such-term-zzq" });
  check("search_memory no-match message", none.toolText.startsWith("No matches for"));
}

// 6. read_project_file
{
  const embedded = "goals/demo-project/files/plan.md";
  const r = await callTool("read_project_file", { path: embedded });
  check("read_project_file serves embedded file", !r.result?.isError && r.toolText.includes("DEMO PLAN"), embedded);
  const abs = await callTool("read_project_file", { path: "~/workspace/" + embedded });
  check("read_project_file accepts ~/workspace/ path", !abs.result?.isError && abs.toolText.includes("DEMO PLAN"));
  const missing = await callTool("read_project_file", { path: "goals/demo-project/files/not-embedded.md" });
  check("read_project_file non-embedded in-allowlist -> clear error", missing.result?.isError && missing.toolText.includes("Not in the snapshot"));
  const excluded = await callTool("read_project_file", { path: "goals/private-demo/files/do-not-publish.md" });
  check("read_project_file excluded project -> not in snapshot", excluded.result?.isError && excluded.toolText.includes("Not in the snapshot"));
  const outside = await callTool("read_project_file", { path: "corpus.md" });
  check("read_project_file outside allowlist -> rejected", outside.result?.isError && outside.toolText.includes("Rejected"));
  const trav = await callTool("read_project_file", { path: "goals/../corpus.md" });
  check("read_project_file traversal -> rejected", trav.result?.isError && trav.toolText.includes("Rejected"));
}

// 6b. read_project_file_bytes (binary path: image + PDF + errors)
{
  const img = await callTool("read_project_file_bytes", { path: "goals/demo-project/files/demo-image.png" });
  const imgBlock = img.json?.result?.content?.[0] ?? {};
  check("bytes: image returns an image content block",
    imgBlock.type === "image" && imgBlock.mimeType === "image/png" &&
    typeof imgBlock.data === "string" && imgBlock.data.length > 0);
  check("bytes: image base64 decodes to a PNG",
    Buffer.from(imgBlock.data ?? "", "base64").subarray(0, 4).toString("hex") === "89504e47");
  const pdf = await callTool("read_project_file_bytes", { path: "goals/demo-project/files/demo-document.pdf" });
  const pdfBlock = pdf.json?.result?.content?.[0] ?? {};
  check("bytes: PDF returns a resource blob block",
    pdfBlock.type === "resource" && pdfBlock.resource?.mimeType === "application/pdf" &&
    typeof pdfBlock.resource?.blob === "string" && pdfBlock.resource.blob.length > 0);
  check("bytes: PDF blob decodes to %PDF",
    Buffer.from(pdfBlock.resource?.blob ?? "", "base64").subarray(0, 4).toString("latin1") === "%PDF");
  const textFile = await callTool("read_project_file_bytes", { path: "goals/demo-project/files/plan.md" });
  check("bytes: text file -> error pointing at read_project_file",
    textFile.result?.isError && textFile.toolText.includes("read_project_file"));
  const missing = await callTool("read_project_file_bytes", { path: "goals/demo-project/files/nope.png" });
  check("bytes: missing file -> Not in the snapshot", missing.result?.isError && missing.toolText.includes("Not in the snapshot"));
  const excluded = await callTool("read_project_file_bytes", { path: "goals/private-demo/files/do-not-publish.md" });
  check("bytes: excluded project -> not in snapshot", excluded.result?.isError && excluded.toolText.includes("Not in the snapshot"));
  const outside = await callTool("read_project_file_bytes", { path: "corpus.png" });
  check("bytes: outside allowlist -> rejected", outside.result?.isError && outside.toolText.includes("Rejected"));
  const trav = await callTool("read_project_file_bytes", { path: "goals/../demo-image.png" });
  check("bytes: traversal -> rejected", trav.result?.isError && trav.toolText.includes("Rejected"));
}

// 7. list_staged_posts
{
  const r = await callTool("list_staged_posts");
  check("list_staged_posts lists both dirs", r.toolText.includes("Short-form video packages") && r.toolText.includes("post-01-demo.txt"));
}

// 8. handoff (no KV, then fake KV)
{
  const r = await callTool("handoff_to_donnie", { task: "Test handoff: check the demo timer." });
  check("handoff (no KV) returns note text", r.toolText.includes("Test handoff: check the demo timer."));
  check("handoff (no KV) says not saved + paste path", /NOT saved/i.test(r.toolText) && /paste/i.test(r.toolText));
  const store = new Map();
  const kvEnv = { BRIDGE_TOKEN: TOKEN, DONNIE_KV: { async put(k, v) { store.set(k, v); } } };
  const r2 = await callTool("handoff_to_donnie", { task: "KV handoff test note." }, { env: kvEnv });
  check("handoff (KV) reports stored", /Handoff saved/i.test(r2.toolText) && r2.toolText.includes("KV handoff test note."));
  check("handoff (KV) actually wrote the note", [...store.values()].some((v) => v.includes("KV handoff test note.")));
}

// 9. auth failures
{
  const noTok = await rpc("tools/list", undefined, { token: null });
  check("missing token -> 401", noTok.status === 401);
  const badTok = await rpc("tools/list", undefined, { token: "wrong-token" });
  check("wrong token -> 401", badTok.status === 401);
}

// 10. notification + GET
{
  const n = await rpc("notifications/initialized", undefined, { notification: true });
  check("notification -> 202 empty body", n.status === 202 && n.text === "");
  const res = await worker.fetch(new Request(URL_MCP, { method: "GET", headers: { Authorization: "Bearer " + TOKEN } }), env);
  check("GET /mcp -> 405", res.status === 405);
  const nf = await worker.fetch(new Request("https://bridge.example.workers.dev/nope", { method: "GET" }), env);
  check("unknown path -> 404", nf.status === 404);
}

// 11. unknown tool
{
  const r = await callTool("nope_tool", {});
  check("unknown tool -> isError", r.result?.isError && r.toolText.includes("Unknown tool"));
}

// 12. OAuth 2.1 door (FakeKV emulates the KV binding incl. TTLs)
class FakeKV {
  constructor() { this.map = new Map(); }
  async get(key) {
    const e = this.map.get(key);
    if (!e) return null;
    if (e.exp !== null && Date.now() > e.exp) { this.map.delete(key); return null; }
    return e.value;
  }
  async put(key, value, opts = {}) {
    let exp = null;
    if (opts.expirationTtl) exp = Date.now() + opts.expirationTtl * 1000;
    else if (opts.expiration) exp = opts.expiration * 1000;
    this.map.set(key, { value: String(value), exp });
  }
  async delete(key) { this.map.delete(key); }
}

const ORIGIN = "https://bridge.example.workers.dev";
const envOauth = { BRIDGE_TOKEN: TOKEN, OAUTH_OWNER_KEY: "dummy-owner", DONNIE_KV: new FakeKV() };

async function rawReq(path, init = {}, envOverride) {
  const res = await worker.fetch(new Request(ORIGIN + path, init), envOverride ?? envOauth);
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status, headers: res.headers, text, json };
}
const formPost = (fields) => ({
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams(fields).toString(),
});

// 12a. Discovery documents
{
  const prm = await rawReq("/.well-known/oauth-protected-resource");
  check("PRM 200 + resource is /mcp", prm.status === 200 && prm.json?.resource === ORIGIN + "/mcp");
  check("PRM names this worker as AS", JSON.stringify(prm.json?.authorization_servers) === JSON.stringify([ORIGIN]));
  const asm = await rawReq("/.well-known/oauth-authorization-server");
  check("AS metadata endpoints", asm.status === 200 &&
    asm.json?.authorization_endpoint === ORIGIN + "/authorize" &&
    asm.json?.token_endpoint === ORIGIN + "/token" &&
    asm.json?.registration_endpoint === ORIGIN + "/register");
  check("AS metadata PKCE S256 + grants", (asm.json?.code_challenge_methods_supported ?? []).includes("S256") &&
    (asm.json?.grant_types_supported ?? []).includes("refresh_token"));
}

// 12b. 401 on /mcp carries the discovery header
{
  const r = await rawReq("/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list" }),
  });
  check("/mcp no token -> 401 + WWW-Authenticate resource_metadata",
    r.status === 401 && (r.headers.get("www-authenticate") ?? "").includes("/.well-known/oauth-protected-resource"));
}

// 12c. Dynamic client registration
let clientId = null;
{
  const r = await rawReq("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "Harness Client", redirect_uris: ["https://client.example/oauth-callback"] }),
  });
  clientId = r.json?.client_id ?? null;
  check("register 201 + client_id", r.status === 201 && !!clientId);
  check("register is a public client (auth method none)", r.json?.token_endpoint_auth_method === "none");
  const bad = await rawReq("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "Bad", redirect_uris: ["ftp://evil.example/cb"] }),
  });
  check("register rejects non-https redirect_uri", bad.status === 400 && bad.json?.error === "invalid_redirect_uri");
}

const REDIRECT = "https://client.example/oauth-callback";
const VERIFIER = "harness-verifier-0123456789-abcdefghijklmnopqrstuvwxyz-0123456789";
const CHALLENGE = createHash("sha256").update(VERIFIER).digest("base64url");
const authorizeFields = (cid, ownerKey) => ({
  response_type: "code", client_id: cid, redirect_uri: REDIRECT,
  code_challenge: CHALLENGE, code_challenge_method: "S256",
  state: "state-xyz", owner_key: ownerKey,
});
async function approveForCode(cid, envOverride) {
  const r = await rawReq("/authorize", formPost(authorizeFields(cid, "dummy-owner")), envOverride);
  const loc = r.headers.get("location") || "";
  return { r, loc, code: loc ? new URL(loc).searchParams.get("code") : null };
}

// 12d. Consent page + owner gate
{
  const q = new URLSearchParams({
    response_type: "code", client_id: clientId, redirect_uri: REDIRECT,
    code_challenge: CHALLENGE, code_challenge_method: "S256", state: "state-xyz",
  }).toString();
  const page = await rawReq("/authorize?" + q);
  check("GET /authorize renders consent naming the client",
    page.status === 200 && page.text.includes("Harness Client") && page.text.includes("Donnie Bridge"));
  const wrong = await rawReq("/authorize", formPost(authorizeFields(clientId, "not-the-owner-key")));
  check("POST /authorize wrong owner key -> denied, no redirect",
    wrong.status === 403 && !wrong.headers.get("location") && wrong.text.includes("not accepted"));
  const { r, loc, code } = await approveForCode(clientId);
  check("POST /authorize correct owner key -> 302 with code + state",
    r.status === 302 && loc.startsWith(REDIRECT) && !!code &&
    new URL(loc).searchParams.get("state") === "state-xyz");

  // wrong PKCE verifier burns the code and errors
  const badTok = await rawReq("/token", formPost({
    grant_type: "authorization_code", code, redirect_uri: REDIRECT,
    client_id: clientId, code_verifier: "wrong-verifier-0123456789-abcdefghijklmnopqrstuvwxyz",
  }));
  check("/token wrong verifier -> invalid_grant", badTok.status === 400 && badTok.json?.error === "invalid_grant");
}

// 12e. Full exchange, MCP access, refresh rotation + reuse detection
{
  const { code } = await approveForCode(clientId);
  const tok = await rawReq("/token", formPost({
    grant_type: "authorization_code", code, redirect_uri: REDIRECT,
    client_id: clientId, code_verifier: VERIFIER,
  }));
  check("/token correct verifier -> access + refresh issued",
    tok.status === 200 && !!tok.json?.access_token && !!tok.json?.refresh_token && tok.json?.token_type === "Bearer");
  const replay = await rawReq("/token", formPost({
    grant_type: "authorization_code", code, redirect_uri: REDIRECT,
    client_id: clientId, code_verifier: VERIFIER,
  }));
  check("/token replayed code -> invalid_grant", replay.status === 400 && replay.json?.error === "invalid_grant");

  const access1 = tok.json.access_token, refresh1 = tok.json.refresh_token;
  const list = await rpc("tools/list", undefined, { token: access1, env: envOauth });
  check("/mcp tools/list with OAuth access token -> 6 tools",
    list.status === 200 && (list.json?.result?.tools ?? []).length === 6);
  const st = await callTool("donnie_status", {}, { token: access1, env: envOauth });
  check("/mcp donnie_status with OAuth access token -> sample item",
    st.status === 200 && st.toolText.includes(expectedItem));
  const badAccess = await rpc("tools/list", undefined, { token: "not-a-real-token", env: envOauth });
  check("/mcp unknown bearer token -> 401", badAccess.status === 401);

  const rot = await rawReq("/token", formPost({
    grant_type: "refresh_token", refresh_token: refresh1, client_id: clientId,
  }));
  check("refresh rotation issues new tokens", rot.status === 200 &&
    !!rot.json?.access_token && !!rot.json?.refresh_token && rot.json.refresh_token !== refresh1);
  const reuse = await rawReq("/token", formPost({
    grant_type: "refresh_token", refresh_token: refresh1, client_id: clientId,
  }));
  check("reused refresh token -> invalid_grant (chain revoked)",
    reuse.status === 400 && reuse.json?.error === "invalid_grant");
  const after = await rpc("tools/list", undefined, { token: rot.json.access_token, env: envOauth });
  check("access token from revoked chain -> 401", after.status === 401);
}

// 12f. Fail closed when OAUTH_OWNER_KEY is not set
{
  const envNoKey = { BRIDGE_TOKEN: TOKEN, DONNIE_KV: new FakeKV() };
  const reg = await rawReq("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_name: "NoKey Client", redirect_uris: [REDIRECT] }),
  }, envNoKey);
  check("register still works without owner key (gate is at approve)", reg.status === 201);
  const denied = await rawReq("/authorize", formPost(authorizeFields(reg.json.client_id, "dummy-owner")), envNoKey);
  check("OAUTH_OWNER_KEY unset -> approve always denied",
    denied.status === 403 && !denied.headers.get("location"));
}

// 13. Second OAuth client flow: a DCR client with a different redirect,
// protocol 2025-06-18, and tool annotations — the shape a Claude-style
// custom connector uses (dynamic registration, no pre-shared secret).
{
  const ALT_REDIRECT = "https://client.example/mcp/auth_callback";
  const reg = await rawReq("/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_name: "Second Client",
      redirect_uris: [ALT_REDIRECT],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
    }),
  });
  check("second client DCR 201 + client_id", reg.status === 201 && !!reg.json?.client_id);
  const altId = reg.json.client_id;
  const aChallenge = createHash("sha256").update(VERIFIER).digest("base64url");
  const q = new URLSearchParams({
    response_type: "code", client_id: altId, redirect_uri: ALT_REDIRECT,
    code_challenge: aChallenge, code_challenge_method: "S256", state: "alt-state",
  }).toString();
  const page = await rawReq("/authorize?" + q);
  check("second client consent page renders", page.status === 200 && page.text.includes("Second Client"));
  const appr = await rawReq("/authorize", formPost({
    response_type: "code", client_id: altId, redirect_uri: ALT_REDIRECT,
    code_challenge: aChallenge, code_challenge_method: "S256",
    state: "alt-state", owner_key: "dummy-owner",
  }));
  const altLoc = appr.headers.get("location") || "";
  const altCode = altLoc ? new URL(altLoc).searchParams.get("code") : null;
  check("second client approve -> 302 to its callback with code + state + iss",
    appr.status === 302 && altLoc.startsWith(ALT_REDIRECT) && !!altCode &&
    new URL(altLoc).searchParams.get("state") === "alt-state" &&
    new URL(altLoc).searchParams.get("iss") === ORIGIN);
  const tok = await rawReq("/token", formPost({
    grant_type: "authorization_code", code: altCode, redirect_uri: ALT_REDIRECT,
    client_id: altId, code_verifier: VERIFIER,
  }));
  check("second client token exchange -> access + refresh",
    tok.status === 200 && !!tok.json?.access_token && !!tok.json?.refresh_token);
  const altAccess = tok.json.access_token;
  const init618 = await rpc("initialize",
    { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "second-client", version: "0" } },
    { token: altAccess, env: envOauth });
  check("initialize 2025-06-18 -> version echoed",
    init618.status === 200 && init618.json?.result?.protocolVersion === "2025-06-18");
  const initOld = await rpc("initialize",
    { protocolVersion: "1999-01-01", capabilities: {}, clientInfo: { name: "second-client", version: "0" } },
    { token: altAccess, env: envOauth });
  check("initialize unknown version -> falls back to 2025-03-26",
    initOld.status === 200 && initOld.json?.result?.protocolVersion === "2025-03-26");
  const list = await rpc("tools/list", undefined, { token: altAccess, env: envOauth });
  const tools = list.json?.result?.tools ?? [];
  check("tools/list -> 6 tools with annotations",
    tools.length === 6 && tools.every((t) => t.annotations && typeof t.annotations.title === "string" &&
      typeof t.annotations.readOnlyHint === "boolean"));
  check("handoff tool is not read-only, not destructive",
    tools.find((t) => t.name === "handoff_to_donnie")?.annotations?.readOnlyHint === false &&
    tools.find((t) => t.name === "handoff_to_donnie")?.annotations?.destructiveHint === false);
  const st = await callTool("donnie_status", {}, { token: altAccess, env: envOauth });
  check("donnie_status via OAuth token -> sample item",
    st.status === 200 && st.toolText.includes(expectedItem));
  const prmPath = await rawReq("/.well-known/oauth-protected-resource/mcp");
  check("PRM path-inserted variant -> 200 + resource /mcp",
    prmPath.status === 200 && prmPath.json?.resource === ORIGIN + "/mcp");
  const unauthInit = await rawReq("/mcp", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 99, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "second-client", version: "0" } } }),
  });
  check("unauthenticated initialize -> 401 + WWW-Authenticate resource_metadata",
    unauthInit.status === 401 && (unauthInit.headers.get("www-authenticate") ?? "").includes("/.well-known/oauth-protected-resource"));
}

const failed = results.filter(([, ok]) => !ok);
console.log("\n" + (results.length - failed.length) + "/" + results.length + " checks passed");
process.exit(failed.length ? 1 : 0);
