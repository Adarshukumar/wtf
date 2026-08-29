// extract_keys.js
// =====================================================================
// Run this in DevTools → Console while on
//   https://image-generation.perchance.org/embed
// after the page has fully loaded and any Turnstile challenge has
// auto-solved (wait 5+ seconds).
//
// It will:
//   1. Dump every localStorage key+value on this origin
//   2. Dump every cookie
//   3. Try to detect the userKey (64-char hex) in localStorage
//   4. Print a single block of text you can paste into a file
// =====================================================================

(function() {
  const out = [];
  out.push("=== EXTRACTED KEYS ===");
  out.push("Origin: " + location.origin);
  out.push("Time:   " + new Date().toISOString());
  out.push("");

  // ---- 1. localStorage ----
  out.push("--- localStorage ---");
  let userKey = null;
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    const v = localStorage.getItem(k) || "";
    out.push(k + " = " + v);
    // Heuristic: 64-char hex
    const m = v.match(/[a-f0-9]{64}/);
    if (m && (k.toLowerCase().includes("user") || k.toLowerCase().includes("key"))) {
      userKey = m[0];
    }
  }
  if (!userKey) {
    // Fallback: scan all localStorage values for 64-char hex
    for (let i = 0; i < localStorage.length; i++) {
      const v = localStorage.getItem(localStorage.key(i)) || "";
      const m = v.match(/[a-f0-9]{64}/);
      if (m) { userKey = m[0]; break; }
    }
  }
  out.push("");

  // ---- 2. sessionStorage ----
  out.push("--- sessionStorage ---");
  for (let i = 0; i < sessionStorage.length; i++) {
    const k = sessionStorage.key(i);
    out.push(k + " = " + sessionStorage.getItem(k));
  }
  if (!userKey) {
    for (let i = 0; i < sessionStorage.length; i++) {
      const v = sessionStorage.getItem(sessionStorage.key(i)) || "";
      const m = v.match(/[a-f0-9]{64}/);
      if (m) { userKey = m[0]; break; }
    }
  }
  out.push("");

  // ---- 3. cookies ----
  out.push("--- cookies ---");
  out.push(document.cookie);
  out.push("");

  // ---- 4. Look for Turnstile widget on the page ----
  out.push("--- Turnstile widget on page ---");
  const turnstileDivs = document.querySelectorAll(
    ".cf-turnstile, [data-sitekey], [data-turnstile-sitekey], iframe[src*='turnstile']"
  );
  if (turnstileDivs.length) {
    turnstileDivs.forEach(d => {
      out.push("Found turnstile element: " + d.outerHTML.slice(0, 300));
    });
  } else {
    out.push("(no turnstile widget on page)");
  }
  out.push("");

  // ---- 5. The actual JSON config to copy-paste into the script ----
  out.push("=== COPY THIS BLOCK INTO .perchance_client/userkeys/current.json ===");
  const config = {
    userKey: userKey,
    cf_clearance: (document.cookie.match(/cf_clearance=([^;]+)/) || [])[1] || null,
    cookies: document.cookie,
    origin: location.origin,
    captured_at: new Date().toISOString(),
  };
  out.push(JSON.stringify(config, null, 2));

  const text = out.join("\n");
  console.log(text);
  // Try to copy to clipboard
  try {
    navigator.clipboard.writeText(text);
    console.log("\n[✓] Copied to clipboard!");
  } catch (e) {
    console.log("\n[ ] Could not copy to clipboard automatically. Select and copy from above.");
  }
  return text;
})();
