# Security

Opsdeck's token grants access to the terminal, GPU process Stop actions, configured agent launch controls, model endpoints and worker actions, Ask relay, and Markdown in `OPSDECK_DOCS_DIR`, with the privileges of the server account. Treat it like an SSH credential.

- Run the server as an unprivileged account and keep it on localhost or a private network behind HTTPS. Do not expose the terminal service directly to the public Internet.
- Generate a unique token of at least 32 characters, keep it outside the repository, and rotate it if a device or browser profile is lost.
- The browser saves the token in local storage so the phone can reconnect. Clear the site's data on a shared device.
- Configure only SSH aliases and a Docs directory that this account is allowed to access. The server does not manage SSH keys or user permissions.

If you spot a vulnerability in a public release, use the repository's **Security → Report a vulnerability** form. This personal project has no guaranteed response time or maintenance commitment. Please avoid posting exploit details in a public issue. If private reporting is unavailable, open a brief issue asking the maintainer for a private contact route.
