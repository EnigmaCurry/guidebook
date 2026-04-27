<script>
  import { onMount, onDestroy, createEventDispatcher, tick } from "svelte";
  import { storageGet, storageSet } from "./storage.js";
  import { THEMES, THEME_NAMES, applyThemeVars, applyCustomThemeVars, generateCustomTheme } from "./themes.js";
  import "vanilla-colorful/hex-color-picker.js";

  export let databaseName = "";
  export const pickerMode = false;
  export const needsSetup = false;
  export let initialTab = null;
  export let highlightSection = null;
  export let clientCount = 0;
  export let authRefreshTrigger = 0;

  const dispatch = createEventDispatcher();

  let update_check_enabled = true;
  let updateCheckResult = null;
  let updateChecking = false;
  let updateSupported = false;
  let updateNotWritable = false;
  let updateApplying = false;
  let updateApplyError = "";
  let updateCustomRepo = false;
  let updateGithubRepo = "";
  let updateBuildRepo = "";
  let updateBuildSha = "";
  let updateOfficialBuild = false;
  let database_right = false;
  let wide_breakpoint = "1200";
  let wide_mode_enabled = true;
  let theme = "dark";
  let themeContrast = 50;
  let themeBrightness = 50;
  let themeHue = 0;
  let themeSaturation = 50;
  let themeGradient = 50;
  let themeGrain = 0;
  let themeGlow = 0;
  let themeScanlines = 0;
  let themeMode = "preset"; // "preset" or "custom"
  let customBg = "#24252b";
  let customText = "#eaeaea";
  let customAccent = "#00ff88";
  let customSecondary = "#00ccff";
  let custom_header = "";

  let sql_query_enabled = false;
  let default_page = "log";

  // Global settings state
  let globalSettingsLoaded = false;

  // Track which per-database settings are from global defaults
  let settingSources = {};
  // Store global default values for use as placeholders
  let globalPlaceholders = {};

  const validTabs = ["features", "appearance", "updates", "data", "auth", "global"];
  let activeTab = (initialTab && validTabs.includes(initialTab)) ? initialTab : "features";
  let settingsLoaded = false;

  $: if (initialTab && validTabs.includes(initialTab)) activeTab = initialTab;
  $: if (highlightSection && settingsLoaded) {
    tick().then(() => {
      const el = document.querySelector(`[data-section="${highlightSection}"]`);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "nearest" });
        el.classList.add("highlight-flash");
        el.addEventListener("animationend", () => el.classList.remove("highlight-flash"), { once: true });
      }
      highlightSection = null;
    });
  }

  let updateCheckLoaded = false;
  $: if (settingsLoaded && !updateCheckLoaded) {
    updateCheckLoaded = true;
    if (update_check_enabled) loadUpdateCheck();
  }

  // Desktop notifications
  let desktopNotifPermission = typeof Notification !== "undefined" ? Notification.permission : "denied";
  let desktopNotifEnabled = storageGet("desktop_notifications_enabled") === "true";
  let popupNotifEnabled = false;
  let testPending = false;

  // Shutdown
  let noShutdown = false;

  // Backup
  let backupMessage = "";
  let backupMessageType = "";
  let backingUp = false;
  let dbInfo = null;
  let backupStatus = null;
  let autoBackupEnabled = true;
  let autoBackupHours = 24;
  let autoBackupMax = 10;
  let backupSaveTimer = null;
  let backupSettingsReady = false;

  // Authentication
  let authEnabled = false;
  let authAuthenticated = false;
  let authSlots = 1;
  let authAllowTransfer = false;
  let authSessions = [];
  let authLoading = true;
  let authTokenUrl = "";
  let authTransferUrl = "";
  let authError = "";
  let authGenerating = false;
  let authTransferring = false;

  async function loadAuthStatus() {
    try {
      const res = await fetch("/api/auth/status");
      if (res.ok) {
        const data = await res.json();
        authEnabled = data.enabled;
        authAuthenticated = data.authenticated;
        authSlots = data.slots;
        authAllowTransfer = data.allow_transfer;
      }
    } catch {}
    authLoading = false;
  }

  async function loadAuthSessions() {
    try {
      const res = await fetch("/api/auth/sessions");
      if (res.ok) {
        authSessions = await res.json();
      }
    } catch {}
  }

  let authShowLinkForm = false;
  let authLinkLabelInput = "";

  function openLoginLinkForm() {
    authShowLinkForm = true;
    authLinkLabelInput = `session-${authSessions.filter(s => !s.is_transfer).length + 1}`;
    authTokenUrl = "";
  }

  async function generateLoginToken() {
    authError = "";
    authGenerating = true;
    authTokenUrl = "";
    try {
      const res = await fetch("/api/auth/generate-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: authLinkLabelInput }),
      });
      if (res.ok) {
        const data = await res.json();
        authTokenUrl = data.login_url;
      } else {
        const data = await res.json().catch(() => null);
        authError = data?.detail || "Failed to generate token";
      }
    } catch (e) {
      authError = e.message;
    }
    authGenerating = false;
    await loadAuthSessions();
  }

  async function generateTransferToken() {
    authError = "";
    authTransferring = true;
    authTransferUrl = "";
    try {
      const res = await fetch("/api/auth/transfer", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        authTransferUrl = data.login_url;
      } else {
        const data = await res.json().catch(() => null);
        authError = data?.detail || "Failed to generate transfer token";
      }
    } catch (e) {
      authError = e.message;
    }
    authTransferring = false;
    await loadAuthSessions();
  }

  async function deleteSession(id) {
    authError = "";
    try {
      const res = await fetch(`/api/auth/sessions/${id}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        authError = data?.detail || "Failed to delete session";
      }
    } catch (e) {
      authError = e.message;
    }
    await loadAuthSessions();
    await loadAuthStatus();
  }

  async function logoutSession() {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {}
    location.reload();
  }

  async function mtlsLogout() {
    mtlsError = "";
    try {
      const res = await fetch("/api/auth/mtls/logout", { method: "POST" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        mtlsError = data?.detail || "Failed to revoke certificate";
        return;
      }
    } catch (e) {
      mtlsError = e.message;
      return;
    }
    location.reload();
  }

  let copiedField = null;
  async function copyToClipboard(text, field) {
    try {
      await navigator.clipboard.writeText(text);
      copiedField = field;
      setTimeout(() => { if (copiedField === field) copiedField = null; }, 1500);
    } catch {}
  }

  function formatAuthTime(epoch) {
    if (!epoch) return "never";
    const d = new Date(epoch * 1000);
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff >= 0) {
      if (diff < 60) return "just now";
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      return `${Math.floor(diff / 86400)}d ago`;
    }
    // Future date
    const fdiff = -diff;
    if (fdiff < 3600) return `in ${Math.floor(fdiff / 60)}m`;
    if (fdiff < 86400) return `in ${Math.floor(fdiff / 3600)}h`;
    return `in ${Math.floor(fdiff / 86400)}d`;
  }

  function shortUserAgent(ua) {
    if (!ua) return "";
    // Extract browser name from user agent
    if (ua.includes("Firefox/")) return "Firefox";
    if (ua.includes("Edg/")) return "Edge";
    if (ua.includes("OPR/") || ua.includes("Opera")) return "Opera";
    if (ua.includes("Chrome/") && ua.includes("Safari/")) return "Chrome";
    if (ua.includes("Safari/") && !ua.includes("Chrome/")) return "Safari";
    if (ua.includes("curl/")) return "curl";
    // Fallback: first 30 chars
    return ua.length > 30 ? ua.slice(0, 30) + "..." : ua;
  }

  $: if (authRefreshTrigger) { loadAuthSessions(); loadAuthStatus(); loadMtlsStatus(); authTokenUrl = ""; authTransferUrl = ""; authShowLinkForm = false; }
  $: activeCertCount = mtlsCerts.filter(c => !c.revoked_at).length;
  $: authAvailableSlots = authSlots === 0 ? Infinity : Math.max(0, authSlots - authSessions.filter(s => !s.is_transfer).length - activeCertCount);
  $: mtlsCurrentCert = mtlsCerts.find(c => c.is_current && !c.revoked_at);
  $: connectedCerts = mtlsCerts.filter(c => !c.revoked_at && c.is_connected);
  $: inactiveCerts = mtlsCerts.filter(c => !c.revoked_at && !c.is_connected);
  $: connectedSessions = authSessions.filter(s => s.is_connected);
  $: inactiveSessions = authSessions.filter(s => !s.is_connected);

  // mTLS
  let mtlsMode = "disabled";
  let mtlsTlsEnabled = true;
  let mtlsProxyMode = false;
  let mtlsCaInitialized = false;
  let mtlsCaFingerprint = null;
  let mtlsCerts = [];
  let mtlsGenerating = false;
  let mtlsError = "";
  let mtlsShowCertModal = false;
  let mtlsCertGenerated = false;
  let mtlsCertDownloadToken = "";
  let mtlsCertPassword = "";
  let mtlsCertFingerprint = "";
  let mtlsCertLabel = "";
  let mtlsCertLabelInput = "";
  let mtlsActivating = false;

  async function loadMtlsStatus() {
    try {
      const res = await fetch("/api/auth/mtls/status");
      if (res.ok) {
        const data = await res.json();
        mtlsMode = data.mode;
        mtlsTlsEnabled = data.tls_enabled;
        mtlsProxyMode = data.proxy_mode;
        mtlsCaInitialized = data.ca_initialized;
        mtlsCaFingerprint = data.ca_fingerprint;
        mtlsCerts = data.certs;
      }
    } catch {}
  }

  function openCertModal() {
    mtlsCertGenerated = false;
    mtlsCertLabelInput = `client-${mtlsCerts.filter(c => !c.revoked_at).length + 1}`;
    mtlsCertDownloadToken = "";
    mtlsCertPassword = "";
    mtlsCertFingerprint = "";
    mtlsCertLabel = "";
    mtlsShowCertModal = true;
  }

  async function generateClientCert() {
    mtlsError = "";
    mtlsGenerating = true;
    try {
      const res = await fetch("/api/auth/mtls/generate-cert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: mtlsCertLabelInput }),
      });
      if (res.ok) {
        const data = await res.json();
        mtlsCertDownloadToken = data.download_token;
        mtlsCertPassword = data.password;
        mtlsCertFingerprint = data.fingerprint;
        mtlsCertLabel = data.label;
        mtlsCertGenerated = true;
      } else {
        const data = await res.json().catch(() => null);
        mtlsError = data?.detail || "Failed to generate certificate";
      }
    } catch (e) {
      mtlsError = e.message;
    }
    mtlsGenerating = false;
    await loadMtlsStatus();
  }

  function dismissCertModal() {
    mtlsShowCertModal = false;
    mtlsCertGenerated = false;
    mtlsCertDownloadToken = "";
    mtlsCertPassword = "";
    mtlsCertFingerprint = "";
    mtlsCertLabel = "";
    mtlsCertLabelInput = "";
  }

  function downloadCert() {
    if (mtlsCertDownloadToken) {
      window.open(`/api/auth/mtls/download/${mtlsCertDownloadToken}`, "_blank");
    }
  }

  async function revokeClientCert(certId) {
    mtlsError = "";
    try {
      const res = await fetch(`/api/auth/mtls/certs/${certId}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        mtlsError = data?.detail || "Failed to revoke certificate";
      }
    } catch (e) {
      mtlsError = e.message;
    }
    await loadMtlsStatus();
  }

  async function activateMtls(mode) {
    mtlsError = "";
    mtlsActivating = true;
    try {
      const res = await fetch("/api/auth/mtls/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (res.ok) {
        await loadMtlsStatus();
        await loadAuthStatus();
      } else {
        const data = await res.json().catch(() => null);
        mtlsError = data?.detail || "Failed to activate mTLS";
      }
    } catch (e) {
      mtlsError = e.message;
    }
    mtlsActivating = false;
  }

  async function loadDbInfo() {
    try {
      const res = await fetch("/api/settings/backup/db-info");
      if (res.ok) dbInfo = await res.json();
    } catch { /* ignore */ }
  }

  async function loadBackupStatus() {
    try {
      const res = await fetch("/api/settings/backup/status");
      if (res.ok) {
        backupStatus = await res.json();
        autoBackupEnabled = backupStatus.auto_enabled;
        autoBackupHours = backupStatus.interval_hours;
        autoBackupMax = backupStatus.max_backups;
        await tick();
        backupSettingsReady = true;
      }
    } catch { /* ignore */ }
  }

  function formatSize(bytes) {
    if (!bytes) return "0 B";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function timeAgo(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    const now = new Date();
    const mins = Math.floor((now - d) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    const days = Math.floor(hrs / 24);
    return `${days}d ago`;
  }

  function formatDue(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    if (d <= now) return "now";
    const mins = Math.floor((d - now) / 60000);
    if (mins < 60) return `in ${mins}m`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `in ${hrs}h`;
    const days = Math.floor(hrs / 24);
    return `in ${days}d`;
  }

  async function saveAutoBackupSettings() {
    if (!backupSettingsReady) return;  // not yet loaded -- skip spurious saves
    clearTimeout(backupSaveTimer);
    backupSaveTimer = setTimeout(async () => {
      try {
        await Promise.all([
          fetch("/api/settings/auto_backup_enabled", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: autoBackupEnabled ? "true" : "false" }),
          }),
          fetch("/api/settings/auto_backup_hours", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: String(autoBackupHours) }),
          }),
          fetch("/api/settings/auto_backup_max", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: String(autoBackupMax) }),
          }),
        ]);
        loadBackupStatus();
      } catch { /* ignore */ }
    }, 300);
  }

  async function performBackup() {
    backingUp = true;
    backupMessage = "";
    try {
      const res = await fetch("/api/settings/backup", { method: "POST" });
      if (res.ok) {
        const data = await res.json();
        const sizeKB = (data.size / 1024).toFixed(1);
        backupMessage = `Saved to ${data.path} (${sizeKB} KB)`;
        backupMessageType = "success";
      } else {
        const data = await res.json().catch(() => null);
        backupMessage = data?.detail || "Backup failed";
        backupMessageType = "error";
      }
    } catch (e) {
      backupMessage = `Backup failed: ${e.message}`;
      backupMessageType = "error";
    }
    backingUp = false;
  }

  // Danger zone
  let dangerConfirmName = "";
  let entryCount = 0;
  let deleteError = "";
  let deleting = false;
  let clearing = false;
  let clearError = "";

  async function clearAllEntries() {
    if (dangerConfirmName !== databaseName) {
      clearError = "Name does not match";
      return;
    }
    let count = "all";
    try {
      const res = await fetch("/api/records/");
      if (res.ok) { const data = await res.json(); count = data.length; }
    } catch {}
    if (!confirm(`Are you sure you want to delete ${count} entries from "${databaseName}"? This cannot be undone.`)) {
      return;
    }
    clearError = "";
    clearing = true;
    try {
      const res = await fetch("/api/records/all", { method: "DELETE" });
      if (res.ok) {
        const data = await res.json();
        clearError = `Deleted ${data.deleted} entries.`;
      } else {
        const data = await res.json().catch(() => null);
        clearError = data?.detail || "Failed to clear entries";
      }
    } catch {
      clearError = "Failed to clear entries";
    }
    clearing = false;
  }

  async function deleteDatabase() {
    if (dangerConfirmName !== databaseName) {
      deleteError = "Name does not match";
      return;
    }
    if (!confirm(`Are you sure you want to permanently delete "${databaseName}"? This cannot be undone.`)) {
      return;
    }
    deleteError = "";
    deleting = true;
    try {
      const res = await fetch("/api/databases/delete", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: databaseName }),
      });
      if (res.ok) {
        const data = await res.json();
        dispatch("deleted", { shutdown: data.shutdown });
      } else {
        const data = await res.json();
        deleteError = data.detail || "Failed to delete database";
      }
    } catch {
      deleteError = "Failed to delete database";
    }
    deleting = false;
  }

  function disconnectOthers() {
    const others = clientCount - 1;
    if (!confirm(`Disconnect ${others} other client${others !== 1 ? "s" : ""}? It may take up to 30s to process this request.`)) return;
    dispatch("disconnect-others");
  }

  async function shutdownServer() {
    if (!confirm("Are you sure you want to shut down the Guidebook server?")) return;
    dispatch("shutdown-pending");
    try {
      const res = await fetch("/api/databases/shutdown", { method: "POST" });
      if (res.ok) {
        dispatch("shutdown");
        dispatch("deleted", { shutdown: true });
      }
    } catch {
      dispatch("shutdown");
      dispatch("deleted", { shutdown: true });
    }
  }

  async function enableDesktopNotifications() {
    if (typeof Notification === "undefined") return;
    const perm = await Notification.requestPermission();
    desktopNotifPermission = perm;
    if (perm === "granted") {
      desktopNotifEnabled = true;
      storageSet("desktop_notifications_enabled", "true");
    }
  }

  function disableDesktopNotifications() {
    desktopNotifEnabled = false;
    storageSet("desktop_notifications_enabled", "false");
  }

  async function sendTestNotification() {
    // Ensure permission is granted before testing
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      const perm = await Notification.requestPermission();
      desktopNotifPermission = perm;
      if (perm === "granted") {
        desktopNotifEnabled = true;
        storageSet("desktop_notifications_enabled", "true");
      }
    }
    testPending = true;
    try {
      await fetch("/api/notifications/test", { method: "POST" });
    } catch {}
    setTimeout(() => { testPending = false; }, 5000);
  }

  async function onThemeChange() {
    applyThemeVars(theme, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
    storageSet("guidebook-theme", theme);
    await saveSetting("theme", theme);
    await saveSetting("theme_mode", "preset");
    dispatch("saved");
  }


  function applyCurrentTheme() {
    if (themeMode === "preset") {
      applyThemeVars(theme, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
    } else {
      applyCustomThemeVars(customBg, customText, customAccent, customSecondary, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
    }
  }

  function broadcastThemePreview(key, value) {
    fetch(`/api/settings/theme-preview?key=${encodeURIComponent(key)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    }).catch(() => {});
  }

  function onContrastInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_contrast", String(themeContrast));
  }
  async function onContrastCommit() {
    applyCurrentTheme();
    await saveSetting("theme_contrast", String(themeContrast));
    dispatch("saved");
  }

  function onBrightnessInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_brightness", String(themeBrightness));
  }
  async function onBrightnessCommit() {
    applyCurrentTheme();
    await saveSetting("theme_brightness", String(themeBrightness));
    dispatch("saved");
  }

  function onHueInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_hue", String(themeHue));
  }
  async function onHueCommit() {
    applyCurrentTheme();
    await saveSetting("theme_hue", String(themeHue));
    dispatch("saved");
  }

  function onSaturationInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_saturation", String(themeSaturation));
  }
  async function onSaturationCommit() {
    applyCurrentTheme();
    await saveSetting("theme_saturation", String(themeSaturation));
    dispatch("saved");
  }

  function onGradientInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_gradient", String(themeGradient));
  }
  async function onGradientCommit() {
    applyCurrentTheme();
    await saveSetting("theme_gradient", String(themeGradient));
    dispatch("saved");
  }

  function onGrainInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_grain", String(themeGrain));
  }
  async function onGrainCommit() {
    applyCurrentTheme();
    await saveSetting("theme_grain", String(themeGrain));
    dispatch("saved");
  }

  function onGlowInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_glow", String(themeGlow));
  }
  async function onGlowCommit() {
    applyCurrentTheme();
    await saveSetting("theme_glow", String(themeGlow));
    dispatch("saved");
  }

  function onScanlinesInput() {
    applyCurrentTheme();
    broadcastThemePreview("theme_scanlines", String(themeScanlines));
  }
  async function onScanlinesCommit() {
    applyCurrentTheme();
    await saveSetting("theme_scanlines", String(themeScanlines));
    dispatch("saved");
  }

  async function resetSliders() {
    themeContrast = 50;
    themeBrightness = 50;
    themeHue = 0;
    themeSaturation = 50;
    themeGradient = 50;
    themeGrain = 0;
    themeGlow = 0;
    themeScanlines = 0;
    applyCurrentTheme();
    await Promise.all([
      saveSetting("theme_contrast", "50"),
      saveSetting("theme_brightness", "50"),
      saveSetting("theme_hue", "0"),
      saveSetting("theme_saturation", "50"),
      saveSetting("theme_gradient", "50"),
      saveSetting("theme_grain", "0"),
      saveSetting("theme_glow", "0"),
      saveSetting("theme_scanlines", "0"),
    ]);
    dispatch("saved");
  }

  async function onThemeModeChange() {
    if (themeMode === "preset") {
      applyThemeVars(theme, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
      storageSet("guidebook-theme", theme);
      await saveSetting("theme_mode", "preset");
    } else {
      applyCustomThemeVars(customBg, customText, customAccent, customSecondary, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
      storageSet("guidebook-theme", "custom");
      await saveSetting("theme_mode", "custom");
      await saveCustomColors();
    }
    dispatch("saved");
  }

  function onCustomColorInput() {
    applyCustomThemeVars(customBg, customText, customAccent, customSecondary, themeContrast, themeBrightness, themeHue, themeSaturation, themeGradient, themeGrain, themeGlow, themeScanlines);
    storageSet("guidebook-theme", "custom");
    broadcastThemePreview("custom_theme_colors", JSON.stringify({ bg: customBg, text: customText, accent: customAccent, secondary: customSecondary }));
  }

  async function onCustomColorCommit() {
    onCustomColorInput();
    await saveCustomColors();
    dispatch("saved");
  }

  function onCustomColorChange() {
    onCustomColorCommit();
  }

  async function saveCustomColors() {
    const colors = JSON.stringify({ bg: customBg, text: customText, accent: customAccent, secondary: customSecondary });
    await saveSetting("custom_theme_colors", colors);
  }

  let colorDragging = false;

  function colorPicker(node, { getValue, setValue }) {
    node.setAttribute("color", getValue());
    const onChange = (e) => { setValue(e.detail.value); onCustomColorInput(); };
    const onDown = () => { colorDragging = true; };
    const onUp = () => { if (colorDragging) { colorDragging = false; onCustomColorCommit(); } };
    node.addEventListener("color-changed", onChange);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("touchend", onUp);
    node.addEventListener("mousedown", onDown);
    node.addEventListener("touchstart", onDown);
    return {
      update({ getValue }) { node.setAttribute("color", getValue()); },
      destroy() {
        node.removeEventListener("color-changed", onChange);
        window.removeEventListener("mouseup", onUp);
        window.removeEventListener("touchend", onUp);
        node.removeEventListener("mousedown", onDown);
        node.removeEventListener("touchstart", onDown);
      },
    };
  }

  // --- Auto-save helpers ---

  async function saveSetting(key, value) {
    await fetch(`/api/settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    // Re-check source: if saved value is blank, it may fall back to global
    try {
      const res = await fetch(`/api/settings/${key}`);
      if (res.ok) {
        const data = await res.json();
        settingSources[key] = data.source || "database";
        if (data.source === "global" && data.value) {
          globalPlaceholders[key] = data.value;
        } else {
          delete globalPlaceholders[key];
        }
      }
    } catch {}
    settingSources = settingSources;
    globalPlaceholders = globalPlaceholders;
  }

  const dirtyFields = new Set();
  const debounceTimers = {};

  function markDirty(key) {
    dirtyFields.add(key);
    clearTimeout(debounceTimers[key]);
    debounceTimers[key] = setTimeout(() => {
      if (dirtyFields.has(key) && fieldSavers[key]) {
        dirtyFields.delete(key);
        fieldSavers[key]();
      }
    }, 2000);
  }

  async function flushPending() {
    for (const key of dirtyFields) {
      if (fieldSavers[key]) await fieldSavers[key]();
    }
    dirtyFields.clear();
  }

  async function switchTab(tab) {
    await flushPending();
    activeTab = tab;
    window.location.hash = `/settings/${tab}`;
  }

  // --- Masonry layout action ---
  function masonry(node) {
    let col1, col2;
    const MIN_WIDTH = 640;

    function collectSections() {
      const direct = [...node.querySelectorAll(":scope > .settings-section")];
      const inCols = col1 ? [...col1.querySelectorAll(":scope > .settings-section"), ...col2.querySelectorAll(":scope > .settings-section")] : [];
      return [...direct, ...inCols];
    }

    function teardownColumns() {
      if (col1 && col1.parentNode === node) {
        const sections = collectSections();
        for (const s of sections) node.appendChild(s);
        if (col1.parentNode === node) node.removeChild(col1);
        if (col2.parentNode === node) node.removeChild(col2);
      }
    }

    function layout() {
      const width = node.parentElement?.offsetWidth || node.offsetWidth;

      if (width < MIN_WIDTH) {
        teardownColumns();
        return;
      }

      const sections = collectSections();
      if (!sections.length) return;

      for (const s of sections) node.appendChild(s);
      if (col1 && col1.parentNode === node) {
        node.removeChild(col1);
        node.removeChild(col2);
      }

      if (!col1) {
        col1 = document.createElement("div");
        col1.className = "masonry-col";
        col2 = document.createElement("div");
        col2.className = "masonry-col";
      }

      const heights = sections.map(s => s.offsetHeight);

      let h1 = 0, h2 = 0;
      const assign1 = [], assign2 = [];
      for (let i = 0; i < sections.length; i++) {
        if (h1 <= h2) {
          assign1.push(sections[i]);
          h1 += heights[i];
        } else {
          assign2.push(sections[i]);
          h2 += heights[i];
        }
      }

      for (const s of assign1) col1.appendChild(s);
      for (const s of assign2) col2.appendChild(s);
      node.appendChild(col1);
      node.appendChild(col2);
    }

    let lastMode = null;

    function layoutIfModeChanged() {
      const width = node.parentElement?.offsetWidth || node.offsetWidth;
      const mode = width < MIN_WIDTH ? "single" : "dual";
      if (mode === lastMode) return;
      lastMode = mode;
      layout();
    }

    const raf = requestAnimationFrame(() => { layout(); lastMode = (node.parentElement?.offsetWidth || node.offsetWidth) < MIN_WIDTH ? "single" : "dual"; });
    let resizeTimer;
    const onResize = () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => requestAnimationFrame(layoutIfModeChanged), 150);
    };
    window.addEventListener("resize", onResize);

    return {
      destroy() {
        cancelAnimationFrame(raf);
        clearTimeout(resizeTimer);
        window.removeEventListener("resize", onResize);
        teardownColumns();
      },
    };
  }

  // --- Per-field auto-save handlers ---

  const fieldSavers = {
    wide_breakpoint: async () => {
      await saveSetting("wide_breakpoint", wide_mode_enabled ? String(wide_breakpoint) : "0");
      dispatch("saved");
    },
    custom_header: async () => {
      await saveSetting("custom_header", custom_header.trim());
      dispatch("saved");
    },
  };

  function onFieldKeydown(e) {
    if (e.key === "Enter") e.target.blur();
  }

  async function onFieldBlur(key) {
    clearTimeout(debounceTimers[key]);
    if (dirtyFields.has(key) && fieldSavers[key]) {
      dirtyFields.delete(key);
      await fieldSavers[key]();
    }
  }

  async function onSqlQueryEnabledChange() {
    await saveSetting("sql_query_enabled", sql_query_enabled ? "true" : "false");
    dispatch("saved");
  }

  async function onUpdateCheckEnabledChange() {
    await saveGlobalSetting("update_check_enabled", update_check_enabled ? "true" : "false");
    if (update_check_enabled) {
      await fetchUpdateCheck();
    } else {
      updateCheckResult = null;
    }
    dispatch("saved");
  }

  async function onDatabaseRightChange() {
    await saveSetting("database_right", database_right ? "true" : "false");
    dispatch("saved");
  }

  async function onWideModeEnabledChange() {
    if (wide_mode_enabled) {
      await saveSetting("wide_breakpoint", String(wide_breakpoint));
    } else {
      await saveSetting("wide_breakpoint", "0");
    }
    dispatch("saved");
  }

  function onWideBreakpointInput() {
    markDirty("wide_breakpoint");
  }

  async function onDefaultPageChange() {
    await saveSetting("default_page", default_page);
    dispatch("saved");
  }

  function onCustomHeaderInput() {
    markDirty("custom_header");
  }

  async function fetchSettings() {
    try {
      const res = await fetch("/api/settings/");
      if (res.ok) {
        const data = await res.json();
        settingSources = {};
        globalPlaceholders = {};
        for (const s of data) {
          if (s.source) settingSources[s.key] = s.source;
          if (s.key === "sql_query_enabled") sql_query_enabled = s.value === "true";
          if (s.key === "update_check_enabled") update_check_enabled = s.value !== "false";
          if (s.key === "wide_breakpoint") {
            if (s.value === "0") {
              wide_mode_enabled = false;
              wide_breakpoint = "1200";
            } else {
              wide_mode_enabled = true;
              wide_breakpoint = s.value || "1500";
            }
          }
          if (s.key === "database_right") database_right = s.value === "true";
          if (s.key === "custom_header") custom_header = s.value || "";
          if (s.key === "default_page") default_page = s.value || "log";
          if (s.key === "theme") theme = s.value || (window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
          if (s.key === "theme_contrast") { const v = parseInt(s.value); themeContrast = isNaN(v) ? 50 : v; }
          if (s.key === "theme_brightness") { const v = parseInt(s.value); themeBrightness = isNaN(v) ? 50 : v; }
          if (s.key === "theme_hue") { const v = parseInt(s.value); themeHue = isNaN(v) ? 0 : v; }
          if (s.key === "theme_saturation") { const v = parseInt(s.value); themeSaturation = isNaN(v) ? 50 : v; }
          if (s.key === "theme_gradient") { const v = parseInt(s.value); themeGradient = isNaN(v) ? 50 : v; }
          if (s.key === "theme_grain") { const v = parseInt(s.value); themeGrain = isNaN(v) ? 0 : v; }
          if (s.key === "theme_glow") { const v = parseInt(s.value); themeGlow = isNaN(v) ? 0 : v; }
          if (s.key === "theme_scanlines") { const v = parseInt(s.value); themeScanlines = isNaN(v) ? 0 : v; }
          if (s.key === "theme_mode") themeMode = s.value || "preset";
          if (s.key === "custom_theme_colors") {
            try {
              const c = JSON.parse(s.value);
              if (c.bg) customBg = c.bg;
              if (c.text) customText = c.text;
              if (c.accent) customAccent = c.accent;
              if (c.secondary) customSecondary = c.secondary;
            } catch {}
          }
          if (s.key === "popup_notifications_enabled") popupNotifEnabled = s.value === "true";
        }
      }
      settingsLoaded = true;
    } catch {}
  }


  async function fetchGlobalSettings() {
    try {
      const res = await fetch("/api/global-settings/");
      if (res.ok) {
        const data = await res.json();
        for (const s of data) {
          if (s.key === "update_check_enabled") update_check_enabled = s.value !== "false";
        }
        globalSettingsLoaded = true;
      }
    } catch {}
  }

  async function saveGlobalSetting(key, value) {
    await fetch(`/api/global-settings/${key}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    });
    // Re-fetch per-database settings so placeholders and fallbacks update
    await fetchSettings();
    dispatch("saved");
  }

  async function loadUpdateCheck() {
    try {
      const res = await fetch("/api/update-check");
      if (res.ok) updateCheckResult = await res.json();
    } catch {}
    try {
      const res = await fetch("/api/update/platform");
      if (res.ok) {
        const data = await res.json();
        updateSupported = (data.supported && data.writable) || false;
        updateNotWritable = data.supported && !data.writable;
        updateBuildRepo = data.build_origin_repo || "";
        updateBuildSha = data.build_git_sha || "";
        updateGithubRepo = data.github_repo || "";
        updateOfficialBuild = data.build_github_actions || false;
        updateCustomRepo = !!updateBuildRepo && updateBuildRepo !== "EnigmaCurry/guidebook";
      }
    } catch {}
  }

  async function skipUpdate() {
    try {
      const res = await fetch("/api/update-check/skip", { method: "POST" });
      if (res.ok && updateCheckResult) {
        updateCheckResult.update_skipped = true;
        updateCheckResult = updateCheckResult; // trigger reactivity
      }
    } catch {}
  }

  function confirmAndApplyUpdate() {
    if (!updateCheckResult) return;
    if (confirm(`Update Guidebook from v${updateCheckResult.current} to v${updateCheckResult.latest}? The server will restart.`)) {
      applyUpdate();
    }
  }

  async function applyUpdate() {
    updateApplying = true;
    updateApplyError = "";
    try {
      const res = await fetch("/api/update/apply", { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        updateApplyError = data.detail || "Update failed";
        updateApplying = false;
        return;
      }
      if (data.status === "up_to_date") {
        updateApplyError = "Already up to date";
        updateApplying = false;
        return;
      }
      // Server is restarting -- poll until it comes back
      await new Promise(r => setTimeout(r, 2000));
      for (let i = 0; i < 30; i++) {
        try {
          const check = await fetch("/api/version");
          if (check.ok) {
            window.location.reload();
            return;
          }
        } catch {}
        await new Promise(r => setTimeout(r, 1000));
      }
      updateApplyError = "Server did not come back after update -- check manually";
      updateApplying = false;
    } catch (e) {
      updateApplyError = "Update failed: " + e.message;
      updateApplying = false;
    }
  }

  async function fetchUpdateCheck() {
    updateChecking = true;
    try {
      const res = await fetch("/api/update-check?bust=true");
      if (res.ok) updateCheckResult = await res.json();
    } catch {}
    updateChecking = false;
  }

  function formatTimeAgo(epochSecs) {
    const diff = Math.floor(Date.now() / 1000 - epochSecs);
    if (diff < 5) return "just now";
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function formatTimeUntil(epochSecs) {
    const diff = Math.floor(epochSecs - Date.now() / 1000);
    if (diff <= 0) return "soon";
    if (diff < 60) return `in ${diff}s`;
    if (diff < 3600) return `in ${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`;
    return `in ${Math.floor(diff / 86400)}d`;
  }

  async function fetchEntryCount() {
    try {
      const res = await fetch("/api/records/");
      if (res.ok) { const data = await res.json(); entryCount = data.length; }
    } catch {}
  }

  async function fetchNoShutdown() {
    try {
      const res = await fetch("/api/version");
      if (res.ok) {
        const data = await res.json();
        noShutdown = !!data.no_shutdown;
        await tick();
        window.dispatchEvent(new Event("resize"));
      }
    } catch {}
  }

  onMount(() => {
    fetchSettings();
    fetchGlobalSettings();
    fetchEntryCount();
    loadDbInfo();
    loadBackupStatus();
    fetchNoShutdown();
    loadAuthStatus();
    loadAuthSessions();
    loadMtlsStatus();
  });

  onDestroy(() => {
    flushPending();
  });
</script>

<div class="settings">
  <h2>Settings <span class="autosave-hint">(are saved automatically on change)</span></h2>

  <div class="tab-bar">
    <button class="tab" class:active={activeTab === "features"} on:click={() => switchTab("features")}>Features</button>
    <button class="tab" class:active={activeTab === "appearance"} on:click={() => switchTab("appearance")}>Appearance</button>
    <button class="tab" class:active={activeTab === "updates"} on:click={() => switchTab("updates")}>Updates</button>
    <button class="tab" class:active={activeTab === "data"} on:click={() => switchTab("data")}>Data</button>
    <button class="tab" class:active={activeTab === "auth"} on:click={() => switchTab("auth")}>Auth</button>
  </div>

  {#if activeTab === "features"}
  <div class="tab-scroll"><div class="tab-content" use:masonry>
  <section class="settings-section">
    <h3>SQL Query (read-only view)</h3>
    <div class="setting-row toggle-row">
      <label>
        <input type="checkbox" bind:checked={sql_query_enabled} on:change={onSqlQueryEnabledChange} />
        Enable SQL query page
      </label>
    </div>
  </section>

  <section class="settings-section">
    <h3>Notifications</h3>
    <div class="setting-row toggle-row">
      {#if desktopNotifPermission === "denied"}
        <span class="hint">Desktop notifications blocked by browser. Allow notifications for this site in your browser settings.</span>
      {:else if desktopNotifPermission === "granted" && desktopNotifEnabled}
        <span style="font-size:0.85rem; color:var(--accent);">Desktop notifications are enabled</span>
        <button class="theme-toggle" on:click={disableDesktopNotifications}>Disable</button>
      {:else if desktopNotifPermission === "granted" && !desktopNotifEnabled}
        <span style="font-size:0.85rem; color:var(--text-muted);">Desktop notifications are disabled</span>
        <button class="theme-toggle" on:click={() => { desktopNotifEnabled = true; storageSet("desktop_notifications_enabled", "true"); }}>Enable</button>
      {:else}
        <button class="theme-toggle" on:click={enableDesktopNotifications}>Enable Desktop Notifications</button>
      {/if}
    </div>
    <p class="hint">In-app notifications are always enabled. Desktop notifications show browser popups when new alerts arrive.</p>
    <div class="setting-row toggle-row" style="margin-top: 0.5rem;">
      <label>
        <input type="checkbox" bind:checked={popupNotifEnabled} on:change={async () => { await saveSetting("popup_notifications_enabled", popupNotifEnabled ? "true" : "false"); dispatch("saved"); }} />
        Popup notifications
      </label>
    </div>
    <p class="hint">Show a modal dialog immediately when new notifications arrive. Harder to miss, but more intrusive.</p>
    <div class="setting-row toggle-row" style="margin-top: 0.5rem;">
      <button class="theme-toggle" on:click={sendTestNotification} disabled={testPending}>
        {testPending ? "Sending in 5s..." : "Send Test Notification"}
      </button>
    </div>
  </section>
  </div></div>
  {/if}

  {#if activeTab === "appearance"}
  <div class="tab-scroll"><div class="tab-content" use:masonry>
  <section class="settings-section">
    <h3>Theme</h3>
    <p class="hint">Theme changes sync live to all open windows. Try opening Guidebook side-by-side in another window to preview your changes on any page.</p>
    <div class="setting-row toggle-row">
      <!-- svelte-ignore a11y-label-has-associated-control -->
      <label>Mode</label>
      <div class="theme-mode-switch">
        <button class="mode-btn" class:active={themeMode === "preset"} on:click={() => { themeMode = "preset"; onThemeModeChange(); }}>Preset</button>
        <button class="mode-btn" class:active={themeMode === "custom"} on:click={() => { themeMode = "custom"; onThemeModeChange(); }}>Custom</button>
      </div>
    </div>
    {#if themeMode === "preset"}
    <div class="setting-row">
      <label for="theme_select">Theme</label>
      <div class="theme-select-row">
        <select id="theme_select" bind:value={theme} on:change={onThemeChange}>
          {#each THEME_NAMES as t}
            <option value={t}>{THEMES[t].label}{t !== "dark" && t !== "light" ? ` (${THEMES[t].base})` : ""}</option>
          {/each}
        </select>
        <button class="contrast-reset" on:click={resetSliders} disabled={themeContrast === 50 && themeBrightness === 50 && themeHue === 0 && themeSaturation === 50 && themeGradient === 50 && themeGrain === 0 && themeGlow === 0 && themeScanlines === 0}>Reset Sliders</button>
      </div>
    </div>
    {:else}
    <!-- svelte-ignore a11y-label-has-associated-control -->
    <div class="color-pickers">
      <div class="color-picker-group">
        <label>Background</label>
        <hex-color-picker use:colorPicker={{ getValue: () => customBg, setValue: (v) => { customBg = v; } }}></hex-color-picker>
        <input type="text" class="color-hex-input" bind:value={customBg} on:input={onCustomColorInput} on:blur={onCustomColorCommit} maxlength="7" />
      </div>
      <div class="color-picker-group">
        <label>Text</label>
        <hex-color-picker use:colorPicker={{ getValue: () => customText, setValue: (v) => { customText = v; } }}></hex-color-picker>
        <input type="text" class="color-hex-input" bind:value={customText} on:input={onCustomColorInput} on:blur={onCustomColorCommit} maxlength="7" />
      </div>
      <div class="color-picker-group">
        <label>Accent</label>
        <hex-color-picker use:colorPicker={{ getValue: () => customAccent, setValue: (v) => { customAccent = v; } }}></hex-color-picker>
        <input type="text" class="color-hex-input" bind:value={customAccent} on:input={onCustomColorInput} on:blur={onCustomColorCommit} maxlength="7" />
      </div>
      <div class="color-picker-group">
        <label>Secondary</label>
        <hex-color-picker use:colorPicker={{ getValue: () => customSecondary, setValue: (v) => { customSecondary = v; } }}></hex-color-picker>
        <input type="text" class="color-hex-input" bind:value={customSecondary} on:input={onCustomColorInput} on:blur={onCustomColorCommit} maxlength="7" />
      </div>
    </div>
    {/if}
    <div class="slider-pair">
      <div class="slider-group">
        <label for="contrast_slider">Contrast <span class="slider-value">{themeContrast}</span></label>
        <div class="slider-control">
          <input id="contrast_slider" type="range" min="40" max="60" bind:value={themeContrast} on:input={onContrastInput} on:change={onContrastCommit} />
        </div>
      </div>
      <div class="slider-group">
        <label for="brightness_slider">Brightness <span class="slider-value">{themeBrightness}</span></label>
        <div class="slider-control">
          <input id="brightness_slider" type="range" min="40" max="60" bind:value={themeBrightness} on:input={onBrightnessInput} on:change={onBrightnessCommit} />
        </div>
      </div>
    </div>
    <div class="slider-pair">
      <div class="slider-group">
        <label for="hue_slider">Hue Shift <span class="slider-value">{themeHue}&deg;</span></label>
        <div class="slider-control">
          <input id="hue_slider" type="range" min="0" max="360" bind:value={themeHue} on:input={onHueInput} on:change={onHueCommit} class="hue-range" />
        </div>
      </div>
      <div class="slider-group">
        <label for="saturation_slider">Saturation <span class="slider-value">{themeSaturation}</span></label>
        <div class="slider-control">
          <input id="saturation_slider" type="range" min="0" max="100" bind:value={themeSaturation} on:input={onSaturationInput} on:change={onSaturationCommit} />
        </div>
      </div>
    </div>
    <div class="slider-pair">
      <div class="slider-group">
        <label for="gradient_slider">Gradient <span class="slider-value">{themeGradient - 50}</span></label>
        <div class="slider-control">
          <input id="gradient_slider" type="range" min="0" max="100" bind:value={themeGradient} on:input={onGradientInput} on:change={onGradientCommit} />
        </div>
      </div>
      <div class="slider-group">
        <label for="grain_slider">Grain <span class="slider-value">{themeGrain}</span></label>
        <div class="slider-control">
          <input id="grain_slider" type="range" min="0" max="100" bind:value={themeGrain} on:input={onGrainInput} on:change={onGrainCommit} />
        </div>
      </div>
    </div>
    <div class="slider-pair">
      <div class="slider-group">
        <label for="glow_slider">Glow <span class="slider-value">{themeGlow}</span></label>
        <div class="slider-control">
          <input id="glow_slider" type="range" min="0" max="100" bind:value={themeGlow} on:input={onGlowInput} on:change={onGlowCommit} />
        </div>
      </div>
      <div class="slider-group">
        <label for="scanlines_slider">Scanlines <span class="slider-value">{themeScanlines}</span></label>
        <div class="slider-control">
          <input id="scanlines_slider" type="range" min="0" max="45" bind:value={themeScanlines} on:input={onScanlinesInput} on:change={onScanlinesCommit} />
        </div>
      </div>
    </div>
    <p class="hint">Grain, Glow, and Scanlines may impact performance on low-powered devices. Set these to 0 to disable. Grain is paper-like unless combined with Scanlines to produce static.</p>
  </section>
  <section class="settings-section" data-section="content">
    <h3>Content</h3>
    <div class="setting-row">
      <label for="custom_header">Custom Header</label>
      <input id="custom_header" type="text" bind:value={custom_header} on:input={onCustomHeaderInput} on:keydown={onFieldKeydown} on:blur={() => onFieldBlur("custom_header")} autocomplete="off" placeholder="Header text" />
      <span class="hint">Custom text displayed in the header.</span>
    </div>
    <div class="setting-row">
      <label for="default_page">Home Page</label>
      <select id="default_page" bind:value={default_page} on:change={onDefaultPageChange}>
        <option value="log">Log</option>
        <option value="media">Media</option>
        <option value="notifications">Notifications</option>
      </select>
    </div>
  </section>
  <section class="settings-section">
    <h3>Wide Mode</h3>
    <div class="setting-row toggle-row">
      <label>
        <input type="checkbox" bind:checked={wide_mode_enabled} on:change={onWideModeEnabledChange} />
        Wide Mode
      </label>
    </div>
    <div class="setting-row">
      <label for="wide_breakpoint">Breakpoint: {wide_breakpoint}px</label>
      <input id="wide_breakpoint" type="range" min="1200" max="2500" step="50" bind:value={wide_breakpoint} on:input={onWideBreakpointInput} on:change={() => onFieldBlur("wide_breakpoint")} disabled={!wide_mode_enabled} />
    </div>
    <div class="setting-row toggle-row">
      <label>
        <input type="checkbox" bind:checked={database_right} on:change={onDatabaseRightChange} disabled={!wide_mode_enabled} />
        Database on right side
      </label>
    </div>
  </section>
  </div></div>
  {/if}

  {#if activeTab === "updates"}
  <div class="tab-scroll"><div class="tab-content" use:masonry>
  <section class="settings-section">
    <h3>Update Checker</h3>
    {#if updateOfficialBuild}
      <div class="setting-row toggle-row">
        <label>
          <input type="checkbox" bind:checked={update_check_enabled} on:change={onUpdateCheckEnabledChange} />
          Check for new Guidebook releases on GitHub
        </label>
      </div>
      {#if update_check_enabled && updateCheckResult}
        <div class="update-status">
          <div>Current version: <strong>v{updateCheckResult.current}</strong>{#if updateBuildSha}
            (<a href="https://github.com/{updateGithubRepo}/commit/{updateBuildSha}" target="_blank" rel="noopener" class="sha-link">{updateBuildSha}</a>){/if}
          {#if updateCheckResult.is_dev}
            — Development version — update checker is disabled
          {:else if updateCheckResult.is_exact}
            — You're running the latest version
          {:else if updateCheckResult.latest && !updateCheckResult.update_available}
            — You're ahead of the latest release (v{updateCheckResult.latest})
          {:else if !updateCheckResult.latest}
            — Unable to check for updates
          {/if}
          </div>
          {#if updateCheckResult.update_available && !updateCheckResult.update_skipped}
            <div><span class="update-available">Update available: v{updateCheckResult.latest}</span></div>
            <div class="update-actions">
              {#if updateSupported}
                <button class="check-now-btn apply-update-btn" on:click={confirmAndApplyUpdate} disabled={updateApplying}>
                  {updateApplying ? "Updating..." : "Apply Update"}
                </button>
                <button class="check-now-btn" on:click={skipUpdate}>Skip</button>
              {:else}
                <a href={updateCheckResult.url} target="_blank" rel="noopener" class="update-available">Download</a>
                {#if updateNotWritable}
                  <span class="update-error">In-app update unavailable: no write permission to the binary location</span>
                {/if}
              {/if}
              {#if updateApplyError}
                <span class="update-error">{updateApplyError}</span>
              {/if}
            </div>
          {:else if updateCheckResult.update_skipped}
            <div>v{updateCheckResult.latest} available (skipped)</div>
            <div class="update-actions">
              {#if updateSupported}
                <button class="check-now-btn apply-update-btn" on:click={confirmAndApplyUpdate} disabled={updateApplying}>
                  {updateApplying ? "Updating..." : "Apply Update"}
                </button>
              {:else}
                <a href={updateCheckResult.url} target="_blank" rel="noopener" class="update-available">Download</a>
                {#if updateNotWritable}
                  <span class="update-error">In-app update unavailable: no write permission to the binary location</span>
                {/if}
              {/if}
              {#if updateApplyError}
                <span class="update-error">{updateApplyError}</span>
              {/if}
            </div>
          {/if}
        </div>
        <div class="update-check-meta">
          {#if updateCheckResult.checked_at}
            Checked {formatTimeAgo(updateCheckResult.checked_at)}{#if updateCheckResult.next_check_at}, next check {formatTimeUntil(updateCheckResult.next_check_at)}{/if}
          {/if}
          <button class="check-now-btn" on:click={fetchUpdateCheck} disabled={updateChecking}>
            {updateChecking ? "Checking..." : "Check now"}
          </button>
        </div>
      {/if}
      {#if updateCustomRepo}
        <div class="update-custom-repo-warning">
          Warning: using custom update source <a href="https://github.com/{updateBuildRepo}" target="_blank" rel="noopener"><strong>{updateBuildRepo}</strong></a>
        </div>
      {/if}
    {:else}
      <div class="update-status">
        Version: <strong>v{updateCheckResult?.current || '...'}</strong>{#if updateBuildSha}
          (<a href="https://github.com/{updateGithubRepo}/commit/{updateBuildSha}" target="_blank" rel="noopener" class="sha-link">{updateBuildSha}</a>){/if}
      </div>
      <p class="hint">Update checking is disabled for local builds.</p>
    {/if}
  </section>
  </div></div>
  {/if}

  {#if activeTab === "data"}
  <div class="tab-scroll"><div class="tab-content" use:masonry>

  <section class="settings-section">
    <h3>Backup</h3>
    <p class="hint">Backup settings are configured separately for each database.</p>
    {#if dbInfo}
      <p class="hint">Database: {dbInfo.path} ({formatSize(dbInfo.size)})</p>
      <p class="hint">Backups: {dbInfo.directory}</p>
    {/if}
    <div class="setting-row">
      <button on:click={performBackup} disabled={backingUp}>
        {backingUp ? "Backing up..." : "Backup Now"}
      </button>
    </div>
    {#if backupMessage}
      <p class="hint" class:danger-error={backupMessageType === "error"} style={backupMessageType === "success" ? "color: var(--accent)" : ""}>{backupMessage}</p>
    {/if}
    <div class="setting-row toggle-row">
      <label class="toggle-label">
        <input type="checkbox" bind:checked={autoBackupEnabled} on:change={saveAutoBackupSettings} />
        Auto-backup
      </label>
    </div>
    {#if autoBackupEnabled}
      <div class="setting-row">
        <label for="backup_interval">Interval (hours)</label>
        <input id="backup_interval" type="number" min="1" max="720" bind:value={autoBackupHours} on:input={saveAutoBackupSettings} style="width: 5rem" />
      </div>
      <div class="setting-row">
        <label for="backup_max">Keep max</label>
        <input id="backup_max" type="number" min="1" max="100" bind:value={autoBackupMax} on:input={saveAutoBackupSettings} style="width: 5rem" />
      </div>
    {/if}
    {#if backupStatus}
      <p class="hint">
        {#if backupStatus.auto_enabled}
          Last auto-backup: {timeAgo(backupStatus.last_backup)} — Next: {formatDue(backupStatus.next_due)}
        {:else}
          Auto-backup disabled
        {/if}
        {#if backupStatus.auto_backup_count > 0 || backupStatus.manual_backup_count > 0}
          — {backupStatus.auto_backup_count} auto, {backupStatus.manual_backup_count} manual
        {/if}
      </p>
    {/if}
  </section>

  {#if !noShutdown}
  <section class="settings-section">
    <h3>Shutdown</h3>
    <p class="hint">Connected clients: {clientCount}</p>
    {#if clientCount > 1}
      <div class="setting-row">
        <button class="warning-btn" on:click={disconnectOthers}>Disconnect all other clients</button>
      </div>
    {/if}
    <div class="setting-row">
      <button class="danger-btn" on:click={shutdownServer}>Shutdown Now</button>
    </div>
  </section>
  {/if}

  {#if databaseName}
    <section class="settings-section danger-zone">
      <h3>Danger Zone</h3>
      <div class="setting-row">
        <label for="danger-confirm">Type <strong>{databaseName}</strong> to enable the Danger Zone</label>
        <input id="danger-confirm" type="text" bind:value={dangerConfirmName} placeholder={databaseName} autocomplete="off" />
      </div>
      <div class="danger-separator"></div>
      <p class="danger-text">Delete all entries from <strong>{databaseName}</strong> but keep the database and settings.</p>
      {#if clearError}
        <p class="danger-error">{clearError}</p>
      {/if}
      <div class="setting-row">
        <button class="danger-btn" on:click={clearAllEntries} disabled={clearing || dangerConfirmName !== databaseName || entryCount === 0}>
          {clearing ? "Clearing..." : entryCount === 0 ? "No entries to clear" : "Clear All Entries"}
        </button>
      </div>
      <div class="danger-separator"></div>
      <p class="danger-text">Permanently delete the database <strong>{databaseName}</strong> and all its data. This cannot be undone.</p>
      {#if deleteError}
        <p class="danger-error">{deleteError}</p>
      {/if}
      <div class="setting-row">
        <button class="danger-btn" on:click={deleteDatabase} disabled={deleting || dangerConfirmName !== databaseName}>
          {deleting ? "Deleting..." : "Delete Database"}
        </button>
      </div>
    </section>
  {/if}
  </div></div>
  {/if}

  {#if activeTab === "auth"}
  <div class="tab-scroll"><div class="tab-content" use:masonry>
  <section class="settings-section">
    <h3>Authentication</h3>
    {#if !authEnabled}
    <p class="hint" style="color: var(--error-color, #e74c3c); font-weight: bold;">⚠ Authentication is disabled via <code style="font-size: 0.75rem; white-space: nowrap">--disable-auth</code>. Anyone can access this app without credentials.</p>
    {:else}
    <p class="hint">{#if mtlsMode === "required"}Authentication is currently enforced with mTLS certificates only.{:else if mtlsMode === "disabled"}Authentication is currently enforced with session cookies only.{:else}Authentication is currently enforced with session cookies and/or mTLS certificates.{/if} Use <code style="font-size: 0.75rem; white-space: nowrap">--disable-auth</code> at startup to turn authentication off completely.</p>
    {/if}
  </section>

  <section class="settings-section">
    <h3>Client Certificates (mTLS)</h3>
    {#if !mtlsTlsEnabled}
      <p class="hint">mTLS is unavailable because TLS is disabled. Remove <code style="font-size: 0.75rem; white-space: nowrap">--no-tls</code> to enable.</p>
    {:else if mtlsProxyMode}
      <p class="hint">mTLS is unavailable in proxy mode. Configure mTLS at your reverse proxy instead.</p>
    {:else}
      <div class="mtls-radio-group">
        <label class="mtls-radio" class:active={mtlsMode === "disabled"}>
          <input type="radio" name="mtls-mode" value="disabled" checked={mtlsMode === "disabled"} disabled={mtlsActivating} on:change={() => activateMtls("disabled")} />
          <span><strong>Disabled</strong> — cookie auth only</span>
        </label>
        <label class="mtls-radio" class:active={mtlsMode === "optional"}>
          <input type="radio" name="mtls-mode" value="optional" checked={mtlsMode === "optional"} disabled={mtlsActivating} on:change={() => activateMtls("optional")} />
          <span><strong>Optional</strong> — both cookie and client certificate accepted</span>
        </label>
        <label class="mtls-radio" class:active={mtlsMode === "required"}>
          <input type="radio" name="mtls-mode" value="required" checked={mtlsMode === "required"} disabled={mtlsActivating} on:change={() => activateMtls("required")} />
          <span><strong>Enforced</strong> — only client certificates accepted</span>
        </label>
      </div>
      <p class="hint" style="margin-top: 0.5rem; color: var(--warning-color, #e6a700);">Mode changes require a server restart to take effect.</p>

      {#if mtlsMode !== "disabled" || mtlsCerts.length > 0}
        <h4 style="margin-top: 1rem; margin-bottom: 0.5rem;">Generate Client Certificate</h4>
        <div class="setting-row">
          <button on:click={openCertModal} disabled={authSlots > 0 && authAvailableSlots <= 0 && !authSessions.some(s => s.is_current)}>
            {authSlots > 0 && authAvailableSlots <= 0 && !authSessions.some(s => s.is_current) ? `No slots available (${authSlots} used)` : "Generate Client Certificate"}
          </button>
        </div>
      {/if}

      {#if mtlsCerts.filter(c => c.revoked_at).length > 0}
        <details class="mtls-revoked-accordion" style="margin-top: 0.75rem;">
          <summary>Revoked Certificates ({mtlsCerts.filter(c => c.revoked_at).length})</summary>
          <div class="session-list mtls-cert-list">
            {#each mtlsCerts.filter(c => c.revoked_at) as cert (cert.id)}
              <div class="session-item session-revoked">
                <div class="session-info">
                  <span class="session-label">{cert.label}</span>
                  <span class="session-meta">
                    Fingerprint: {cert.fingerprint_sha256.slice(0, 16)}...
                    — Issued {formatAuthTime(cert.issued_at)}
                    — Revoked {formatAuthTime(cert.revoked_at)}
                  </span>
                </div>
              </div>
            {/each}
          </div>
        </details>
      {/if}
    {/if}

    {#if mtlsCaFingerprint}
      <div class="mtls-ca-box">
        <span class="mtls-ca-label">Certificate Authority</span>
        <span class="mtls-ca-fingerprint">SHA-256: {mtlsCaFingerprint}</span>
        <a href="/api/auth/mtls/ca.pem" download="guidebook-ca.pem" class="mtls-ca-download">Download CA Certificate</a>
      </div>
    {/if}

    {#if mtlsError}
      <p class="danger-error" style="margin-top: 0.5rem;">{mtlsError}</p>
    {/if}
  </section>


  <section class="settings-section">
    <h3>Clients</h3>
    {#if connectedCerts.length > 0 || connectedSessions.length > 0}
      <div class="session-list">
        {#each connectedCerts as cert (cert.serial_number)}
          <div class="session-item" class:session-current={cert.is_current}>
            <div class="session-info">
              <span class="session-label">
                <span class="auth-badge mtls">mTLS</span>
                {cert.label}
                {#if cert.is_current} <strong>(you)</strong>{/if}
              </span>
              <span class="session-meta">
                Fingerprint: {cert.fingerprint_sha256.slice(0, 16)}...
                — Issued {formatAuthTime(cert.issued_at)}
              </span>
            </div>
            {#if !cert.is_current}
              <button class="session-delete" on:click={() => revokeClientCert(cert.id)} title="Revoke this certificate">Revoke</button>
            {/if}
          </div>
        {/each}
        {#each connectedSessions as session (session.id)}
          <div class="session-item" class:session-current={session.is_current}>
            <div class="session-info">
              <span class="session-label">
                <span class="auth-badge cookie">cookie</span>
                {shortUserAgent(session.user_agent) || "Unknown browser"}{#if session.last_ip} — {session.last_ip}{/if}
                {#if session.is_current && !mtlsCurrentCert} <strong>(you)</strong>{/if}
              </span>
              <span class="session-meta">Created {formatAuthTime(session.created_at)}{#if session.last_seen_at} — last seen {formatAuthTime(session.last_seen_at)}{/if}{#if session.expires_at} — expires {formatAuthTime(session.expires_at)}{/if}</span>
            </div>
            {#if !session.is_current || mtlsCurrentCert}
              <button class="session-delete" on:click={() => deleteSession(session.id)} title="Revoke this session">Revoke</button>
            {/if}
          </div>
        {/each}
      </div>
    {/if}

    {#if inactiveCerts.length > 0 || inactiveSessions.length > 0}
      <h4 style="margin-top: 0.75rem; margin-bottom: 0.4rem; font-size: 0.8rem; color: var(--text-dim);">Inactive</h4>
      <div class="session-list">
        {#each inactiveCerts as cert (cert.serial_number)}
          <div class="session-item">
            <div class="session-info">
              <span class="session-label">
                <span class="auth-badge mtls">mTLS</span>
                {cert.label}
              </span>
              <span class="session-meta">
                Fingerprint: {cert.fingerprint_sha256.slice(0, 16)}...
                — Issued {formatAuthTime(cert.issued_at)}
              </span>
            </div>
            <button class="session-delete" on:click={() => revokeClientCert(cert.id)} title="Revoke this certificate">Revoke</button>
          </div>
        {/each}
        {#each inactiveSessions as session (session.id)}
          <div class="session-item">
            <div class="session-info">
              <span class="session-label">
                <span class="auth-badge cookie">cookie</span>
                {shortUserAgent(session.user_agent) || "Unknown browser"}{#if session.last_ip} — {session.last_ip}{/if}
                {#if session.is_transfer} <em>(invited)</em>{/if}
              </span>
              <span class="session-meta">{#if session.last_seen_at}Last seen {formatAuthTime(session.last_seen_at)}{:else}Never connected{/if}{#if session.expires_at} — expires {formatAuthTime(session.expires_at)}{/if}</span>
            </div>
            <button class="session-delete" on:click={() => deleteSession(session.id)} title="Revoke this session">Revoke</button>
          </div>
        {/each}
      </div>
    {/if}

    {#if connectedCerts.length === 0 && connectedSessions.length === 0 && inactiveCerts.length === 0 && inactiveSessions.length === 0}
      <p class="hint">No clients.</p>
    {/if}
  </section>

  {#if mtlsMode !== "required"}
  <section class="settings-section">
    <h3>Generate Login Link</h3>
    <p class="hint">Generate a one-time login URL to share with another browser.</p>
    {#if !authShowLinkForm && !authTokenUrl}
      <div class="setting-row">
        <button on:click={openLoginLinkForm} disabled={authSlots > 0 && authAvailableSlots <= 0}>
          {authSlots > 0 && authAvailableSlots <= 0 ? `No slots available (${authSlots}/${authSlots} used)` : "Generate Login Link"}
        </button>
      </div>
      {#if authSlots > 0 && authAvailableSlots <= 0}
        <p class="hint">Pass <code style="font-size: 0.75rem; white-space: nowrap">--auth-slots X</code> at startup to allow more concurrent sessions.</p>
      {/if}
    {:else if !authTokenUrl}
      <div style="margin-top: 0.5rem;">
        <span style="font-size: 0.8rem; opacity: 0.7; display: block; margin-bottom: 0.3rem;">Label</span>
        <input type="text" bind:value={authLinkLabelInput} placeholder="session-1" style="width: 100%; box-sizing: border-box; margin-bottom: 0.5rem;" />
        <p class="hint" style="margin-bottom: 0.5rem;">Choose a name to identify this session (e.g. browser, device, or person).</p>
        <div class="setting-row" style="gap: 0.5rem;">
          <button on:click={() => { authShowLinkForm = false; }} style="margin-right: auto;">Cancel</button>
          <button on:click={generateLoginToken} disabled={authGenerating || !authLinkLabelInput.trim()}>
            {authGenerating ? "Generating..." : "Generate"}
          </button>
        </div>
      </div>
    {:else}
      <div class="token-url-box">
        <input type="text" value={authTokenUrl} readonly on:click={(e) => e.target.select()} />
        <button class="copy-btn" class:copied={copiedField === 'token'} on:click={() => copyToClipboard(authTokenUrl, 'token')}>{copiedField === 'token' ? 'Copied!' : 'Copy'}</button>
      </div>
      <p class="hint">Share this link with the new browser. It expires in 5 minutes and is consumed on first use.</p>
    {/if}
  </section>
  {/if}

  {#if authAllowTransfer && mtlsMode !== "required"}
  <section class="settings-section">
    <h3>Transfer Session</h3>
    <p class="hint">Move your current session to a new browser. Your current session will be logged out as soon as the new browser logs in.</p>
    <div class="setting-row">
      <button class="warning-btn" on:click={generateTransferToken} disabled={authTransferring}>
        {authTransferring ? "Generating..." : "Generate Transfer Link"}
      </button>
    </div>
    {#if authTransferUrl}
      <div class="token-url-box">
        <input type="text" value={authTransferUrl} readonly on:click={(e) => e.target.select()} />
        <button class="copy-btn" class:copied={copiedField === 'transfer'} on:click={() => copyToClipboard(authTransferUrl, 'transfer')}>{copiedField === 'transfer' ? 'Copied!' : 'Copy'}</button>
      </div>
      <p class="hint" style="color: var(--warning-color, #e6a700);">Opening this link in another browser will log you out of this one. Expires in 5 minutes.</p>
    {/if}
  </section>
  {/if}

  <section class="settings-section">
    <h3>Logout</h3>
    {#if mtlsCurrentCert}
      <p class="hint">Revoke your current client certificate. You will need a new mTLS certificate to reconnect. Otherwise you may restart the server with <code style="font-size: 0.75rem; white-space: nowrap">--reset-auth</code> to setup auth again from scratch.</p>
      <div class="setting-row">
        <button class="danger-btn" on:click={mtlsLogout}>Revoke Certificate &amp; Logout</button>
      </div>
    {:else}
      <p class="hint">Log out of this browser session. You will need a new login link to access the server again.</p>
      <div class="setting-row">
        <button class="danger-btn" on:click={logoutSession}>Logout</button>
      </div>
    {/if}
  </section>

  {#if authError}
    <section class="settings-section">
      <p class="danger-error">{authError}</p>
    </section>
  {/if}
  </div></div>
  {/if}

</div>

{#if mtlsShowCertModal}
<!-- svelte-ignore a11y-click-events-have-key-events -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="mtls-modal-backdrop" on:click={dismissCertModal}>
  <!-- svelte-ignore a11y-click-events-have-key-events -->
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="mtls-modal" on:click|stopPropagation>
    {#if !mtlsCertGenerated}
      <div class="mtls-modal-header">
        <h3>Generate Client Certificate</h3>
        <button class="modal-close" on:click={dismissCertModal}>&times;</button>
      </div>
      <div class="mtls-modal-body">
        <div style="margin-bottom: 1rem;">
          <span style="font-size: 0.8rem; opacity: 0.7; display: block; margin-bottom: 0.3rem;">Label</span>
          <input type="text" bind:value={mtlsCertLabelInput} placeholder="client-1" style="width: 100%; box-sizing: border-box;" />
        </div>
        <p class="hint">Choose a name to identify this certificate (e.g. browser name, device, or person).</p>
      </div>
      <div class="mtls-modal-footer">
        <button on:click={dismissCertModal} style="margin-right: auto;">Cancel</button>
        <button on:click={generateClientCert} disabled={mtlsGenerating || !mtlsCertLabelInput.trim()}>
          {mtlsGenerating ? "Generating..." : "Generate"}
        </button>
      </div>
    {:else}
      <div class="mtls-modal-header">
        <h3>Client Certificate Generated</h3>
        <button class="modal-close" on:click={dismissCertModal}>&times;</button>
      </div>
      <div class="mtls-modal-body">
        <p style="color: var(--warning-color, #e6a700); font-weight: bold; margin-bottom: 1rem;">
          This information will not be shown again. The download link is single-use.
        </p>
        <div style="margin-bottom: 1rem;">
          <span style="font-size: 0.8rem; opacity: 0.7; display: block;">Label</span>
          <div style="font-family: monospace;">{mtlsCertLabel}</div>
        </div>
        <div style="margin-bottom: 1rem;">
          <span style="font-size: 0.8rem; opacity: 0.7; display: block;">Fingerprint (SHA-256)</span>
          <div style="font-family: monospace; font-size: 0.8rem; word-break: break-all;">{mtlsCertFingerprint}</div>
        </div>
        <div style="margin-bottom: 1rem;">
          <span style="font-size: 0.8rem; opacity: 0.7; display: block;">Import Password</span>
          <div class="token-url-box">
            <input type="text" value={mtlsCertPassword} readonly on:click={(e) => e.target.select()} />
            <button class="copy-btn" class:copied={copiedField === 'mtls-pw'} on:click={() => copyToClipboard(mtlsCertPassword, 'mtls-pw')}>{copiedField === 'mtls-pw' ? 'Copied!' : 'Copy'}</button>
          </div>
        </div>
        <div style="margin-bottom: 1rem;">
          <button on:click={downloadCert} style="width: 100%;">Download .p12 Certificate</button>
        </div>
        <p class="hint">
          1. Download the .p12 file above.<br>
          2. Import it into your browser's certificate store using the password shown.<br>
          3. When prompted by the server, select this certificate.
        </p>
      </div>
      <div class="mtls-modal-footer">
        <button on:click={dismissCertModal}>Done</button>
      </div>
    {/if}
  </div>
</div>
{/if}

<style>
  .settings {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 5rem);
    overflow: hidden;
  }
  .settings > h2,
  .settings > .tab-bar {
    max-width: 1100px;
    width: 100%;
    margin-left: auto;
    margin-right: auto;
    box-sizing: border-box;
  }

  .tab-bar {
    display: flex;
    gap: 0;
    margin-bottom: 1rem;
    border-bottom: 1px solid var(--border);
  }

  .tab {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text);
    padding: 0.5rem 0.5rem;
    font-size: 0.8rem;
    font-weight: bold;
    cursor: pointer;
    font-family: inherit;
  }

  .tab:hover {
    background: var(--accent);
    color: var(--accent-text);
    font-weight: bold;
  }

  .tab.active {
    color: var(--accent);
    border-bottom-color: var(--accent);
  }

  .tab.active:hover {
    background: var(--accent);
    color: var(--accent-text);
  }

  .tab-scroll {
    flex: 1;
    overflow-y: auto;
    min-height: 0;
  }
  .tab-content {
    display: flex;
    flex-wrap: wrap;
    gap: 0 1rem;
    max-width: 1100px;
    width: 100%;
    margin: 0 auto;
    box-sizing: border-box;
  }

  /* Single-column fallback (no masonry columns created) */
  .tab-content > :global(.settings-section) {
    width: 100%;
  }

  :global(.masonry-col) {
    flex: 1;
    min-width: 0;
  }

  h2 {
    color: var(--accent);
    font-size: 1.2rem;
    margin: 0 0 1rem 0;
  }

  .autosave-hint {
    font-size: 0.7rem;
    font-weight: normal;
    color: var(--text-muted);
  }

  .settings-section {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.75rem 1rem;
    margin-bottom: 1rem;
  }
  .settings-section:global(.highlight-flash) {
    animation: highlight-pulse 1.5s ease-out;
  }
  @keyframes highlight-pulse {
    0%, 10% { border-color: var(--accent); box-shadow: 0 0 8px color-mix(in srgb, var(--accent) 40%, transparent); }
    30% { border-color: var(--border); box-shadow: none; }
    50%, 60% { border-color: var(--accent); box-shadow: 0 0 8px color-mix(in srgb, var(--accent) 40%, transparent); }
    100% { border-color: var(--border); box-shadow: none; }
  }

  h3 {
    color: var(--accent);
    font-size: 0.9rem;
    margin: 0 0 0.75rem 0;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .setting-row {
    margin-bottom: 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  label {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  input:not([type="range"]):not([type="checkbox"]), select {
    background: var(--bg-input);
    border: 1px solid var(--border-input);
    color: var(--text);
    padding: 0.4rem 0.5rem;
    font-family: inherit;
    font-size: 0.9rem;
    border-radius: 3px;
    width: 100%;
    max-width: 20rem;
  }

  input:not([type="range"]):not([type="checkbox"]):focus, select:focus {
    outline: none;
    border-color: var(--accent);
  }

  input[type="range"] {
    width: 100%;
    max-width: 20rem;
    accent-color: var(--accent);
  }

  input:disabled, select:disabled {
    opacity: 0.4;
  }

  input[type="checkbox"] {
    accent-color: var(--accent);
  }

  .color-pickers {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
  }

  @media (max-width: 360px) {
    .color-pickers {
      grid-template-columns: 1fr;
    }
  }

  .color-picker-group {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.4rem;
  }

  .color-picker-group label {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-weight: bold;
  }

  .color-picker-group hex-color-picker {
    width: 120px;
    height: 120px;
    touch-action: none;
  }

  .color-hex-input {
    width: 5.5rem !important;
    text-align: center;
    font-size: 0.8rem !important;
  }

  .slider-pair {
    display: flex;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .slider-group {
    flex: 1;
    min-width: 10rem;
  }
  .slider-group label {
    display: block;
    font-size: 0.85rem;
    margin-bottom: 0.25rem;
    color: var(--text-muted);
  }
  .slider-value {
    opacity: 0.6;
    font-size: 0.8rem;
  }
  .hue-range {
    -webkit-appearance: none;
    appearance: none;
    height: 6px;
    border-radius: 3px;
    background: linear-gradient(to right, #ff0000, #ffff00, #00ff00, #00ffff, #0000ff, #ff00ff, #ff0000);
    outline: none;
  }
  .hue-range::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--text);
    border: 2px solid var(--bg);
    cursor: pointer;
  }
  .hue-range::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--text);
    border: 2px solid var(--bg);
    cursor: pointer;
  }
  .slider-control {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .slider-control input[type="range"] {
    flex: 1;
    accent-color: var(--accent);
  }
  .theme-select-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .contrast-reset {
    font-family: inherit;
    font-size: 0.8rem;
    padding: 0.2rem 0.6rem;
    background: var(--btn-secondary);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 3px;
    cursor: pointer;
  }
  .contrast-reset:hover:not(:disabled) {
    color: var(--accent-text);
  }
  .contrast-reset:disabled {
    opacity: 0.4;
    cursor: default;
  }

  .theme-mode-switch {
    display: flex;
    gap: 0;
    border: 1px solid var(--border);
    border-radius: 3px;
    overflow: hidden;
  }

  .mode-btn {
    padding: 0.3rem 1rem;
    font-family: inherit;
    font-size: 0.8rem;
    font-weight: bold;
    border: none;
    cursor: pointer;
    background: var(--bg-input);
    color: var(--text-muted);
  }

  .mode-btn.active {
    background: var(--accent);
    color: var(--accent-text);
  }

  .mode-btn:hover:not(.active) {
    background: var(--menu-hover);
  }

  button {
    background: var(--accent);
    color: var(--accent-text);
    border: none;
    padding: 0.5rem 1.5rem;
    font-family: inherit;
    font-size: 0.9rem;
    font-weight: bold;
    border-radius: 3px;
    cursor: pointer;
  }

  button:hover:not(:disabled) {
    background: var(--accent-hover);
  }

  button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .hint {
    font-size: 0.7rem;
    color: var(--text-dim);
  }

  .toggle-row {
    flex-direction: row;
    align-items: center;
    gap: 0.75rem;
  }

  .theme-toggle {
    background: var(--btn-secondary);
    color: var(--text);
    padding: 0.3rem 1rem;
    font-size: 0.85rem;
  }

  .theme-toggle:hover {
    background: var(--btn-secondary-hover);
  }

  p.hint {
    font-size: 0.7rem;
    color: var(--text-dim);
    margin: 0;
  }
  p.hint + .setting-row {
    margin-top: 0.5rem;
  }

  .danger-zone {
    border-color: #ff4444;
  }

  .danger-zone h3 {
    color: #ff4444;
  }

  .danger-text {
    font-size: 0.85rem;
    color: var(--text-muted);
    margin: 0 0 0.75rem;
    line-height: 1.4;
  }

  .danger-error {
    color: #ff6666;
    font-size: 0.8rem;
    margin: 0 0 0.5rem;
  }

  .danger-separator {
    border-top: 1px solid #ff444444;
    margin: 0.75rem 0;
  }

  .danger-btn {
    background: #ff4444;
    color: #fff;
  }

  .danger-btn:hover:not(:disabled) {
    background: #cc3333;
  }

  .danger-btn:disabled {
    background: #ff4444;
    opacity: 0.4;
  }
  .warning-btn {
    background: #e67e22;
    color: #fff;
  }
  .warning-btn:hover {
    background: #cf6d17;
  }
  .update-status {
    margin-top: 0.5rem;
    font-size: 0.9rem;
    color: var(--text-muted);
  }
  .update-available {
    color: #2ecc40;
    font-weight: bold;
    text-decoration: none;
  }
  .update-available:hover {
    text-decoration: underline;
  }
  .update-actions {
    margin-top: 0.3rem;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .update-check-meta {
    margin-top: 0.7rem;
    font-size: 0.8rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .check-now-btn {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    cursor: pointer;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg-input, transparent);
    color: var(--text);
    align-self: flex-start;
  }
  .check-now-btn:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .apply-update-btn {
    background: #2ecc40;
    color: var(--accent-text);
    font-weight: bold;
    border-color: #2ecc40;
    margin-left: 0.5rem;
  }
  .apply-update-btn:disabled {
    background: #2ecc40;
    opacity: 0.6;
  }
  .update-error {
    color: #e74c3c;
    font-size: 0.8rem;
    margin-left: 0.5rem;
  }
  .update-custom-repo-warning {
    margin-top: 0.5rem;
    padding: 0.4rem 0.6rem;
    font-size: 0.85rem;
    color: #f39c12;
    border: 1px solid #f39c12;
    border-radius: 4px;
    background: rgba(243, 156, 18, 0.1);
  }
  .update-custom-repo-warning a {
    color: #2ecc40;
  }
  .sha-link {
    font-family: monospace;
    color: var(--accent);
  }
  .session-list {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .session-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg-input);
  }
  .session-current {
    border-color: var(--accent);
  }
  .session-info {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 0;
  }
  .session-label {
    font-size: 0.85rem;
    color: var(--text);
  }
  .session-meta {
    font-size: 0.7rem;
    color: var(--text-dim);
  }
  .session-delete {
    font-size: 0.75rem;
    padding: 0.2rem 0.5rem;
    background: var(--btn-secondary, #333);
    color: var(--text-dim, #888);
    border: 1px solid var(--border, #3a3b3f);
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .session-delete:hover:not(:disabled) {
    background: #ff4444 !important;
    color: #fff;
    border-color: #ff4444;
  }
  .token-url-box {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.5rem;
    align-items: center;
  }
  .token-url-box input {
    flex: 1;
    font-size: 0.75rem !important;
    padding: 0.3rem 0.5rem;
    background: var(--bg-deep, #111);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 3px;
    font-family: monospace;
  }
  .copy-btn {
    font-size: 0.75rem;
    padding: 0.3rem 0.6rem;
    background: var(--btn-secondary);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 3px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .copy-btn:hover {
    background: var(--btn-secondary-hover);
  }
  .copy-btn.copied {
    background: var(--success-color, #2ea043);
    color: #fff;
    border-color: var(--success-color, #2ea043);
  }

  /* mTLS modal */
  .mtls-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    z-index: 100000;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .mtls-modal {
    background: var(--bg-card, var(--bg-primary, #24252b));
    border: 1px solid var(--border, var(--border-color, #3a3b3f));
    border-radius: 8px;
    max-width: 480px;
    width: 90%;
    max-height: 90vh;
    overflow-y: auto;
  }
  .mtls-modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem;
    border-bottom: 1px solid var(--border-color);
  }
  .mtls-modal-header h3 {
    margin: 0;
    font-size: 1rem;
  }
  .mtls-modal-body {
    padding: 1rem;
  }
  .mtls-modal-footer {
    padding: 1rem;
    border-top: 1px solid var(--border-color);
    display: flex;
    justify-content: flex-end;
  }
  .session-revoked {
    opacity: 0.5;
  }
  .auth-badge {
    display: inline-block;
    font-size: 0.6rem;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 0.1rem 0.35rem;
    border-radius: 3px;
    vertical-align: middle;
    margin-right: 0.3rem;
  }
  .auth-badge.mtls {
    background: var(--accent, #00ff88);
    color: #111;
  }
  .auth-badge.cookie {
    background: var(--accent, #00ff88);
    color: #111;
  }
  .mtls-cert-list {
    max-height: 12rem;
    overflow-y: auto;
  }
  .mtls-ca-box {
    margin-top: 0.75rem;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--border, var(--border-color, #3a3b3f));
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .mtls-ca-label {
    font-size: 0.75rem;
    font-weight: bold;
    color: var(--text-dim);
  }
  .mtls-ca-fingerprint {
    font-size: 0.65rem;
    font-family: monospace;
    word-break: break-all;
    color: var(--text-dim);
  }
  .mtls-ca-download {
    font-size: 0.75rem;
    color: var(--accent, #00ff88);
    text-decoration: none;
    margin-top: 0.2rem;
  }
  .mtls-ca-download:hover {
    text-decoration: underline;
  }

  .mtls-revoked-accordion {
    font-size: 0.8rem;
  }
  .mtls-revoked-accordion summary {
    cursor: pointer;
    color: var(--text-dim);
    user-select: none;
  }
  .mtls-revoked-accordion summary:hover {
    color: var(--text);
  }
  .mtls-revoked-accordion .session-list {
    margin-top: 0.4rem;
  }

  /* mTLS radio group */
  .mtls-radio-group {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border, var(--border-color, #3a3b3f));
    border-radius: 6px;
    overflow: hidden;
  }
  .mtls-radio {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.5rem 0.75rem;
    font-size: 0.8rem;
    cursor: pointer;
    margin: 0;
    border-bottom: 1px solid var(--border, var(--border-color, #3a3b3f));
    transition: background 0.15s;
  }
  .mtls-radio:last-child {
    border-bottom: none;
  }
  .mtls-radio:hover {
    background: var(--bg-hover, rgba(255, 255, 255, 0.04));
  }
  .mtls-radio.active {
    background: var(--bg-hover, rgba(255, 255, 255, 0.06));
  }
  .mtls-radio input[type="radio"] {
    margin: 0;
    flex: 0 0 auto;
    width: 14px;
    height: 14px;
    cursor: pointer;
  }
  .mtls-radio span {
    flex: 1;
    min-width: 0;
  }
  .mtls-radio input[type="radio"]:disabled {
    cursor: wait;
  }
</style>
