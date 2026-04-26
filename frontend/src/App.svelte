<script>
  import { onMount, onDestroy, tick } from "svelte";
  import Records from "./Records.svelte";
  import Settings from "./Settings.svelte";
  import About from "./About.svelte";
  import Notifications from "./Notifications.svelte";
  import DatabasePicker from "./DatabasePicker.svelte";
  import Welcome from "./Welcome.svelte";
  import Query from "./Query.svelte";
  import Scratchpad from "./Scratchpad.svelte";
  import Media from "./Media.svelte";
  import Icon from "@iconify/svelte";
  import iconBook from "@iconify-icons/twemoji/open-book";
  import iconBell from "@iconify-icons/twemoji/bell";
  import iconPlus from "@iconify-icons/twemoji/heavy-plus-sign";
  import iconCamera from "@iconify-icons/twemoji/camera";
  import { setDatabase, storageGet, storageSet, migrateStorage } from "./storage.js";
  import { applyThemeVars, applyCustomThemeVars, resolveDefaultTheme } from "./themes.js";

  const DUAL_RIGHT_PAGES = new Set(["notifications", "media"]);

  function parseHash() {
    const hash = window.location.hash.slice(1) || "/";
    if (hash === "/picker") return { page: "picker", editId: null, dualRight: null };
    if (hash === "/about") return { page: "about", editId: null, dualRight: null };
    if (hash === "/settings" || hash.startsWith("/settings/")) {
      const settingsTab = hash.split("/")[2] || null;
      return { page: "settings", editId: null, dualRight: null, settingsTab };
    }
    if (hash === "/query" || hash.startsWith("/query?")) {
      const qm = hash.indexOf("?");
      const sp = qm >= 0 ? new URLSearchParams(hash.slice(qm + 1)) : null;
      return { page: "query", editId: null, dualRight: null, querySql: sp?.get("sql") || "" };
    }
    if (hash === "/scratchpad") return { page: "scratchpad", editId: null, dualRight: null };
    if (hash === "/media") return { page: isWide() ? "dual" : "media", editId: null, dualRight: "media" };
    if (hash === "/notifications") return { page: isWide() ? "dual" : "notifications", editId: null, dualRight: "notifications" };
    if (hash === "/database" || hash === "/records") return { page: isWide() ? "dual" : "records", editId: null, dualRight: null };
    if (hash === "/add") return { page: isWide() ? "dual" : "add", editId: null, dualRight: null };
    // Dual with subpage
    const dualMatch = hash.match(/^\/dual(?:\/(\w+))?$/);
    if (dualMatch) {
      const sub = dualMatch[1] || "notifications";
      return { page: "dual", editId: null, dualRight: DUAL_RIGHT_PAGES.has(sub) ? sub : "notifications" };
    }
    const recordMatch = hash.match(/^\/records\/(\d+)$/);
    if (recordMatch) return { page: isWide() ? "dual" : "add", editId: parseInt(recordMatch[1], 10), dualRight: null };
    return { page: isWide() ? "dual" : "records", editId: null, dualRight: null };
  }

  let wideBreakpoint = 1200;
  let wide = typeof window !== "undefined" && window.innerWidth >= 1200;
  let _parsed = parseHash();
  let { page, editId } = _parsed;
  let dualRightPage = _parsed.dualRight || "notifications";
  let previousPage = "records";
  let defaultPage = "records";
  let settingsTab = _parsed.settingsTab || null;
  let settingsHighlight = null;
  let querySql = _parsed.querySql || "";
  let prefill = null;
  let formDirty = false;
  let dualShowForm = !!editId || (page === "dual" && (window.location.hash.slice(1) === "/add"));
  let recordsRef = null;
  let recordAutoCreated = false;
  let databaseRight = false;
  let mediaSearchQuery = "";
  let dualSplit = 50;
  let draggingSplit = false;

  function onDividerDown(e) {
    e.preventDefault();
    draggingSplit = true;
    const onMove = (ev) => {
      const clientX = ev.touches ? ev.touches[0].clientX : ev.clientX;
      const layout = e.target.closest(".dual-layout");
      if (!layout) return;
      const rect = layout.getBoundingClientRect();
      let pct = databaseRight
        ? 100 - ((clientX - rect.left) / rect.width) * 100
        : ((clientX - rect.left) / rect.width) * 100;
      if (pct < 10) pct = 10;
      if (pct > 90) pct = 90;
      dualSplit = pct;
    };
    const onUp = () => {
      draggingSplit = false;
      storageSet("dualSplit", String(dualSplit));
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      window.removeEventListener("touchmove", onMove);
      window.removeEventListener("touchend", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("touchmove", onMove);
    window.addEventListener("touchend", onUp);
  }

  let menuOpen = false;
  let customHeader = "";
  let appVersion = "";
  let updateAvailable = false;
  let updateChecked = false;
  let updateDev = false;
  let updateExact = false;
  let updateUrl = "";
  let updateLatest = "";
  let updateHasUpdate = false;
  let updateSkipped = false;
  let updateSupported = false;
  let appFrozen = true;
  let sqlQueryEnabled = false;
  let noShutdown = false;
  let unreadCount = 0;
  let prevUnreadCount = -1;
  let clientCount = 0;
  let disconnectNonce = "";
  let eventSource = null;
  let notifRefreshTrigger = 0;
  let sseHeartbeatTimer = null;
  const SSE_TIMEOUT_MS = 11000;
  let authRefreshTrigger = 0;
  let popupNotifications = [];
  let popupNotifEnabled = false;
  let showPopup = false;
  let activeDesktopNotif = null;
  let welcomeAcknowledged = true; // assume true until checked
  let welcomeChecked = false;
  let pickerMode = false;
  let databaseReady = false;
  let databaseOpen = false;
  let currentDatabase = "";
  let pendingDatabase = "";
  let showDatabaseSwitcher = false;
  let switcherDatabases = [];
  let authBlocked = false; // true when server returns 401
  let authLoginError = "";
  let authConfirmToken = ""; // set when ?auth_token= is in URL, awaiting user confirmation
  let authConfirmUrl = ""; // the full URL for copying
  let authConfirming = false;
  let authConfirmCopied = false;

  async function handleAuthToken() {
    const params = new URLSearchParams(window.location.search);
    const token = params.get("auth_token");
    if (!token) return false;
    // Check if the token is still valid before showing confirmation
    try {
      const res = await fetch("/api/auth/check-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (res.ok) {
        const data = await res.json();
        if (!data.valid) {
          // Token already used or expired — redirect to base URL for server 401 page
          const url = new URL(window.location.href);
          url.searchParams.delete("auth_token");
          window.location.replace(url.pathname);
          return true;
        }
      }
    } catch {}
    // Token is valid — show confirmation screen
    authConfirmUrl = window.location.href;
    authConfirmToken = token;
    // Remove token from URL bar (but keep it in state)
    const url = new URL(window.location.href);
    url.searchParams.delete("auth_token");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    return true;
  }

  async function confirmAuthLogin() {
    authConfirming = true;
    authLoginError = "";
    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: authConfirmToken }),
      });
      if (res.ok) {
        location.reload();
        return;
      } else {
        const data = await res.json().catch(() => null);
        authLoginError = data?.detail || "Login failed";
      }
    } catch (e) {
      authLoginError = "Login failed: " + e.message;
    }
    authConfirming = false;
  }

  async function checkAuthStatus() {
    try {
      const res = await fetch("/api/auth/status");
      if (res.ok) {
        const data = await res.json();
        if (data.enabled && data.configured && !data.authenticated) {
          authBlocked = true;
          return false;
        }
      }
    } catch {}
    return true;
  }

  async function checkWelcome() {
    // The welcome screen only shows once — before the first database is created.
    // We use the /api/databases/mode endpoint (auth-exempt) to check.
    try {
      const res = await fetch("/api/databases/mode");
      if (res.ok) {
        const data = await res.json();
        welcomeAcknowledged = !!data.has_databases;
      }
    } catch {}
    welcomeChecked = true;
  }

  async function handleWelcomeComplete(e) {
    welcomeAcknowledged = true;
    const database = e.detail.database;
    if (database) {
      currentDatabase = database;
      databaseOpen = true;
      databaseReady = true;
      setDatabase(database);
      applyTheme();
      await startAppServices();
      navigate(isWide() ? "dual" : "records");
    } else {
      // Skip was clicked — proceed with normal startup
      await checkDatabaseMode();
      if (databaseOpen) {
        setDatabase(currentDatabase);
        applyTheme();
        fetchWideBreakpoint();
        await startAppServices();
      } else if (pickerMode) {
        page = "picker";
      }
    }
  }

  async function checkDatabaseMode() {
    try {
      const res = await fetch("/api/databases/mode");
      if (res.ok) {
        const data = await res.json();
        pickerMode = data.picker;
        noShutdown = data.no_shutdown;
      }
    } catch {}
    try {
      const cur = await fetch("/api/databases/current");
      if (cur.ok) {
        const data = await cur.json();
        databaseOpen = data.is_open;
        currentDatabase = data.name || "";
        pendingDatabase = data.pending || "";
      }
    } catch {}
    if (!pickerMode && !databaseOpen && !pendingDatabase) {
      databaseOpen = true;
    }
    databaseReady = true;
  }

  async function startAppServices() {
    fetchVersion();
    fetchUpdateCheck();
    fetchCustomHeader();
    await fetchDefaultPage();
    fetchPopupNotifEnabled();
    await fetchDatabaseRight();
    await fetchSqlQueryEnabled();
    fetchUnreadCount();
    connectSSE();
  }

  function setShutdownState() {
    serverShutdown = true;
    stopAppServices();
    document.title = "Close this tab";
    const link = document.querySelector("link[rel~='icon']") || document.createElement("link");
    link.rel = "icon";
    link.href = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>💤</text></svg>";
    document.head.appendChild(link);
  }

  function setDisconnectedState() {
    serverDisconnected = true;
    stopAppServices();
    document.title = "Disconnected";
    startAutoReconnect();
  }

  function clearDisconnectedState() {
    serverDisconnected = false;
    reconnecting = false;
    stopAutoReconnect();
    document.title = "Guidebook";
  }

  function clearShutdownState() {
    serverShutdown = false;
    databaseClosed = false;
    shutdownPendingSince = 0;
    document.title = "Guidebook";
    const link = document.querySelector("link[rel~='icon']");
    if (link) link.href = "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>📓</text></svg>";
  }

  async function reloadIfAlive() {
    try {
      const res = await fetch("/api/databases/current");
      if (res.ok) {
        location.reload();
      } else {
        alert("Server is not available yet.");
      }
    } catch {
      alert("Server is not available yet.");
    }
  }

  async function attemptReconnect() {
    try {
      const res = await fetch("/api/databases/current");
      if (res.ok) {
        const data = await res.json();
        if (serverDisconnected) {
          clearDisconnectedState();
          if (data.is_open && data.name === currentDatabase) {
            startAppServices();
          } else {
            databaseClosed = true;
            serverShutdown = true;
            document.title = "Close this tab";
          }
        } else {
          clearShutdownState();
          startAppServices();
        }
      } else {
        if (!serverDisconnected) alert("Server is not available yet.");
      }
    } catch {
      if (!serverDisconnected) alert("Server is not available yet.");
    }
  }

  async function startAutoReconnect() {
    autoReconnectDelay = 2000;
    reconnectStartedAt = Date.now();
    // Try immediately first, then start backoff schedule
    reconnecting = true;
    await attemptReconnect();
    reconnecting = false;
    if (serverDisconnected) {
      scheduleAutoReconnect();
    }
  }

  function scheduleAutoReconnect() {
    const delaySec = Math.round(autoReconnectDelay / 1000);
    reconnectCountdown = delaySec;
    clearInterval(countdownInterval);
    if (delaySec > 1) {
      countdownInterval = setInterval(() => {
        reconnectCountdown = Math.max(0, reconnectCountdown - 1);
        if (reconnectCountdown <= 0) clearInterval(countdownInterval);
      }, 1000);
    }
    autoReconnectTimer = setTimeout(async () => {
      clearInterval(countdownInterval);
      reconnectCountdown = 0;
      reconnecting = true;
      await attemptReconnect();
      reconnecting = false;
      if (serverDisconnected) {
        if (Date.now() - reconnectStartedAt > 61000) {
          stopAutoReconnect();
          serverDisconnected = false;
          databaseClosed = true;
          serverShutdown = true;
          stopAppServices();
          document.title = "Close this tab";
          return;
        }
        autoReconnectDelay = Math.min(autoReconnectDelay * 2, 10000);
        scheduleAutoReconnect();
      }
    }, autoReconnectDelay);
  }

  function stopAutoReconnect() {
    clearTimeout(autoReconnectTimer);
    autoReconnectTimer = null;
    clearInterval(countdownInterval);
    countdownInterval = null;
    autoReconnectDelay = 1000;
    reconnectCountdown = 0;
  }

  function stopAppServices() {
    clearTimeout(sseHeartbeatTimer);
    sseHeartbeatTimer = null;
    if (eventSource) { eventSource.close(); eventSource = null; }
  }

  function handleDatabaseOpened() {
    location.reload();
  }

  async function openDatabaseSwitcher() {
    try {
      const res = await fetch("/api/databases/");
      if (res.ok) {
        const data = await res.json();
        switcherDatabases = data.filter(lb => lb.name !== currentDatabase && !lb.locked);
      }
    } catch {}
    showDatabaseSwitcher = true;
  }

  let switchingDatabase = false;

  async function switchDatabase(name) {
    showDatabaseSwitcher = false;
    switchingDatabase = true;
    try {
      await fetch("/api/databases/close", { method: "POST" });
      const res = await fetch("/api/databases/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (res.ok) {
          window.location.hash = "/";
          location.reload();
        }
    } catch {}
    switchingDatabase = false;
  }

  let serverShutdown = false;
  let serverDisconnected = false;
  let shutdownPendingSince = 0;
  let databaseClosed = false;
  let autoReconnectTimer = null;
  let autoReconnectDelay = 1000;
  let reconnecting = false;
  let reconnectCountdown = 0;
  let countdownInterval = null;
  let reconnectStartedAt = 0;

  async function confirmPendingDatabase() {
    try {
      const res = await fetch("/api/databases/confirm", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        currentDatabase = data.name;
        setDatabase(currentDatabase);
        dualSplit = parseFloat(storageGet("dualSplit")) || 50;
        applyTheme();
        pendingDatabase = "";
        databaseOpen = true;
        page = isWide() ? "dual" : "records";
        window.location.hash = "/";
        startAppServices();
      }
    } catch {}
  }

  async function declinePendingDatabase() {
    try {
      await fetch("/api/databases/decline", { method: "POST" });
    } catch {}
    setShutdownState();
  }

  async function shutdownFromMenu() {
    menuOpen = false;
    shutdownPendingSince = Date.now();
    try {
      const res = await fetch("/api/databases/shutdown", { method: "POST" });
      if (res.ok) setShutdownState();
    } catch {
      setShutdownState();
    }
  }

  async function closeDatabase() {
    menuOpen = false;
    try {
      await fetch("/api/databases/close", { method: "POST" });
    } catch {}
    location.reload();
  }

  function resetSseHeartbeat() {
    clearTimeout(sseHeartbeatTimer);
    sseHeartbeatTimer = setTimeout(() => {
      if (serverShutdown || serverDisconnected) return;
      if (shutdownPendingSince && Date.now() - shutdownPendingSince < 30000) {
        setShutdownState();
      } else {
        setDisconnectedState();
      }
    }, SSE_TIMEOUT_MS);
  }

  function connectSSE() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource("/api/events/stream");
    resetSseHeartbeat();
    eventSource.addEventListener("unread", (e) => {
      const data = JSON.parse(e.data);
      const newCount = data.count;
      if (newCount > unreadCount && prevUnreadCount >= 0) {
        if (typeof Notification !== "undefined"
            && Notification.permission === "granted"
            && storageGet("desktop_notifications_enabled") === "true") {
          if (activeDesktopNotif) activeDesktopNotif.close();
          activeDesktopNotif = new Notification("Guidebook", {
            body: `You have ${newCount} unread notification${newCount > 1 ? "s" : ""}`,
          });
        }
        if (popupNotifEnabled) {
          showPopupNotifications();
        }
      } else if (newCount < unreadCount && activeDesktopNotif) {
        activeDesktopNotif.close();
        activeDesktopNotif = null;
      }
      prevUnreadCount = unreadCount;
      unreadCount = newCount;
      notifRefreshTrigger++;
    });
    eventSource.addEventListener("notification", (e) => {
      notifRefreshTrigger++;
      authRefreshTrigger++;
    });
    eventSource.addEventListener("update-check", () => {
      fetchUpdateCheck();
    });
    eventSource.addEventListener("keepalive", () => {
      resetSseHeartbeat();
    });
    eventSource.addEventListener("shutdown", () => {
      setShutdownState();
    });
    eventSource.addEventListener("clients", (e) => {
      const data = JSON.parse(e.data);
      clientCount = data.count;
    });
    eventSource.addEventListener("disconnect", (e) => {
      const data = JSON.parse(e.data);
      if (data.nonce && data.nonce === disconnectNonce) {
        disconnectNonce = "";
        return;  // this client initiated the disconnect
      }
      if (eventSource) { eventSource.close(); eventSource = null; }
      setShutdownState();
    });
    eventSource.addEventListener("auth-revoked", () => {
      location.reload();
    });
    eventSource.addEventListener("theme-changed", () => applyTheme());
    eventSource.addEventListener("theme-preview", (e) => {
      const { key, value } = JSON.parse(e.data);
      _themeState[key] = value;
      applyThemeFromState(_themeState);
    });
    eventSource.addEventListener("database-changed", () => {
      if (switchingDatabase || !welcomeAcknowledged) return;
      // Navigate home before reloading — the new database may not support the current page
      window.location.hash = "/";
      setTimeout(() => location.reload(), 100);
    });
    eventSource.onerror = () => {
      if (serverShutdown) return;
      // EventSource auto-reconnects; nothing to do
    };
  }

  async function fetchUnreadCount() {
    try {
      const res = await fetch("/api/notifications/unread-count");
      if (res.ok) {
        const data = await res.json();
        unreadCount = data.count;
        prevUnreadCount = data.count;
      }
    } catch {}
  }

  async function showPopupNotifications() {
    try {
      const res = await fetch("/api/notifications/");
      if (res.ok) {
        const all = await res.json();
        popupNotifications = all.filter(n => !n.read);
        if (popupNotifications.length > 0) showPopup = true;
      }
    } catch {}
  }

  async function dismissPopup() {
    // Mark all shown as read
    for (const n of popupNotifications) {
      try { await fetch(`/api/notifications/${n.id}/read`, { method: "PUT" }); } catch {}
    }
    showPopup = false;
    popupNotifications = [];
    fetchUnreadCount();
  }

  function dismissPopupKeepUnread() {
    showPopup = false;
    popupNotifications = [];
  }

  function handleNotificationClick() {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().then(perm => {
        if (perm === "granted") {
          storageSet("desktop_notifications_enabled", "true");
        }
      });
    }
    navigate("notifications");
  }

  async function fetchVersion() {
    try {
      const res = await fetch("/api/version");
      if (res.ok) {
        const data = await res.json();
        appVersion = data.version || "";
        noShutdown = !!data.no_shutdown;
        appFrozen = data.frozen !== false;
      }
    } catch {}
  }

  async function fetchUpdateCheck() {
    try {
      const res = await fetch("/api/update-check");
      if (res.ok) {
        const data = await res.json();
        updateChecked = !!data.latest;
        updateDev = data.is_dev || false;
        updateExact = data.is_exact || false;
        updateLatest = data.latest || "";
        updateUrl = data.url || "";
        updateHasUpdate = data.update_available || false;
        updateSkipped = data.update_skipped || false;
      }
    } catch {}
    try {
      const res = await fetch("/api/update/platform");
      if (res.ok) {
        const data = await res.json();
        updateSupported = data.supported || false;
      }
    } catch {}
    // Only show "Update Available" banner for GitHub release binaries with a newer version (and not skipped)
    updateAvailable = updateSupported && updateHasUpdate && !updateSkipped;
  }

  async function skipUpdate() {
    try {
      const res = await fetch("/api/update-check/skip", { method: "POST" });
      if (res.ok) {
        updateSkipped = true;
        updateAvailable = false;
      }
    } catch {}
  }

  async function fetchPopupNotifEnabled() {
    try {
      const res = await fetch("/api/settings/popup_notifications_enabled");
      if (res.ok) {
        const data = await res.json();
        popupNotifEnabled = data.value === "true";
      }
    } catch {}
  }

  async function fetchDefaultPage() {
    try {
      const res = await fetch("/api/settings/default_page");
      if (res.ok) {
        const data = await res.json();
        defaultPage = data.value || "records";
      }
    } catch {}
  }

  async function fetchCustomHeader() {
    try {
      const res = await fetch("/api/settings/custom_header");
      if (res.ok) {
        const data = await res.json();
        customHeader = data.value || "";
      }
    } catch {}
  }

  async function fetchDatabaseRight() {
    try {
      const res = await fetch("/api/settings/database_right");
      if (res.ok) {
        const data = await res.json();
        databaseRight = data.value === "true";
      }
    } catch {}
  }

  async function fetchSqlQueryEnabled() {
    try {
      const res = await fetch("/api/settings/sql_query_enabled");
      if (res.ok) {
        const data = await res.json();
        sqlQueryEnabled = data.value === "true";
      }
    } catch {}
  }


  function isWide() {
    return typeof window !== "undefined" && window.innerWidth >= wideBreakpoint;
  }

  function goHome() {
    navigate(defaultPage);
  }

  async function fetchWideBreakpoint() {
    try {
      const res = await fetch("/api/settings/wide_breakpoint");
      if (res.ok) {
        const data = await res.json();
        const v = parseInt(data.value, 10);
        if (v === 0) wideBreakpoint = Infinity;
        else if (v > 0) wideBreakpoint = v;
      }
    } catch {}
    wide = isWide();
  }

  let previousHash = "";
  let navigating = false;

  function navigate(p) {
    if (p === "back") {
      if (previousHash) {
        navigating = true;
        window.location.hash = previousHash;
        previousHash = "";
        const parsed = parseHash();
        page = parsed.page;
        if (parsed.dualRight) dualRightPage = parsed.dualRight;
        editId = null;
        menuOpen = false;
        setTimeout(() => { navigating = false; }, 0);
        return;
      }
      p = previousPage;
    }
    // Block navigation away from dirty form (but allow switching dual right pane)
    if (formDirty && (page === "add" || page === "dual")) {
      const stayingOnDual = isWide() && (p === "add" || p === "records" || DUAL_RIGHT_PAGES.has(p));
      if (!stayingOnDual) {
        alert("Save or cancel your current record before navigating away.");
        return;
      }
    }
    // Redirect disabled pages to home
    if (p === "query" && !sqlQueryEnabled) p = "records";

    if (isWide() && (p === "add" || p === "records" || DUAL_RIGHT_PAGES.has(p))) {
      if (DUAL_RIGHT_PAGES.has(p)) dualRightPage = p;
      p = "dual";
    }
    if (page !== p) {
      previousPage = page;
      previousHash = window.location.hash.slice(1) || "/";
    }
    const wasPage = page;
    page = p;
    // Don't clear editId when staying on dual (switching right pane only)
    if (!(wasPage === "dual" && p === "dual")) editId = null;
    menuOpen = false;
    navigating = true;
    if (p === "dual") {
      window.location.hash = `/dual/${dualRightPage}`;
    } else {
      const paths = { records: "/records", add: "/add", query: "/query", notifications: "/notifications", media: "/media", scratchpad: "/scratchpad", settings: settingsTab ? `/settings/${settingsTab}` : "/settings", about: "/about", picker: "/picker" };
      window.location.hash = paths[p] || "/";
    }
    setTimeout(() => { navigating = false; }, 0);
  }

  async function onHashChange() {
    if (navigating) return;
    const parsed = parseHash();
    let p = parsed.page;
    // Redirect disabled pages
    if (p === "query" && !sqlQueryEnabled) p = isWide() ? "dual" : "records";
    // Don't clear editId when staying on dual (e.g. switching right pane)
    if (!(page === "dual" && p === "dual" && editId && !parsed.editId)) {
      editId = parsed.editId;
    }
    settingsTab = parsed.settingsTab || null;
    querySql = parsed.querySql || "";
    page = p;
    if (parsed.dualRight) {
      dualRightPage = parsed.dualRight;
    }
    if (databaseOpen) {
      fetchWideBreakpoint();
    }
  }

  function applySystemTheme() {
    const sysPref = resolveDefaultTheme();
    storageSet("guidebook-theme", sysPref);
    applyThemeVars(sysPref);
  }

  function applyThemeFromCache() {
    const cached = storageGet("guidebook-theme");
    const theme = cached || resolveDefaultTheme();
    applyThemeVars(theme);
  }

  let _themeState = {};

  function applyThemeFromState(s) {
    const contrast = isNaN(parseInt(s.theme_contrast)) ? 50 : parseInt(s.theme_contrast);
    const brightness = isNaN(parseInt(s.theme_brightness)) ? 50 : parseInt(s.theme_brightness);
    const hue = isNaN(parseInt(s.theme_hue)) ? 0 : parseInt(s.theme_hue);
    const saturation = isNaN(parseInt(s.theme_saturation)) ? 50 : parseInt(s.theme_saturation);
    const gradient = isNaN(parseInt(s.theme_gradient)) ? 50 : parseInt(s.theme_gradient);
    const grain = isNaN(parseInt(s.theme_grain)) ? 0 : parseInt(s.theme_grain);
    const glow = isNaN(parseInt(s.theme_glow)) ? 0 : parseInt(s.theme_glow);
    const scanlines = isNaN(parseInt(s.theme_scanlines)) ? 0 : parseInt(s.theme_scanlines);
    if (s.theme_mode === "custom" && s.custom_theme_colors) {
      try {
        const c = JSON.parse(s.custom_theme_colors);
        if (c.bg && c.text && c.accent && c.vfo) {
          applyCustomThemeVars(c.bg, c.text, c.accent, c.vfo, contrast, brightness, hue, saturation, gradient, grain, glow, scanlines);
          storageSet("guidebook-theme", "custom");
          return;
        }
      } catch {}
    }
    if (s.theme) {
      storageSet("guidebook-theme", s.theme);
      applyThemeVars(s.theme, contrast, brightness, hue, saturation, gradient, grain, glow, scanlines);
    }
  }

  async function applyTheme() {
    applyThemeFromCache();
    try {
      const res = await fetch("/api/settings/");
      if (res.ok) {
        const data = await res.json();
        _themeState = {};
        for (const s of data) _themeState[s.key] = s.value;
      }
      applyThemeFromState(_themeState);
      return;
    } catch {}
    const sysPref = resolveDefaultTheme();
    storageSet("guidebook-theme", sysPref);
    applyThemeVars(sysPref);
  }

  function onGlobalKeydown(e) {
    // Ignore if typing in an input/textarea/select/button
    const tag = e.target.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || tag === "BUTTON") return;

    if (e.key === "1") {
      e.preventDefault();
      navigate("records");
    } else if (e.key === "2") {
      e.preventDefault();
      navigate("query");
    } else if (e.key === "3") {
      e.preventDefault();
      navigate("notifications");
    } else if (e.key === "4") {
      e.preventDefault();
      navigate("settings");
    } else if (e.key === "5") {
      e.preventDefault();
      navigate("about");
    } else if (e.key === "/") {
      e.preventDefault();
      if (page !== "records" && page !== "dual") navigate("records");
      requestAnimationFrame(() => {
        const el = document.getElementById("records-search");
        if (el) el.focus();
      });
    } else if (e.key === "n" || e.key === "N") {
      e.preventDefault();
      dualShowForm = true; prefill = null; editId = null;
      navigate("add");
    } else if (e.key === "w" || e.key === "W") {
      e.preventDefault();
      if (page === "dual") {
        navigate("records");
      } else {
        navigate("dual");
      }
    } else if (e.key === "?") {
      e.preventDefault();
      navigate("about");
    } else if (e.key === "ArrowDown" && recordsRef) {
      e.preventDefault();
      recordsRef.selectNext();
    } else if (e.key === "ArrowUp" && recordsRef) {
      e.preventDefault();
      recordsRef.selectPrev();
    } else if (e.key === "Enter" && recordsRef) {
      e.preventDefault();
      recordsRef.openSelected();
    } else if (e.key === "Escape" && recordsRef) {
      recordsRef.cancelIfClean();
    } else if ((e.key === "PageDown" || e.key === "PageUp" || e.key === "Home" || e.key === "End") && recordsRef) {
      const tw = document.querySelector(".table-wrap");
      if (tw) {
        e.preventDefault();
        if (e.key === "Home") tw.scrollTo({ top: 0, behavior: "smooth" });
        else if (e.key === "End") tw.scrollTo({ top: tw.scrollHeight, behavior: "smooth" });
        else tw.scrollBy({ top: e.key === "PageDown" ? tw.clientHeight : -tw.clientHeight, behavior: "smooth" });
      }
    }
  }

  onMount(async () => {
    migrateStorage();
    fetchVersion();
    applySystemTheme();
    window.addEventListener("keydown", onGlobalKeydown);
    window.addEventListener("hashchange", onHashChange);
    window.addEventListener("resize", onResize);
    // Handle auth token from URL (login link)
    const tokenHandled = await handleAuthToken();
    if (tokenHandled) return;
    // Check if auth blocks us
    const authOk = await checkAuthStatus();
    if (!authOk) return;
    connectSSE(); // connect early to prevent auto-shutdown during welcome
    await checkWelcome();
    if (!welcomeAcknowledged) return; // Welcome screen will handle the rest
    await checkDatabaseMode();
    setDatabase(currentDatabase);
    dualSplit = parseFloat(storageGet("dualSplit")) || 50;
    if (databaseOpen) {
      applyTheme();
      fetchWideBreakpoint();
    }
    if (databaseOpen) {
      await startAppServices();
      // Navigate to default page on initial load (no specific hash)
      const initHash = window.location.hash.slice(1) || "/";
      if (initHash === "/" && defaultPage !== "records") {
        navigate(defaultPage);
      }
    } else if (pickerMode) {
      page = "picker";
    }
  });

  function onResize() {
    wide = isWide();
    if (page === "dual" && !wide) {
      if (formDirty || dualShowForm || editId) {
        // Keep dual page alive so Records component isn't destroyed;
        // the right pane is hidden via CSS when not wide
      } else {
        navigate(dualRightPage);
      }
    } else if (wide && (page === "records" || DUAL_RIGHT_PAGES.has(page))) {
      if (DUAL_RIGHT_PAGES.has(page)) dualRightPage = page;
      navigate("dual");
    }
  }

  onDestroy(() => {
    if (eventSource) eventSource.close();
    window.removeEventListener("hashchange", onHashChange);
    window.removeEventListener("resize", onResize);
    window.removeEventListener("keydown", onGlobalKeydown);
  });
