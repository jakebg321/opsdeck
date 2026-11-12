import Capacitor
import WebKit

class OpsdeckViewController: CAPBridgeViewController {

    // Tailnet first (works anywhere, real TLS); home LAN when Tailscale is off.
    // Both must be listed in capacitor.config.json server.allowNavigation.
    private static let tailnetOrigin = "https://opsdeck.example.com:8444"
    private static let lanOrigin = "http://192.0.2.10:8091"
    private static let origins = [tailnetOrigin, lanOrigin]
    private static let tokenKey = "opsdeck-token"
    private var foregroundObserver: NSObjectProtocol?

    // NOTE: do NOT inject via webViewConfiguration(for:). Capacitor's
    // prepareWebView() calls that hook and then REPLACES the whole
    // configuration.userContentController with its delegation handler's
    // controller, discarding any WKUserScript added there. That is why the
    // token box kept showing despite the token being in Info.plist.
    //
    // Instead the script is added (1) in webView(with:configuration:), which
    // Capacitor calls with the FINAL configuration immediately before the
    // WKWebView is constructed, and (2) in capacitorDidLoad(), which runs
    // after the web view exists but before viewDidLoad starts the first
    // navigation. Either hook alone suffices; both fire, and the script is
    // idempotent (same localStorage setItem), so the second add is harmless.

    override func webView(with frame: CGRect, configuration: WKWebViewConfiguration) -> WKWebView {
        if let script = Self.tokenUserScript() {
            configuration.userContentController.addUserScript(script)
        }
        return super.webView(with: frame, configuration: configuration)
    }

    override func capacitorDidLoad() {
        if let script = Self.tokenUserScript() {
            webView?.configuration.userContentController.addUserScript(script)
        }
        // The shell is only a window onto your server, so every launch must show the
        // current deploy. Old servers sent index.html with no Cache-Control and
        // WebKit's disk cache kept serving that stale page (and its old bundle)
        // across launches and reinstalls. Drop the HTTP caches only (localStorage,
        // i.e. the token and snippets, stays) and reload once they are gone.
        let caches: Set<String> = [WKWebsiteDataTypeDiskCache, WKWebsiteDataTypeMemoryCache,
                                   WKWebsiteDataTypeFetchCache, WKWebsiteDataTypeServiceWorkerRegistrations]
        WKWebsiteDataStore.default().removeData(ofTypes: caches, modifiedSince: .distantPast) { [weak self] in
            self?.connect(forceReload: true)
        }
        // Network can change while backgrounded (left home, Tailscale toggled):
        // re-check on every return to the foreground.
        foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main
        ) { [weak self] _ in
            self?.connect(forceReload: false)
        }
    }

    deinit {
        if let observer = foregroundObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    /// Load whichever address answers as opsdeck, tailnet first. Stays put when
    /// the current page is already on a reachable address and no reload is forced.
    private func connect(forceReload: Bool) {
        Self.firstReachable(Self.origins) { [weak self] origin in
            DispatchQueue.main.async {
                guard let self = self, let webView = self.webView else { return }
                let current = webView.url.flatMap { url -> String? in
                    guard let scheme = url.scheme, let host = url.host else { return nil }
                    return url.port.map { "\(scheme)://\(host):\($0)" } ?? "\(scheme)://\(host)"
                }
                guard let origin = origin else {
                    if forceReload { webView.reloadFromOrigin() }  // neither answered: let the page show its own error
                    return
                }
                if origin != current, let url = URL(string: origin + "/") {
                    webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
                } else if forceReload {
                    webView.reloadFromOrigin()
                }
            }
        }
    }

    private static func firstReachable(_ candidates: [String], done: @escaping (String?) -> Void) {
        guard let origin = candidates.first else { return done(nil) }
        probe(origin) { ok in
            if ok { done(origin) } else { firstReachable(Array(candidates.dropFirst()), done: done) }
        }
    }

    /// GET <origin>/api/hello and require {"app":"opsdeck"}, so a stranger's box at
    /// a configured LAN address on some other Wi-Fi is never loaded or handed the token.
    private static func probe(_ origin: String, done: @escaping (Bool) -> Void) {
        guard let url = URL(string: origin + "/api/hello") else { return done(false) }
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3
        config.timeoutIntervalForResource = 4
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        URLSession(configuration: config).dataTask(with: url) { data, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200, let data = data,
                  let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  body["app"] as? String == "opsdeck" else {
                return done(false)
            }
            done(true)
        }.resume()
    }

    private static func tokenUserScript() -> WKUserScript? {
        guard let token = Bundle.main.object(forInfoDictionaryKey: "OpsdeckToken") as? String,
              !token.isEmpty, !token.contains("$(") else {
            return nil
        }
        // JSON-encode so the token is safely embedded in the script source.
        guard let wrapped = try? JSONSerialization.data(withJSONObject: [token], options: []),
              var arrayJSON = String(data: wrapped, encoding: .utf8),
              arrayJSON.count >= 2 else {
            return nil
        }
        arrayJSON.removeFirst()
        arrayJSON.removeLast()
        guard let originsJSON = try? JSONSerialization.data(withJSONObject: origins, options: []),
              let originsList = String(data: originsJSON, encoding: .utf8) else {
            return nil
        }
        let source = "(function(){try{if(\(originsList).indexOf(window.location.origin)<0){return;}var t=\(arrayJSON);if(!t){return;}window.localStorage.setItem(\"\(tokenKey)\",t);}catch(e){}})();"
        return WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true)
    }
}
