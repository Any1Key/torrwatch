const SUPPORTED = new Set(["nnmclub.to", "rutracker.org", "kinozal.tv", "kinozal.guru"]);
const COOKIE_ALLOWLIST = {
  "nnmclub.to": new Set(["phpbb2mysql_4_data", "phpbb2mysql_4_sid", "cf_clearance"])
};
const serverUrl = document.querySelector("#serverUrl");
const pairingToken = document.querySelector("#pairingToken");
const importButton = document.querySelector("#importButton");
const site = document.querySelector("#site");
const status = document.querySelector("#status");

function baseDomain(hostname) {
  const host = hostname.toLowerCase().replace(/^www\./, "");
  return [...SUPPORTED].find((domain) => host === domain || host.endsWith(`.${domain}`)) || null;
}

function setStatus(message, kind = "") {
  status.textContent = message;
  status.className = `status ${kind}`;
}

async function currentTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

async function inspectTab() {
  const tab = await currentTab();
  if (!tab?.url) throw new Error("Не удалось определить активную вкладку.");
  const parsed = new URL(tab.url);
  const domain = baseDomain(parsed.hostname);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("Откройте страницу поддерживаемого HTTPS-трекера.");
  if (!domain) throw new Error("Текущий домен не поддерживается.");
  site.textContent = `Трекер: ${domain}`;
  return { parsed, domain };
}

async function loadSettings() {
  const values = await chrome.storage.local.get(["serverUrl"]);
  serverUrl.value = values.serverUrl || "http://127.0.0.1:8080";
  try { await inspectTab(); } catch (error) { site.textContent = error.message; }
}

async function importSession() {
  importButton.disabled = true;
  setStatus("Получаю данные текущего трекера…");
  try {
    const { parsed, domain } = await inspectTab();
    const token = pairingToken.value.trim();
    const base = serverUrl.value.trim().replace(/\/+$/, "");
    if (!token) throw new Error("Введите одноразовый код из TorrWatch.");
    if (!/^https?:\/\//i.test(base)) throw new Error("Адрес TorrWatch должен начинаться с http:// или https://.");
    const serverOrigin = new URL(base).origin;
    const permission = `${serverOrigin}/*`;
    const hasPermission = await chrome.permissions.contains({ origins: [permission] });
    if (!hasPermission && !(await chrome.permissions.request({ origins: [permission] }))) throw new Error("Разрешите расширению доступ к адресу TorrWatch.");

    const allowedCookies = COOKIE_ALLOWLIST[domain];
    const cookieUrls = [...new Set([parsed.href, `https://${domain}/`, `https://www.${domain}/`])];
    const partitionKeys = [...new Set([`https://${parsed.hostname}`, `https://${domain}`, `https://www.${domain}`])].map((topLevelSite) => ({ topLevelSite }));
    const cookieQueries = [
      ...cookieUrls.map((url) => ({ url })),
      { domain },
      { domain: `.${domain}` },
      ...cookieUrls.flatMap((url) => partitionKeys.map((partitionKey) => ({ url, partitionKey })))
    ];
    const cookieSets = await Promise.all(cookieQueries.map(async (query) => {
      try { return await chrome.cookies.getAll(query); } catch { return []; }
    }));
    const allCookies = [...new Map(cookieSets.flat().map((cookie) => [`${cookie.name}|${cookie.domain}|${cookie.path}`, cookie])).values()];
    const cookies = allowedCookies ? allCookies.filter((cookie) => allowedCookies.has(cookie.name)) : allCookies;
    if (!cookies.length) throw new Error("В текущей вкладке нет нужных Cookie трекера.");
    if (allowedCookies) {
      const found = new Set(cookies.map((cookie) => cookie.name));
      const missing = [...allowedCookies].filter((name) => !found.has(name));
      if (missing.length) throw new Error(`Не найдены Cookie: ${missing.join(", ")}. Откройте NNM-Club в авторизованной вкладке и обновите страницу.`);
      setStatus(`Найдены Cookie: ${[...found].join(", ")}. Передаю…`);
    }
    const response = await fetch(`${base}/session-import/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TorrWatch-Extension": "0.1.0" },
      body: JSON.stringify({ token, domain, source_url: parsed.href, user_agent: navigator.userAgent, cookies: cookies.map((cookie) => ({ name: cookie.name, value: cookie.value, path: cookie.path, secure: cookie.secure, http_only: cookie.httpOnly, expiration_date: cookie.expirationDate || null })) })
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || `Сервер ответил HTTP ${response.status}.`);
    await chrome.storage.local.set({ serverUrl: base });
    pairingToken.value = "";
    setStatus(payload.message || "Сессия передана и сохранена.", "success");
    await chrome.tabs.create({ url: `${base}/configure/sessions?plugin=${encodeURIComponent(payload.plugin)}` });
  } catch (error) {
    setStatus(error.message || "Не удалось передать сессию.", "error");
  } finally { importButton.disabled = false; }
}

importButton.addEventListener("click", importSession);
loadSettings();
