const { app, BrowserWindow, globalShortcut, session } = require("electron");
const path = require("path");

// Configure via environment or CLI: GUIDEBOOK_SSB_HOST, GUIDEBOOK_SSB_PORT
// CLI: guidebook-ssb --host 192.168.1.50 --port 8443
const args = parseArgs(process.argv.slice(2));
const HOST = args.host || process.env.GUIDEBOOK_SSB_HOST || "127.0.0.1";
const PORT = parseInt(args.port || process.env.GUIDEBOOK_SSB_PORT || "4280", 10);
const ALLOWED_ORIGIN = `https://${HOST}:${PORT}`;
const START_URL = `${ALLOWED_ORIGIN}/`;

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--host" && argv[i + 1]) result.host = argv[++i];
    else if (argv[i] === "--port" && argv[i + 1]) result.port = argv[++i];
  }
  return result;
}

function isAllowedURL(url) {
  return url.startsWith(ALLOWED_ORIGIN + "/") || url === ALLOWED_ORIGIN;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 900,
    frame: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webgl: true,
      // Allow self-signed cert on localhost
      webSecurity: true,
    },
  });

  // Remove application menu entirely
  win.setMenu(null);

  // Block navigation to disallowed URLs
  win.webContents.on("will-navigate", (event, url) => {
    if (!isAllowedURL(url)) {
      event.preventDefault();
    }
  });

  // Block new windows / window.open to disallowed URLs
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedURL(url)) {
      return { action: "allow", overrideBrowserWindowOptions: { frame: false } };
    }
    return { action: "deny" };
  });

  // Trust the self-signed cert on the configured host
  session.defaultSession.setCertificateVerifyProc((request, callback) => {
    if (request.hostname === HOST && request.port === PORT) {
      callback(0); // Trust
    } else {
      callback(-2); // Use default verification (reject if invalid)
    }
  });

  win.loadURL(START_URL);
}

// Disable default keyboard shortcuts
function blockShortcuts() {
  const blocked = [
    "CommandOrControl+R",
    "CommandOrControl+Shift+R",
    "CommandOrControl+L",
    "CommandOrControl+T",
    "CommandOrControl+N",
    "CommandOrControl+W",
    "CommandOrControl+Shift+I",
    "CommandOrControl+Shift+J",
    "F5",
    "F11",
    "F12",
    "Alt+Left",
    "Alt+Right",
    "Alt+Home",
    "CommandOrControl+H",
    "CommandOrControl+J",
    "CommandOrControl+G",
    "CommandOrControl+P",
    "CommandOrControl+U",
    "CommandOrControl+Shift+Delete",
  ];

  for (const key of blocked) {
    globalShortcut.register(key, () => {
      // Swallow the shortcut
    });
  }
}

app.whenReady().then(() => {
  blockShortcuts();
  createWindow();
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  app.quit();
});
