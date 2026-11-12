# Phone setup for a private tailnet

These steps are for a separate installation from this source copy. They do not change an existing Opsdeck service or Tailscale Serve route.

1. Complete the local setup in [README.md](README.md), then confirm `http://127.0.0.1:8091/api/hello` answers on the server.
2. Install Tailscale on the server and phone and sign both into the same tailnet. On the server, run `tailscale serve status` first and choose an unused HTTPS port. Do not reset existing Serve configuration.
3. For example, with port 8444 unused, run `tailscale serve --bg --https=8444 http://127.0.0.1:8091`. Run `tailscale serve status` again and copy the HTTPS URL it reports. [Tailscale Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
4. Set `OPSDECK_ALLOWED_ORIGINS` on the Opsdeck server to that URL's **origin** (scheme, hostname, and port, without a trailing slash), then restart the Opsdeck server. This permits the Terminal WebSocket through the HTTPS proxy. Keep `OPSDECK_TOKEN` in the server's private environment.
5. On the phone, open that HTTPS URL in Safari while Tailscale is connected. Enter the token in Opsdeck. Check that Fleet loads and Terminal says **connected**.
6. In Safari, use **Share → Add to Home Screen**, enable **Open as Web App** when offered, and tap **Add**. Open the new icon and enter the token there if prompted. [Apple iPhone guide](https://support.apple.com/guide/iphone/iphea86e5236/ios)

Tailscale Serve is limited to the tailnet; Tailscale Funnel is a separate public exposure feature. This guide has not yet been verified on a phone outside the home network.
