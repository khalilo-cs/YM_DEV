// The only script in the shell: shows the reason passed by the native side (as text, never as HTML).
const reason = new URLSearchParams(location.search).get("error");
if (reason !== null) {
  document.getElementById("starting").hidden = true;
  document.getElementById("failed").hidden = false;
  document.getElementById("reason").textContent = reason.slice(0, 4000);
}
