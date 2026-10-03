/*
 * NetSanctum Capture — promise wrapper for extension APIs.
 *
 * Firefox exposes `chrome.*` as the callback-based namespace, and in
 * Manifest V2 it does NOT return promises there (Bugzilla 1711570). Awaiting
 * `chrome.tabs.sendMessage(...)` on Firefox therefore yields `undefined`, and
 * the caller carries on with nothing. Callbacks, by contrast, work in every
 * browser this extension targets, so every asynchronous call goes through here.
 */
function netsanctumCall(object, method, ...args) {
  return new Promise((resolve, reject) => {
    try {
      object[method](...args, (result) => {
        // lastError must be read inside the callback or it is lost.
        const failure = typeof chrome !== "undefined" && chrome.runtime ? chrome.runtime.lastError : null;
        if (failure) reject(new Error(failure.message || String(failure)));
        else resolve(result);
      });
    } catch (error) {
      reject(error instanceof Error ? error : new Error(String(error)));
    }
  });
}

function netsanctumStorageGet(keys) {
  return netsanctumCall(chrome.storage.local, "get", keys);
}

function netsanctumStorageSet(values) {
  return netsanctumCall(chrome.storage.local, "set", values);
}
