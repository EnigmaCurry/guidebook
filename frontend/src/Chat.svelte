<script>
  import { onMount, onDestroy, tick } from "svelte";
  import Icon from "@iconify/svelte";
  import iconLocked from "@iconify-icons/twemoji/locked";
  import iconPlug from "@iconify-icons/twemoji/electric-plug";
  import iconCross from "@iconify-icons/twemoji/cross-mark";
  import P2P from "./P2P.svelte";
  import * as p2p from "./p2p.js";

  let rooms = [];
  let peers = [];
  let trusted = [];
  let pendingVerifications = [];
  let activeRoom = null;
  let messages = [];
  let messageInput = "";
  let sending = false;
  let chatStatus = { active: false, cn: null, fingerprint: null, lobby_joined: false };
  let messagesEnd;
  let messageInputEl;
  let p2pRoom = null;
  let p2pPeer = null;
  let p2pState = p2p.getState();

  async function loadStatus() {
    try {
      const res = await fetch("/api/chat/status");
      if (res.ok) chatStatus = await res.json();
    } catch {}
  }

  async function loadRooms() {
    try {
      const res = await fetch("/api/chat/rooms");
      if (res.ok) {
        rooms = await res.json();
        if (!activeRoom && rooms.length > 0) {
          activeRoom = rooms[0].id;
          loadMessages();
        }
      }
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
    await tick();
    if (messageInputEl) messageInputEl.focus();
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

  async function defriend(peer) {
    if (!confirm(`Remove ${peer.cn} as a friend? This will close your private room.`)) return;
    await fetch(`/api/chat/trusted/${encodeURIComponent(peer.fingerprint)}`, { method: "DELETE" });
    await loadTrusted();
    await loadRooms();
    if (activeRoom && !rooms.find(r => r.id === activeRoom)) {
      activeRoom = rooms.length > 0 ? rooms[0].id : null;
      if (activeRoom) loadMessages();
    }
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

  $: trustedSet = new Set(trusted.map(t => t.fingerprint));
  $: onlinePeerSet = new Set(peers.map(p => p.fingerprint));

  function peerStatus(room, _p2p, _online) {
    if (_p2p.roomId === room.id && _p2p.connectionState === "connected") {
      if (_p2p.routeType === "relay") return "p2p-relay";
      return "p2p";
    }
    if (_online.has(room.fingerprint)) return "online";
    return "offline";
  }

  function peerStatusTooltip(room, _p2p, _online) {
    const status = peerStatus(room, _p2p, _online);
    if (status === "p2p") {
      if (_p2p.routeType === "srflx") return "Direct P2P via NAT traversal (STUN)";
      return "Direct P2P via LAN";
    }
    if (status === "p2p-relay") return "P2P via TURN relay";
    if (status === "online") return "Online via NATS (no P2P)";
    return "Offline";
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
    if (!activeRoom && rooms.length > 0) {
      activeRoom = rooms[0].id;
      loadMessages();
    }
    if (activeRoom && !rooms.find(r => r.id === activeRoom)) {
      activeRoom = rooms.length > 0 ? rooms[0].id : null;
      if (activeRoom) loadMessages();
    }
  }

  function onChatDefriended(e) {
    loadTrusted();
    loadRooms();
  }

  let unsubP2P;

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
    window.addEventListener("chat-defriended", onChatDefriended);
    unsubP2P = p2p.onChange(s => {
      p2pState = s;
      // Auto-open P2P panel when connection is established from any page
      if (s.roomId && !p2pRoom) {
        const room = rooms.find(r => r.id === s.roomId);
        if (room) { p2pRoom = room.id; p2pPeer = room; }
      }
      // Clear panel when connection closes
      if (!s.roomId && p2pRoom) { p2pRoom = null; p2pPeer = null; }
    });
  });

  onDestroy(() => {
    window.removeEventListener("chat-message", onChatMessage);
    window.removeEventListener("chat-peers", onChatPeers);
    window.removeEventListener("chat-verify-request", onChatVerifyRequest);
    window.removeEventListener("chat-verify-complete", onChatVerifyComplete);
    window.removeEventListener("chat-rooms", onChatRooms);
    window.removeEventListener("chat-defriended", onChatDefriended);
    if (unsubP2P) unsubP2P();
  });
</script>

<div class="chat-container">
  <div class="chat-sidebar">
    <div class="sidebar-header">Rooms</div>
    {#each rooms as room}
      <div class="room-row" title={peerStatusTooltip(room, p2pState, onlinePeerSet)}>
        <button
          class="room-item"
          class:active={activeRoom === room.id}
          on:click={() => selectRoom(room.id)}
        >
          <span class="peer-status-dot" class:status-p2p={peerStatus(room, p2pState, onlinePeerSet) === "p2p"} class:status-p2p-relay={peerStatus(room, p2pState, onlinePeerSet) === "p2p-relay"} class:status-online={peerStatus(room, p2pState, onlinePeerSet) === "online"} class:status-offline={peerStatus(room, p2pState, onlinePeerSet) === "offline"}></span>
          <span class="room-name">{room.name}</span>
        </button>
        <button
          class="btn-small btn-p2p-connect"
          class:btn-p2p-active={p2pState.roomId === room.id}
          disabled={p2pState.roomId && p2pState.roomId !== room.id}
          on:click|stopPropagation={() => {
            if (p2pState.roomId === room.id) {
              p2p.hangup();
            } else {
              p2pRoom = room.id;
              p2pPeer = room;
              p2p.connect(room.id, room.name, chatStatus.fingerprint, room.fingerprint);
            }
          }}
          title={p2pState.roomId === room.id ? "Disconnect P2P" : "P2P Connect"}
        ><Icon icon={iconPlug} width={14} /></button>
      </div>
    {/each}
    {#if rooms.length === 0}
      <p class="sidebar-hint">No rooms yet. Verify a peer to start chatting.</p>
    {/if}

    {#if chatStatus.lobby_joined}
      <div class="sidebar-header" style="margin-top: 1em;">Peers</div>
      {#each peers as peer}
        <div class="peer-item">
          <div class="peer-cn">{peer.cn}</div>
          <div class="peer-fp">{shortFingerprint(peer.fingerprint)}</div>
          {#if trustedSet.has(peer.fingerprint)}
            <span class="peer-badge trusted">Friends</span>
            <button class="btn-small btn-defriend" on:click={() => defriend(peer)} title="Remove friend"><Icon icon={iconCross} width={12} /></button>
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
    {#if p2pRoom && p2pPeer}
      <P2P
        roomId={p2pRoom}
        peerName={p2pPeer.name}
        ownFingerprint={chatStatus.fingerprint}
        peerFingerprint={p2pPeer.fingerprint}
      />
    {/if}
    {#if !chatStatus.active}
      <div class="chat-empty">
        <p>Chat is not active. Enable NATS Chat in Settings.</p>
      </div>
    {:else if !activeRoom}
      <div class="chat-empty">
        <p>Verify a peer to start a private conversation.</p>
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
          bind:this={messageInputEl}
          bind:value={messageInput}
          on:keydown={handleKeydown}
          placeholder="Send a message..."
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
    flex: 1;
    min-height: 0;
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

  .room-row {
    display: flex;
    align-items: center;
    gap: 0.25em;
    min-width: 0;
  }

  .peer-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .status-offline {
    background: var(--error, #f44336);
  }

  .status-online {
    background: var(--warning, #ff9800);
  }

  .status-p2p {
    background: var(--success, #4caf50);
    animation: pulse-glow-green 2s ease-in-out infinite;
  }

  .status-p2p-relay {
    background: #42a5f5;
    animation: pulse-glow-blue 2s ease-in-out infinite;
  }

  @keyframes pulse-glow-green {
    0%, 100% { opacity: 1; box-shadow: 0 0 3px var(--success, #4caf50); }
    50% { opacity: 0.6; box-shadow: 0 0 8px var(--success, #4caf50); }
  }

  @keyframes pulse-glow-blue {
    0%, 100% { opacity: 1; box-shadow: 0 0 3px #42a5f5; }
    50% { opacity: 0.6; box-shadow: 0 0 8px #42a5f5; }
  }

  .btn-p2p-connect {
    flex-shrink: 0;
    font-size: 0.65rem;
    padding: 0.15em 0.4em;
    border: 1px solid var(--border, #444);
    border-radius: 3px;
    background: var(--bg, #222);
    color: var(--text-muted, #888);
    cursor: pointer;
  }

  .btn-p2p-connect:hover:not(:disabled) {
    background: var(--bg-hover, #2a2a2a);
    color: var(--text, #ccc);
  }

  .btn-p2p-connect:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .btn-p2p-active {
    border-color: var(--accent, #e6a700);
    color: var(--accent, #e6a700);
  }

  .room-item {
    display: flex;
    align-items: center;
    gap: 0.5em;
    flex: 1;
    min-width: 0;
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

  .btn-defriend {
    border-color: #a44;
    color: #f88;
    margin-left: 0.3em;
    padding: 1px 5px;
    font-size: 0.7rem;
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
