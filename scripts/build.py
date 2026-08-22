#!/usr/bin/env python3
"""Turn what the distributions publish into one shape ServerBox can unpack.

Reads sources.json, fetches each upstream file, checks it against the digest
recorded there, normalises it, and writes manifest.json plus the artifacts.

Three things the normalisation does, each because the app or the engine could
not do it for itself:

  layout      An OCI image layout becomes a plain tar. Rocky publishes no
              plain rootfs, only an image — index.json naming a manifest, the
              manifest naming layers — and flattening it here is the whole
              reason the app can stop carrying an image reader.

  modes       Every directory gets its owner-write bit back. Rocky ships 17
              at 0555, /usr/bin and /usr/lib among them. Both platforms run
              the guest as a fake root over a real unprivileged uid, so a
              package manager cannot create its temp files there and every
              install fails partway through unpacking.

  device      Device nodes are dropped. Neither platform can create one
  nodes       without root, and both build their own /dev at boot.

Deliberately *not* done: stripping documentation or locales. It would save
tens of megabytes and it would also make the package database disagree with
the filesystem, so `rpm --verify` and `dpkg --verify` would report a system
that had been tampered with. A large honest image beats a small lying one.

Usage:
    scripts/build.py --out dist [--only alpine,rocky]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def gnu_tar() -> str:
    """GNU tar, which is what the reproducible flags below need.

    CI runs on Linux where `tar` is GNU already. macOS ships bsdtar, which has
    no --sort and no --mtime, so a build there would produce an archive whose
    bytes depend on directory order — and a digest nobody else can reproduce
    is a digest that cannot be checked. `brew install gnu-tar` provides gtar.
    """
    for name in ("gtar", "gnutar", "tar"):
        path = shutil.which(name)
        if not path:
            continue
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False
        ).stdout
        if "GNU tar" in out:
            return path
    raise SystemExit(
        "GNU tar is required for a reproducible archive; on macOS: brew install gnu-tar"
    )

# How long a fetched manifest stays acceptable. Long enough that a quiet
# period upstream does not strand anyone, short enough that a copy replayed
# after this repository stops publishing eventually stops being believed.
VALID_FOR = timedelta(days=180)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url: str, dest: Path, expected: str) -> None:
    """Downloads `url` to `dest` and refuses anything but `expected`.

    The first link of the chain. Everything this repository publishes is
    derived from a file that matched a digest recorded in sources.json, so a
    mirror serving something else produces a failed build and not a signed
    artifact.
    """
    log(f"  fetching {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)
    got = sha256_of(tmp)
    if got != expected:
        tmp.unlink(missing_ok=True)
        raise SystemExit(
            f"digest mismatch for {url}\n  expected {expected}\n  got      {got}"
        )
    tmp.replace(dest)


def extract_plain(archive: Path, into: Path) -> None:
    # The system tar rather than tarfile: symlinks, hardlinks and long names in
    # a real rootfs are what a hand-rolled extractor gets subtly wrong, and
    # `/bin/sh -> busybox` becoming a copy is a rootfs of one program
    # pretending to be two hundred. Device nodes fail without root and are
    # skipped, which is what is wanted.
    # Device nodes need root and are expected to fail; anything else is worth
    # seeing. Silencing the lot is how a tree broken by, say, a case-insensitive
    # host filesystem would look exactly like a good one.
    res = subprocess.run(
        [gnu_tar(), "xf", str(archive), "-C", str(into)],
        check=False,
        capture_output=True,
        text=True,
    )
    complaints = [
        line
        for line in res.stderr.splitlines()
        if line.strip() and "Cannot mknod" not in line and "Cannot mkfifo" not in line
    ]
    for line in complaints[:10]:
        log(f"    tar: {line}")
    if complaints:
        log(f"    tar had {len(complaints)} complaint(s) that were not device nodes")


def extract_oci(archive: Path, into: Path, workdir: Path) -> None:
    """Applies an OCI image layout's layers, in order, onto `into`."""
    layout = workdir / "oci"
    layout.mkdir()
    subprocess.run([gnu_tar(), "xf", str(archive), "-C", str(layout)], check=True)

    def blob(digest: str) -> Path:
        algo, value = digest.split(":", 1)
        return layout / "blobs" / algo / value

    index = json.loads((layout / "index.json").read_text())
    manifests = index.get("manifests") or []
    if not manifests:
        raise SystemExit("the image index names no manifest")
    manifest = json.loads(blob(manifests[0]["digest"]).read_text())
    layers = manifest.get("layers") or []
    log(f"  {len(layers)} layer(s)")

    for layer in layers:
        media = layer.get("mediaType", "")
        if media.endswith("+zstd"):
            raise SystemExit("zstd layers are not supported")
        extract_plain(blob(layer["digest"]), into)
        apply_whiteouts(into)


