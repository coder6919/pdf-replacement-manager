// Sends a minimal hint {file_name, url (no query/fragment), referrer} for completed PDF downloads from
// websites the user explicitly allow-listed. Never reads cookies, page content, credentials or file bytes.

async function settings() {
  const s = await chrome.storage.local.get({ port: 47821, token: "", hosts: [] });
  return s;
}

function hostAllowed(url, hosts) {
  let h;
  try { h = new URL(url).hostname.toLowerCase(); } catch (e) { return false; }
  return hosts.some((p) => {
    p = p.trim().toLowerCase();
    if (!p) return false;
    if (p.startsWith("*.")) return h === p.slice(2) || h.endsWith(p.slice(1));
    return h === p;
  });
}

function clean(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "blob:") return "blob:";
    return u.origin + u.pathname;          // drop query string and fragment (may hold temporary tokens)
  } catch (e) { return ""; }
}

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state || delta.state.current !== "complete") return;
  const [item] = await chrome.downloads.search({ id: delta.id });
  if (!item) return;
  const isPdf = (item.mime || "").includes("pdf") || /\.pdf$/i.test(item.filename || "");
  if (!isPdf) return;
  const s = await settings();
  const source = item.finalUrl || item.url;
  const page = item.referrer || "";
  if (!s.token || !(hostAllowed(source, s.hosts) || hostAllowed(page, s.hosts))) return;
  const name = (item.filename || "").split(/[\\/]/).pop();
  try {
    await fetch(`http://127.0.0.1:${s.port}/v1/hint`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-PRM-Token": s.token },
      body: JSON.stringify({ file_name: name, url: clean(source), referrer: clean(page) }),
    });
  } catch (e) {
    // The desktop app is not running or the bridge is disabled: downloads are still detected locally.
  }
});
