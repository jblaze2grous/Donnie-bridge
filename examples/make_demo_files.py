#!/usr/bin/env python3
"""Create the demo binary files used by the examples workspace.

The Donnie Bridge snapshot can carry binary files (PDFs, pictures) as base64
blobs. Two tiny demo binaries ship as base64 strings in this script so the
whole repository stays text-only; run this once to materialize them before
running the snapshot generator:

    python3 examples/make_demo_files.py
"""
import base64
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
TARGET = HERE / "goals" / "demo-project" / "files"

DEMO_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAJ0lEQVR4nGO4Y2ODFdmciMKKGEY10ESDTdQdrOjDJRusaFQDTTQAAC2NiJB1ayGmAAAAAElFTkSuQmCC"
DEMO_PDF_B64 = "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCA2MTIgNzkyXSAvUmVzb3VyY2VzIDw8IC9Gb250IDw8IC9GMSA0IDAgUiA+PiA+PiAvQ29udGVudHMgNSAwIFIgPj4KZW5kb2JqCjQgMCBvYmoKPDwgL1R5cGUgL0ZvbnQgL1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhID4+CmVuZG9iago1IDAgb2JqCjw8IC9MZW5ndGggMjI0ID4+CnN0cmVhbQpCVCAvRjEgMTQgVGYgNTYgNzU2IFRkIChEZW1vIFBERiAtIHNhbXBsZSBjb250ZW50IGZvciB0aGUgRG9ubmllIEJyaWRnZSB0ZW1wbGF0ZS4pIFRqIDAgLTIyIFRkIChUaGlzIGZpbGUgaXMgb2J2aW91c2x5IGZha2UgZGVtbyBkYXRhLikgVGogMCAtMjIgVGQgKEl0IGV4aXN0cyBzbyB0aGUgZ2VuZXJhdG9yJ3MgYmluYXJ5IFwoUERGL2ltYWdlXCkgcGF0aCBpcyBleGVyY2lzZWQuKSBUaiBFVAplbmRzdHJlYW0KZW5kb2JqCnhyZWYKMCA2CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAwOSAwMDAwMCBuIAowMDAwMDAwMDU4IDAwMDAwIG4gCjAwMDAwMDAxMTUgMDAwMDAgbiAKMDAwMDAwMDI0MSAwMDAwMCBuIAowMDAwMDAwMzExIDAwMDAwIG4gCnRyYWlsZXIKPDwgL1NpemUgNiAvUm9vdCAxIDAgUiA+PgpzdGFydHhyZWYKNTg2CiUlRU9GCg=="


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)
    (TARGET / "demo-image.png").write_bytes(base64.b64decode(DEMO_PNG_B64))
    (TARGET / "demo-document.pdf").write_bytes(base64.b64decode(DEMO_PDF_B64))
    print("Wrote demo-image.png and demo-document.pdf to", TARGET)


if __name__ == "__main__":
    main()
