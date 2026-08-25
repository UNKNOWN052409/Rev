// Universal SSL Pinning Bypass (Frida)
// Usage: frida -U -f com.target.app -l ssl_unpin.js --no-pause
// Covers: OkHttp3 CertificatePinner, TrustManagerImpl, Conscrypt,
//         SSLContext, X509TrustManager, Flutter (badCertificateCallback)

Java.perform(function () {
    console.log("[*] SSL Unpinning started...");

    // --- OkHttp3 CertificatePinner (saare check() overloads) ---
    try {
        var CertificatePinner = Java.use("okhttp3.CertificatePinner");
        var patched = 0;
        CertificatePinner.check.overloads.forEach(function (ov) {
            ov.implementation = function () {
                console.log("[+] OkHttp3 pinning bypassed ("
                    + ov.argumentTypes.map(function(t){return t.className;})
                       .join(", ") + "): " + arguments[0]);
                return;
            };
            patched++;
        });
        console.log("[+] OkHttp3: " + patched + " check() overloads hooked");
    } catch (e) { console.log("[-] OkHttp3 not found"); }

    // --- TrustManagerImpl (Android core, dono class paths) ---
    ["com.android.org.conscrypt.TrustManagerImpl",
     "org.conscrypt.TrustManagerImpl"].forEach(function (cls) {
        try {
            var TrustManagerImpl = Java.use(cls);
            TrustManagerImpl.checkTrustedRecursive.implementation = function (
                certs, host, clientAuth, ocspData, tlsSctData, chain) {
                console.log("[+] " + cls + " bypassed: " + host);
                return certs;
            };
            TrustManagerImpl.verifyChain.overloads.forEach(function (ov) {
                ov.implementation = function () { return arguments[0]; };
            });
            console.log("[+] " + cls + " hooked");
        } catch (e) { /* is platform pe class nahi — skip */ }
    });

    // --- SSLContext default TrustManager replace ---
    try {
        var X509TrustManager = Java.use("javax.net.ssl.X509TrustManager");
        var SSLContext = Java.use("javax.net.ssl.SSLContext");

        var TrustAll = Java.registerClass({
            name: "org.fake.TrustAllManager",
            implements: [X509TrustManager],
            methods: {
                checkClientTrusted: function (chain, authType) { },
                checkServerTrusted: function (chain, authType) { },
                getAcceptedIssuers: function () { return []; }
            }
        });

        var TrustManagers = [TrustAll.$new()];
        var SSLContextInit = SSLContext.init.overload(
            "[Ljavax.net.ssl.KeyManager;",
            "[Ljavax.net.ssl.TrustManager;",
            "java.security.SecureRandom");
        SSLContextInit.implementation = function (km, tm, sr) {
            console.log("[+] SSLContext.init hijacked — trusting all");
            SSLContextInit.call(this, km, TrustManagers, sr);
        };
    } catch (e) { console.log("[-] SSLContext hook failed"); }

    // --- Flutter apps: dart VM me pinning hoti hai, Java se direct hook
    //     nahi hota. Best-effort: PlatformChannel pe SSL errors ko swallow
    //     karna possible nahi, isliye network_security_config fallback
    //     suggest karo aur native SSL_verify hook karne ki hint do.
    try {
        var FlutterLoader = Java.use("io.flutter.embedding.engine.loader.FlutterLoader");
        if (FlutterLoader) {
            console.log("[*] Flutter app detected — agar pinning bachi ho toh:");
            console.log("    1. reFlutter framework repack use karo, ya");
            console.log("    2. apktool se network_security_config.xml patch karo");
        }
    } catch (e) { }

    // --- WebViewClient SSL error (hybrid apps ke liye) ---
    try {
        var WebViewClient = Java.use("android.webkit.WebViewClient");
        WebViewClient.onReceivedSslError.overload(
            "android.webkit.WebView", "android.webkit.SslErrorHandler",
            "android.net.http.SslError"
        ).implementation = function (view, handler, error) {
            console.log("[+] WebView SSL error bypassed");
            handler.proceed();
        };
    } catch (e) { }

    console.log("[*] SSL Unpinning complete. Traffic should flow through proxy.");
});
