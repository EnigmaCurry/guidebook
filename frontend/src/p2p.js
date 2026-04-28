/**
 * P2P WebRTC connection manager.
 *
 * Runs at the app level — handles signaling events regardless of which
 * page is active.  Exposes state via onChange callbacks and the getState()
 * snapshot so UI components can subscribe without owning the connection.
 */

let pc = null;
let dc = null;
let makingOffer = false;
let roomId = null;
let peerName = null;
let ownFingerprint = null;
let peerFingerprint = null;
let iceDisconnectTimer = null;

let state = {
  connectionState: "idle",
  iceState: "",
  signalingState: "",
  dcOpen: false,
  routeType: "", // "direct" | "relay" | ""
  roomId: null,
  peerName: null,
  ownFingerprint: null,
  peerFingerprint: null,
  polite: false,
  debugLog: [],
  pingResults: [],
  localCandidates: [],
  remoteCandidates: [],
  localSdp: "",
  remoteSdp: "",
};

const listeners = new Set();

function notify() {
  state = { ...state };
  for (const fn of listeners) fn(state);
}

export function onChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function getState() {
  return state;
}

function log(msg) {
  const ts = new Date().toLocaleTimeString("en-US", { hour12: false, fractionalSecondDigits: 3 });
  state.debugLog = [...state.debugLog, `[${ts}] ${msg}`];
  notify();
}

async function detectRouteType() {
  if (!pc) return;
  try {
    const stats = await pc.getStats();
    for (const report of stats.values()) {
      if (report.type === "candidate-pair" && report.state === "succeeded") {
        const localId = report.localCandidateId;
        const local = stats.get(localId);
        if (local && local.candidateType === "relay") {
          state.routeType = "relay";
          log("Route: relay (TURN)");
        } else {
          state.routeType = "direct";
          log("Route: direct");
        }
        notify();
        return;
      }
    }
  } catch {}
}

async function sendSignal(type, fields = {}) {
  if (!roomId) return;
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

function setupDataChannel(channel) {
  dc = channel;
  channel.onopen = () => {
    state.connectionState = "connected";
    state.dcOpen = true;
    dc = channel;
    log("Data channel open");
    detectRouteType();
    channel.send(JSON.stringify({ type: "sync-request" }));
    log("Sync request sent");
  };
  channel.onclose = () => {
    log("Data channel closed");
    dc = null;
    state.dcOpen = false;
    notify();
  };
  channel.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === "ping") {
        channel.send(JSON.stringify({ type: "pong", ts: msg.ts }));
      } else if (msg.type === "pong") {
        const rtt = Date.now() - msg.ts;
        state.pingResults = [...state.pingResults, rtt];
        log(`Pong: ${rtt}ms RTT`);
      } else if (msg.type === "sync-request") {
        handleSyncRequest(channel);
      } else if (msg.type === "sync-offer") {
        handleSyncOffer(msg.records, channel);
      } else if (msg.type === "sync-attachment") {
        handleSyncAttachment(msg);
      } else if (msg.type === "sync-delete-attachment") {
        handleSyncDeleteAttachment(msg);
      } else if (msg.type === "sync-delete-record") {
        handleSyncDeleteRecord(msg);
      } else if (msg.type === "sync-ack") {
        log(`Sync ack: ${msg.accepted} accepted, ${msg.skipped} skipped`);
      }
    } catch {}
  };
}