def apply_whiteouts(tree: Path) -> None:
    """Acts on the markers tar just wrote out as ordinary files.

    A layer deletes a path by carrying `.wh.<name>` in its place, and says
    "everything already here is gone" with `.wh..wh..opq`. tar writes both as
    files because that is what they are in the archive; only a reader of the
    image knows they are instructions.
    """
    for path in sorted(tree.rglob(".wh.*")):
        name = path.name
        if name == ".wh..wh..opq":
            for child in path.parent.iterdir():
                if child != path:
                    remove(child)
        else:
            remove(path.parent / name[len(".wh.") :])
        path.unlink(missing_ok=True)


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def make_dirs_writable(tree: Path) -> int:
    """Gives every directory its owner-write bit back. Returns how many needed it."""
    fixed = 0
    for dirpath, _dirnames, _files in os.walk(tree):
        mode = os.stat(dirpath).st_mode & 0o7777
        if not mode & 0o200:
            os.chmod(dirpath, mode | 0o200)
            fixed += 1
    return fixed


def make_files_readable(tree: Path) -> int:
    """Gives owner-read to any file that has none. Returns how many needed it.

    Rocky and Ubuntu both ship /etc/shadow and friends at mode 0000. On a real
    system that is fine because root bypasses the check; in the guest "root" is
    a fiction over the app's own uid, which is also the owner — so 0000 means
    unreadable to everything, not just to the unprivileged.

    It also has to be done for the archive to be written at all: tar reads the
    file to copy it, and cannot.

    Nothing is loosened beyond the owner. Every process in the guest runs as
    the same real uid, so a mode that distinguished them would be describing an
    isolation that is not there.
    """
    fixed = 0
    for dirpath, _dirnames, files in os.walk(tree):
        for name in files:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            mode = os.stat(path).st_mode & 0o7777
            if not mode & 0o400:
                os.chmod(path, mode | 0o400)
                fixed += 1
    return fixed


def repack(tree: Path, dest: Path, compression: str) -> None:
    """Writes `tree` as one tar, sorted, with no host-specific metadata.

    Sorted and numeric-owner so that two builds of the same input produce the
    same bytes: a digest that changes for no reason is one nobody can check.

    `gzip -n` rather than tar's own `-z`, because a gzip container carries a
    timestamp of its own that --mtime does not reach. Without it two builds of
    identical input differ in exactly four bytes — measured, not assumed: the
    inner tars hashed the same and the headers read 4896896a against 5596896a.
    xz has no such field and needs no equivalent, but it is pinned to one
    thread for the same reason: thread count changes the block layout.
    """
    program = {"gzip": "gzip -n", "xz": "xz -T1"}[compression]
    subprocess.run(
        [
            gnu_tar(),
            "-c",
            f"--use-compress-program={program}",
            "--numeric-owner",
            "--sort=name",
            "--mtime=@0",
            "--owner=0",
            "--group=0",
            "-f",
            str(dest),
            "-C",
            str(tree),
            ".",
        ],
        check=True,
    )


