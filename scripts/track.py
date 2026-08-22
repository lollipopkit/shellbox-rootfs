#!/usr/bin/env python3
"""Looks for a newer upstream file, within each series that is pinned.

Within the series on purpose. A point release is a change this repository can
take unattended: same branch, same package manager, same everything the app
knows. Moving between series is not — Alpine is deliberately held at 3.22
because 3.23 ships apk-tools 3, whose fetches fail under proot on Android, and
no amount of automation can be trusted to know that. So this reports a newer
series and changes nothing.

A distribution holds several releases and each is one pin, moving on its own.
Adding one is never this script's doing: a new series needs its repository
file checked against the app's, and its package manager tried under proot and
under the ish engine.

    scripts/track.py            # report
    scripts/track.py --write    # update sources.json in place

Prints nothing and exits 0 when everything is current, so a workflow can use
"did the file change" as its signal.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get(url: str) -> str:
    with urllib.request.urlopen(url) as r:
        return r.read().decode("utf-8", "replace")


def version_key(v: str) -> tuple:
    """Compares 9.8-20260525.0 and 3.22.5 the way a person would."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-]", v))


def candidates(directory: str, pattern: str) -> list[tuple[str, str]]:
    """(version, filename) for every file in `directory` matching `pattern`.

    Scraped from the index page. These are plain Apache/nginx listings and have
    been for years; a redesign shows up as "nothing found", which the caller
    reports rather than silently treating as "up to date".
    """
    html = get(directory)
    found = {}
    for m in re.finditer(pattern, html):
        found[m.group("version")] = m.group(0)
    return sorted(found.items(), key=lambda kv: version_key(kv[0]))


def digest_from_sums(url: str, filename: str) -> str | None:
    """The digest upstream publishes, if it publishes one next to the file."""
    try:
        text = get(url)
    except Exception:
        return None
    for line in text.splitlines():
        # Two shapes: `<sha>  <name>` and `SHA256 (<name>) = <sha>`.
        if line.strip().endswith(filename) and len(line.split()) == 2:
            return line.split()[0]
        m = re.match(rf"SHA256 \({re.escape(filename)}\) = ([0-9a-f]{{64}})", line)
        if m:
            return m.group(1)
    return None


def check_alpine(release: dict) -> tuple[str, str, str] | None:
    branch = release["branch"]  # v3.22
    base = f"https://dl-cdn.alpinelinux.org/alpine/{branch}/releases/aarch64/"
    series = branch.lstrip("v")
    found = candidates(
        base,
        rf"alpine-minirootfs-(?P<version>{re.escape(series)}\.\d+)-aarch64\.tar\.gz",
    )
    if not found:
        return None
    version, name = found[-1]
    return version, base + name, digest_from_sums(base + name + ".sha256", name)


def check_ubuntu(release: dict) -> tuple[str, str, str] | None:
    # The directory is the series (24.04), which the pinned version may carry a
    # point release on top of (24.04.4). Taking it off the version rather than
    # naming it separately keeps one place for it to be wrong.
    series = ".".join(release["version"].split(".")[:2])
    base = f"https://cdimage.ubuntu.com/ubuntu-base/releases/{series}/release/"
    found = candidates(
        base,
        rf"ubuntu-base-(?P<version>{re.escape(series)}(?:\.\d+)?)-base-arm64\.tar\.gz",
    )
    if not found:
        return None
    version, name = found[-1]
    return version, base + name, digest_from_sums(base + "SHA256SUMS", name)


def check_rocky(release: dict) -> tuple[str, str, str] | None:
    major = release["branch"]  # 9
    base = f"https://dl.rockylinux.org/pub/rocky/{major}/images/aarch64/"
    found = candidates(
        base,
        rf"Rocky-{major}-Container-Base-(?P<version>{major}\.\d+-\d+\.\d+)"
        rf"\.aarch64\.oci\.tar\.xz",
    )
    if not found:
        return None
    build, name = found[-1]
    # The manifest's version is the release (9.8); the build date only ever
    # appears in the file name, which is what keeps the pin from moving.
    return build, base + name, digest_from_sums(base + name + ".CHECKSUM", name)


CHECKS = {
    "alpine": check_alpine,
    "ubuntu": check_ubuntu,
    "rocky": check_rocky,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    path = ROOT / "sources.json"
    doc = json.loads(path.read_text())
    changed = False

    for name, spec in doc["distros"].items():
        check = CHECKS.get(name)
        if check is None:
            log(f"{name}: no tracker, skipped")
            continue
        # Every release, each within its own series. A distribution holding
        # two of them has two pins that move independently, and checking only
        # the first would leave the others frozen at whatever shipped.
        for release in spec["releases"]:
            where = f"{name} {release['branch']}"
            try:
                latest = check(release)
            except Exception as e:
                log(f"{where}: could not check ({e})")
                continue
            if latest is None:
                log(f"{where}: found nothing at upstream — has the listing changed?")
                continue

            version, url, digest = latest
            if url == release["upstream"]["url"]:
                log(f"{where}: current")
                continue
            if digest is None:
                log(f"{where}: {url} is newer, but upstream publishes no digest "
                    f"beside it — update by hand")
                continue

            log(f"{where}: newer file\n    {url}\n    {digest}")
            changed = True
            if args.write:
                release["upstream"]["url"] = url
                release["upstream"]["sha256"] = digest
                # Alpine and Ubuntu name the release in the file; Rocky's
                # version is the release and the build date is only in the
                # name.
                if name != "rocky":
                    release["version"] = version

    if changed and args.write:
        path.write_text(json.dumps(doc, indent=2) + "\n")
        log("\nsources.json updated")
    sys.exit(0)


if __name__ == "__main__":
    main()
