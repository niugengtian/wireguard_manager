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
      setTimeout(() => {
        URL.revokeObjectURL(objectUrl);
        window.location.reload();
      }, 250);
    } catch (_error) {
      if (button) button.disabled = false;
    }
  });
}
