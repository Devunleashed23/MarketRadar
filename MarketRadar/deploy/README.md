# Alternate Clouds demo

Build context is the repository root. The GitHub workflow builds the image, checks startup, login roles, the protected browser viewer and a synthetic walkthrough, then publishes an immutable commit tag to GHCR.

Use a Docker service on Alternate Clouds with **port 8080**, 2 CPU cores, 4 GiB RAM, no GPU, and a persistent volume at `/data` writable by UID/GID 10001. The provider must support Chromium sandbox user namespaces; keep the sandbox enabled. The local Docker example uses the supplied seccomp profile.

Set these runtime values through the cloud dashboard, never in source:

- `MARKETRADAR_PUBLIC_URL`: the service's HTTPS URL, with no path.
- `MARKETRADAR_OWNER_PASSWORD`: a dedicated demo password, at least 16 characters.
- `MARKETRADAR_DEMO_PASSWORD`: a different password, at least 16 characters.
- `AF_API_KEY`: Alternate Clouds inference credential.
- `AF_ORG_ID`: organization that funds inference.
- Optional `MARKETRADAR_CLOUD_MODEL`: defaults to `gpt-5.6-luna`.

The owner login runs live research and opens `/browser/vnc.html#autoconnect=1&resize=scale&path=browser/websockify` to sign in to Google manually. The `demo` login can explore labeled samples, review saved reports and export results. It cannot launch live research, control the browser, configure integrations or publish copies.

For a live presentation: sign in as `owner`, verify Keyword Planner access in the cloud browser, run one product query, show source evidence, then accept or decline follow-ups and record a rationale. Browser login and CAPTCHA remain human steps. Sample mode is explicitly synthetic and does not prove live collection.

Only the HTTPS app port is exposed. VNC and browser-control services bind to loopback. The container does not include a Codex login or local browser profile. Model credentials and dashboard passwords are stripped from browser subprocesses. This is one shared demo workspace, not a multi-tenant product.

Deployment is only complete after the live URL, cloud browser and one real inference call succeed. A created cloud service or successful source build alone does not establish this.
