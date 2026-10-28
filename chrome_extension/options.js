const $ = (id) => document.getElementById(id);

chrome.storage.local.get({ port: 47821, token: "", hosts: [] }).then((s) => {
  $("port").value = s.port;
  $("token").value = s.token;
  $("hosts").value = s.hosts.join("\n");
});

$("save").addEventListener("click", async () => {
  const hosts = $("hosts").value.split("\n").map((x) => x.trim()).filter(Boolean);
  await chrome.storage.local.set({ port: Number($("port").value) || 47821, token: $("token").value.trim(), hosts });
  $("msg").textContent = "Saved.";
});
