(function () {
    const form = document.getElementById("share-bootstrap-form");
    const input = document.getElementById("share-bootstrap-secret");
    const status = document.getElementById("share-bootstrap-status");
    if (!form || !input || !status) return;

    const secret = window.location.hash.slice(1);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    if (!secret || secret.length > 256) {
        status.textContent = "This private link is invalid or incomplete.";
        return;
    }
    input.value = secret;
    form.submit();
})();
