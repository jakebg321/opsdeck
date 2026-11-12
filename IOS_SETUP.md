# Native iPhone shell

This is the Capacitor/Xcode wrapper from my personal Opsdeck, with example network addresses and bundle ID. It probes the configured HTTPS and LAN origins for `/api/hello`, opens the first Opsdeck response, refreshes when the phone changes networks, and can preload the access token from an Xcode build setting. The web app is also usable directly in Safari.

To point this wrapper at your own installation on a Mac:

1. Replace `opsdeck.example.com:8444` and `192.0.2.10:8091` in `capacitor.config.json` and `ios/App/App/OpsdeckViewController.swift`. Keep the allowed navigation hosts in `capacitor.config.json` aligned with the origins in Swift. If you change the LAN IP, change its exception-domain key in `ios/App/App/Info.plist`, or use HTTPS instead.
2. Change the example `appId` and Xcode `PRODUCT_BUNDLE_IDENTIFIER` to your own unique bundle ID. Choose your own Xcode signing team.
3. Run `npm ci`, `npm --prefix web ci`, `npm --prefix web run build`, and `npx cap sync ios` from the repository root. The sync step replaces the placeholder files under `ios/App/App/public` with your built web app.
4. Open `ios/App/App.xcodeproj` in Xcode and build for your device. You can enter the Opsdeck token in the app's token dialog. To preload it, supply `OPSDECK_TOKEN` as a private Xcode build setting; never commit the token or a configured `.xcconfig` file. A token embedded in an installed app can be extracted by someone with access to that app.

The public copy has no signing identity, real network origin, or token. This source has not been installed or verified on another person's iPhone.
