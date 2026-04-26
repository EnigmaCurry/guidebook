<script>
  import { onMount, onDestroy, createEventDispatcher } from "svelte";

  const dispatch = createEventDispatcher();

  export let searchQuery = "";
  export let selectedTags = [];
  export let selectedRecordId = null;

  let media = [];
  let loading = true;
  let previewIndex = -1;
  let eventSource = null;
  let typeFilter = "all";

  $: displayMedia = selectedRecordId
    ? media.filter(m => m.record_id === selectedRecordId)
    : media;

  $: previewItem = previewIndex >= 0 && previewIndex < displayMedia.length
    ? displayMedia[previewIndex] : null;

  $: searchQuery, selectedTags, typeFilter, fetchMedia();

  function downloadUrl(item) {
    return `/api/records/${item.record_id}/attachments/${item.id}/download`;
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  }

  async function fetchMedia() {
    loading = true;
    try {
      const params = new URLSearchParams();
      if (searchQuery) params.set("q", searchQuery);
      if (selectedTags.length > 0) params.set("tags", selectedTags.join(","));
      if (typeFilter !== "all") params.set("type", typeFilter);
      const qs = params.toString();
      const url = "/api/media/" + (qs ? `?${qs}` : "");
      const res = await fetch(url);
      if (res.ok) media = await res.json();
    } catch {}
    loading = false;
  }

  function openPreview(idx) {
    previewIndex = idx;
  }

  function previewNext() {
    if (displayMedia.length > 1) previewIndex = (previewIndex + 1) % displayMedia.length;
  }

  function previewPrev() {
    if (displayMedia.length > 1) previewIndex = (previewIndex - 1 + displayMedia.length) % displayMedia.length;
  }

  function connectSSE() {
    eventSource = new EventSource("/api/records/stream");
    eventSource.addEventListener("records-changed", () => fetchMedia());
  }

  onMount(() => {
    fetchMedia();
    connectSSE();
  });

  onDestroy(() => {
    if (eventSource) eventSource.close();
  });
</script>

