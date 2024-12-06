document
  .getElementById("fileInput")
  .addEventListener("change", function (event) {
    const files = event.target.files;
    const previewContainer = document.getElementById("previewContainer");
    const fileNameInput = document.getElementById("fileName");
    previewContainer.innerHTML = ""; // Clear previous previews

    Array.from(files).forEach((file) => {
      const fileType = file.type;
      const allowedTypes = [
        "image/jpeg",
        "image/png",
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      ];

      if (allowedTypes.includes(fileType)) {
        const previewItem = document.createElement("div");
        previewItem.classList.add("preview-item");

        if (fileType.startsWith("image/")) {
          const img = document.createElement("img");
          img.src = URL.createObjectURL(file);
          img.alt = file.name;
          previewItem.appendChild(img);
        } else {
          const icon = document.createElement("div");
          icon.classList.add("document-icon");
          icon.textContent = "📄";
          previewItem.appendChild(icon);
        }

        const fileName = document.createElement("div");
        fileName.classList.add("file-name");
        fileName.textContent = file.name;
        previewItem.appendChild(fileName);

        fileNameInput.value = Array.from(files)
          .map((file) => file.name)
          .join("; ");
      } else {
        alert(`File type not allowed: ${file.name}`);
      }
    });
  });