async function createPC() {
  if (pc) return;
  log("Creating RTCPeerConnection");
  state.connectionState = "connecting";
  notify();

  let iceServers = [];
  try {
    const res = await fetch("/api/chat/ice-servers");
    if (res.ok) iceServers = await res.json();
  } catch {}
  if (iceServers.length > 0) {
    log(`ICE servers: ${iceServers.map((s) => s.urls).join(", ")}`);
  }

  pc = new RTCPeerConnection({ iceServers });

  pc.onicecandidate = ({ candidate }) => {
    if (candidate) {
      state.localCandidates = [...state.localCandidates, `${candidate.protocol || ""} ${candidate.address || ""}:${candidate.port || ""} ${candidate.type || ""}`];
      notify();
      sendSignal("webrtc-ice", {
        candidate: candidate.candidate,
        sdpMid: candidate.sdpMid,
        sdpMLineIndex: candidate.sdpMLineIndex,
      });
    }
  };

  pc.oniceconnectionstatechange = () => {
    state.iceState = pc.iceConnectionState;
    log(`ICE: ${state.iceState}`);
    if (iceDisconnectTimer) { clearTimeout(iceDisconnectTimer); iceDisconnectTimer = null; }
    if (state.iceState === "disconnected") {
      iceDisconnectTimer = setTimeout(() => {
        if (pc && pc.iceConnectionState === "disconnected") {
          log("ICE disconnected timeout — closing");
          cleanup(false);
        }
      }, 5000);
    }
  };

  pc.onconnectionstatechange = () => {
    state.connectionState = pc.connectionState;
    log(`Connection: ${state.connectionState}`);
    if (state.connectionState === "failed" || state.connectionState === "closed" || state.connectionState === "disconnected") {
      cleanup(false);
    }
  };

  pc.onsignalingstatechange = () => {
    state.signalingState = pc.signalingState;
    notify();
  };

  pc.onnegotiationneeded = async () => {
    try {
      makingOffer = true;
      await pc.setLocalDescription();
      state.localSdp = pc.localDescription.sdp;
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

  dc = pc.createDataChannel("p2p-test");
  setupDataChannel(dc);
  log("Created data channel");
}

function isStale() {
  if (!pc) return false;
  const cs = pc.connectionState;
  const ice = pc.iceConnectionState;
  return cs === "failed" || cs === "closed" || cs === "disconnected"
    || ice === "failed" || ice === "closed" || ice === "disconnected";
}

/** Start a P2P connection to a peer (called from UI). */
export async function connect(rid, name, ownFp, peerFp) {
  if (pc && isStale()) { log("Clearing stale connection"); cleanup(true); }
  if (pc) return;
  roomId = rid;
  peerName = name;
  ownFingerprint = ownFp;
  peerFingerprint = peerFp;
  state.roomId = rid;
  state.peerName = name;
  state.ownFingerprint = ownFp;
  state.peerFingerprint = peerFp;
  state.polite = ownFp < peerFp;
  await createPC();
}

/**
 * Push a single record (and its attachments) to the connected peer
 * if the peer is a recipient. If not connected, auto-connects first
 * (the on-open sync will deliver the record).
 */
export async function pushRecord(record) {
  if (!record.recipients || record.recipients.length === 0) return;

  // If connected and peer is a recipient, push immediately
  if (dc && dc.readyState === "open" && peerFingerprint) {
    if (record.recipients.includes(peerFingerprint)) {
      dc.send(JSON.stringify({ type: "sync-offer", records: [record] }));
      log(`Pushed record to peer: ${record.title}`);
      await pushAttachments(record);
      return;
    }
  }

  // Not connected (or connected to a different peer) — auto-connect
  // to the first recipient that has a mutual room
  await autoConnectForRecipients(record.recipients);
}

async function pushAttachments(record) {
  if (!record.id || !dc || dc.readyState !== "open") return;
  try {
    const attRes = await fetch(`/api/records/${record.id}/attachments/`);
    if (!attRes.ok) return;
    const atts = await attRes.json();
    for (const att of atts) {
      const b64 = await fetchAttachmentAsBase64(record.id, att.id);
      if (!b64) continue;
      dc.send(
        JSON.stringify({
          type: "sync-attachment",
          record_uuid: record.uuid,
          filename: att.filename,
          content_type: att.content_type,
          data: b64,
        })
      );
    }
    if (atts.length > 0) log(`Pushed ${atts.length} attachments`);
  } catch {}
}

/**
 * Notify the connected peer that an attachment was deleted.
 * Auto-connects if needed.
 */
export async function pushDeleteAttachment(recordUuid, filename, recipients) {
  if (!recipients || recipients.length === 0) return;

  if (dc && dc.readyState === "open" && peerFingerprint) {
    if (recipients.includes(peerFingerprint)) {
      dc.send(
        JSON.stringify({
          type: "sync-delete-attachment",
          record_uuid: recordUuid,
          filename: filename,
        })
      );
      log(`Pushed attachment deletion: ${filename}`);
      return;
    }
  }

  // Not connected — auto-connect; on-open sync won't delete, but the
  // attachment won't exist on our side so it won't be sent either.
  // For an explicit delete we need to queue it and send after connect.
  // For now, auto-connect so future syncs are consistent.
  await autoConnectForRecipients(recipients);
}

/**
 * Notify the connected peer that a record was deleted.
 * Auto-connects if needed.
 */
export async function pushDeleteRecord(recordUuid, recipients) {
  if (!recipients || recipients.length === 0) return;

  if (dc && dc.readyState === "open" && peerFingerprint) {
    if (recipients.includes(peerFingerprint)) {
      dc.send(
        JSON.stringify({
          type: "sync-delete-record",
          record_uuid: recordUuid,
        })
      );
      log(`Pushed record deletion: ${recordUuid}`);
      return;
    }
  }

  await autoConnectForRecipients(recipients);
}

async function autoConnectForRecipients(recipients) {
  // Already connected to one of the recipients — on-open sync will handle it
  if (pc && !isStale() && peerFingerprint && recipients.includes(peerFingerprint))
    return;
  try {
    const [statusRes, roomsRes] = await Promise.all([
      fetch("/api/chat/status"),
      fetch("/api/chat/rooms"),
    ]);
    if (!statusRes.ok || !roomsRes.ok) return;
    const chatStatus = await statusRes.json();
    const rooms = await roomsRes.json();
    if (!chatStatus.fingerprint) return;

    for (const fp of recipients) {
      const room = rooms.find((r) => r.fingerprint === fp);
      if (!room) continue;
      log(`Auto-connecting to ${room.name} for record sync`);
      connect(room.id, room.name, chatStatus.fingerprint, room.fingerprint);
      return;
    }
  } catch {}
}

export function sendPing() {
  if (dc && dc.readyState === "open") {
    dc.send(JSON.stringify({ type: "ping", ts: Date.now() }));
    log("Ping sent");
  }
}

export function hangup() {
  sendSignal("webrtc-hangup");
  cleanup(false);
}

function cleanup(silent) {
  if (iceDisconnectTimer) { clearTimeout(iceDisconnectTimer); iceDisconnectTimer = null; }
  if (dc) { try { dc.close(); } catch {} dc = null; }
  if (pc) { try { pc.close(); } catch {} pc = null; }
  roomId = null;
  peerName = null;
  ownFingerprint = null;
  peerFingerprint = null;
  makingOffer = false;
  state.connectionState = "idle";
  state.iceState = "";
  state.signalingState = "";
  state.dcOpen = false;
  state.routeType = "";
  state.roomId = null;
  state.peerName = null;
  state.ownFingerprint = null;
  state.peerFingerprint = null;
  state.polite = false;
  state.localCandidates = [];
  state.remoteCandidates = [];
  state.localSdp = "";
  state.remoteSdp = "";
  if (!silent) log("Connection closed");
  else notify();
}

// --- Event handlers called from App.svelte SSE dispatch ---

export async function handleOffer(detail, rooms, chatStatus) {
  if (detail.room_id === roomId && pc) {
    if (isStale()) {
      // Stale connection to same room — tear down and reconnect
      log("Tearing down stale connection for re-offer");
      cleanup(true);
    } else {
      // Active connection to this room — handle as renegotiation
      _handleOfferInternal(detail);
      return;
    }
  }
  if (pc && isStale()) { log("Clearing stale connection"); cleanup(true); }
  if (pc) return; // busy with a healthy connection to another peer

  // Auto-connect: find the room to get peer info
  const room = rooms.find(r => r.id === detail.room_id);
  if (!room) return;
  roomId = room.id;
  peerName = room.name;
  ownFingerprint = chatStatus.fingerprint;
  peerFingerprint = room.fingerprint;
  state.roomId = roomId;
  state.peerName = peerName;
  state.ownFingerprint = ownFingerprint;
  state.peerFingerprint = peerFingerprint;
  state.polite = ownFingerprint < peerFingerprint;
  await createPC();
  _handleOfferInternal(detail);
}

async function _handleOfferInternal(detail) {
  log("Received offer");
  const polite = ownFingerprint < peerFingerprint;
  const offerCollision = makingOffer || pc.signalingState !== "stable";
  const ignoreOffer = !polite && offerCollision;
  if (ignoreOffer) {
    log("Ignoring colliding offer (impolite)");
    return;
  }
  try {
    await pc.setRemoteDescription({ type: "offer", sdp: detail.sdp });
    state.remoteSdp = detail.sdp;
    await pc.setLocalDescription();
    state.localSdp = pc.localDescription.sdp;
    log("Sending answer");
    await sendSignal("webrtc-answer", { sdp: pc.localDescription.sdp });
  } catch (err) {
    log(`Handle offer error: ${err.message}`);
  }
}

export function handleAnswer(detail) {
  if (!pc || detail.room_id !== roomId) return;
  log("Received answer");
  pc.setRemoteDescription({ type: "answer", sdp: detail.sdp })
    .then(() => { state.remoteSdp = detail.sdp; notify(); })
    .catch(err => log(`Handle answer error: ${err.message}`));
}

export function handleIce(detail) {
  if (!pc || detail.room_id !== roomId) return;
  const polite = ownFingerprint < peerFingerprint;
  const candidate = new RTCIceCandidate({
    candidate: detail.candidate,
    sdpMid: detail.sdpMid,
    sdpMLineIndex: detail.sdpMLineIndex,
  });
  state.remoteCandidates = [...state.remoteCandidates, `${candidate.protocol || ""} ${candidate.address || ""}:${candidate.port || ""} ${candidate.type || ""}`];
  notify();
  pc.addIceCandidate(candidate).catch(err => {
    if (!polite || pc.signalingState !== "stable") return;
    log(`ICE candidate error: ${err.message}`);
  });
}

export function handleHangup(detail) {
  if (detail.room_id !== roomId) return;
  log("Remote peer hung up");
  cleanup(false);
}

// --- P2P Record Sync ---

const SYNC_BATCH_SIZE = 50;

async function fetchAttachmentAsBase64(recordId, attId) {
  const res = await fetch(
    `/api/records/${recordId}/attachments/${attId}/download`
  );
  if (!res.ok) return null;
  const blob = await res.blob();
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onloadend = () => {
      // Strip the data:...;base64, prefix
      const b64 = reader.result.split(",")[1];
      resolve(b64);
    };
    reader.readAsDataURL(blob);
  });
}

