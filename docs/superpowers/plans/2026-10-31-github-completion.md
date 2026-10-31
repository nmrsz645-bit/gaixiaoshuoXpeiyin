# GitHub source and Windows release completion plan

**Goal:** Publish complete, documented stable 1.0.30 in the existing public repository and keep 1.0.31-beta separately documented and released in the existing private repository.

**Architecture:** Stable runtime code stays identical to the released source at `ad6c3aa3f32139102c55630cb6017d2c45bb8018`. The private beta derives from stable commit `3260809b9fc1a2f31a4ff675ddca214a103443ce`; its interface/installer changes remain a prerelease. Reuse immutable Windows artifacts after source, archive, privacy and checksum verification; never alter production OSS.

**Tech Stack:** Windows, CPython 3.13, Tkinter, PyInstaller 6.14.2, edge-tts 7.2.5; NSIS 3 for the beta installer.

## Constraints and verification focus

- Existing repository visibility and ownership stay unchanged; no force pushes, history deletion or replaced release assets.
- Publish only tracked source and safe templates. Scan reachable history and extracted packages for secrets; review protected runtime filenames and documentation for private material.
- Distinguish stable 1.0.30, beta 1.0.31-beta, source commits, Windows artifact hashes, and unexecuted online business acceptance.
- Do not launch production queues, real AI/TTS/Webhook calls, installers against existing installations, or update agents. No OSS/download-site writes.
- A pre-existing cross-drive `os.path.relpath` regression fixture requires test TEMP on the checkout drive; record the initial failure and the isolated environment rerun.

## Task 1: Establish provenance and baseline

- [x] Read repository instructions and compare GitHub branches/tags/releases with local source and Windows artifact directories.
- [x] Confirm account ownership, public/private visibility and workflows' production effects.
- [ ] Run the stable 409-test suite and beta suite in isolated temporary directories.
- [ ] Scan both source histories with checksum-verified Gitleaks, and compare beta build source with the remote beta branch.

## Task 2: Complete version-specific documentation

Files: `README.md`, `使用说明.txt`, `HANDOFF.md`, `TIMEOFF.md`, `AGENTS.md`, `examples/configuration/`, and `docs/releases/2026-10-31-github-verification.md` in each applicable branch.

- [ ] Document installation, initial configuration, operating modes, protected state, errors and recovery evidence.
- [ ] Document architecture/modules, actual dependencies, development/test/build commands, credential acquisition and empty templates.
- [ ] Document stable/beta repository relationship, release procedure, actual source/artifact provenance, completed verification and remaining limits.
- [ ] Remove obsolete current-version instructions; use HANDOFF as the maintained entry point.
- [ ] Confirm runtime source unchanged against original stable/beta commits.

## Task 3: Audit and publish

- [ ] Validate ZIP CRC, manifest hashes, version/layout and protected filenames; scan extracted text for secrets and review installer contents.
- [ ] Review the complete branch independently before publication, address material findings and scan final working trees/history.
- [ ] Commit documentation normally, recheck remote heads and fast-forward the authorized existing branches only.
- [ ] Create missing `v1.0.30` stable and `v1.0.31-beta` prerelease tags/releases; upload immutable Windows artifacts plus SHA256SUMS and version-specific docs.
- [ ] Do not publish a beta as stable or upload private beta code to the public repository.

## Task 4: Verify remote delivery

- [ ] Verify remote full commits, key source blobs, README/HANDOFF/templates and release flags/assets.
- [ ] Download the GitHub assets and compare SHA-256/ZIP integrity with the audited local originals.
- [ ] Report actual tests, downloads, hashes and remaining unexecuted business/installer checks. Keep local audit evidence outside Git.
