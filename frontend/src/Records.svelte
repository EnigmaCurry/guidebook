<script>
  import { onMount, onDestroy, createEventDispatcher, tick } from "svelte";
  import { storageGet, storageSet } from "./storage.js";

  const dispatch = createEventDispatcher();

  export let editId = null;
  export let showForm = false;
  export let prefill = null;
  export let formDirty = false;
  export let autoCreated = false;

  let records = [];
  let loading = true;
  let searchQuery = "";
  let searchTimeout = null;
  let formTitle = "";
  let formContent = "";
  let formTags = "";
  let formId = null;
  let formAutoCreated = false;
  let saving = false;
  let error = "";
  let tableWrapEl;
  let selectedIndex = -1;
  let origTitle = "";
  let origContent = "";
  let origTags = "";
  let attachments = [];
  let uploadingFiles = false;
  let attachmentError = "";
  let fileInput;
  let dragOver = false;
  let previewUrl = null;
  let previewType = "image";

  // --- Column definitions ---
  const defaultColumnOrder = ["timestamp", "title", "tags", "content", "updated_at"];
  const flexColumns = new Set(["title", "content"]);
  const columnDefs = {
    timestamp: { key: "timestamp", label: "Created" },
    title: { key: "title", label: "Title" },
    tags: { key: "tags", label: "Tags" },
    content: { key: "content", label: "Content" },
    updated_at: { key: "updated_at", label: "Edited" },
  };

  // --- Sort state ---
  let sortCol = storageGet("logSortCol") || "timestamp";
  let sortAsc = storageGet("logSortAsc") === "true";

  // --- Column order ---
  function loadColumnOrder() {
    try {
      const saved = JSON.parse(storageGet("logColumnOrder"));
      if (Array.isArray(saved) && saved.length > 0) {
        // Filter to only valid keys, append any new defaults
        const valid = saved.filter(k => columnDefs[k]);
        for (const k of defaultColumnOrder) {
          if (!valid.includes(k)) valid.push(k);
        }
        return valid;
      }
    } catch {}
    return [...defaultColumnOrder];
  }

  let columnOrder = loadColumnOrder();
  $: columns = columnOrder.map(k => columnDefs[k]);

  // --- Drag to reorder ---
  let dragCol = null;
  let dragOverCol = null;

  function onColDragStart(e, key) {
    dragCol = key;
    e.dataTransfer.effectAllowed = "move";
  }

  function onColDragOver(e, key) {
    e.preventDefault();
    dragOverCol = key;
  }

  function onColDrop(e, key) {
    e.preventDefault();
    if (dragCol && dragCol !== key) {
      const from = columnOrder.indexOf(dragCol);
      const to = columnOrder.indexOf(key);
      if (from >= 0 && to >= 0) {
        columnOrder.splice(from, 1);
        columnOrder.splice(to, 0, dragCol);
        columnOrder = [...columnOrder];
        storageSet("logColumnOrder", JSON.stringify(columnOrder));
      }
    }
    dragCol = null;
    dragOverCol = null;
  }

  function onColDragEnd() {
    dragCol = null;
    dragOverCol = null;
  }

  // --- Column resizing ---
  let resizeCol = null;
  let resizeColKey = null;
  let resizeStartX = 0;
  let resizeStartW = 0;

  function loadColumnWidths() {
    try { return JSON.parse(storageGet("logColumnWidths")) || {}; } catch { return {}; }
  }

  let columnWidths = loadColumnWidths();

  function startColResize(e, key) {
    e.preventDefault();
    e.stopPropagation();
    const th = e.target.parentElement;
    resizeCol = th;
    resizeColKey = key;
    resizeStartX = e.clientX;
    resizeStartW = th.offsetWidth;
    window.addEventListener("mousemove", onColResize);
    window.addEventListener("mouseup", stopColResize);
  }

  function onColResize(e) {
    if (!resizeCol) return;
    const diff = e.clientX - resizeStartX;
    const newW = Math.max(30, resizeStartW + diff);
    resizeCol.style.width = newW + "px";
  }

  function stopColResize() {
    if (resizeCol && resizeColKey) {
      columnWidths[resizeColKey] = resizeCol.style.width;
      storageSet("logColumnWidths", JSON.stringify(columnWidths));
    }
    resizeCol = null;
    resizeColKey = null;
    window.removeEventListener("mousemove", onColResize);
    window.removeEventListener("mouseup", stopColResize);
  }

  // --- Auto-size columns ---
  let columnsAutoSized = false;

  async function autoSizeColumns() {
    if (columnsAutoSized) return;
    const needsAuto = columns.some(col => !columnWidths[col.key]);
    if (!needsAuto) { columnsAutoSized = true; return; }
    if (!tableWrapEl) return;
    const table = tableWrapEl.querySelector("table");
    if (!table) return;

    table.style.tableLayout = "auto";
    await tick();
    await new Promise(r => requestAnimationFrame(r));

    const ths = table.querySelectorAll("thead th");
    const measured = {};
    ths.forEach((th, i) => {
      if (i < columns.length) measured[columns[i].key] = th.offsetWidth;
    });

    table.style.tableLayout = "fixed";
    ths.forEach((th, i) => {
      if (i < columns.length) {
        const key = columns[i].key;
        if (columnWidths[key]) th.style.width = columnWidths[key];
        else if (measured[key]) th.style.width = measured[key] + "px";
      }
    });
    columnsAutoSized = true;
  }

  // --- Sorting ---
  function toggleSort(key) {
    if (sortCol === key) {
      sortAsc = !sortAsc;
    } else {
      sortCol = key;
      sortAsc = key === "timestamp" || key === "updated_at" ? false : true;
    }
    storageSet("logSortCol", sortCol);
    storageSet("logSortAsc", String(sortAsc));
  }

  $: sortedRecords = (() => {
    const sorted = [...records].sort((a, b) => {
      let va = a[sortCol] ?? "";
      let vb = b[sortCol] ?? "";
      if (sortCol === "timestamp" || sortCol === "updated_at") {
        return sortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      }
      va = String(va).toLowerCase();
      vb = String(vb).toLowerCase();
      return sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    });
    return sorted;
  })();

  // --- Data ---
  $: if (editId !== null && editId !== formId) {
    formAutoCreated = autoCreated;
    loadRecord(editId);
  }

  $: if (prefill) {
    formId = null;
    formTitle = prefill.title || "";
    formContent = prefill.content || "";
    formTags = prefill.tags || "";
    showForm = true;
    formDirty = true;
    dispatch("prefillconsumed");
  }

  $: if (showForm && !formDirty && !formId && !formTitle) {
    // showForm was set externally (e.g. + button in App.svelte)
    tick().then(() => document.getElementById("rec-title")?.focus());
  }

  // Mark form dirty when content differs from original
  $: if (showForm && !formId && (formTitle || formContent || formTags)) {
    formDirty = true;
  }
  $: if (showForm && formId && (formTitle !== origTitle || formContent !== origContent || formTags !== origTags)) {
    formDirty = true;
  }

  let recordsEventSource = null;

  function connectRecordsSSE() {
    if (recordsEventSource) recordsEventSource.close();
    recordsEventSource = new EventSource("/api/records/stream");
    recordsEventSource.addEventListener("records-changed", () => {
      fetchRecords();
    });
    recordsEventSource.onerror = () => {};
  }

  export function selectNext() {
    if (sortedRecords.length === 0) return;
    selectedIndex = selectedIndex < sortedRecords.length - 1 ? selectedIndex + 1 : 0;
    scrollToSelected();
  }

  export function selectPrev() {
    if (sortedRecords.length === 0) return;
    selectedIndex = selectedIndex > 0 ? selectedIndex - 1 : sortedRecords.length - 1;
    scrollToSelected();
  }

  export function openSelected() {
    if (selectedIndex >= 0 && selectedIndex < sortedRecords.length) {
      editRecord(sortedRecords[selectedIndex]);
    }
  }

  export function cancelIfClean() {
    if (showForm && !formDirty) {
      cancelForm();
      return true;
    }
    return false;
  }

  function scrollToSelected() {
    tick().then(() => {
      const row = tableWrapEl?.querySelector("tr.selected");
      if (row) row.scrollIntoView({ block: "nearest" });
    });
  }

  onMount(() => {
    fetchRecords();
    connectRecordsSSE();
  });

  onDestroy(() => {
    if (recordsEventSource) recordsEventSource.close();
  });

  async function fetchRecords() {
    loading = true;
    try {
      let url = "/api/records/";
      if (searchQuery) url += `?q=${encodeURIComponent(searchQuery)}`;
      const res = await fetch(url);
      if (res.ok) {
        records = await res.json();
        selectedIndex = -1;
        await tick();
        autoSizeColumns();
      }
    } catch {}
    loading = false;
  }

  function onSearchInput() {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(fetchRecords, 300);
  }

  async function startNew() {
    formId = null;
    formTitle = "";
    formContent = "";
    formTags = "";
    error = "";
    attachments = [];
    attachmentError = "";
    showForm = true;
    dispatch("editchange", null);
    await tick();
    document.getElementById("rec-title")?.focus();
  }

  async function loadRecord(id) {
    try {
      const res = await fetch(`/api/records/${id}`);
      if (res.ok) {
        const r = await res.json();
        formId = r.id;
        formTitle = origTitle = r.title || "";
        formContent = origContent = r.content || "";
        formTags = origTags = r.tags || "";
        showForm = true;
        formDirty = false;
        fetchAttachments(r.id);
      }
    } catch {}
  }

  function editRecord(r) {
    formId = r.id;
    formTitle = origTitle = r.title || "";
    formContent = origContent = r.content || "";
    formTags = origTags = r.tags || "";
    error = "";
    attachmentError = "";
    showForm = true;
    formDirty = false;
    dispatch("editchange", r.id);
    fetchAttachments(r.id);
  }

  async function saveRecord() {
    if (!formTitle.trim()) {
      error = "Title is required";
      return;
    }
    saving = true;
    error = "";
    const body = {
      title: formTitle.trim(),
      content: formContent.trim() || null,
      tags: formTags.trim() || null,
    };
    try {
      let res;
      if (formId) {
        res = await fetch(`/api/records/${formId}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } else {
        res = await fetch("/api/records/", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }
      if (res.ok) {
        formAutoCreated = false;
        cancelForm();
        await fetchRecords();
      } else {
        const data = await res.json().catch(() => null);
        error = data?.detail || "Failed to save";
      }
    } catch (e) {
      error = e.message || "Failed to save";
    }
    saving = false;
  }

  async function deleteRecord(id) {
    if (!confirm("Delete this record?")) return;
    try {
      await fetch(`/api/records/${id}`, { method: "DELETE" });
      if (formId === id) cancelForm();
      await fetchRecords();
    } catch {}
  }

  async function cancelForm() {
    if (formAutoCreated && formId) {
      try {
        await fetch(`/api/records/${formId}`, { method: "DELETE" });
      } catch {}
    }
    const wasAutoCreated = formAutoCreated;
    showForm = false;
    formId = null;
    formAutoCreated = false;
    selectedIndex = -1;
    formTitle = "";
    formContent = "";
    formTags = "";
    error = "";
    attachments = [];
    attachmentError = "";
    formDirty = false;
    if (wasAutoCreated) await fetchRecords();
    dispatch("navigate", "records");
  }

  function formatTimestamp(ts) {
    if (!ts) return "";
    return ts.replace("T", " ").replace("Z", "z");
  }

  function relativeTime(ts) {
    if (!ts) return "";
    const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
    const diff = Date.now() - d.getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  function cellValue(record, key) {
    const v = record[key];
    if (v == null) return "";
    if (key === "timestamp" || key === "updated_at") return formatTimestamp(v);
    return String(v);
  }

  // --- Attachments ---
  async function fetchAttachments(recordId) {
    attachmentError = "";
    try {
      const res = await fetch(`/api/records/${recordId}/attachments/`);
      if (res.ok) attachments = await res.json();
    } catch {}
  }

  async function uploadFiles(files) {
    if (!formId || !files.length) return;
    uploadingFiles = true;
    attachmentError = "";
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    try {
      const res = await fetch(`/api/records/${formId}/attachments/`, {
        method: "POST",
        body: fd,
      });
      if (res.ok) {
        await fetchAttachments(formId);
      } else {
        const data = await res.json().catch(() => null);
        attachmentError = data?.detail || "Upload failed";
      }
    } catch (e) {
      attachmentError = e.message || "Upload failed";
    }
    uploadingFiles = false;
  }

  async function deleteAttachment(attId) {
    if (!confirm("Delete this attachment? This cannot be undone.")) return;
    try {
      await fetch(`/api/records/${formId}/attachments/${attId}`, { method: "DELETE" });
      await fetchAttachments(formId);
    } catch {}
  }

  function handleDrop(e) {
    e.preventDefault();
    dragOver = false;
    if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
  }

  function handlePaste(e) {
    if (!formId) return;
    const items = e.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      uploadFiles(files);
    }
  }

  function handleFileInput(e) {
    if (e.target.files?.length) uploadFiles(e.target.files);
    e.target.value = "";
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  function attDownloadUrl(att) {
    return `/api/records/${formId}/attachments/${att.id}/download`;
  }

  let pageDragOver = false;

  async function handlePageDrop(e) {
    e.preventDefault();
    pageDragOver = false;
    if (showForm || !e.dataTransfer?.files?.length) return;
    const files = e.dataTransfer.files;
    const firstFileName = files[0].name.replace(/\.[^.]+$/, "") || "Untitled";
    try {
      // Create the record
      const res = await fetch("/api/records/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: firstFileName }),
      });
      if (!res.ok) return;
      const record = await res.json();

      // Upload attachments before opening the form (which may destroy this component)
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      await fetch(`/api/records/${record.id}/attachments/`, {
        method: "POST",
        body: fd,
      });

      // Now open the edit form — this may navigate and destroy this component
      formAutoCreated = true;
      dispatch("dropcreated", record.id);
      editRecord(record);
    } catch {}
  }
</script>


<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="records-page" class:page-drag-active={pageDragOver && !showForm}
     on:dragover|preventDefault={() => { if (!showForm) pageDragOver = true; }}
     on:dragleave|self={() => { pageDragOver = false; }}
     on:drop={handlePageDrop}>
  <div class="records-header">
    <div class="search-bar">
      <input
        id="records-search"
        type="text"
        placeholder="Search records..."
        bind:value={searchQuery}
        on:input={onSearchInput}
        on:keydown={e => { if (e.key === "Escape" || e.key === "Enter") e.target.blur(); }}
      />
    </div>
  </div>

  {#if showForm}
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="record-form" on:keydown={e => { if (e.ctrlKey && e.key === "Enter") { e.preventDefault(); saveRecord(); } }} on:paste={handlePaste}>
      <h3>{formId ? "Edit Record" : "New Record"}</h3>
      <div class="form-field">
        <label for="rec-title">Title</label>
        <input id="rec-title" type="text" bind:value={formTitle} placeholder="Record title" />
      </div>
      <div class="form-field">
        <label for="rec-content">Content</label>
        <textarea id="rec-content" bind:value={formContent} placeholder="Content (optional)" rows="4"></textarea>
      </div>
      <div class="form-field">
        <label for="rec-tags">Tags</label>
        <input id="rec-tags" type="text" bind:value={formTags} placeholder="Tags (optional, comma-separated)" />
      </div>
      {#if formId}
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="attachments-section" class:drag-active={dragOver}
             on:dragover|preventDefault={() => { dragOver = true; }}
             on:dragleave={() => { dragOver = false; }}
             on:drop={handleDrop}>
          <label>Attachments</label>
          {#if attachments.length > 0}
            <div class="attachment-list">
              {#each attachments as att (att.id)}
                <div class="attachment-item">
                  {#if att.content_type.startsWith("image/")}
                    <!-- svelte-ignore a11y-click-events-have-key-events -->
                    <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
                    <img src={attDownloadUrl(att)} alt={att.filename} class="attachment-thumb" on:click={() => { previewUrl = attDownloadUrl(att); previewType = "image"; }} />
                  {:else if att.content_type.startsWith("audio/")}
                    <audio controls preload="metadata" src={attDownloadUrl(att)} class="attachment-audio"></audio>
                  {:else if att.content_type.startsWith("video/")}
                    <video controls preload="metadata" src={attDownloadUrl(att)} class="attachment-video"
                      on:click|stopPropagation={() => { previewUrl = attDownloadUrl(att); previewType = "video"; }}></video>
                  {/if}
                  <div class="attachment-info">
                    <a href={attDownloadUrl(att)} target="_blank" class="attachment-name">{att.filename}</a>
                    <span class="attachment-size">{formatFileSize(att.size)}</span>
                    <button class="attachment-delete-btn" on:click|stopPropagation={() => deleteAttachment(att.id)}>x</button>
                  </div>
                </div>
              {/each}
            </div>
          {/if}
          <div class="attachment-upload">
            <button class="attachment-choose-btn" on:click={() => fileInput.click()}>Choose files</button>
            <span class="drop-hint">or drag &amp; drop / paste</span>
            <input bind:this={fileInput} type="file" multiple style="display:none" on:change={handleFileInput} />
          </div>
          {#if uploadingFiles}
            <p class="status">Uploading...</p>
          {/if}
          {#if attachmentError}
            <p class="error">{attachmentError}</p>
          {/if}
        </div>
      {/if}
      {#if error}
        <p class="error">{error}</p>
      {/if}
      <div class="form-actions">
        {#if formId}
          <button class="delete-btn" on:click={() => deleteRecord(formId)}>Delete</button>
        {/if}
        <span class="form-actions-right">
          <button class="cancel-btn" on:click={cancelForm}>Cancel</button>
          <button class="save-btn" on:click={saveRecord} disabled={saving}>
            {saving ? "Saving..." : (formId ? "Update" : "Create")}
          </button>
        </span>
      </div>
    </div>
  {/if}

  {#if loading}
    <p class="status">Loading...</p>
  {:else if records.length === 0}
    <p class="status">{searchQuery ? "No records match your search." : "No records yet. Click '+' to create one."}</p>
  {:else}
    <div class="table-wrap" bind:this={tableWrapEl}>
      <table>
        <thead>
          <tr>
            {#each columns as col (col.key)}
              <th class:drag-over={dragOverCol === col.key && dragCol !== col.key}
                  on:dragover={e => onColDragOver(e, col.key)}
                  on:drop={e => onColDrop(e, col.key)}
                  style={columnWidths[col.key] ? `width: ${columnWidths[col.key]}` : ""}>
                <!-- svelte-ignore a11y-click-events-have-key-events -->
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <span class="col-label" draggable="true"
                      on:dragstart={e => onColDragStart(e, col.key)}
                      on:dragend={onColDragEnd}
                      on:click={() => toggleSort(col.key)}>
                  {col.label}{#if sortCol === col.key}{sortAsc ? " ▲" : " ▼"}{/if}
                </span>
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <span class="resize-handle" on:mousedown={e => startColResize(e, col.key)}></span>
              </th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each sortedRecords as r, i (r.id)}
            <!-- svelte-ignore a11y-click-events-have-key-events -->
            <tr class="clickable" class:editing={formId === r.id} class:selected={selectedIndex === i} title={relativeTime(r.timestamp)} on:click={() => editRecord(r)}>
              {#each columns as col (col.key)}
                {#if col.key === "title"}
                  <td class="title-cell">{r.title}</td>
                {:else if col.key === "tags"}
                  <td class="tags-cell">{r.tags || ""}</td>
                {:else if col.key === "content"}
                  <td class="truncate">{r.content || ""}</td>
                {:else}
                  <td>{cellValue(r, col.key)}</td>
                {/if}
              {/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if previewUrl}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="lightbox" on:click|self={() => { previewUrl = null; }}>
    {#if previewType === "video"}
      <!-- svelte-ignore a11y-media-has-caption -->
      <video controls autoplay src={previewUrl} class="lightbox-video"></video>
    {:else}
      <img src={previewUrl} alt="Preview" />
    {/if}
  </div>
{/if}

<svelte:window on:keydown={e => { if (e.key === "Escape" && previewUrl) { e.preventDefault(); previewUrl = null; } }} />

<style>
  .records-page {
    padding: 0;
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    flex: 1;
  }

  .records-page.page-drag-active {
    outline: 2px dashed var(--accent, #00ff88);
    outline-offset: -4px;
    background: rgba(0, 255, 136, 0.03);
  }

  .records-header {
    display: flex;
    gap: 0.75rem;
    align-items: center;
    padding: 0.5rem 0.75rem;
    flex-shrink: 0;
  }

  .search-bar {
    flex: 1;
  }

  .search-bar input {
    width: 100%;
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 6px;
    background: var(--bg-input, transparent);
    color: var(--text, #eaeaea);
    font-size: 0.85rem;
  }


  .record-form {
    background: var(--bg-card, #24252b);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    margin: 0 0.75rem 0.5rem;
    flex-shrink: 0;
  }

  .record-form h3 {
    margin: 0 0 0.75rem;
    font-size: 1rem;
    color: var(--text, #eaeaea);
  }

  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-bottom: 0.75rem;
  }

  .form-field label {
    font-size: 0.75rem;
    color: var(--text-dim, #888);
  }

  .form-field input,
  .form-field textarea {
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 4px;
    background: var(--bg-input, transparent);
    color: var(--text, #eaeaea);
    font-size: 0.9rem;
    font-family: inherit;
    resize: vertical;
  }

  .error {
    color: #ff4444;
    font-size: 0.8rem;
    margin: 0 0 0.5rem;
  }

  .form-actions {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 0.5rem;
  }

  .form-actions-right {
    display: flex;
    gap: 0.5rem;
    margin-left: auto;
  }

  .delete-btn {
    background: none;
    border: 1px solid #cc3333;
    color: #cc3333;
    border-radius: 4px;
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .delete-btn:hover {
    background: rgba(204, 51, 51, 0.15);
  }

  .cancel-btn {
    background: none;
    border: 1px solid var(--border, #3a3b3f);
    color: var(--text-dim, #888);
    border-radius: 4px;
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    font-size: 0.85rem;
  }

  .save-btn {
    background: var(--accent, #00ff88);
    color: var(--accent-text, #000);
    border: none;
    border-radius: 4px;
    padding: 0.35rem 0.75rem;
    cursor: pointer;
    font-weight: 600;
    font-size: 0.85rem;
  }

  .save-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .status {
    color: var(--text-dim, #888);
    font-size: 0.9rem;
    text-align: center;
    margin: 2rem 0;
  }

  /* --- Table --- */

  .table-wrap {
    flex: 1;
    overflow: auto;
    min-height: 0;
  }

  table {
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 0.85rem;
    table-layout: fixed;
  }

  th {
    text-align: left;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border);
    padding: 0.3rem 0.5rem;
    white-space: nowrap;
    position: sticky;
    top: 0;
    background: var(--bg);
    z-index: 1;
    overflow: hidden;
  }

  .col-label {
    cursor: pointer;
    user-select: none;
  }

  .col-label:hover {
    color: var(--accent);
  }

  th.drag-over {
    border-left: 2px solid var(--accent);
  }

  .resize-handle {
    position: absolute;
    right: 0;
    top: 0;
    bottom: 0;
    width: 5px;
    cursor: col-resize;
    background: transparent;
  }

  .resize-handle:hover,
  .resize-handle:active {
    background: var(--accent);
    opacity: 0.4;
  }

  td {
    padding: 0.3rem 0.5rem;
    border-bottom: 1px solid var(--bg-card);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  td.title-cell {
    color: var(--accent-highlight, var(--accent));
    font-weight: bold;
  }

  td.truncate {
    max-width: 30ch;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  tr.clickable {
    cursor: pointer;
  }

  tbody tr:hover {
    background: var(--row-hover);
  }

  tr.selected {
    background: var(--row-hover);
    outline: 1px solid var(--accent, #00ff88);
    outline-offset: -1px;
  }

  tr.editing {
    background: var(--row-editing);
  }

  /* --- Attachments --- */

  .attachments-section {
    border: 1px dashed var(--border, #3a3b3f);
    border-radius: 6px;
    padding: 0.75rem;
    margin-bottom: 0.75rem;
    transition: border-color 0.15s;
  }

  .attachments-section.drag-active {
    border-color: var(--accent, #00ff88);
    background: rgba(0, 255, 136, 0.04);
  }

  .attachments-section > label {
    font-size: 0.75rem;
    color: var(--text-dim, #888);
    display: block;
    margin-bottom: 0.5rem;
  }

  .attachment-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-bottom: 0.5rem;
  }

  .attachment-item {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    font-size: 0.8rem;
  }

  .attachment-thumb {
    max-height: 60px;
    max-width: 80px;
    border-radius: 4px;
    object-fit: cover;
    cursor: pointer;
  }

  .attachment-thumb:hover {
    opacity: 0.8;
  }

  .lightbox {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.85);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    cursor: pointer;
  }

  .lightbox img,
  .lightbox-video {
    max-width: 90vw;
    max-height: 90vh;
    object-fit: contain;
    border-radius: 4px;
  }

  .attachment-audio {
    width: 100%;
    max-width: 300px;
    height: 36px;
  }

  .attachment-video {
    max-width: 300px;
    max-height: 180px;
    border-radius: 4px;
    cursor: pointer;
  }

  .attachment-info {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
  }

  .attachment-name {
    color: var(--accent, #00ff88);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 200px;
  }

  .attachment-name:hover {
    text-decoration: underline;
  }

  .attachment-size {
    color: var(--text-dim, #888);
    font-size: 0.75rem;
    flex-shrink: 0;
  }

  .attachment-delete-btn {
    background: none;
    border: none;
    color: #cc3333;
    cursor: pointer;
    font-size: 0.85rem;
    padding: 0 0.3rem;
    flex-shrink: 0;
  }

  .attachment-delete-btn:hover {
    color: #ff4444;
  }

  .attachment-upload {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .attachment-choose-btn {
    background: none;
    border: 1px solid var(--border, #3a3b3f);
    color: var(--text-dim, #888);
    border-radius: 4px;
    padding: 0.25rem 0.6rem;
    cursor: pointer;
    font-size: 0.8rem;
  }

  .attachment-choose-btn:hover {
    border-color: var(--accent, #00ff88);
    color: var(--text, #eaeaea);
  }

  .drop-hint {
    font-size: 0.75rem;
    color: var(--text-dim, #888);
  }
</style>
