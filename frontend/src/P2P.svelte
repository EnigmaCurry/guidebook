<script>
  import { onMount, onDestroy } from "svelte";
  import * as p2p from "./p2p.js";

  export let roomId;
  export let peerName;
  export let ownFingerprint;
  export let peerFingerprint;

  let s = p2p.getState();
  let debugOpen = false;
  let unsubscribe;

  $: active = s.roomId === roomId;
  $: polite = ownFingerprint < peerFingerprint;

  onMount(() => {
    unsubscribe = p2p.onChange(next => { s = next; });
  });

  onDestroy(() => {
    if (unsubscribe) unsubscribe();
  });

  function connect() {
    p2p.connect(roomId, peerName, ownFingerprint, peerFingerprint);
  }

  function stateColor(state, routeType) {
    if (state === "connected") return routeType === "relay" ? "#42a5f5" : "var(--success, #4caf50)";
    if (state === "connecting") return "var(--warning, #ff9800)";
    if (state === "failed") return "var(--error, #f44336)";
    return "var(--text-muted, #888)";
  }
</script>

<div class="p2p-panel">
  <div class="p2p-header">
    <span class="p2p-status-dot" style="background: {stateColor(active ? s.connectionState : 'idle', s.routeType)}"></span>
    <span class="p2p-title">P2P: {peerName}</span>
    <span class="p2p-state">{active ? (s.connectionState === "connected" && s.routeType ? `${s.connectionState} (${s.routeType})` : s.connectionState) : "idle"}</span>
    <div class="p2p-actions">
      {#if !active || s.connectionState === "idle"}
        <button class="btn-p2p" on:click={connect} disabled={s.roomId && !active}>Connect</button>
      {:else}
        <button class="btn-p2p btn-hangup" on:click={p2p.hangup}>Hangup</button>
      {/if}
      {#if active && s.dcOpen}
        <button class="btn-p2p" on:click={p2p.sendPing}>Ping</button>
      {/if}
    </div>
  </div>

  {#if active && s.pingResults.length > 0}
    <div class="ping-results">
      <span class="ping-label">RTT:</span>
      {#each s.pingResults.slice(-10) as rtt}
        <span class="ping-value">{rtt}ms</span>
      {/each}
    </div>
  {/if}

  {#if active}
    <button class="debug-toggle" on:click={() => debugOpen = !debugOpen}>
      {debugOpen ? "Hide" : "Show"} Debug {s.debugLog.length > 0 ? `(${s.debugLog.length})` : ""}
    </button>

    {#if debugOpen}
      <div class="debug-panel">
        <div class="debug-section">
          <div class="debug-row"><strong>Role:</strong> {polite ? "polite" : "impolite"}</div>
          <div class="debug-row"><strong>Connection:</strong> {s.connectionState}</div>
          <div class="debug-row"><strong>ICE:</strong> {s.iceState || "n/a"}</div>
          <div class="debug-row"><strong>Signaling:</strong> {s.signalingState || "n/a"}</div>
        </div>

        {#if s.localCandidates.length > 0}
          <div class="debug-section">
            <strong>Local candidates ({s.localCandidates.length}):</strong>
            <div class="candidate-list">{#each s.localCandidates as c}<div class="candidate">{c}</div>{/each}</div>
          </div>
        {/if}

        {#if s.remoteCandidates.length > 0}
          <div class="debug-section">
            <strong>Remote candidates ({s.remoteCandidates.length}):</strong>
            <div class="candidate-list">{#each s.remoteCandidates as c}<div class="candidate">{c}</div>{/each}</div>
          </div>
        {/if}

        {#if s.localSdp}
          <details class="debug-section">
            <summary>Local SDP</summary>
            <pre class="sdp">{s.localSdp}</pre>
          </details>
        {/if}

        {#if s.remoteSdp}
          <details class="debug-section">
            <summary>Remote SDP</summary>
            <pre class="sdp">{s.remoteSdp}</pre>
          </details>
        {/if}

        <div class="debug-log">
          {#each s.debugLog as entry}
            <div class="log-entry">{entry}</div>
          {/each}
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .p2p-panel {
    border: 1px solid var(--border, #333);
    border-radius: 6px;
    margin: 0.5em;
    background: var(--bg-sidebar, var(--bg, #1a1a1a));
    overflow: hidden;
  }

  .p2p-header {
    display: flex;
    align-items: center;
    gap: 0.5em;
    padding: 0.5em 0.75em;
    border-bottom: 1px solid var(--border, #333);
  }

  .p2p-status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .p2p-title {
    font-weight: bold;
    font-size: 0.9rem;
    color: var(--text, #ccc);
  }

  .p2p-state {
    font-size: 0.75rem;
    color: var(--text-muted, #888);
    flex: 1;
  }

  .p2p-actions {
    display: flex;
    gap: 0.25em;
  }

  .btn-p2p {
    padding: 0.2em 0.6em;
    border: 1px solid var(--border, #444);
    border-radius: 4px;
    background: var(--bg, #222);
    color: var(--text, #ccc);
    cursor: pointer;
    font-size: 0.8rem;
  }

  .btn-p2p:hover {
    background: var(--bg-hover, #2a2a2a);
  }

  .btn-p2p:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn-hangup {
    border-color: var(--error, #f44336);
    color: var(--error, #f44336);
  }

  .ping-results {
    display: flex;
    gap: 0.5em;
    padding: 0.25em 0.75em;
    font-size: 0.8rem;
    color: var(--text-muted, #888);
    flex-wrap: wrap;
    align-items: center;
  }

  .ping-label {
    font-weight: bold;
  }

  .ping-value {
    background: var(--bg, #222);
    padding: 0.1em 0.4em;
    border-radius: 3px;
    font-family: monospace;
    font-size: 0.75rem;
  }

  .debug-toggle {
    display: block;
    width: 100%;
    padding: 0.25em 0.75em;
    border: none;
    border-top: 1px solid var(--border, #333);
    background: transparent;
    color: var(--text-muted, #888);
    cursor: pointer;
    font-size: 0.75rem;
    text-align: left;
  }

  .debug-toggle:hover {
    background: var(--bg-hover, #2a2a2a);
  }

  .debug-panel {
    border-top: 1px solid var(--border, #333);
    padding: 0.5em 0.75em;
    font-size: 0.75rem;
    max-height: 300px;
    overflow-y: auto;
  }

  .debug-section {
    margin-bottom: 0.5em;
    color: var(--text, #ccc);
  }

  .debug-row {
    margin: 0.15em 0;
  }

  .candidate-list {
    margin-top: 0.15em;
  }

  .candidate {
    font-family: monospace;
    font-size: 0.7rem;
    color: var(--text-muted, #888);
    padding: 0.1em 0;
  }

  .sdp {
    font-size: 0.65rem;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 150px;
    overflow-y: auto;
    background: var(--bg, #111);
    padding: 0.5em;
    border-radius: 4px;
    margin-top: 0.25em;
  }

  .debug-log {
    border-top: 1px solid var(--border, #333);
    padding-top: 0.25em;
    margin-top: 0.5em;
  }

  .log-entry {
    font-family: monospace;
    font-size: 0.7rem;
    color: var(--text-muted, #888);
    padding: 0.1em 0;
  }
</style>
