# shellbox-rootfs

The Linux systems [ServerBox](https://github.com/lollipopkit/flutter_server_box)
installs, repacked into one shape, described by a signed manifest.

## Why this exists

The app used to carry each distribution's URL and digest compiled in. That
works until a distribution republishes: Rocky rebuilds its container base every
few weeks, and the pin an app shipped last month names a file that is no longer
there. Fixing it meant a release.

It also meant the app carried a reader for every shape a distribution chose to
publish. Rocky publishes no plain rootfs at all, only an OCI image layout —
`index.json` naming a manifest, the manifest naming layers — so the app had to
walk one on a phone, in Dart, holding a decompressed layer in memory.

So: normalise here, describe the result in a manifest, and let the app read the
manifest.

## What the normalisation does

| | |
| --- | --- |
| layout | An OCI image becomes a plain tar. This is what lets the app stop carrying an image reader. |
| directory modes | Every directory gets its owner-write bit back. Rocky ships 17 at 0555, `/usr/bin` and `/usr/lib` among them, and both platforms run the guest as a fake root over a real unprivileged uid — so a package manager cannot create its temp files and every install fails partway through unpacking. |
| file modes | Files with no owner-read get it. `/etc/shadow` ships 0000, which on a real system is fine because root bypasses the check; in the guest "root" is a fiction over the owner's own uid, so 0000 means unreadable to everything. |
| device nodes | Dropped. Neither platform can create one without root, and both build their own `/dev` at boot. |

Deliberately **not** done: stripping documentation or locales. It would save
tens of megabytes and it would also make the package database disagree with the
filesystem, so `rpm --verify` and `dpkg --verify` would report a system that had
been tampered with. A large honest image beats a small lying one.

Flattening Rocky's image and re-compressing it happens to save 32 MB anyway —
80.8 MB upstream against 48.7 MB — because an OCI wrapper around a gzipped
layer is not a compact way to ship a filesystem.

## The trust chain

Three parties, each deciding exactly one thing:

- **the app** decides *which key*, by carrying the public half compiled in;
- **this repository** decides *which bytes*, by signing the manifest;
- **a mirror** decides only *where the bytes come from*.

That is the invariant the app's compiled-in digests used to provide, kept rather
than traded away for convenience.

`sources.json` holds an upstream digest for every distribution, and
`scripts/build.py` refuses anything that does not match it. So everything
published here derives from a file that matched a digest a human committed, and
a compromised mirror produces a failed build rather than a signed artifact.
The release notes print both digests, so the claim is checkable: fetch the same
upstream file, run the same script, compare.

Builds are reproducible — sorted entries, zeroed mtimes and owners, `gzip -n`
because a gzip container carries a timestamp that `--mtime` does not reach.

### Rotating the key

The private half is not here and never was. Generate one, keep it in the
maintainer's keychain, and put the base64 PEM in the `MANIFEST_SIGNING_KEY`
secret. Rotating means shipping an app build, which is the point: a key a
server could replace would protect nothing.

`publish.yml` verifies its own signature against the key the app carries before
releasing, so a secret holding the wrong key fails the run instead of shipping
something every device rejects.

## Layout

    sources.json          what each distribution publishes, and its digest
    scripts/build.py      fetch, verify, normalise, repack, write manifest.json
    scripts/track.py      look for a newer upstream file within the pinned series
    scripts/notes.py      release notes, including both digests

## Publishing

`publish.yml`, run by hand, with a tag and a serial.

The serial must only ever increase. The app refuses a manifest whose serial is
below the highest it has already accepted — a signature stays valid as long as
the key does, so without that check an old signed copy could be replayed to pin
a device to a rootfs whose problems are known.

## Following upstream

`track-upstream.yml` runs weekly and opens a pull request when a newer file
appears **within the series** a distribution is pinned to. Moving between series
is reported and never automated: Alpine is held at 3.22 because 3.23 ships
apk-tools 3, whose network fetches fail under proot on Android, and no tracker
can be trusted to know that.

Merging a pull request moves the pin. It does not publish.
