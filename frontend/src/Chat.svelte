<script>
  import { onMount, onDestroy, tick } from "svelte";

  let rooms = [];
  let peers = [];
  let trusted = [];
  let pendingVerifications = [];
  let activeRoom = "lobby";
  let messages = [];
  let messageInput = "";
  let sending = false;
  let showPeers = false;
  let chatStatus = { active: false, cn: null, fingerprint: null, lobby_joined: false };
  let messagesEnd;

  async function loadStatus() {
    try {
      const res = await fetch("/api/chat/status");
      if (res.ok) chatStatus = await res.json();
    } catch {}
  }

  async function loadRooms() {
    try {
      const res = await fetch("/api/chat/rooms");
      if (res.ok) rooms = await res.json();
    } catch {}
  }

  async function loadPeers() {
    try {
      const res = await fetch("/api/chat/peers");
      if (res.ok) peers = await res.json();
    } catch {}
  }

  async function loadTrusted() {
    try {
      const res = await fetch("/api/chat/trusted");
      if (res.ok) trusted = await res.json();
    } catch {}
  }

  async function loadPending() {
    try {
      const res = await fetch("/api/chat/verify/pending");
      if (res.ok) pendingVerifications = await res.json();
    } catch {}
  }

  async function loadMessages() {
    try {
      const res = await fetch(`/api/chat/rooms/${activeRoom}/messages?limit=200`);
      if (res.ok) messages = await res.json();
      await tick();
      scrollToBottom();
    } catch {}
  }

  async function sendMessage() {
    if (!messageInput.trim() || sending) return;
    sending = true;
    try {
      await fetch(`/api/chat/rooms/${activeRoom}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: messageInput }),
      });
      messageInput = "";
    } catch {}
    sending = false;
  }

  async function verifyPeer(fingerprint) {
    await fetch(`/api/chat/verify/${encodeURIComponent(fingerprint)}`, { method: "POST" });
  }

  async function acceptVerification(fingerprint) {
    await fetch(`/api/chat/verify/${encodeURIComponent(fingerprint)}/accept`, { method: "POST" });
    pendingVerifications = pendingVerifications.filter(p => p.fingerprint !== fingerprint);
    await loadTrusted();
    await loadRooms();
  }

  async function rejectVerification(fingerprint) {
    await fetch(`/api/chat/verify/${encodeURIComponent(fingerprint)}/reject`, { method: "POST" });
    pendingVerifications = pendingVerifications.filter(p => p.fingerprint !== fingerprint);
  }

  function selectRoom(roomId) {
    activeRoom = roomId;
    loadMessages();
  }

  function scrollToBottom() {
    if (messagesEnd) {
      messagesEnd.scrollIntoView({ behavior: "smooth" });
    }
  }

  function formatTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function shortFingerprint(fp) {
    if (!fp) return "";
    return fp.split(":").slice(0, 4).join(":");
  }

  function isOwnMessage(msg) {
    return msg.fingerprint === chatStatus.fingerprint;
  }

  function isTrusted(fingerprint) {
    return trusted.some(t => t.fingerprint === fingerprint);
  }

  function isPendingOutgoing(fingerprint) {
    // We don't track this client-side yet, just return false
    return false;
  }

  function handleKeydown(e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  function onChatMessage(e) {
    const msg = e.detail;
    if (msg.room === activeRoom) {
      messages = [...messages, msg];
      tick().then(scrollToBottom);
    }
  }

  function onChatPeers(e) {
    peers = e.detail.peers || [];
  }

  function onChatVerifyRequest(e) {
    loadPending();
  }

  function onChatVerifyComplete(e) {
    loadTrusted();
    loadRooms();
  }

  function onChatRooms(e) {
    rooms = e.detail.rooms || [];
  }

  onMount(() => {
    loadStatus();
    loadRooms();
    loadPeers();
    loadTrusted();
    loadPending();
    loadMessages();

    window.addEventListener("chat-message", onChatMessage);
    window.addEventListener("chat-peers", onChatPeers);
    window.addEventListener("chat-verify-request", onChatVerifyRequest);
    window.addEventListener("chat-verify-complete", onChatVerifyComplete);
    window.addEventListener("chat-rooms", onChatRooms);
  });

  onDestroy(() => {
    window.removeEventListener("chat-message", onChatMessage);
    window.removeEventListener("chat-peers", onChatPeers);
    window.removeEventListener("chat-verify-request", onChatVerifyRequest);
    window.removeEventListener("chat-verify-complete", onChatVerifyComplete);
    window.removeEventListener("chat-rooms", onChatRooms);
  });
</script>

<div class="chat-container">
  <div class="chat-sidebar">
    <div class="sidebar-header">Rooms</div>
    {#each rooms as room}
      <button
        class="room-item"
        class:active={activeRoom === room.id}
        on:click={() => selectRoom(room.id)}
      >
        <span class="room-icon">{room.type === "lobby" ? "🌐" : "🔒"}</span>
        <span class="room-name">{room.name}</span>
      </button>
    {/each}
    {#if rooms.length === 0}
      <p class="sidebar-hint">No rooms available. Enable lobby or verify a peer.</p>
    {/if}

    <div class="sidebar-header" style="margin-top: 1em;">
      Peers
      {#if chatStatus.lobby_joined}
        <button class="btn-icon" on:click={() => { showPeers = !showPeers; if (showPeers) loadPeers(); }} title="Toggle peer list">
          {showPeers ? "▲" : "▼"}
        </button>
      {/if}
    </div>
    {#if showPeers && chatStatus.lobby_joined}
      {#each peers as peer}
        <div class="peer-item">
          <div class="peer-cn">{peer.cn}</div>
          <div class="peer-fp">{shortFingerprint(peer.fingerprint)}</div>
          {#if isTrusted(peer.fingerprint)}
            <span class="peer-badge trusted">Trusted</span>
          {:else}
            <button class="btn-small" on:click={() => verifyPeer(peer.fingerprint)}>Verify</button>
          {/if}
        </div>
      {/each}
      {#if peers.length === 0}
        <p class="sidebar-hint">No peers online.</p>
      {/if}
    {/if}

    {#if pendingVerifications.length > 0}
      <div class="sidebar-header" style="margin-top: 1em;">Verification Requests</div>
      {#each pendingVerifications as req}
        <div class="verify-request">
          <div class="peer-cn">{req.cn}</div>
          <div class="peer-fp">{shortFingerprint(req.fingerprint)}</div>
          <div class="verify-actions">
            <button class="btn-small btn-accept" on:click={() => acceptVerification(req.fingerprint)}>Accept</button>
            <button class="btn-small btn-reject" on:click={() => rejectVerification(req.fingerprint)}>Reject</button>
          </div>
        </div>
      {/each}
    {/if}
  </div>

  <div class="chat-main">
    {#if !chatStatus.active}
      <div class="chat-empty">
        <p>Chat is not active. Enable NATS Chat in Settings.</p>
      </div>
    {:else}
      <div class="chat-messages">
        {#each messages as msg}
          <div class="message" class:own={isOwnMessage(msg)}>
            <div class="message-header">
              <span class="message-cn" class:own-cn={isOwnMessage(msg)}>{msg.cn}</span>
              <span class="message-time">{formatTime(msg.ts)}</span>
            </div>
            <div class="message-text">{msg.text}</div>
          </div>
        {/each}
        <div bind:this={messagesEnd}></div>
      </div>

      <div class="chat-input">
        <input
          type="text"
          bind:value={messageInput}
          on:keydown={handleKeydown}
          placeholder={activeRoom === "lobby" ? "Message the lobby..." : "Send a message..."}
          disabled={sending}
        />
        <button class="btn-send" on:click={sendMessage} disabled={sending || !messageInput.trim()}>Send</button>
      </div>
    {/if}
  </div>
</div>

<style>
  .chat-container {
    display: flex;
    height: calc(100vh - 60px);
    overflow: hidden;
  }

  .chat-sidebar {
    width: 240px;
    min-width: 200px;
    border-right: 1px solid var(--border, #333);
    overflow-y: auto;
    padding: 0.5em;
    background: var(--bg-sidebar, var(--bg, #1a1a1a));
  }

  .sidebar-header {
    font-size: 0.75rem;
    text-transform: uppercase;
    color: var(--text-muted, #888);
    padding: 0.5em 0.5em 0.25em;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .sidebar-hint {
    font-size: 0.8rem;
    color: var(--text-muted, #888);
    padding: 0.25em 0.5em;
    margin: 0;
  }

  .room-item {
    display: flex;
    align-items: center;
    gap: 0.5em;
    width: 100%;
    padding: 0.5em;
    border: none;
    background: transparent;
    color: var(--text, #ccc);
    cursor: pointer;
    border-radius: 4px;
    font-size: 0.9rem;
    text-align: left;
  }

  .room-item:hover {
    background: var(--bg-hover, #2a2a2a);
  }

  .room-item.active {
    background: var(--accent, #e6a700);
    color: var(--bg, #111);
  }

  .room-icon {
    font-size: 1rem;
  }

  .room-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .peer-item, .verify-request {
    padding: 0.4em 0.5em;
    border-bottom: 1px solid var(--border, #333);
  }

  .peer-cn {
    font-size: 0.85rem;
    color: var(--text, #ccc);
  }

  .peer-fp {
    font-size: 0.7rem;
    color: var(--text-muted, #888);
    font-family: monospace;
  }

  .peer-badge {
    display: inline-block;
    font-size: 0.7rem;
    padding: 1px 6px;
    border-radius: 3px;
    margin-top: 2px;
  }

  .peer-badge.trusted {
    background: #2d5a2d;
    color: #8f8;
  }

  .verify-actions {
    display: flex;
    gap: 0.3em;
    margin-top: 0.3em;
  }

  .btn-small {
    font-size: 0.75rem;
    padding: 2px 8px;
    border: 1px solid var(--border, #555);
    background: var(--bg-input, #222);
    color: var(--text, #ccc);
    border-radius: 3px;
    cursor: pointer;
  }

  .btn-small:hover {
    background: var(--bg-hover, #333);
  }

  .btn-accept {
    border-color: #4a4;
    color: #8f8;
  }

  .btn-reject {
    border-color: #a44;
    color: #f88;
  }

  .btn-icon {
    background: none;
    border: none;
    color: var(--text-muted, #888);
    cursor: pointer;
    font-size: 0.7rem;
    padding: 0 4px;
  }

  .chat-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 0;
  }

  .chat-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
    color: var(--text-muted, #888);
  }

  .chat-messages {
    flex: 1;
    overflow-y: auto;
    padding: 1em;
    display: flex;
    flex-direction: column;
    gap: 0.3em;
  }

  .message {
    max-width: 80%;
    padding: 0.4em 0.7em;
    border-radius: 6px;
    background: var(--bg-input, #222);
  }

  .message.own {
    align-self: flex-end;
    background: var(--accent, #e6a700);
    color: var(--bg, #111);
  }

  .message-header {
    display: flex;
    justify-content: space-between;
    gap: 1em;
    font-size: 0.7rem;
    margin-bottom: 2px;
  }

  .message-cn {
    font-weight: bold;
    color: var(--accent, #e6a700);
  }

  .message-cn.own-cn {
    color: var(--bg, #111);
  }

  .message-time {
    color: var(--text-muted, #888);
    white-space: nowrap;
  }

  .message.own .message-time {
    color: var(--bg, #333);
  }

  .message-text {
    font-size: 0.9rem;
    word-break: break-word;
    white-space: pre-wrap;
  }

  .chat-input {
    display: flex;
    gap: 0.5em;
    padding: 0.5em 1em;
    border-top: 1px solid var(--border, #333);
    background: var(--bg, #1a1a1a);
  }

  .chat-input input {
    flex: 1;
    padding: 0.5em 0.75em;
    border: 1px solid var(--border-input, #444);
    border-radius: 4px;
    background: var(--bg-input, #222);
    color: var(--text, #ccc);
    font-size: 0.9rem;
  }

  .chat-input input:focus {
    outline: none;
    border-color: var(--accent, #e6a700);
  }

  .btn-send {
    padding: 0.5em 1em;
    border: none;
    border-radius: 4px;
    background: var(--accent, #e6a700);
    color: var(--bg, #111);
    font-size: 0.9rem;
    cursor: pointer;
    font-weight: bold;
  }

  .btn-send:hover:not(:disabled) {
    opacity: 0.9;
  }

  .btn-send:disabled {
    opacity: 0.5;
    cursor: default;
  }

  @media (max-width: 600px) {
    .chat-sidebar {
      width: 180px;
      min-width: 140px;
    }
  }
</style>
