<script>
  import { onMount, createEventDispatcher } from "svelte";

  const dispatch = createEventDispatcher();

  let databaseName = "guidebook";
  let existingDatabases = [];
  let saving = false;
  let error = "";

  onMount(async () => {
    try {
      const res = await fetch("/api/databases/");
      if (res.ok) {
        const data = await res.json();
        existingDatabases = data.map(d => d.name);
        if (existingDatabases.length > 0) {
          databaseName = existingDatabases[0];
        }
      }
    } catch {}
  });

  async function saveGlobal(key, value) {
    if (!value) return;
    await fetch(`/api/instance-settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  }

  async function finishDatabase() {
    saving = true;
    error = "";
    try {
      const name = databaseName.trim() || "guidebook";
      await saveGlobal("welcome_acknowledged", "true");
      const res = await fetch("/api/databases/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const createRes = await fetch("/api/databases/create", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (!createRes.ok) {
          const data = await createRes.json().catch(() => null);
          error = data?.detail || "Failed to create database";
          saving = false;
          return;
        }
      }
      saving = false;
      dispatch("complete", { database: databaseName || "guidebook" });
    } catch (e) {
      error = e.message || "Something went wrong";
      saving = false;
    }
  }

  async function skip() {
    saving = true;
    error = "";
    try {
      await saveGlobal("welcome_acknowledged", "true");
      dispatch("complete", { database: null });
    } catch (e) {
      error = e.message || "Something went wrong";
      saving = false;
    }
  }

  function onNameKeydown(e) {
    if (!/[a-zA-Z0-9_-]/.test(e.key) && e.key.length === 1) {
      e.preventDefault();
    }
  }

  function validateName(name) {
    if (name.startsWith("__")) return "Name must not start with '__'";
    if (name && !/^[a-zA-Z0-9_-]+$/.test(name)) return "Letters, numbers, hyphens, underscores only";
    return "";
  }

  $: nameError = validateName(databaseName.trim());
</script>

<div class="welcome-overlay">
  <div class="welcome-panel">
    <h1>Welcome to Guidebook</h1>
    <p class="subtitle">Choose a name for your project database, or select an existing one.</p>

    <div class="fields">
      <div class="field">
        <label for="w-database">Project Name</label>
        {#if existingDatabases.length > 0}
          <select id="w-database" bind:value={databaseName} style="max-width: 14rem">
            {#each existingDatabases as name}
              <option value={name}>{name}</option>
            {/each}
          </select>
        {:else}
          <input id="w-database" type="text" bind:value={databaseName} autocomplete="off" on:keydown={onNameKeydown} placeholder="guidebook" style="max-width: 14rem" />
          {#if nameError}
            <span class="field-error">{nameError}</span>
          {:else}
            <span class="hint">Letters, numbers, hyphens, underscores only</span>
          {/if}
        {/if}
      </div>
    </div>

    {#if error}
      <p class="error">{error}</p>
    {/if}

    <div class="actions">
      <button class="skip-link" on:click={skip} disabled={saving}>Skip</button>
      <button class="continue-btn" on:click={finishDatabase} disabled={saving || !!nameError}>
        {saving ? "Setting up..." : "Continue"}
      </button>
    </div>
  </div>
</div>

<style>
  .welcome-overlay {
    position: fixed;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg, #1a1b1e);
    z-index: 1000;
  }

  .welcome-panel {
    background: var(--bg-card, #24252b);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 12px;
    padding: 2rem 2.5rem;
    max-width: 480px;
    width: 90vw;
  }

  h1 {
    margin: 0 0 0.25rem;
    font-size: 1.5rem;
    color: var(--text, #eaeaea);
  }

  .subtitle {
    margin: 0 0 1.5rem;
    font-size: 0.85rem;
    color: var(--text-dim, #888);
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .field label {
    font-size: 0.8rem;
    color: var(--text-dim, #888);
  }

  .field select,
  .field input {
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 4px;
    background: var(--bg-input, transparent);
    color: var(--text, #eaeaea);
    font-size: 0.9rem;
  }

  .hint {
    font-size: 0.7rem;
    color: var(--text-dim, #888);
  }

  .error, .field-error {
    color: #ff4444;
    font-size: 0.7rem;
  }
  .error {
    margin: 0.75rem 0 0;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 1rem;
    margin-top: 1.5rem;
  }

  .skip-link {
    background: none;
    border: none;
    color: var(--text-dim, #888);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0.4rem 0.8rem;
    text-decoration: underline;
  }

  .skip-link:hover {
    color: var(--text, #eaeaea);
  }

  .continue-btn {
    background: var(--accent, #00ff88);
    color: var(--accent-text);
    border: none;
    border-radius: 6px;
    padding: 0.5rem 1.5rem;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
  }

  .continue-btn:hover {
    opacity: 0.9;
  }

  .continue-btn:disabled, .skip-link:disabled {
    opacity: 0.5;
    cursor: default;
  }
</style>
