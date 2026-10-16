Deployment source references
============================

The Docker/Linux/browser image has **not been built or run on the development
host**, which has no Docker runtime. `ci-smoke.yml` is a workflow template: activate
it under `.github/workflows/` and require a successful run before deployment.
The host-independent supervisor tests do not replace the container smoke test.

- [Playwright container guidance](https://playwright.dev/docs/docker): non-root
  Chromium, sandbox/user-namespace support, matching package/browser versions,
  shared-memory allocation and process reaping.
- [nginx auth request](https://nginx.org/en/docs/http/ngx_http_auth_request_module.html):
  both browser static resources and WebSocket upgrades use the backend's owner
  authorization endpoint. Basic owner/demo credentials are handled by the app.
- [noVNC nginx proxy example](https://github.com/novnc/noVNC/wiki/Proxying-with-nginx):
  static viewer assets and the separately proxied WebSocket endpoint.
- `seccomp_profile.json` is the official Playwright Docker profile retrieved from
  [Microsoft's source](https://github.com/microsoft/playwright/blob/main/utils/docker/seccomp_profile.json).
  SHA-256 at retrieval: `cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
  The file is committed locally; CI does not download a mutable profile.
- `package-lock.json` retains registry integrity values from the project's
  installed `@playwright/cli@0.1.18` lock entries and its exact
  `playwright`/`playwright-core@1.63.0-alpha-2026-08-05` dependencies.

Expose only container port **8080** through an HTTPS ingress. Keep **5900**, **6080**
and **8877** internal. Bootstrap starts as root only to prepare the fixed `/data`
directories, then drops supplementary groups and permanently sets **UID/GID 10001**
before starting services. It never recursively changes ownership. The volume contains private browser login state
and report history. Use one replica for this single-owner browser demo.

The host must permit Chromium's sandbox and required user namespaces. The CI
template uses the official seccomp profile; the deployment platform must support
equivalent restrictions. Failure to launch is fatal; there is no unsandboxed
fallback. This image is not verified on Alternate Clouds until its build and
runtime smoke tests pass there or on a compatible Linux container runtime.