def build_one(name: str, release: dict, out: Path, tag: str) -> dict:
    """One release of one distribution: fetch, normalise, repack, describe.

    Named `<distro>-<version>` throughout rather than `<distro>`, because two
    releases of one distribution are two artifacts in one release directory
    and a name that carried only the distribution would have the second
    overwrite the first.
    """
    log(f"{name} {release['version']}:")
    up = release["upstream"]
    compression = release["repack"]["compression"]
    suffix = {"gzip": "tar.gz", "xz": "tar.xz"}[compression]

    with tempfile.TemporaryDirectory(prefix=f"sbrootfs-{name}-") as td:
        work = Path(td)
        archive = work / "upstream"
        fetch(up["url"], archive, up["sha256"])

        tree = work / "tree"
        tree.mkdir()
        if up["layout"] == "oci":
            extract_oci(archive, tree, work)
        else:
            extract_plain(archive, tree)

        fixed = make_dirs_writable(tree)
        log(f"  {fixed} directory mode(s) made writable")
        readable = make_files_readable(tree)
        log(f"  {readable} file mode(s) given owner-read")

        artifact = out / f"{name}-{release['version']}-arm64.{suffix}"
        repack(tree, artifact, compression)

    digest = sha256_of(artifact)
    size = artifact.stat().st_size
    log(f"  {artifact.name}  {size} bytes  {digest[:16]}…")

    base = f"https://github.com/lollipopkit/shellbox-rootfs/releases/download/{tag}"
    return {
        "version": release["version"],
        "branch": release["branch"],
        "rootfs": {
            "url": f"{base}/{artifact.name}",
            "sha256": digest,
            "size_bytes": size,
            # Always plain: flattening an image layout is the point.
            "layout": "plain",
            "compression": compression,
            # These are on one host, which is not any distribution's mirror.
            "follows_mirror": False,
        },
        "upstream": {
            "url": up["url"],
            "sha256": up["sha256"],
            "size_bytes": upstream_size(up["url"]),
            "layout": up["layout"],
            "compression": up["compression"],
            "follows_mirror": up["follows_mirror"],
        },
    }


def upstream_size(url: str) -> int:
    """Content-Length, so the app can show a size for the fallback source too."""
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as r:
        length = r.headers.get("Content-Length")
    if not length:
        raise SystemExit(f"no Content-Length for {url}")
    return int(length)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--tag", default="dev", help="release tag the URLs point at")
    ap.add_argument("--serial", type=int, default=1)
    ap.add_argument("--only", default="", help="comma-separated subset")
    ap.add_argument("--now", default="", help="RFC3339, for a reproducible build")
    args = ap.parse_args()

    sources = json.loads((ROOT / "sources.json").read_text())["distros"]
    wanted = [s for s in args.only.split(",") if s] or list(sources)
    unknown = [s for s in wanted if s not in sources]
    if unknown:
        raise SystemExit(f"not in sources.json: {', '.join(unknown)}")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    now = (
        datetime.fromisoformat(args.now.replace("Z", "+00:00"))
        if args.now
        else datetime.now(timezone.utc)
    ).replace(microsecond=0)

    distros = {}
    for name in wanted:
        spec = sources[name]
        # Refused rather than resolved: two releases of one series would be one
        # picker row hiding another, and the app's "is this an update" answer
        # would come out as whichever this loop happened to write last.
        branches = [r["branch"] for r in spec["releases"]]
        duplicate = {b for b in branches if branches.count(b) > 1}
        if duplicate:
            raise SystemExit(f"{name}: two releases share a branch: {duplicate}")
        distros[name] = {
            "label": spec["label"],
            "package_manager": spec["package_manager"],
            "default_mirror": spec["default_mirror"],
            # In the order sources.json gives them. The first is what a plain
            # install gets, so this is a decision and not a serialisation
            # detail — sorting it here would move that decision into a
            # comparison of version strings no two distributions spell alike.
            "releases": [
                build_one(name, release, out, args.tag)
                for release in spec["releases"]
            ],
        }

    manifest = {
        "schema": 2,
        "serial": args.serial,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "valid_until": (now + VALID_FOR).isoformat().replace("+00:00", "Z"),
        "distros": distros,
    }
    # Sorted and newline-terminated: the bytes are what gets signed, so two
    # runs over the same input have to produce the same file.
    # Sorted keys, but `releases` is a list and keeps the order above.
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    log(f"\nwrote {out / 'manifest.json'}")


if __name__ == "__main__":
    main()
