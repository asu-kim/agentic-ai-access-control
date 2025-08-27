document.addEventListener("DOMContentLoaded", () => {
  const checkbox = document.getElementById("task-completed");
  const form = document.getElementById("complete-form");
  if (checkbox && form) {
    checkbox.addEventListener("change", () => { if (checkbox.checked) form.submit(); });
  }
});

// Remaining time: 1-second client tick + 5-second server refresh
let __remain = null;
function __renderRemain() {
  const el = document.getElementById("remaining-seconds");
  if (el && __remain !== null) { el.textContent = Math.max(0, __remain); }
}
async function refreshRemaining() {
  try {
    const r = await fetch("/session-remaining", { credentials: "same-origin" });
    if (!r.ok) return;
    const data = await r.json();
    if (typeof data.seconds === "number") {
      __remain = data.seconds;
      __renderRemain();
    }
  } catch (e) {}
}
setInterval(() => {
  if (__remain !== null) {
    __remain = Math.max(0, __remain - 1);
    __renderRemain();
  }
}, 1000);
setInterval(refreshRemaining, 5000);
refreshRemaining();