async function handleSyncRequest(channel) {
  try {
    const res = await fetch("/api/records/?limit=10000");
    if (!res.ok) return;
    const records = await res.json();
    const forPeer = records.filter(
      (r) => r.recipients && r.recipients.includes(peerFingerprint)
    );

    // Fetch attachment metadata for each record
    for (const rec of forPeer) {
      try {
        const attRes = await fetch(`/api/records/${rec.id}/attachments/`);
        if (attRes.ok) {
          const atts = await attRes.json();
          rec.attachments = atts.map((a) => ({
            id: a.id,
            filename: a.filename,
            content_type: a.content_type,
            size: a.size,
          }));
        }
      } catch {}
    }

    // Send record metadata in batches
    for (let i = 0; i < forPeer.length; i += SYNC_BATCH_SIZE) {
      const batch = forPeer.slice(i, i + SYNC_BATCH_SIZE);
      channel.send(JSON.stringify({ type: "sync-offer", records: batch }));
    }
    if (forPeer.length === 0) {
      channel.send(JSON.stringify({ type: "sync-offer", records: [] }));
    }
    log(`Sync offer sent: ${forPeer.length} records`);

    // Send attachment data for each record
    let attCount = 0;
    for (const rec of forPeer) {
      if (!rec.attachments || rec.attachments.length === 0) continue;
      for (const att of rec.attachments) {
        const b64 = await fetchAttachmentAsBase64(rec.id, att.id);
        if (!b64) continue;
        channel.send(
          JSON.stringify({
            type: "sync-attachment",
            record_uuid: rec.uuid,
            filename: att.filename,
            content_type: att.content_type,
            data: b64,
          })
        );
        attCount++;
      }
    }
    if (attCount > 0) log(`Sent ${attCount} attachments`);
  } catch (err) {
    log(`Sync request error: ${err.message}`);
  }
}

