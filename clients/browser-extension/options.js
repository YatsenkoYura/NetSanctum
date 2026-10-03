/*
 * NetSanctum Capture — options page.
 *
 * Holds the address, the bootstrap token, and the destination every capture
 * lands in. The token is never sent anywhere except the configured NetSanctum.
 */
(function () {
  "use strict";

  const FIELDS = ["serverUrl", "accessToken", "collectionId", "tags", "openDashboard", "videoQuality"];
  const status = document.getElementById("status");

  function say(message, isError) {
    status.textContent = message;
    status.classList.toggle("error", Boolean(isError));
  }

  function readForm() {
    return {
      serverUrl: document.getElementById("serverUrl").value.trim().replace(/\/+$/, ""),
      accessToken: document.getElementById("accessToken").value.trim(),
      collectionId: document.getElementById("collection").value ? Number(document.getElementById("collection").value) : null,
      tags: document
        .getElementById("tags")
        .value.split(",")
        .map((tag) => tag.trim())
        .filter(Boolean)
        .slice(0, 20),
      openDashboard: document.getElementById("openDashboard").checked,
      videoQuality: document.getElementById("videoQuality").value || "720",
    };
  }

  // The <select> starts empty, so the saved collection is re-applied whenever
  // the list is (re)loaded rather than only at first render.
  let wantedCollectionId = null;

  async function restore() {
    const stored = await netsanctumStorageGet(FIELDS);
    document.getElementById("serverUrl").value = stored.serverUrl || "";
    document.getElementById("accessToken").value = stored.accessToken || "";
    document.getElementById("tags").value = (stored.tags || []).join(", ");
    document.getElementById("openDashboard").checked = stored.openDashboard !== false;
    document.getElementById("videoQuality").value = stored.videoQuality || "720";
    wantedCollectionId = stored.collectionId ?? null;
  }

  async function loadCollections() {
    const form = readForm();
    if (!form.serverUrl || !form.accessToken) {
      say("Set the address and token first", true);
      return;
    }
    say("Loading…");
    try {
      const login = await fetch(`${form.serverUrl}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: form.accessToken }),
      });
      if (!login.ok) {
        let payload = null;
        try {
          payload = await login.json();
        } catch (error) {
          /* keep the generic message */
        }
        throw new Error(netsanctumErrorText(login.status, payload, "NetSanctum rejected the access token"));
      }
      const { access_token: accessToken } = await login.json();
      const response = await fetch(`${form.serverUrl}/api/vault/collections`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        let payload = null;
        try {
          payload = await response.json();
        } catch (error) {
          /* keep the generic message */
        }
        throw new Error(netsanctumErrorText(response.status, payload, `Collections returned ${response.status}`));
      }
      const collections = await response.json();
      const select = document.getElementById("collection");      select.innerHTML = '<option value="">No collection</option>';
      for (const collection of collections) {
        const option = document.createElement("option");
        option.value = String(collection.id);
        option.textContent = collection.name;
        if (String(collection.id) === String(wantedCollectionId)) option.selected = true;
        select.appendChild(option);
      }
      wantedCollectionId = select.value ? Number(select.value) : null;
      say(`${collections.length} collections loaded`);
    } catch (error) {
      say(netsanctumSafeText(error && error.message ? error.message : error, "Something went wrong"), true);
    }
  }

  document.getElementById("save").addEventListener("click", async () => {
    const form = readForm();
    if (!form.serverUrl) {
      say("The NetSanctum address is required", true);
      return;
    }
    if (!/^https?:\/\//i.test(form.serverUrl)) {
      say("Include the scheme, for example http://localhost:3000", true);
      return;
    }
    await netsanctumStorageSet(form);
    say("Settings saved");
  });

  document.getElementById("reloadCollections").addEventListener("click", loadCollections);

  restore();
})();
