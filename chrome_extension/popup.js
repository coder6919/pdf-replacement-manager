(async () => {
  const list = document.getElementById("list");
  const s = await chrome.storage.local.get({ port: 47821, token: "" });
  if (!s.token) { list.textContent = "Not paired. Open Settings and paste the token from the app."; return; }
  try {
    const r = await fetch(`http://127.0.0.1:${s.port}/v1/status`, { headers: { "X-PRM-Token": s.token } });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    list.textContent = "";
    if (!data.jobs.length) list.textContent = "No downloads processed yet.";
    for (const j of data.jobs) {
      const row = document.createElement("div");
      row.className = "row";
      const a = document.createElement("span");
      a.textContent = j.file_name;
      const b = document.createElement("span");
      b.textContent = j.state.replace("_", " ");
      b.className = j.state;
      row.append(a, b);
      list.append(row);
    }
  } catch (e) {
    list.textContent = "The desktop app is not reachable (not running, or the bridge is disabled in Settings).";
  }
})();
