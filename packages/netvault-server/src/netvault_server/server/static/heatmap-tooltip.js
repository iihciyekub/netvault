const heatmapTooltip = document.createElement("div");
heatmapTooltip.className = "heatmap-tooltip";
heatmapTooltip.setAttribute("role", "tooltip");
document.body.appendChild(heatmapTooltip);

let activeTip = null;

const positionTooltip = (event) => {
  if (!activeTip) return;
  const offset = 12;
  const { innerWidth, innerHeight } = window;
  const rect = heatmapTooltip.getBoundingClientRect();
  let left = event.clientX + offset;
  let top = event.clientY + offset;
  if (left + rect.width + 8 > innerWidth) {
    left = event.clientX - rect.width - offset;
  }
  if (top + rect.height + 8 > innerHeight) {
    top = event.clientY - rect.height - offset;
  }
  heatmapTooltip.style.transform = `translate(${Math.max(8, left)}px, ${Math.max(8, top)}px)`;
};

document.querySelectorAll(".heat-cell[data-tip]").forEach((cell) => {
  cell.addEventListener("pointerenter", (event) => {
    activeTip = cell.getAttribute("data-tip");
    heatmapTooltip.textContent = activeTip;
    heatmapTooltip.classList.add("is-visible");
    positionTooltip(event);
  });

  cell.addEventListener("pointermove", positionTooltip);

  cell.addEventListener("pointerleave", () => {
    activeTip = null;
    heatmapTooltip.classList.remove("is-visible");
  });
});
