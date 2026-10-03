# Third-party notices

`seccomp_profile.json` is derived from Microsoft Playwright v1.63.0:
https://github.com/microsoft/playwright/blob/v1.63.0/utils/docker/seccomp_profile.json

Copyright Microsoft Corporation. Licensed under Apache License 2.0; the license is
included as `LICENSE.playwright`. The sole semantic change adds an explicit
`clone3` ENOSYS denial so modern glibc can fall back to the allowed `clone` syscall. The browser image and Python dependencies retain
their own licenses. The profile hash and verification limits are documented in
`docs/browser-worker.md`.
