#!/usr/bin/env python3
"""Release notes: what is in this release and what it was built from.

The upstream URL and digest of every artifact, because that is the whole of
the provenance claim. Anyone can fetch the same upstream file, check it against
the digest printed here, run scripts/build.py, and compare the result — which
is what makes "repacked by us" a checkable statement rather than one that has
to be taken on trust.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    manifest = json.loads(Path(sys.argv[1]).read_text())
    out: list[str] = []

    out.append(f"Manifest serial `{manifest['serial']}`, ")
    out[-1] += f"valid until `{manifest['valid_until']}`."
    out.append("")
    out.append(
        "`manifest.json` is signed; `manifest.json.sig` is a raw Ed25519 "
        "signature over its exact bytes. ServerBox carries the public key and "
        "refuses anything that does not verify, is older than what it has "
        "already accepted, or has expired."
    )
    out.append("")

    for name, d in sorted(manifest["distros"].items()):
        r, u = d["rootfs"], d["upstream"]
        saved = u["size_bytes"] - r["size_bytes"]
        out.append(f"### {d['label']} {d['version']}")
        out.append("")
        out.append(f"| | file | sha256 | size |")
        out.append(f"| --- | --- | --- | --- |")
        out.append(
            f"| repacked | `{r['url'].rsplit('/', 1)[-1]}` "
            f"| `{r['sha256']}` | {mb(r['size_bytes'])} |"
        )
        out.append(
            f"| upstream | [{u['url'].rsplit('/', 1)[-1]}]({u['url']}) "
            f"| `{u['sha256']}` | {mb(u['size_bytes'])} |"
        )
        out.append("")
        if u["layout"] != r["layout"]:
            out.append(
                f"Flattened from `{u['layout']}` to `{r['layout']}`."
            )
        # Only when it is worth a sentence. Alpine's differs by a few kilobytes
        # either way depending on gzip's mood, and reporting that as a result
        # invites reading noise as a trend.
        if abs(saved) > 1 << 20:
            out.append(
                f"Repacking {'saves' if saved > 0 else 'costs'} "
                f"{mb(abs(saved))}{'' if saved > 0 else ' more'}."
            )
        out.append("")

    print("\n".join(out))


def mb(n: int) -> str:
    return f"{n / 1048576:.1f} MB"


if __name__ == "__main__":
    main()
