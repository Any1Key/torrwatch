const SUPPORTED = new Set(["nnmclub.to", "rutracker.org", "kinozal.tv", "kinozal.guru"]);
const COOKIE_ALLOWLIST = {
  "nnmclub.to": new Set(["phpbb2mysql_4_data", "phpbb2mysql_4_sid", "cf_clearance"])
};
const KINOZAL_COOKIE_NAMES = ["uid", "pass", "cf_clearance"];
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
  return { parsed, domain, tab };
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
    const { parsed, domain, tab } = await inspectTab();
    const token = pairingToken.value.trim();
    const base = serverUrl.value.trim().replace(/\/+$/, "");
    if (!token) throw new Error("Введите одноразовый код из TorrWatch.");
    if (!/^https?:\/\//i.test(base)) throw new Error("Адрес TorrWatch должен начинаться с http:// или https://.");
    const serverOrigin = new URL(base).origin;
    const permission = `${serverOrigin}/*`;
    const hasPermission = await chrome.permissions.contains({ origins: [permission] });
    if (!hasPermission && !(await chrome.permissions.request({ origins: [permission] }))) throw new Error("Разрешите расширению доступ к адресу TorrWatch.");

    const allowedCookies = COOKIE_ALLOWLIST[domain];
    const cookieHosts = domain === "kinozal.guru"
      ? ["kinozal.guru", "www.kinozal.guru", "dl.kinozal.guru"]
      : [domain, `www.${domain}`];
    const cookieUrls = [...new Set([parsed.href, ...cookieHosts.map((host) => `https://${host}/`)])];
    const partitionKeys = [...new Set([`https://${parsed.hostname}`, ...cookieHosts.map((host) => `https://${host}`)])].map((topLevelSite) => ({ topLevelSite }));
    const store = tab.cookieStoreId ? { storeId: tab.cookieStoreId } : {};
    const cookieQueries = [
      ...cookieUrls.map((url) => ({ url })),
      ...cookieHosts.flatMap((host) => [{ domain: host }, { domain: `.${host}` }]),
      ...cookieUrls.flatMap((url) => partitionKeys.map((partitionKey) => ({ url, partitionKey })))
    ].map((query) => ({ ...query, ...store }));
    if (domain === "kinozal.guru") {
      cookieQueries.push(
        ...KINOZAL_COOKIE_NAMES.flatMap((name) => [
          ...cookieUrls.map((url) => ({ name, url, ...store })),
          ...cookieHosts.flatMap((host) => [
            { name, domain: host, ...store },
            { name, domain: `.${host}`, ...store }
          ])
        ])
      );
    }
    const cookieSets = await Promise.all(cookieQueries.map(async (query) => {
      try { return await chrome.cookies.getAll(query); } catch { return []; }
    }));
    // Some Chromium builds omit host-only cookies from domain/url queries when
    // the active tab uses a separate cookie store. Read the store once more
    // and keep only cookies belonging to the active tracker domain.
    try {
      const storeCookies = await chrome.cookies.getAll(store);
      const normalisedDomain = domain.replace(/^\./, "");
      cookieSets.push(storeCookies.filter((cookie) => {
        const cookieDomain = String(cookie.domain || "").toLowerCase().replace(/^\./, "");
        return cookieDomain === normalisedDomain || cookieDomain.endsWith(`.${normalisedDomain}`);
      }));
    } catch {}
    const allCookies = [...new Map(cookieSets.flat().map((cookie) => [`${cookie.name}|${cookie.domain}|${cookie.path}`, cookie])).values()];
    const cookies = allowedCookies ? allCookies.filter((cookie) => allowedCookies.has(cookie.name)) : allCookies;
    if (!cookies.length) throw new Error("В текущей вкладке нет нужных Cookie трекера.");
    if (domain === "kinozal.guru") {
      const found = new Set(cookies.map((cookie) => cookie.name));
      const missing = ["uid", "pass"].filter((name) => !found.has(name));
      if (missing.length) throw new Error(`Не найдены Cookie Kinozal: ${missing.join(", ")}. Проверьте домен и профиль браузера.`);
      setStatus(`Найдены Cookie Kinozal: ${[...found].join(", ")}. Передаю…`);
    }
    if (allowedCookies) {
      const found = new Set(cookies.map((cookie) => cookie.name));
      const missing = [...allowedCookies].filter((name) => !found.has(name));
      if (missing.length) throw new Error(`Не найдены Cookie: ${missing.join(", ")}. Откройте NNM-Club в авторизованной вкладке и обновите страницу.`);
      setStatus(`Найдены Cookie: ${[...found].join(", ")}. Передаю…`);
    } else if (domain !== "kinozal.guru") {
      setStatus(`Найдено Cookie Kinozal: ${cookies.length}. Передаю…`);
    }
    const response = await fetch(`${base}/session-import/complete`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-TorrWatch-Extension": "0.1.0" },
      body: JSON.stringify({ token, domain, source_url: parsed.href, user_agent: navigator.userAgent, cookies: cookies.map((cookie) => ({ name: cookie.name, value: cookie.value, domain: cookie.domain, path: cookie.path, secure: cookie.secure, http_only: cookie.httpOnly, expiration_date: cookie.expirationDate || null })) })
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
