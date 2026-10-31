#!/usr/bin/env python3
"""Deploy dist/worker.js to your bridge Worker WITHOUT wiping bindings.

Why this exists: a naive script upload that sends `"bindings": []` (or
omits bindings) removes the Worker's KV binding and forces a dashboard
re-bind. Cloudflare keeps worker-level secrets across script uploads,
but drops bindings the upload does not redeclare — so this uploader
redeclares the KV binding (namespace id from the configuration below)
and never touches any secret.

Setup: create a Cloudflare API token with "Workers Scripts: Edit"
permission and put it in your environment — it is read from the
environment only, never from a file and never hardcoded:

  export CLOUDFLARE_API_TOKEN=<your token>
  export BRIDGE_KV_NAMESPACE_ID=<your KV namespace id>   # or edit below

Usage:
  deploy_preserve.py upload <account_id> <script_name> <worker_js_path>
  deploy_preserve.py fetch-script <account_id> <script_name> <out_path>
  deploy_preserve.py settings <account_id> <script_name>
"""
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

HOST = "api.cloudflare.com"
BASE = f"https://{HOST}/client/v4"

# --- Configuration (placeholders — replace with your own values) ---
# Your KV namespace id: pass it via the BRIDGE_KV_NAMESPACE_ID env var,
# or replace the placeholder below. It is an identifier, not a secret,
# but a public fork of this template should not carry your real one.
KV_NAMESPACE_ID = os.environ.get("BRIDGE_KV_NAMESPACE_ID", "YOUR_KV_NAMESPACE_ID")
KV_BINDING_NAME = "DONNIE_KV"        # binding name the Worker code reads
COMPATIBILITY_DATE = "2025-01-01"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def call(method, path, body=None, content_type="application/json"):
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        raise SystemExit(
            "CLOUDFLARE_API_TOKEN is not set. Create an API token with "
            "Workers Scripts: Edit permission and export it first."
        )
    data = None
    headers = {"Authorization": "Bearer " + token}
    if body is not None:
        if content_type == "application/json":
            data = json.dumps(body).encode("utf-8")
        else:
            data = body
        headers["Content-Type"] = content_type
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upload(account_id, script_name, js_path):
    if KV_NAMESPACE_ID == "YOUR_KV_NAMESPACE_ID":
        raise SystemExit(
            "KV_NAMESPACE_ID is still the placeholder. Set "
            "BRIDGE_KV_NAMESPACE_ID to your KV namespace id first — "
            "uploading with the placeholder would bind the wrong namespace."
        )
    with open(js_path, "rb") as f:
        script = f.read()
    boundary = "----cfdeploy" + uuid.uuid4().hex
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": COMPATIBILITY_DATE,
        "bindings": [
            {
                "type": "kv_namespace",
                "name": KV_BINDING_NAME,
                "namespace_id": KV_NAMESPACE_ID,
            }
        ],
    }
    parts = []
    parts.append(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="metadata"\r\n'
        "Content-Type: application/json\r\n\r\n".encode()
        + json.dumps(metadata).encode()
        + b"\r\n"
    )
    parts.append(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        "Content-Type: application/javascript+module\r\n\r\n".encode()
        + script
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    status, raw = call(
        "PUT",
        f"/accounts/{account_id}/workers/scripts/{script_name}",
        body=b"".join(parts),
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw[:500].decode("utf-8", errors="replace")}
    print(json.dumps({"http_status": status, "response": payload}, indent=2)[:4000])
    return 0 if status == 200 else 1


def fetch_script(account_id, script_name, out_path):
    status, raw = call(
        "GET", f"/accounts/{account_id}/workers/scripts/{script_name}"
    )
    if status != 200:
        print(json.dumps({"http_status": status, "response": raw[:500].decode("utf-8", errors="replace")}, indent=2))
        return 1
    with open(out_path, "wb") as f:
        f.write(raw)
    print(json.dumps({"http_status": status, "bytes": len(raw), "out": out_path}, indent=2))
    return 0


def settings(account_id, script_name):
    status, raw = call(
        "GET", f"/accounts/{account_id}/workers/scripts/{script_name}/settings"
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {"raw": raw[:500].decode("utf-8", errors="replace")}
    print(json.dumps({"http_status": status, "response": payload}, indent=2)[:4000])
    return 0 if status == 200 else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "upload":
        return upload(sys.argv[2], sys.argv[3], sys.argv[4])
    if cmd == "fetch-script":
        return fetch_script(sys.argv[2], sys.argv[3], sys.argv[4])
    if cmd == "settings":
        return settings(sys.argv[2], sys.argv[3])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