</script>

<main class:picker-mode={pickerMode && !databaseOpen} class:dual-mode={page === "dual"} class:records-mode={page === "records" || page === "add"} class:query-mode={page === "query"} class:scratchpad-mode={page === "scratchpad"} class:settings-mode={page === "settings"}>
  {#if authConfirmToken}
    <div class="auth-confirm">
      <div class="auth-confirm-panel">
        <h1>Create Session</h1>
        <p class="auth-confirm-desc">You are about to lock in a long-term browser session with Guidebook. This browser will be the only one able to access the app unless additional sessions are granted.</p>
        {#if authLoginError}
          <p class="auth-confirm-error">{authLoginError}</p>
        {/if}
        <button class="auth-confirm-btn" on:click={confirmAuthLogin} disabled={authConfirming}>
          {authConfirming ? "Creating session..." : "Create Session"}
        </button>
        <div class="auth-confirm-separator"><span>or</span></div>
        <div class="auth-confirm-url-box">
          <label>Copy this one-time login URL to open in a different browser</label>
          <div class="auth-confirm-url-row">
            <input type="text" value={authConfirmUrl} readonly on:click={(e) => e.target.select()} />
            <button class="auth-confirm-copy" class:copied={authConfirmCopied} on:click={() => { navigator.clipboard.writeText(authConfirmUrl).then(() => { authConfirmCopied = true; setTimeout(() => authConfirmCopied = false, 1500); }).catch(() => {}); }}>{authConfirmCopied ? "Copied!" : "Copy"}</button>
          </div>
        </div>
      </div>
    </div>
  {:else if authBlocked}
    <div class="auth-blocked">
      <p>You need a login link from the owner to access this site.</p>
      {#if authLoginError}
        <p class="auth-blocked-error">{authLoginError}</p>
      {/if}
    </div>
  {:else if serverShutdown}
    <div class="welcome-container">
      <div class="welcome-card">
        <p>{databaseClosed ? "This database has been closed." : "Server has shut down."}</p>
        <button class="welcome-btn" on:click={reloadIfAlive}>Reconnect</button>
      </div>
    </div>
  {:else if welcomeChecked && !welcomeAcknowledged}
    <Welcome on:complete={handleWelcomeComplete} />
  {:else if pendingDatabase}
    <header>
      <div class="header-left">
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <h1 class="app-title"><span class="title-full">Guidebook</span><span class="title-short">GB</span>{#if appVersion}<span class="app-version" title={!appFrozen ? "Local build" : updateChecked && updateExact ? "Up to date" : updateChecked && updateDev ? "Development version" : !updateChecked ? "Enable update checker in the settings" : ""} on:click={() => { navigate("about"); }} style="cursor: pointer">v{appVersion}{#if updateSupported && updateChecked && updateExact}<span class="up-to-date-check">✔</span>{/if}{#if (updateChecked && updateDev) || !appFrozen}<span class="dev-version">🚧</span>{/if}{#if updateAvailable} <button class="update-link-btn" title={"v" + updateLatest + " available"} on:click|stopPropagation={() => { settingsTab = "updates"; navigate("settings"); }}>Update Available</button><button class="update-skip-btn" title="Skip this version" on:click|stopPropagation={skipUpdate}>✕</button>{/if}</span>{/if}</h1>
      </div>
      <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
      <span class="client-count" on:click={() => { settingsTab = "auth"; navigate("settings"); }} title="Connected clients">{clientCount} client{clientCount !== 1 ? "s" : ""}</span>
    </header>
    <div class="welcome-container">
      <div class="welcome-card">
        <h2>Create New Database?</h2>
        <p>The database <strong>{pendingDatabase}</strong> does not exist yet. Would you like to create it?</p>
        <div class="welcome-buttons">
          <button class="welcome-btn confirm" on:click={confirmPendingDatabase}>Yes, create it</button>
          <button class="welcome-btn decline" on:click={declinePendingDatabase}>No, shut down</button>
        </div>
      </div>
    </div>
  {:else if !databaseReady}
    <!-- waiting for database mode check -->
  {:else if pickerMode && !databaseOpen}
    <DatabasePicker on:databaseopened={handleDatabaseOpened} on:shutdown-pending={() => { shutdownPendingSince = Date.now(); }} on:shutdown={setShutdownState} showShutdown={!noShutdown} />
  {:else}
  <header>
    <div class="header-left">
      <div class="title-group">
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
        <h1 class="app-title" on:click={goHome} style="cursor: pointer"><span class="title-full">Guidebook</span><span class="title-short">GB</span></h1>
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        {#if appVersion}<span class="app-version" title={!appFrozen ? "Local build" : updateChecked && updateExact ? "Up to date" : updateChecked && updateDev ? "Development version" : !updateChecked ? "Enable update checker in the settings" : ""} on:click={() => { navigate("about"); }} style="cursor: pointer">v{appVersion}{#if updateSupported && updateChecked && updateExact}<span class="up-to-date-check">✔</span>{/if}{#if (updateChecked && updateDev) || !appFrozen}<span class="dev-version">🚧</span>{/if}{#if updateAvailable} <button class="update-link-btn" title={"v" + updateLatest + " available"} on:click|stopPropagation={() => { settingsTab = "updates"; navigate("settings"); }}>Update Available</button><button class="update-skip-btn" title="Skip this version" on:click|stopPropagation={skipUpdate}>✕</button>{/if}</span>{/if}
      </div>
      {#if customHeader}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <span class="custom-header" on:click={() => { settingsTab = "appearance"; settingsHighlight = "content"; navigate("settings"); }} style="cursor: pointer">{customHeader}{#if currentDatabase}<span class="database-name" class:database-switchable={pickerMode} title={pickerMode ? "Switch database" : "Current database: " + currentDatabase} on:click|stopPropagation={() => { if (pickerMode) openDatabaseSwitcher(); }}>{currentDatabase}</span>{/if}</span>
      {:else if currentDatabase}
        <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
        <span class="database-name" class:database-switchable={pickerMode} title={pickerMode ? "Switch database" : "Current database: " + currentDatabase} on:click|stopPropagation={() => { if (pickerMode) openDatabaseSwitcher(); }}>{currentDatabase}</span>
      {/if}
    </div>
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
    <span class="client-count" on:click={() => { settingsTab = "auth"; navigate("settings"); }} title="Connected clients">{clientCount} client{clientCount !== 1 ? "s" : ""}</span>
    <div class="hamburger-wrap">
      {#if wide}
        <button class="add-btn dual-btn" class:active-nav={dualRightPage === "media"} on:click={() => { dualRightPage = "media"; navigate("media"); }} title="Records & Media">{#if dualRightPage === "media" && !databaseRight}<Icon icon={iconBook} width={14} />{/if}<Icon icon={iconCamera} width={18} />{#if dualRightPage === "media" && databaseRight}<Icon icon={iconBook} width={14} />{/if}</button>
        <button class="add-btn dual-btn" class:active-nav={dualRightPage === "notifications"} on:click={handleNotificationClick} title="Records & Notifications">{#if dualRightPage === "notifications" && !databaseRight}<Icon icon={iconBook} width={14} />{/if}{#if unreadCount > 0}<span class="notif-badge">{unreadCount > 99 ? "99+" : unreadCount}</span>{:else}<Icon icon={iconBell} width={18} />{/if}{#if dualRightPage === "notifications" && databaseRight}<Icon icon={iconBook} width={14} />{/if}</button>
      {:else}
        <button class="add-btn" on:click={() => navigate("records")} title="Records"><Icon icon={iconBook} width={18} /></button>
        <button class="add-btn" class:active-nav={page === "media"} on:click={() => navigate("media")} title="Media"><Icon icon={iconCamera} width={18} /></button>
        <button class="add-btn" class:active-nav={page === "scratchpad"} on:click={() => navigate("scratchpad")} title="Scratchpad">💭</button>
        <button class="add-btn notification-btn" class:has-unread={unreadCount > 0} on:click={handleNotificationClick} title="Notifications">
          {#if unreadCount > 0}
            <span class="notif-badge">{unreadCount > 99 ? "99+" : unreadCount}</span>
          {:else}
            <Icon icon={iconBell} width={18} />
          {/if}
        </button>
      {/if}
      <button class="add-btn add-record-btn" on:click={() => { dualShowForm = true; prefill = null; editId = null; if (page === "dual") { /* already on dual */ } else navigate("add"); }} title="New Record"><Icon icon={iconPlus} width={18} /></button>
      <button class="hamburger" on:click={() => menuOpen = !menuOpen} aria-label="Menu">
        <span class="bar"></span>
        <span class="bar"></span>
        <span class="bar"></span>
      </button>
      {#if menuOpen}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="menu-backdrop" on:click={() => menuOpen = false}></div>
        <nav class="menu">
          <button class="menu-item" class:active={page === "records" || page === "dual"} on:click={() => navigate("records")}>Records</button>
          <button class="menu-item" class:active={page === "add"} on:click={() => navigate("add")}>New Record</button>
          <button class="menu-item" class:active={page === "media" || (page === "dual" && dualRightPage === "media")} on:click={() => navigate("media")}>Media</button>
          <button class="menu-item" class:active={page === "notifications" || (page === "dual" && dualRightPage === "notifications")} on:click={() => navigate("notifications")}>Notifications{#if unreadCount > 0} ({unreadCount}){/if}</button>
          {#if sqlQueryEnabled}<button class="menu-item" class:active={page === "query"} on:click={() => navigate("query")}>SQL Query</button>{/if}
          <button class="menu-item" class:active={page === "scratchpad"} on:click={() => navigate("scratchpad")}>Scratchpad</button>
          <button class="menu-item" class:active={page === "settings"} on:click={() => navigate("settings")}>Settings</button>
          <button class="menu-item" class:active={page === "about"} on:click={() => navigate("about")}>About</button>
          {#if pickerMode}
            <div class="menu-separator"></div>
            <button class="menu-item close-database" on:click={closeDatabase}>Close Database</button>
          {/if}
        </nav>
      {/if}
    </div>
  </header>

  {#if page === "dual"}
    <div class="dual-layout" class:dual-narrow={!wide} class:dragging={draggingSplit} class:dual-reversed={databaseRight}>
      <div class="dual-pane" style="flex: 0 0 {dualSplit}%">
        <Records bind:this={recordsRef} showForm={dualShowForm || !!prefill || !!editId} {prefill} editId={editId} autoCreated={recordAutoCreated} bind:formDirty on:dropcreated={() => { recordAutoCreated = true; }} on:editchange={e => { editId = e.detail; dualShowForm = !!e.detail; }} on:navigate={e => { recordAutoCreated = false; if (e.detail === "records" || e.detail === "back") { prefill = null; editId = null; dualShowForm = false; if (!wide) navigate(dualRightPage); } else navigate(e.detail); }} on:prefillconsumed={() => prefill = null} on:searchchange={e => { mediaSearchQuery = e.detail; }} />
      </div>
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="dual-divider" on:mousedown={onDividerDown} on:touchstart={onDividerDown}></div>
      <div class="dual-pane" style="flex: 1">
        {#if dualRightPage === "media"}
          <Media searchQuery={mediaSearchQuery} />
        {:else if dualRightPage === "notifications"}
          <Notifications refreshTrigger={notifRefreshTrigger} on:countchange={() => fetchUnreadCount()} />
        {/if}
      </div>
    </div>
  {:else}
    <div class="page-content">
    {#if page === "records"}
      <Records bind:this={recordsRef} showForm={false} on:dropcreated={e => { recordAutoCreated = true; }} on:editchange={e => { editId = e.detail; navigate("add"); window.location.hash = `/records/${e.detail}`; }} on:navigate={e => navigate(e.detail)} on:searchchange={e => { mediaSearchQuery = e.detail; }} />
    {:else if page === "add"}
      <Records bind:this={recordsRef} showForm={true} editId={editId} {prefill} autoCreated={recordAutoCreated} bind:formDirty on:editchange={e => { editId = e.detail; window.location.hash = e.detail ? `/records/${e.detail}` : "/add"; }} on:navigate={e => { recordAutoCreated = false; navigate(e.detail); }} on:prefillconsumed={() => prefill = null} on:searchchange={e => { mediaSearchQuery = e.detail; }} />
    {:else if page === "query"}
      <Query initialSql={querySql} />
    {:else if page === "media"}
      <Media searchQuery={mediaSearchQuery} />
    {:else if page === "notifications"}
      <Notifications refreshTrigger={notifRefreshTrigger} on:countchange={() => fetchUnreadCount()} />
    {:else if page === "settings"}
      <Settings databaseName={currentDatabase} pickerMode={pickerMode} initialTab={settingsTab} bind:highlightSection={settingsHighlight} {clientCount} {authRefreshTrigger} on:disconnect-others={async () => { const nonce = Math.random().toString(36).slice(2); disconnectNonce = nonce; try { await fetch("/api/events/disconnect-others", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ nonce }) }); } catch {} }} on:deleted={e => { if (e.detail.shutdown) { setShutdownState(); } else { stopAppServices(); databaseOpen = false; currentDatabase = ""; page = "picker"; applySystemTheme(); } }} on:setupcomplete={async () => { await fetchDatabaseRight(); await fetchSqlQueryEnabled(); navigate(isWide() ? "dual" : "records"); }} on:saved={async () => { fetchCustomHeader(); fetchDefaultPage(); applyTheme(); fetchPopupNotifEnabled(); await fetchDatabaseRight(); await fetchSqlQueryEnabled(); fetchShutdownMenuEnabled(); fetchUpdateCheck(); }} on:shutdown-pending={() => { shutdownPendingSince = Date.now(); }} on:shutdown={() => { setShutdownState(); }} />
    {:else if page === "scratchpad"}
      <Scratchpad />
    {:else if page === "about"}
      <About />
    {/if}
    </div>
  {/if}
  {/if}
{#if showDatabaseSwitcher}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="switcher-overlay" on:click|self={() => showDatabaseSwitcher = false}>
    <div class="switcher-panel">
      <h3>Switch Database</h3>
      {#if switcherDatabases.length > 0}
        <div class="switcher-list">
          {#each switcherDatabases as lb}
            <button class="switcher-item" on:click={() => switchDatabase(lb.name)}>{lb.name}</button>
          {/each}
        </div>
      {:else}
        <p class="switcher-empty">No other databases available</p>
      {/if}
      <div class="switcher-actions">
        <button class="switcher-cancel" on:click={() => showDatabaseSwitcher = false}>Cancel</button>
      </div>
    </div>
  </div>
{/if}
</main>

{#if serverDisconnected}
  <div class="disconnect-backdrop">
    <div class="disconnect-modal">
      <p>Server has been disconnected.</p>
      <p class="disconnect-status">{reconnecting ? "Reconnecting…" : reconnectCountdown > 0 ? `Retrying in ${reconnectCountdown}s…` : "Waiting to reconnect…"}</p>
      <button class="welcome-btn" on:click={attemptReconnect}>Reconnect Now</button>
    </div>
  </div>
{/if}

{#if showPopup}
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="popup-backdrop" on:click={dismissPopup}>
    <!-- svelte-ignore a11y-click-events-have-key-events -->
    <!-- svelte-ignore a11y-no-static-element-interactions -->
    <div class="popup-modal" on:click|stopPropagation>
      <div class="popup-header">
        <span class="popup-title">New Notifications</span>
        <button class="popup-close" on:click={dismissPopup}>✕</button>
      </div>
      <div class="popup-body">
        {#each popupNotifications as notif (notif.id)}
          <div class="popup-notif">
            <div class="popup-notif-title">{notif.title}</div>
            <div class="popup-notif-text">{notif.text}</div>
            <div class="popup-notif-time">{notif.timestamp.replace("T", " ").replace("Z", "z")}</div>
          </div>
        {/each}
      </div>
      <div class="popup-footer">
        <button class="popup-btn" on:click={dismissPopupKeepUnread}>Keep Unread</button>
        <button class="popup-btn" on:click={dismissPopup}>OK</button>
        <button class="popup-btn popup-btn-go" on:click={() => { dismissPopup(); navigate("notifications"); }}>View All</button>
      </div>
    </div>
  </div>
{/if}

<style>
  /* Default (dark) variables — overridden at runtime by themes.js */
  :global(:root) {
    --bg: #24252b;
    --bg-card: #2a2d3e;
    --bg-input: #5a5c6a;
    --bg-deep: #11111b;
    --border: #5a5c6a;
    --border-input: #6e7080;
    --text: #eaeaea;
    --text-muted: #b0b2be;
    --text-dim: #8a8c98;
    --text-dimmer: #6e7080;
    --accent: #00ff88;
    --accent-hover: #00cc6a;
    --accent-highlight: #ffcc00;
    --accent-secondary: #00ccff;
    --secondary-bg: #111218;
    --secondary-border: #555;
    --secondary-text: #00ccff;
    --accent-delete: #cc3333;
    --accent-delete-hover: #aa2222;
    --accent-error: #ff6b6b;
    --accent-text: #000000;
    --bg-gradient: var(--bg);
    --glow-shadow: none;
    --glow-shadow-sm: none;
    --glow-text-shadow: none;
    --btn-secondary: #6e7080;
    --btn-secondary-hover: #5a5c6a;
    --row-hover: #44465a;
    --row-editing: #3a5a3a;
    --bar-color: #eaeaea;
    --menu-bg: #2e303a;
    --menu-hover: #3e404a;
  }

  :global(*, *::before, *::after) {
    box-sizing: border-box;
  }

  :global(body) {
    margin: 0;
    padding: 0;
    background: var(--bg-gradient, var(--bg));
    background-attachment: fixed;
    color: var(--text);
    font-family: "Courier New", Courier, monospace;
    font-size: 14px;
    overflow-x: clip;
  }

  main {
    max-width: 100%;
    margin: 0 auto;
    padding: 1rem;
  }

  .page-content {
    max-width: 1100px;
    margin: 0 auto;
  }

  :global(main.records-mode) {
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-sizing: border-box;
  }

  :global(main.records-mode) .page-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    max-width: none;
    margin: 0;
  }

  :global(main.picker-mode) {
    padding: 0;
    height: 100vh;
    overflow: hidden;
  }

  :global(main.settings-mode) {
    height: 100vh;
    overflow: hidden;
    box-sizing: border-box;
  }

  :global(main.settings-mode) .page-content {
    max-width: none;
  }

  :global(main.query-mode) {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
    box-sizing: border-box;
  }

  :global(main.query-mode) .page-content {
    max-width: 100%;
    margin: 0;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  :global(main.scratchpad-mode) {
    display: flex;
    flex-direction: column;
    height: 100vh;
    overflow: hidden;
    box-sizing: border-box;
  }

  :global(main.scratchpad-mode) .page-content {
    max-width: 100%;
    margin: 0;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    user-select: none;
    flex-wrap: wrap;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border);
    padding-bottom: 0.5rem;
    margin-bottom: 1rem;
  }

  .header-left {
    display: flex;
    align-items: baseline;
    gap: 1rem;
    flex-wrap: wrap;
    min-width: 0;
  }

  h1 {
    margin: 0;
    color: var(--accent);
    font-size: 1.6rem;
  }

  .custom-header {
    color: var(--text-muted);
    font-size: 1rem;
    font-weight: normal;
    font-family: system-ui, sans-serif;
  }

  .app-version,
  .database-name {
    display: block;
    color: var(--text-muted);
    font-size: 0.6rem;
    font-weight: normal;
    line-height: 1;
    margin-top: 0.05rem;
  }
  .database-switchable {
    cursor: pointer;
    text-decoration: underline;
    text-decoration-style: dotted;
  }
  .database-switchable:hover {
    color: var(--accent);
  }
  .switcher-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .switcher-panel {
    background: var(--bg-card, #24252b);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 12px;
    padding: 1.5rem 2rem;
    min-width: 240px;
    max-width: 360px;
    width: 90vw;
  }
  .switcher-panel h3 {
    margin: 0 0 1rem;
    font-size: 1.1rem;
    color: var(--text);
  }
  .switcher-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    max-height: 300px;
    overflow-y: auto;
  }
  .switcher-item {
    background: var(--bg-input, transparent);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 6px;
    padding: 0.5rem 0.75rem;
    color: var(--text);
    font-size: 0.9rem;
    cursor: pointer;
    text-align: left;
  }
  .switcher-item:hover {
    background: var(--menu-hover, #333);
    border-color: var(--accent);
  }
  .switcher-empty {
    color: var(--text-dim);
    font-size: 0.85rem;
  }
  .switcher-actions {
    margin-top: 1rem;
    display: flex;
    justify-content: flex-end;
  }
  .switcher-cancel {
    background: none;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.4rem 1rem;
    color: var(--text-dim);
    cursor: pointer;
    font-size: 0.85rem;
  }
  .switcher-cancel:hover {
    color: var(--text);
    border-color: var(--text-dim);
  }

  .up-to-date-check {
    margin-left: 0.2rem;
    opacity: 0.7;
    font-size: 0.5rem;
  }

  .dev-version {
    margin-left: 0.2rem;
    font-size: 0.5rem;
  }

  .update-link-btn {
    color: #2ecc40;
    background: none;
    border: none;
    font-weight: bold;
    margin-left: 0.3rem;
    cursor: pointer;
    font-size: inherit;
    font-family: inherit;
    padding: 0;
  }
  .update-link-btn:hover {
    text-decoration: underline;
  }
  .update-skip-btn {
    color: var(--text-muted);
    background: none;
    border: none;
    cursor: pointer;
    font-size: 0.75em;
    padding: 0 0 0 0.3rem;
    opacity: 0.6;
  }
  .update-skip-btn:hover {
    opacity: 1;
  }

  .hamburger-wrap {
    position: relative;
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .client-count {
    font-size: 0.75rem;
    color: var(--text-dim);
    cursor: pointer;
    white-space: nowrap;
  }
  .client-count:hover {
    color: var(--text);
  }

  .add-btn {
    background: color-mix(in srgb, var(--accent) 15%, var(--bg-card));
    box-shadow: var(--glow-shadow-sm);
    color: #fff;
    border: none;
    font-size: 1.2rem;
    font-weight: bold;
    width: 28px;
    height: 28px;
    line-height: 1;
    border-radius: 4px;
    cursor: pointer;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .add-btn.dual-btn {
    width: auto;
    padding: 0 0.4rem;
    font-size: 0.9rem;
  }
  .add-btn.dual-btn.active-nav {
    border-bottom: 2px solid var(--border);
  }

  .add-btn:hover {
    background: color-mix(in srgb, var(--accent) 30%, var(--bg-card));
  }

  .add-record-btn :global(svg path) {
    fill: var(--accent);
  }

  .notification-btn {
    font-size: 0.9rem;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .notification-btn.has-unread {
    background: #cc8800;
    border: 2px solid #ffcc00;
  }

  .notification-btn.has-unread:hover {
    background: #b07700;
  }

  .notif-badge {
    font-size: 0.75rem;
    font-weight: bold;
  }

  .hamburger {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.3rem;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .hamburger:hover {
    background: none;
  }

  .bar {
    display: block;
    width: 22px;
    height: 2px;
    background: var(--bar-color);
    border-radius: 1px;
  }

  .menu-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 10001;
  }

  .menu {
    position: absolute;
    top: 100%;
    right: 0;
    background: var(--menu-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    min-width: 150px;
    z-index: 10002;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    max-height: calc(100vh - 4rem);
    overflow-y: auto;
  }

  @media (max-height: 600px) {
    .menu {
      display: grid;
      grid-template-columns: 1fr 1fr;
      min-width: 280px;
    }
    .menu-separator {
      grid-column: 1 / -1;
    }
  }

  .menu-item {
    background: none;
    border: none;
    color: var(--text);
    padding: 0.6rem 1rem;
    font-family: inherit;
    font-size: 0.9rem;
    font-weight: normal;
    text-align: left;
    cursor: pointer;
    border-radius: 0;
  }

  .menu-item:hover {
    background: var(--menu-hover);
  }

  .menu-item.active {
    color: var(--accent);
    font-weight: bold;
    border: 1px solid gold;
    border-radius: 3px;
  }

  .menu-separator {
    border-top: 1px solid var(--border);
    margin: 0.25rem 0;
  }

  .menu-item.close-database {
    color: var(--text-muted);
  }

  .menu-item.menu-shutdown {
    color: var(--danger, #e74c3c);
  }

  .auth-confirm {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: var(--bg, #1a1b1e);
  }
  .auth-confirm-panel {
    background: var(--bg-card, #24252b);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 12px;
    padding: 2rem 2.5rem;
    max-width: 500px;
    width: 90vw;
  }
  .auth-confirm-panel h1 {
    margin: 0 0 0.5rem;
    font-size: 1.5rem;
    color: var(--text, #eaeaea);
  }
  .auth-confirm-desc {
    font-size: 0.85rem;
    color: var(--text-dim, #888);
    line-height: 1.5;
    margin: 0 0 1.5rem;
  }
  .auth-confirm-separator {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin: 1.5rem 0;
    color: var(--text-dim, #888);
    font-size: 0.8rem;
  }
  .auth-confirm-separator::before,
  .auth-confirm-separator::after {
    content: "";
    flex: 1;
    border-top: 1px solid var(--border, #3a3b3f);
  }
  .auth-confirm-url-box {
    margin-bottom: 0;
  }
  .auth-confirm-url-box label {
    display: block;
    font-size: 0.75rem;
    color: var(--text-dim, #888);
    margin-bottom: 0.35rem;
  }
  .auth-confirm-url-row {
    display: flex;
    gap: 0.5rem;
  }
  .auth-confirm-url-row input {
    flex: 1;
    padding: 0.4rem 0.6rem;
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 4px;
    background: var(--bg-input, transparent);
    color: var(--text, #eaeaea);
    font-size: 0.8rem;
    font-family: monospace;
  }
  .auth-confirm-copy {
    padding: 0.4rem 0.8rem;
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 4px;
    background: var(--bg-input, #2a2b30);
    color: var(--text, #eaeaea);
    font-size: 0.8rem;
    cursor: pointer;
    white-space: nowrap;
  }
  .auth-confirm-copy:hover {
    background: var(--border, #3a3b3f);
  }
  .auth-confirm-copy.copied {
    background: var(--accent, #00ff88);
    color: var(--accent-text, #000);
    border-color: var(--accent, #00ff88);
  }
  .auth-confirm-error {
    color: #ff4444;
    font-size: 0.8rem;
    margin: 0 0 1rem;
  }
  .auth-confirm-btn {
    width: 100%;
    padding: 0.75rem 1.5rem;
    background: var(--accent, #00ff88);
    color: var(--accent-text, #000);
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
  }
  .auth-confirm-btn:hover:not(:disabled) {
    opacity: 0.9;
  }
  .auth-confirm-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .auth-blocked {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 100vh;
    background: #111;
    color: #999;
    font-family: sans-serif;
    font-size: 0.95rem;
  }
  .auth-blocked-error {
    color: #ff4444;
    font-size: 0.85rem;
  }
  .welcome-container {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: calc(100vh - 60px);
    padding: 1rem;
  }

  .welcome-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 2rem;
    width: 100%;
    max-width: 480px;
    text-align: center;
  }

  .welcome-card h2 {
    margin: 0 0 1rem;
    color: var(--accent);
    font-size: 1.4rem;
  }

  .welcome-card p {
    color: var(--text);
    margin: 0 0 1.5rem;
    line-height: 1.5;
  }

  .welcome-buttons {
    display: flex;
    gap: 1rem;
    justify-content: center;
  }

  .welcome-btn {
    padding: 0.6rem 1.5rem;
    border: none;
    border-radius: 6px;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
  }

  .welcome-btn.confirm {
    background: var(--accent);
    color: #111;
  }

  .welcome-btn.confirm:hover {
    background: var(--accent-hover);
  }

  .welcome-btn.decline {
    background: var(--bg-input);
    color: var(--text);
    border: 1px solid var(--border);
  }

  .welcome-btn.decline:hover {
    background: var(--border);
  }

  .title-short {
    display: none;
  }

  main.dual-mode {
    max-width: 100%;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    padding-bottom: 0;
  }

  .dual-layout {
    display: flex;
    gap: 0;
    flex: 1;
    min-height: 0;
  }

  .dual-pane {
    flex: 1;
    min-width: 0;
    overflow-y: auto;
    padding: 0 0.75rem;
    display: flex;
    flex-direction: column;
  }
  .dual-reversed {
    flex-direction: row-reverse;
  }
  .dual-divider {
    width: 5px;
    cursor: col-resize;
    background: var(--border);
    flex-shrink: 0;
    transition: background 0.15s;
  }
  .dual-divider:hover, .dual-layout.dragging .dual-divider {
    background: var(--accent);
  }
  .dual-layout.dragging {
    user-select: none;
    cursor: col-resize;
  }
  .dual-narrow .dual-divider {
    display: none;
  }
  .dual-narrow .dual-pane:last-child {
    display: none;
  }

  @media (max-width: 600px) {
    .title-full {
      display: none;
    }
    .title-short {
      display: inline;
    }
    main {
      padding: 0.5rem;
    }
    header {
      gap: 0.25rem;
    }
    .header-left {
      gap: 0.5rem;
    }
  }

  .disconnect-backdrop {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.6);
    z-index: 30000;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .disconnect-modal {
    background: var(--bg-card);
    border: 1px solid var(--accent);
    border-radius: 6px;
    padding: 1.5rem 2rem;
    text-align: center;
    max-width: 360px;
    width: 90%;
  }

  .disconnect-modal p {
    margin: 0 0 0.5rem;
    color: var(--fg);
  }

  .disconnect-status {
    font-size: 0.85rem;
    color: var(--fg-dim);
    margin-bottom: 1rem !important;
  }

  .popup-backdrop {
    position: fixed;
    top: 0; left: 0; right: 0; bottom: 0;
    background: rgba(0,0,0,0.6);
    z-index: 20000;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .popup-modal {
    background: var(--bg-card);
    border: 1px solid var(--accent);
    border-radius: 6px;
    width: 90%;
    max-width: 480px;
    max-height: 80vh;
    display: flex;
    flex-direction: column;
  }

  .popup-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.6rem 0.8rem;
    border-bottom: 1px solid var(--border);
  }

  .popup-title {
    font-weight: bold;
    font-size: 0.95rem;
    color: var(--accent);
  }

  .popup-close {
    background: none;
    border: none;
    color: var(--text-muted);
    font-size: 1rem;
    cursor: pointer;
    padding: 0.2rem;
  }

  .popup-close:hover { color: var(--text); }

  .popup-body {
    padding: 0.6rem 0.8rem;
    overflow-y: auto;
    flex: 1;
  }

  .popup-notif {
    padding: 0.4rem 0;
    border-bottom: 1px solid var(--border);
  }

  .popup-notif:last-child { border-bottom: none; }

  .popup-notif-title {
    font-weight: bold;
    font-size: 0.85rem;
    color: var(--text);
  }

  .popup-notif-text {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin-top: 0.15rem;
  }

  .popup-notif-time {
    font-size: 0.7rem;
    color: var(--text-dim);
    margin-top: 0.1rem;
  }

  .popup-footer {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    padding: 0.6rem 0.8rem;
    border-top: 1px solid var(--border);
  }

  .popup-btn {
    background: var(--btn-secondary, #3e404a);
    color: var(--text);
    border: none;
    padding: 0.35rem 0.8rem;
    font-family: inherit;
    font-size: 0.8rem;
    border-radius: 3px;
    cursor: pointer;
  }

  .popup-btn:hover { background: var(--btn-secondary-hover, #4e505a); }

  .popup-btn-go {
    background: var(--accent);
    color: var(--accent-text);
    font-weight: bold;
  }

  .popup-btn-go:hover { background: var(--accent-hover); }
</style>
