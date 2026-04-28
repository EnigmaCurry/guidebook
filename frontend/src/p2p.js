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

let state = {
  connectionState: "idle",
  iceState: "",
  signalingState: "",
  dcOpen: false,
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
      }
    } catch {}
  };
}

function createPC() {
  if (pc) return;
  log("Creating RTCPeerConnection");
  state.connectionState = "connecting";
  notify();

  pc = new RTCPeerConnection({
    iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
  });

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
  };

  pc.onconnectionstatechange = () => {
    state.connectionState = pc.connectionState;
    log(`Connection: ${state.connectionState}`);
    if (state.connectionState === "failed" || state.connectionState === "closed") {
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

/** Start a P2P connection to a peer (called from UI). */
export function connect(rid, name, ownFp, peerFp) {
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
  createPC();
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

export function handleOffer(detail, rooms, chatStatus) {
  if (detail.room_id === roomId && pc) {
    // Already connected to this room — handle normally
    _handleOfferInternal(detail);
    return;
  }
  if (pc) return; // busy with another connection

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
  createPC();
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
