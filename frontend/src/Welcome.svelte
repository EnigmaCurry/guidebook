<script>
  import { onMount, createEventDispatcher } from "svelte";

  const dispatch = createEventDispatcher();

  let logbookName = "guidebook";
  let existingLogbooks = [];
  let saving = false;
  let error = "";
  let step = "logbook"; // "logbook" or "auth"
  let authRequired = false; // forced by env/CLI
  let authConfigured = false;
  let authLoading = true;

  onMount(async () => {
    try {
      const res = await fetch("/api/logbooks/");
      if (res.ok) {
        const data = await res.json();
        existingLogbooks = data.map(d => d.name);
        if (existingLogbooks.length > 0) {
          logbookName = existingLogbooks[0];
        }
      }
    } catch {}
    // Check auth status
    try {
      const res = await fetch("/api/auth/status");
      if (res.ok) {
        const data = await res.json();
        authRequired = data.required;
        authConfigured = data.configured;
      }
    } catch {}
    authLoading = false;
  });

  async function saveGlobal(key, value) {
    if (!value) return;
    await fetch(`/api/global-settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
  }

  async function finishLogbook() {
    saving = true;
    error = "";
    try {
      const name = logbookName.trim() || "guidebook";
      await saveGlobal("default_logbook_name", name);
      await saveGlobal("welcome_acknowledged", "true");
      const res = await fetch("/api/logbooks/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (!res.ok) {
        const createRes = await fetch("/api/logbooks/create", {
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
      // Skip auth step if already configured (e.g. --require-auth)
      if (authConfigured) {
        saving = false;
        dispatch("complete", { logbook: logbookName || "guidebook" });
        return;
      }
      saving = false;
      step = "auth";
    } catch (e) {
      error = e.message || "Something went wrong";
      saving = false;
    }
  }

  async function lockSession() {
    saving = true;
    error = "";
    try {
      const res = await fetch("/api/auth/lock", { method: "POST" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        error = data?.detail || "Failed to lock session";
        saving = false;
        return;
      }
      dispatch("complete", { logbook: logbookName.trim() || "guidebook" });
    } catch (e) {
      error = e.message || "Something went wrong";
      saving = false;
    }
  }

  async function skipAuth() {
    saving = true;
    error = "";
    try {
      const res = await fetch("/api/auth/skip", { method: "POST" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        error = data?.detail || "Failed to skip auth";
        saving = false;
        return;
      }
      dispatch("complete", { logbook: logbookName.trim() || "guidebook" });
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
      dispatch("complete", { logbook: null });
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

  $: nameError = validateName(logbookName.trim());
</script>

<div class="welcome-overlay">
  <div class="welcome-panel">
    {#if step === "logbook"}
      <h1>Welcome to Guidebook</h1>
      <p class="subtitle">Choose a name for your project database, or select an existing one.</p>

      <div class="fields">
        <div class="field">
          <label for="w-logbook">Project Name</label>
          {#if existingLogbooks.length > 0}
            <select id="w-logbook" bind:value={logbookName} style="max-width: 14rem">
              {#each existingLogbooks as name}
                <option value={name}>{name}</option>
              {/each}
            </select>
          {:else}
            <input id="w-logbook" type="text" bind:value={logbookName} autocomplete="off" on:keydown={onNameKeydown} placeholder="guidebook" style="max-width: 14rem" />
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
        <button class="continue-btn" on:click={finishLogbook} disabled={saving || !!nameError}>
          {saving ? "Setting up..." : "Continue"}
        </button>
      </div>
    {:else if step === "auth"}
      <h1>Session Security</h1>
      {#if authRequired}
        <p class="subtitle">Authentication is required for this instance. Lock your session to this browser to continue.</p>
        <div class="auth-options">
          <button class="auth-btn lock-btn" on:click={lockSession} disabled={saving}>
            {saving ? "Locking..." : "Lock to this browser"}
          </button>
        </div>
        <p class="hint">This creates a secure cookie that restricts access to only this browser session. No one else on the network will be able to use this server.</p>
      {:else}
        <p class="subtitle">Choose how to secure your Guidebook instance.</p>
        <div class="auth-options">
          <div class="auth-option">
            <button class="auth-btn lock-btn" on:click={lockSession} disabled={saving}>
              {saving ? "Locking..." : "Lock to this browser"}
            </button>
            <p class="auth-desc">Creates a secure cookie that restricts access to only this browser session. Recommended if your server is accessible on a network.</p>
          </div>
          <div class="auth-separator">
            <span>or</span>
          </div>
          <div class="auth-option">
            <button class="auth-btn skip-auth-btn" on:click={skipAuth} disabled={saving}>
              Skip — no authentication
            </button>
            <p class="auth-desc warning-text">Anyone on the network will be able to access your server. Only choose this if you trust all devices on your network.</p>
          </div>
        </div>
      {/if}

      {#if error}
        <p class="error">{error}</p>
      {/if}
    {/if}
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

  /* Auth step styles */
  .auth-options {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    margin-top: 1rem;
  }

  .auth-option {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .auth-btn {
    border: none;
    border-radius: 6px;
    padding: 0.6rem 1.5rem;
    font-size: 0.9rem;
    font-weight: 600;
    cursor: pointer;
    width: 100%;
  }

  .auth-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .lock-btn {
    background: var(--accent, #00ff88);
    color: var(--accent-text, #000);
  }

  .lock-btn:hover:not(:disabled) {
    opacity: 0.9;
  }

  .skip-auth-btn {
    background: var(--bg-input, #5a5c6a);
    color: var(--text, #eaeaea);
    border: 1px solid var(--border, #3a3b3f);
  }

  .skip-auth-btn:hover:not(:disabled) {
    background: var(--border, #3a3b3f);
  }

  .auth-desc {
    font-size: 0.75rem;
    color: var(--text-dim, #888);
    margin: 0;
    line-height: 1.4;
  }

  .warning-text {
    color: #e6a700;
  }

  .auth-separator {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    color: var(--text-dim, #888);
    font-size: 0.8rem;
  }

  .auth-separator::before,
  .auth-separator::after {
    content: "";
    flex: 1;
    border-top: 1px solid var(--border, #3a3b3f);
  }
</style>
