<script>
  import { onMount, onDestroy, createEventDispatcher } from "svelte";

  const dispatch = createEventDispatcher();

  export let roomId;
  export let peerName;
  export let ownFingerprint;
  export let peerFingerprint;
  export let pendingOffer = null;

  let pc = null;
  let dc = null;
  let connectionState = "idle";
  let iceState = "";
  let signalingState = "";
  let makingOffer = false;
  let debugLog = [];
  let debugOpen = false;
  let pingResults = [];
  let localCandidates = [];
  let remoteCandidates = [];
  let localSdp = "";
  let remoteSdp = "";

  $: polite = ownFingerprint < peerFingerprint;
  $: dcOpen = dc && dc.readyState === "open";

  function log(msg) {
    const ts = new Date().toLocaleTimeString("en-US", { hour12: false, fractionalSecondDigits: 3 });
    debugLog = [...debugLog, `[${ts}] ${msg}`];
  }

  async function sendSignal(type, fields = {}) {
    try {
      await fetch(`/api/chat/rooms/${roomId}/signal`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type, ...fields, ts: Date.now() / 1000 }),
      });
    } catch (err) {
      log(`Signal send error: ${err.message}`);
    }
  }

  function connect() {
    if (pc) return;
    log("Creating RTCPeerConnection");
    connectionState = "connecting";

    pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });

    pc.onicecandidate = ({ candidate }) => {
      if (candidate) {
        localCandidates = [...localCandidates, `${candidate.protocol || ""} ${candidate.address || ""}:${candidate.port || ""} ${candidate.type || ""}`];
        sendSignal("webrtc-ice", {
          candidate: candidate.candidate,
          sdpMid: candidate.sdpMid,
          sdpMLineIndex: candidate.sdpMLineIndex,
        });
      }
    };

    pc.oniceconnectionstatechange = () => {
      iceState = pc.iceConnectionState;
      log(`ICE: ${iceState}`);
    };

    pc.onconnectionstatechange = () => {
      connectionState = pc.connectionState;
      log(`Connection: ${connectionState}`);
      if (connectionState === "failed" || connectionState === "closed") {
        cleanup(false);
      }
    };

    pc.onsignalingstatechange = () => {
      signalingState = pc.signalingState;
    };

    pc.onnegotiationneeded = async () => {
      try {
        makingOffer = true;
        await pc.setLocalDescription();
        localSdp = pc.localDescription.sdp;
        log("Sending offer");
        await sendSignal("webrtc-offer", { sdp: pc.localDescription.sdp });
      } catch (err) {
        log(`Offer error: ${err.message}`);
      } finally {
        makingOffer = false;
      }
    };

    pc.ondatachannel = (e) => {
      log(`Remote data channel: ${e.channel.label}`);
      setupDataChannel(e.channel);
    };

    // Create data channel (only the impolite peer creates it to avoid duplicates,
    // but with perfect negotiation either side can — the offerer creates it)
    dc = pc.createDataChannel("p2p-test");
    setupDataChannel(dc);
    log("Created data channel");
  }

  function setupDataChannel(channel) {
    dc = channel;
    channel.onopen = () => {
      connectionState = "connected";
      dc = channel;
      log("Data channel open");
    };
    channel.onclose = () => {
      log("Data channel closed");
      dc = null;
    };
    channel.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === "ping") {
          channel.send(JSON.stringify({ type: "pong", ts: msg.ts }));
        } else if (msg.type === "pong") {
          const rtt = Date.now() - msg.ts;
          pingResults = [...pingResults, rtt];
          log(`Pong: ${rtt}ms RTT`);
        }
      } catch {}
    };
  }

  function sendPing() {
    if (dc && dc.readyState === "open") {
      dc.send(JSON.stringify({ type: "ping", ts: Date.now() }));
      log("Ping sent");
    }
  }

  async function handleOffer(detail) {
    if (detail.room_id !== roomId) return;
    log("Received offer");

    if (!pc) connect();

    const offerCollision = makingOffer || pc.signalingState !== "stable";
    const ignoreOffer = !polite && offerCollision;
    if (ignoreOffer) {
      log("Ignoring colliding offer (impolite)");
      return;
    }

    try {
      await pc.setRemoteDescription({ type: "offer", sdp: detail.sdp });
      remoteSdp = detail.sdp;
      await pc.setLocalDescription();
      localSdp = pc.localDescription.sdp;
      log("Sending answer");
      await sendSignal("webrtc-answer", { sdp: pc.localDescription.sdp });
    } catch (err) {
      log(`Handle offer error: ${err.message}`);
    }
  }

  async function handleAnswer(detail) {
    if (detail.room_id !== roomId) return;
    log("Received answer");
    try {
      await pc.setRemoteDescription({ type: "answer", sdp: detail.sdp });
      remoteSdp = detail.sdp;
    } catch (err) {
      log(`Handle answer error: ${err.message}`);
    }
  }

  async function handleIce(detail) {
    if (detail.room_id !== roomId) return;
    try {
      const candidate = new RTCIceCandidate({
        candidate: detail.candidate,
        sdpMid: detail.sdpMid,
        sdpMLineIndex: detail.sdpMLineIndex,
      });
      remoteCandidates = [...remoteCandidates, `${candidate.protocol || ""} ${candidate.address || ""}:${candidate.port || ""} ${candidate.type || ""}`];
      await pc.addIceCandidate(candidate);
    } catch (err) {
      if (!polite || pc.signalingState !== "stable") return;
      log(`ICE candidate error: ${err.message}`);
    }
  }

  function handleHangup(detail) {
    if (detail.room_id !== roomId) return;
    log("Remote peer hung up");
    cleanup(false);
  }

  function hangup() {
    sendSignal("webrtc-hangup");
    cleanup(false);
  }

  function cleanup(silent = false) {
    if (dc) {
      try { dc.close(); } catch {}
      dc = null;
    }
    if (pc) {
      try { pc.close(); } catch {}
      pc = null;
    }
    connectionState = "idle";
    iceState = "";
    signalingState = "";
    makingOffer = false;
    localCandidates = [];
    remoteCandidates = [];
    localSdp = "";
    remoteSdp = "";
    if (!silent) log("Connection closed");
  }

  function onOffer(e) { handleOffer(e.detail); }
  function onAnswer(e) { handleAnswer(e.detail); }
  function onIce(e) { handleIce(e.detail); }
  function onHangup(e) { handleHangup(e.detail); }

  onMount(() => {
    window.addEventListener("webrtc-offer", onOffer);
    window.addEventListener("webrtc-answer", onAnswer);
    window.addEventListener("webrtc-ice", onIce);
    window.addEventListener("webrtc-hangup", onHangup);
    // If opened due to an incoming offer, auto-connect and handle it
    if (pendingOffer) {
      handleOffer(pendingOffer);
      dispatch("offer-consumed");
    }
  });

  onDestroy(() => {
    window.removeEventListener("webrtc-offer", onOffer);
    window.removeEventListener("webrtc-answer", onAnswer);
    window.removeEventListener("webrtc-ice", onIce);
    window.removeEventListener("webrtc-hangup", onHangup);
    if (pc) {
      sendSignal("webrtc-hangup");
      cleanup(true);
    }
  });

  function stateColor(state) {
    if (state === "connected") return "var(--success, #4caf50)";
    if (state === "connecting") return "var(--warning, #ff9800)";
    if (state === "failed") return "var(--error, #f44336)";
    return "var(--text-muted, #888)";
  }
