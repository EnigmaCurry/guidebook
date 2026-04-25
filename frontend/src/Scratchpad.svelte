<script>
  import { onMount, onDestroy } from "svelte";

  let content = "";
  let textareaEl;
  let eventSource = null;
  let suppressNextUpdate = false;

  async function fetchContent() {
    try {
      const res = await fetch("/api/scratchpad/");
      if (res.ok) {
        const data = await res.json();
        content = data.content;
      }
    } catch {}
  }

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/api/scratchpad/stream");

    eventSource.addEventListener("scratchpad", (e) => {
      if (suppressNextUpdate) {
        suppressNextUpdate = false;
        return;
      }
      const data = JSON.parse(e.data);
      const el = textareaEl;
      if (el) {
        const start = el.selectionStart;
        const end = el.selectionEnd;
        content = data.content;
        requestAnimationFrame(() => {
          if (textareaEl) {
            textareaEl.selectionStart = Math.min(start, content.length);
            textareaEl.selectionEnd = Math.min(end, content.length);
          }
        });
      } else {
        content = data.content;
      }
    });

    eventSource.onerror = () => {};
  }

  let sendTimer = null;

  function onInput() {
    suppressNextUpdate = true;
    if (sendTimer) clearTimeout(sendTimer);
    sendTimer = setTimeout(() => {
      fetch("/api/scratchpad/", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      }).catch(() => {});
    }, 100);
  }

  onMount(() => {
    fetchContent();
    connectSSE();
  });

  onDestroy(() => {
    if (eventSource) eventSource.close();
    if (sendTimer) clearTimeout(sendTimer);
  });
</script>

<div class="scratchpad-container">
  <h2>Scratchpad</h2>
  <p class="hint">Ephemeral shared notepad — not saved to disk.</p>
  <textarea
    bind:this={textareaEl}
    bind:value={content}
    on:input={onInput}
    placeholder="Type here — all connected clients see changes instantly …"
    spellcheck="false"
  ></textarea>
</div>

<style>
  .scratchpad-container {
    display: flex;
    flex-direction: column;
    height: 100%;
    padding: 1rem;
    box-sizing: border-box;
  }
  h2 {
    margin: 0 0 0.25rem;
    font-size: 1.2rem;
    color: var(--heading-color, inherit);
  }
  .hint {
    margin: 0 0 0.75rem;
    font-size: 0.85rem;
    opacity: 0.6;
  }
  textarea {
    flex: 1;
    width: 100%;
    min-height: 200px;
    padding: 0.75rem;
    font-family: inherit;
    font-size: 0.95rem;
    line-height: 1.5;
    border: 1px solid var(--border-color, #ccc);
    border-radius: 6px;
    background: var(--input-bg, #fff);
    color: var(--text-color, #222);
    resize: vertical;
    box-sizing: border-box;
  }
  textarea:focus {
    outline: none;
    border-color: var(--accent-color, #4a90d9);
    box-shadow: 0 0 0 2px rgba(74, 144, 217, 0.2);
  }
  textarea::placeholder {
    opacity: 0.4;
  }
</style>
