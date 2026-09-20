# Local distribution bundle

Build from a clean committed checkout using Python 3.12, the repository's required uv version,
and the locked Node/npm dependencies:

```sh
uv run --no-project --python 3.12 python scripts/build_distribution.py --output-dir /absolute/output-a
uv run --no-project --python 3.12 python scripts/build_distribution.py --output-dir /absolute/output-b
```

Builds run in a fresh export of the exact tracked commit, excluding ignored build caches and
untracked artifacts. The output directories must be outside the checkout. Existing bundles are never overwritten.
Compare the two printed SHA-256 values; identical commits and build tools should yield identical
ZIP bytes. The archive contains one noneditable wheel, matching Web assets, hash-locked runtime
requirements, example scenarios, INSTALL.md, a source commit manifest and SHA256SUMS.

Extract into a separate directory, run `shasum -a 256 -c SHA256SUMS`, then follow INSTALL.md.
Installation needs Python 3.12 and access to the configured dependency index unless packages are
already cached. Running the local product requires neither Node.js nor the source checkout.

This command only builds an unpublished artifact. A release requires its own verified version,
exact commit, artifact hashes and explicit Product Owner authorization before tagging or publishing.
