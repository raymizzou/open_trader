Authentic offline legacy input for the Issue344 public cold-restart producer.

`source.tar.gz` is a deterministic gzip of read-only `git archive` at the commit and selected paths in `manifest.json`. File bytes are original Git objects. The test verifies every member and archive hash before data-filtered extraction. This fixture permits the real old configure/pause/normal-round API to produce legacy state without Git history or runtime network access.
