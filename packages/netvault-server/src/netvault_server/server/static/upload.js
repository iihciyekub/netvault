const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const fileSummary = document.getElementById("file-summary");

if (dropzone && fileInput && fileSummary) {
  const updateSummary = () => {
    const count = fileInput.files.length;
    if (!count) {
      fileSummary.textContent = "Choose files";
    } else if (count === 1) {
      fileSummary.textContent = fileInput.files[0].name;
    } else {
      fileSummary.textContent = `${count} files selected`;
    }
  };

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, () => dropzone.classList.add("is-dragging"));
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, () => dropzone.classList.remove("is-dragging"));
  });

  fileInput.addEventListener("change", updateSummary);
}