</script>

<div class="p2p-panel">
  <div class="p2p-header">
    <span class="p2p-status-dot" style="background: {stateColor(connectionState)}"></span>
    <span class="p2p-title">P2P: {peerName}</span>
    <span class="p2p-state">{connectionState}</span>
    <div class="p2p-actions">
      {#if connectionState === "idle"}
        <button class="btn-p2p" on:click={connect}>Connect</button>
      {:else}
        <button class="btn-p2p btn-hangup" on:click={hangup}>Hangup</button>
      {/if}
      {#if dcOpen}
        <button class="btn-p2p" on:click={sendPing}>Ping</button>
      {/if}
      <button class="btn-p2p btn-close" on:click={() => { if (pc) hangup(); }}>Close</button>
    </div>
  </div>

  {#if pingResults.length > 0}
    <div class="ping-results">
      <span class="ping-label">RTT:</span>
      {#each pingResults.slice(-10) as rtt}
        <span class="ping-value">{rtt}ms</span>
      {/each}
    </div>
  {/if}

  <button class="debug-toggle" on:click={() => debugOpen = !debugOpen}>
    {debugOpen ? "Hide" : "Show"} Debug {debugLog.length > 0 ? `(${debugLog.length})` : ""}
  </button>

  {#if debugOpen}
    <div class="debug-panel">
      <div class="debug-section">
        <div class="debug-row"><strong>Role:</strong> {polite ? "polite" : "impolite"}</div>
        <div class="debug-row"><strong>Connection:</strong> {connectionState}</div>
        <div class="debug-row"><strong>ICE:</strong> {iceState || "n/a"}</div>
        <div class="debug-row"><strong>Signaling:</strong> {signalingState || "n/a"}</div>
      </div>

      {#if localCandidates.length > 0}
        <div class="debug-section">
          <strong>Local candidates ({localCandidates.length}):</strong>
          <div class="candidate-list">{#each localCandidates as c}<div class="candidate">{c}</div>{/each}</div>
        </div>
      {/if}

      {#if remoteCandidates.length > 0}
        <div class="debug-section">
          <strong>Remote candidates ({remoteCandidates.length}):</strong>
          <div class="candidate-list">{#each remoteCandidates as c}<div class="candidate">{c}</div>{/each}</div>
        </div>
      {/if}

      {#if localSdp}
        <details class="debug-section">
          <summary>Local SDP</summary>
          <pre class="sdp">{localSdp}</pre>
        </details>
      {/if}

      {#if remoteSdp}
        <details class="debug-section">
          <summary>Remote SDP</summary>
          <pre class="sdp">{remoteSdp}</pre>
        </details>
      {/if}

      <div class="debug-log">
        {#each debugLog as entry}
          <div class="log-entry">{entry}</div>
        {/each}
      </div>
    </div>
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

  .btn-hangup {
    border-color: var(--error, #f44336);
    color: var(--error, #f44336);
  }

  .btn-close {
    border-color: var(--text-muted, #666);
    color: var(--text-muted, #888);
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
