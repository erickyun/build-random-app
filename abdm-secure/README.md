# AB Download Manager 2.0.3 Secure Build

This build coordinator checks out fixed source revisions of:

- `kerollosy/ab-download-manager` at `203b8ea059bc18e70d972d48e41267dcfd20dd13`
- `kerollosy/ab-download-manager-browser-integration` at `93da0915d7c9b9f1154909db55338ab91ed777d9`

It then replaces only the yt-dlp integration files with the patches in this directory and builds the Windows x64 application and Chrome extension.

## Security changes

- Accept only HTTP/HTTPS yt-dlp URLs with valid hosts.
- Reject embedded URL credentials, control characters and option-like input.
- Pass user URLs after the `--` end-of-options marker.
- Add `--ignore-config` so a user or machine-level yt-dlp config cannot silently inject commands or plugins.
- Pin yt-dlp to `2026.06.09` and verify its official SHA-256.
- Pin FFmpeg to BtbN build `autobuild-2026-07-23-14-16`; verify the official checksum manifest and then the ZIP.
- Pin Deno to `v2.9.4`; verify its immutable release checksum before extraction.
- Download to temporary files and replace binaries atomically only after verification.
- Serialize tool installation to avoid partially written executables.
- Build the matching browser extension from its exact source commit.

The resulting Windows installer is still unsigned because no Authenticode certificate is available. Always compare its SHA-256 with the release's `SHA256SUMS.txt`.