async function handleSyncOffer(records, channel) {
  let accepted = 0;
  let skipped = 0;
  for (const rec of records) {
    try {
      // Add the sender as a recipient so updates flow back
      if (peerFingerprint) {
        const recipients = rec.recipients || [];
        if (!recipients.includes(peerFingerprint)) {
          rec.recipients = [...recipients, peerFingerprint];
        }
      }
      const res = await fetch("/api/records/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rec),
      });
      if (res.ok) {
        const result = await res.json();
        if (result.action === "created" || result.action === "updated")
          accepted++;
        else skipped++;
      }
    } catch {}
  }
  channel.send(JSON.stringify({ type: "sync-ack", accepted, skipped }));
  log(`Sync complete: ${accepted} accepted, ${skipped} skipped`);
}

async function handleSyncAttachment(msg) {
  try {
    const res = await fetch("/api/records/sync-attachment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        record_uuid: msg.record_uuid,
        filename: msg.filename,
        content_type: msg.content_type,
        data: msg.data,
      }),
    });
    if (res.ok) {
      const result = await res.json();
      if (result.action === "created") {
        log(`Attachment synced: ${msg.filename}`);
      }
    }
  } catch (err) {
    log(`Attachment sync error: ${err.message}`);
  }
}

async function handleSyncDeleteAttachment(msg) {
  try {
    const res = await fetch("/api/records/sync-delete-attachment", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        record_uuid: msg.record_uuid,
        filename: msg.filename,
      }),
    });
    if (res.ok) {
      const result = await res.json();
      if (result.action === "deleted") {
        log(`Attachment deleted via sync: ${msg.filename}`);
      }
    }
  } catch (err) {
    log(`Attachment delete sync error: ${err.message}`);
  }
}

async function handleSyncDeleteRecord(msg) {
  try {
    const res = await fetch("/api/records/sync-delete-record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record_uuid: msg.record_uuid }),
    });
    if (res.ok) {
      const result = await res.json();
      if (result.action === "deleted") {
        log(`Record deleted via sync: ${msg.record_uuid}`);
      }
    }
  } catch (err) {
    log(`Record delete sync error: ${err.message}`);
  }
}
