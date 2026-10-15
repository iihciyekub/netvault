// Apply the saved palette before styles load to avoid a light flash on dark pages.
try {
  document.documentElement.dataset.theme = localStorage.getItem("netvault:theme:v1") === "dark" ? "dark" : "light";
} catch {
  document.documentElement.dataset.theme = "light";
}