<div class="media-page">
  <div class="media-filter-bar">
    {#each [["all","All"],["image","Images"],["video","Video"],["audio","Audio"],["document","Document"]] as [value, label]}
      <label class="filter-option" class:active={typeFilter === value}>
        <input type="radio" name="typeFilter" {value} bind:group={typeFilter} />
        {label}
      </label>
    {/each}
  </div>
  {#if loading && media.length === 0}
    <p class="status">Loading media...</p>
  {:else if displayMedia.length === 0}
    <p class="status">{searchQuery || selectedRecordId || selectedTags.length > 0 || typeFilter !== "all" ? "No media attachments matching filters." : "No media attachments yet."}</p>
  {:else}
    <div class="media-grid">
      {#each displayMedia as item, i (item.id)}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="media-card">
          <div class="media-thumb" on:click={() => openPreview(i)}>
            {#if item.content_type.startsWith("image/")}
              <img src={downloadUrl(item)} alt={item.filename} loading="lazy" />
            {:else if item.content_type.startsWith("video/")}
              <!-- svelte-ignore a11y-media-has-caption -->
              <video preload="metadata" src={downloadUrl(item)}></video>
              <div class="play-badge">&#9654;</div>
            {:else if item.content_type.startsWith("audio/")}
              <div class="audio-badge">&#9835;</div>
            {:else}
              <div class="doc-badge">&#128196;</div>
            {/if}
          </div>
          <div class="media-label" on:click={() => dispatch("openrecord", item.record_id)}>
            <span class="media-filename" title={item.filename}>{item.filename}</span>
            <span class="media-record-title" title={item.record_title}>{item.record_title}</span>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

{#if previewItem}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="lightbox" on:click|self={() => { previewIndex = -1; }}>
    <button class="lightbox-close" on:click|stopPropagation={() => { previewIndex = -1; }}>&times;</button>
    {#if displayMedia.length > 1}
      <button class="lightbox-arrow lightbox-prev" on:click|stopPropagation={previewPrev}>&lsaquo;</button>
    {/if}
    <div class="lightbox-content">
      {#if previewItem.content_type.startsWith("video/")}
        <!-- svelte-ignore a11y-media-has-caption -->
        <video controls autoplay src={downloadUrl(previewItem)} class="lightbox-media"></video>
      {:else if previewItem.content_type.startsWith("audio/")}
        <div class="lightbox-audio-wrap">
          <div class="lightbox-audio-icon">&#9835;</div>
          <div class="lightbox-audio-name">{previewItem.filename}</div>
          <audio controls autoplay src={downloadUrl(previewItem)} class="lightbox-audio"></audio>
        </div>
      {:else if previewItem.content_type.startsWith("image/")}
        <img src={downloadUrl(previewItem)} alt={previewItem.filename} />
      {:else}
        <div class="lightbox-audio-wrap">
          <div class="doc-badge lightbox-doc-icon">&#128196;</div>
          <div class="lightbox-audio-name">{previewItem.filename}</div>
          <a class="doc-download" href={downloadUrl(previewItem)} download={previewItem.filename}>Download</a>
        </div>
      {/if}
      <div class="lightbox-caption">{previewItem.record_title} &mdash; {previewItem.filename} ({formatFileSize(previewItem.size)})</div>
    </div>
    {#if displayMedia.length > 1}
      <button class="lightbox-arrow lightbox-next" on:click|stopPropagation={previewNext}>&rsaquo;</button>
    {/if}
  </div>
{/if}

<svelte:window on:keydown={e => {
  if (previewItem) {
    if (e.key === "Escape") { e.preventDefault(); previewIndex = -1; }
    else if (e.key === "ArrowRight") { e.preventDefault(); previewNext(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); previewPrev(); }
  }
}} />

<style>
  .media-page {
    padding: 0.5rem;
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    flex: 1;
    overflow: auto;
  }

  .media-filter-bar {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.25rem;
    padding: 0.25rem 0.25rem 0.5rem;
    flex-shrink: 0;
  }

  .filter-option {
    cursor: pointer;
    padding: 0.2rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    color: var(--text-muted, #888);
    border: 1px solid transparent;
    transition: all 0.15s;
    user-select: none;
  }

  .filter-option input {
    display: none;
  }

  .filter-option:hover {
    color: var(--text, #eaeaea);
  }

  .filter-option.active {
    color: var(--accent, #00ff88);
    border-color: var(--accent, #00ff88);
  }

  .doc-badge {
    font-size: 2.5rem;
  }

  .lightbox-doc-icon {
    font-size: 4rem;
  }

  .doc-download {
    color: var(--accent, #00ff88);
    text-decoration: none;
    padding: 0.4rem 1rem;
    border: 1px solid var(--accent, #00ff88);
    border-radius: 4px;
    font-size: 0.85rem;
  }

  .doc-download:hover {
    background: var(--accent, #00ff88);
    color: var(--bg, #1a1b20);
  }

  .status {
    text-align: center;
    color: var(--text-muted, #888);
    padding: 2rem 1rem;
  }

  .media-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 0.5rem;
    padding: 0.25rem;
  }

  .media-card {
    background: var(--bg-card, #24252b);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 6px;
    overflow: hidden;
    transition: border-color 0.15s;
  }

  .media-card:hover {
    border-color: var(--accent, #00ff88);
  }

  .media-thumb {
    width: 100%;
    aspect-ratio: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-input, #1a1b20);
    position: relative;
    overflow: hidden;
  }

  .media-thumb img,
  .media-thumb video {
    width: 100%;
    height: 100%;
    object-fit: cover;
  }

  .play-badge {
    position: absolute;
    bottom: 4px;
    right: 4px;
    background: rgba(0, 0, 0, 0.6);
    color: #fff;
    border-radius: 50%;
    width: 24px;
    height: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.7rem;
  }

  .audio-badge {
    font-size: 2.5rem;
    color: var(--accent, #00ff88);
  }

  .media-thumb {
    cursor: pointer;
  }

  .media-label {
    padding: 0.3rem 0.4rem;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    min-width: 0;
    cursor: pointer;
  }

  .media-label:hover {
    background: var(--bg-input, #1a1b20);
  }

  .media-filename {
    font-size: 0.7rem;
    color: var(--text, #eaeaea);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .media-record-title {
    font-size: 0.65rem;
    color: var(--text-muted, #888);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Lightbox */
  .lightbox {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.85);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 9999;
    cursor: pointer;
  }

  .lightbox-content {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    flex: 1;
    min-width: 0;
    cursor: default;
    gap: 0.75rem;
  }

  .lightbox-content img,
  .lightbox-media {
    max-width: 85vw;
    max-height: 80vh;
    object-fit: contain;
    border-radius: 4px;
  }

  .lightbox-caption {
    color: rgba(255, 255, 255, 0.7);
    font-size: 0.8rem;
    text-align: center;
    max-width: 80vw;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .lightbox-audio-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    cursor: default;
  }

  .lightbox-audio-icon {
    font-size: 4rem;
    color: var(--accent, #00ff88);
  }

  .lightbox-audio-name {
    color: var(--text, #eaeaea);
    font-size: 1rem;
  }

  .lightbox-audio {
    width: 400px;
    max-width: 80vw;
  }

  .lightbox-close {
    position: absolute;
    top: 0.5rem;
    right: 0.75rem;
    background: none;
    border: none;
    color: rgba(255, 255, 255, 0.7);
    font-size: 2.5rem;
    cursor: pointer;
    z-index: 10;
    line-height: 1;
  }

  .lightbox-close:hover {
    color: #fff;
  }

  .lightbox-arrow {
    background: none;
    border: none;
    color: rgba(255, 255, 255, 0.7);
    font-size: 3rem;
    cursor: pointer;
    padding: 0 1rem;
    user-select: none;
    flex-shrink: 0;
  }

  .lightbox-arrow:hover {
    color: #fff;
  }
</style>
