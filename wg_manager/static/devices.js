"use strict";

for (const form of document.querySelectorAll("form[data-reset-download]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    if (button) button.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
      });
      const disposition = response.headers.get("Content-Disposition") || "";
      if (!response.ok || !disposition.toLowerCase().startsWith("attachment;")) {
        window.location.reload();
        return;
      }
      const payload = await response.blob();
      const match = disposition.match(/filename="([^"]+)"/i);
      const link = document.createElement("a");
      const objectUrl = URL.createObjectURL(payload);
      link.href = objectUrl;
      link.download = match ? match[1] : "wireguard-reset.conf";
      document.body.appendChild(link);
      link.click();
      link.remove();
      if (button) {
        button.textContent = "已下载，正在自动切换 / Downloaded; activating";
      }
      const pendingPublicKey = response.headers.get("X-WireGuard-Reset-Public-Key");
      const csrf = form.querySelector("input[name='_csrf']")?.value;
      if (form.dataset.activateUrl && pendingPublicKey && csrf) {
        const activation = new FormData();
        activation.set("_csrf", csrf);
        activation.set("expected_pending_public_key", pendingPublicKey);
        try {
          await fetch(form.dataset.activateUrl, {
            method: "POST",
            body: activation,
            credentials: "same-origin",
            redirect: "manual",
            keepalive: true,
          });
        } catch (_activationError) {
          // The old tunnel may disappear before the HTTP response returns.
        }
      }
      setTimeout(() => {
        URL.revokeObjectURL(objectUrl);
      }, 5000);
    } catch (_error) {
      if (button) button.disabled = false;
    }
  });
}
