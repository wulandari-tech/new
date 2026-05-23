# ═══════════════════════════════════════════════════════════
# 🌐 IvaSms Client — Playwright Async API (native asyncio)
#    Tidak ada konflik dengan Telegram bot event loop
# ═══════════════════════════════════════════════════════════

import json
import logging
import os
import subprocess
import time
import asyncio
import html
import re
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
from bs4 import BeautifulSoup
_RUNTIME_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "IvaSms-api")
if _RUNTIME_CONFIG_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_CONFIG_DIR)

from runtime_config import (
    IVASMS_EMAIL,
    IVASMS_PASSWORD,
    IVASMS_CREDENTIALS_FILE,
    IVASMS_COOKIES_FILE,
    IVASMS_STATE_DIR,
)

logger = logging.getLogger(__name__)


IVAS_NODE_BRIDGE_SCRIPT = r"""
const https = require("https");
const zlib = require("zlib");

const payload = JSON.parse(process.env.IVAS_NODE_BRIDGE_PAYLOAD || "{}");
const BASE_URL = payload.base_url || "https://www.ivasms.com";
let COOKIES = payload.cookies || {};
const USER_AGENT = payload.user_agent || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36";

function cookieString() {
  return Object.entries(COOKIES).map(([k, v]) => `${k}=${v}`).join("; ");
}

function getXsrf() {
  try { return decodeURIComponent(COOKIES["XSRF-TOKEN"] || ""); }
  catch { return COOKIES["XSRF-TOKEN"] || ""; }
}

function clean(text) {
  return (text || "")
    .replace(/<[^>]+>/g, "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&#039;/g, "'")
    .replace(/\s+/g, " ")
    .trim();
}

function makeRequest(method, path, body, contentType, extraHeaders = {}) {
  return new Promise((resolve, reject) => {
    const headers = {
      "User-Agent": USER_AGENT,
      "Accept": "*/*",
      "Accept-Encoding": "gzip, deflate, br",
      "Accept-Language": "en-PK,en;q=0.9",
      "Cookie": cookieString(),
      "X-Requested-With": "XMLHttpRequest",
      "X-XSRF-TOKEN": getXsrf(),
      "X-CSRF-TOKEN": getXsrf(),
      "Origin": BASE_URL,
      "Referer": `${BASE_URL}/portal/sms/received`,
      ...extraHeaders,
    };

    if (method === "POST" && body) {
      headers["Content-Type"] = contentType;
      headers["Content-Length"] = Buffer.byteLength(body);
    }

    const req = https.request(BASE_URL + path, { method, headers }, res => {
      if (res.headers["set-cookie"]) {
        res.headers["set-cookie"].forEach(c => {
          const sc = c.split(";")[0];
          const ki = sc.indexOf("=");
          if (ki > -1) {
            const k = sc.substring(0, ki).trim();
            const v = sc.substring(ki + 1).trim();
            if (k === "XSRF-TOKEN" || k === "ivas_sms_session") {
              COOKIES[k] = v;
            }
          }
        });
      }

      const chunks = [];
      res.on("data", d => chunks.push(d));
      res.on("end", () => {
        let buf = Buffer.concat(chunks);
        try {
          const enc = res.headers["content-encoding"];
          if (enc === "gzip") buf = zlib.gunzipSync(buf);
          else if (enc === "br") buf = zlib.brotliDecompressSync(buf);
          else if (enc === "deflate") buf = zlib.inflateSync(buf);
        } catch {}

        const text = buf.toString("utf-8");
        if (res.statusCode === 401 || res.statusCode === 419 || text.includes('"message":"Unauthenticated"')) {
          return reject(new Error("SESSION_EXPIRED"));
        }
        if (res.statusCode >= 400) {
          return reject(new Error(`HTTP ${res.statusCode} for ${path}: ${text.substring(0, 200)}`));
        }
        resolve({ status: res.statusCode, body: text, cookies: COOKIES });
      });
    });

    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

async function fetchToken() {
  const resp = await makeRequest("GET", "/portal/sms/received", null, null, {
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Referer": `${BASE_URL}/portal/sms/received`,
  });
  const match = resp.body.match(/name="_token"\s+value="([^"]+)"/) ||
                resp.body.match(/"csrf-token"\s+content="([^"]+)"/);
  return match ? match[1] : null;
}

async function getRanges(token, fromDate, toDate) {
  const boundary = "----WebKitFormBoundary6I2Js7TBhcJuwIqw";
  const parts = [
    `--${boundary}\r\nContent-Disposition: form-data; name="from"\r\n\r\n${fromDate}`,
    `--${boundary}\r\nContent-Disposition: form-data; name="to"\r\n\r\n${toDate}`,
    `--${boundary}\r\nContent-Disposition: form-data; name="_token"\r\n\r\n${token}`,
    `--${boundary}--`,
  ].join("\r\n");

  const resp = await makeRequest(
    "POST",
    "/portal/sms/received/getsms",
    parts,
    `multipart/form-data; boundary=${boundary}`,
    { "Referer": `${BASE_URL}/portal/sms/received`, "Accept": "text/html, */*; q=0.01" }
  );

  const ranges = [...resp.body.matchAll(/toggleRange\('([^']+)'/g)].map(m => m[1]);
  return { ranges, html: resp.body, cookies: COOKIES };
}

async function getNumbers(token) {
  const ts = Date.now();
  const path = `/portal/numbers?draw=1`
    + `&columns[0][data]=number_id&columns[0][name]=id&columns[0][orderable]=false`
    + `&columns[1][data]=Number`
    + `&columns[2][data]=range`
    + `&columns[3][data]=A2P`
    + `&columns[4][data]=LimitA2P`
    + `&columns[5][data]=limit_cli_a2p`
    + `&columns[6][data]=limit_cli_did_a2p`
    + `&columns[7][data]=action&columns[7][searchable]=false&columns[7][orderable]=false`
    + `&order[0][column]=1&order[0][dir]=desc`
    + `&start=0&length=5000&search[value]=&_=${ts}`;

  const resp = await makeRequest("GET", path, null, null, {
    "Referer": `${BASE_URL}/portal/numbers`,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-CSRF-TOKEN": token,
  });

  let json;
  try { json = JSON.parse(resp.body); } catch { json = {}; }
  const data = Array.isArray(json.data) ? json.data : [];
  return {
    sEcho: 2,
    iTotalRecords: String(json.recordsTotal || data.length),
    iTotalDisplayRecords: String(json.recordsFiltered || data.length),
    aaData: data.map(row => [row.range || "", "", String(row.Number || ""), "Weekly", ""]),
    cookies: COOKIES,
  };
}

function parseSMSRows(html, range, number, date) {
  const rows = [];
  const trAll = [...html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)];
  for (const trM of trAll) {
    const row = trM[1];
    if (row.includes("<th")) continue;
    const senderM = row.match(/class="cli-tag"[^>]*>([^<]+)</);
    const msgM = row.match(/class="msg-text"[^>]*>([\s\S]*?)<\/div>/i);
    const timeM = row.match(/class="time-cell"[^>]*>\s*([0-9:]+)\s*</);
    const message = msgM ? clean(msgM[1]) : "";
    if (!message) continue;
    rows.push({
      date: `${date} ${timeM ? timeM[1].trim() : "00:00:00"}`,
      range,
      number: String(number),
      sender: senderM ? senderM[1].trim() : "SMS",
      message,
      currency: "$",
      amount: 0,
    });
  }
  return rows;
}

async function getSMS(token, fromDate, toDate, rawOptions) {
  const { ranges } = await getRanges(token, fromDate, toDate);
  const allRows = [];
  let rawHtml = "";
  let rawRange = rawOptions?.range || "";
  let rawNumber = rawOptions?.number || "";

  for (const range of ranges) {
    const body = new URLSearchParams({ _token: token, start: fromDate, end: toDate, range }).toString();
    const r2 = await makeRequest(
      "POST",
      "/portal/sms/received/getsms/number",
      body,
      "application/x-www-form-urlencoded",
      { "Referer": `${BASE_URL}/portal/sms/received`, "Accept": "text/html, */*; q=0.01" }
    );

    const numbers = [...r2.body.matchAll(/toggleNum[^(]+\('(\d+)'/g)].map(m => m[1]);
    for (const number of numbers) {
      const b3 = new URLSearchParams({ _token: token, start: fromDate, end: toDate, Number: number, Range: range }).toString();
      const r3 = await makeRequest(
        "POST",
        "/portal/sms/received/getsms/number/sms",
        b3,
        "application/x-www-form-urlencoded",
        { "Referer": `${BASE_URL}/portal/sms/received`, "Accept": "text/html, */*; q=0.01" }
      );

      if (!rawHtml && (!rawRange || rawRange === range) && (!rawNumber || rawNumber === String(number))) {
        rawHtml = r3.body;
        rawRange = range;
        rawNumber = String(number);
      }

      allRows.push(...parseSMSRows(r3.body, range, number, fromDate));
    }
  }

  return { rows: allRows, ranges, rawHtml, rawRange, rawNumber, cookies: COOKIES };
}

async function main() {
  const action = payload.action;
  const fromDate = payload.from_date || new Date().toISOString().slice(0, 10);
  const toDate = payload.to_date || fromDate;
  const token = await fetchToken();

  if (action === "status") {
    return { ok: !!token, active: !!token, token, cookies: COOKIES };
  }
  if (!token) throw new Error("SESSION_EXPIRED");
  if (action === "numbers") return await getNumbers(token);
  if (action === "ranges") return await getRanges(token, fromDate, toDate);
  if (action === "sms") return await getSMS(token, fromDate, toDate, payload.raw_options || null);
  if (action === "raw_sms") return await getSMS(token, fromDate, toDate, payload.raw_options || null);
  throw new Error(`Unknown action: ${action}`);
}

main()
  .then(result => process.stdout.write(JSON.stringify({ ok: true, result, cookies: COOKIES })))
  .catch(error => process.stdout.write(JSON.stringify({ ok: false, error: String(error && error.message ? error.message : error), cookies: COOKIES })));
"""


class IVASSMSClient:
    """IvaSms client — Playwright Async, bypass Cloudflare dengan stealth."""

    def __init__(self):
        self.base_url = os.getenv("IVASMS_BASE_URL", "https://www.ivasms.com")
        self._base_urls = self._build_base_urls()
        self.logged_in = False
        self.csrf_token = ""
        self._playwright = None
        self._browser = None
        self._page = None
        self._context = None
        self._cookies_file = IVASMS_COOKIES_FILE
        self._display = None
        self._browser_profile_dir = os.getenv(
            "IVASMS_BROWSER_PROFILE_DIR",
            os.path.join(os.getcwd(), ".ivasms-browser-profile"),
        )
        self._session_lock = asyncio.Lock()
        self._state_dir = Path(IVASMS_STATE_DIR)
        self._credentials_file = Path(IVASMS_CREDENTIALS_FILE)
        self._http_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116.0.0.0 Safari/537.36"
        )
        self._prefer_browser_fetch_until = 0.0

    def _ensure_state_dir(self):
        self._state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def cookies_path(self):
        return Path(self._cookies_file)

    @property
    def credentials_path(self):
        return self._credentials_file

    def _is_windows(self):
        return os.name == "nt"

    def _build_base_urls(self):
        preferred = self.base_url.rstrip("/")
        variants = [
            preferred,
            "https://ivasms.com",
            "https://www.ivasms.com",
        ]
        seen = set()
        result = []
        for url in variants:
            if url and url not in seen:
                seen.add(url)
                result.append(url)
        return result

    def _is_sms_received_url(self, url):
        return "/portal/sms/received" in str(url or "")

    def _is_portal_like_url(self, url):
        url = str(url or "")
        return "/portal" in url or "/dashboard" in url

    def _parse_range_names_html(self, html_text):
        if not html_text:
            return []
        ranges = [match.group(1).strip() for match in re.finditer(r"toggleRange\('([^']+)'", html_text)]
        return list(dict.fromkeys([range_name for range_name in ranges if range_name]))

    def _parse_number_details_html(self, html_text):
        if not html_text:
            return []
        soup = BeautifulSoup(html_text, "html.parser")
        number_details = []
        seen_numbers = set()
        items = soup.select("div.card.card-body")
        if not items:
            items = soup.select("div.nrow")
        for item in items:
            phone_node = item.select_one(".col-sm-4") or item.select_one(".nnum")
            if not phone_node:
                continue
            phone_number = self._strip_html(phone_node.get_text(" ", strip=True))
            if not phone_number or phone_number in seen_numbers:
                continue
            seen_numbers.add(phone_number)
            count_node = item.select_one(".col-3:nth-child(2) p") or item.select_one(".v-count")
            paid_node = item.select_one(".col-3:nth-child(3) p") or item.select_one(".v-paid")
            unpaid_node = item.select_one(".col-3:nth-child(4) p") or item.select_one(".v-unpaid")
            revenue_node = item.select_one(".col-3:nth-child(5) p span.currency_cdr") or item.select_one(".v-rev")
            onclick = item.get("onclick", "") or phone_node.get("onclick", "") or ""
            id_match = re.search(r"\(\s*'([^']+)'", onclick)
            number_details.append(
                {
                    "phone_number": phone_number,
                    "count": self._strip_html(count_node.get_text(" ", strip=True)) if count_node else "0",
                    "paid": self._strip_html(paid_node.get_text(" ", strip=True)) if paid_node else "0",
                    "unpaid": self._strip_html(unpaid_node.get_text(" ", strip=True)) if unpaid_node else "0",
                    "revenue": self._strip_html(revenue_node.get_text(" ", strip=True)) if revenue_node else "0",
                    "id_number": id_match.group(1).strip() if id_match else "",
                }
            )
        return number_details

    def _parse_otp_message_html(self, html_text):
        if not html_text:
            return ""
        soup = BeautifulSoup(html_text, "html.parser")
        node = soup.select_one(".col-9.col-sm-6 p") or soup.select_one(".msg-text") or soup.select_one("p")
        return self._strip_html(node.get_text(" ", strip=True)) if node else ""

    def _build_browser_fingerprint(self):
        chrome_version = os.getenv("IVASMS_CHROME_VERSION", "136.0.0.0")
        if self._is_windows():
            return {
                "viewport": {"width": 1440, "height": 900},
                "user_agent": (
                    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{chrome_version} Safari/537.36"
                ),
                "platform": "Win32",
                "sec_ch_ua_platform": "Windows",
            }

        return {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                f"Mozilla/5.0 (X11; Linux x86_64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{chrome_version} Safari/537.36"
            ),
            "platform": "Linux x86_64",
            "sec_ch_ua_platform": "Linux",
        }

    async def _load_browser_engine(self):
        from playwright.async_api import async_playwright

        logger.info("Using bundled Playwright Chromium engine")
        return async_playwright, "playwright"

    async def _ensure_playwright_chromium(self):
        logger.warning("Playwright Chromium missing. Installing browser runtime...")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if stdout:
            logger.info(stdout.decode("utf-8", errors="replace").strip())
        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            if error_text:
                logger.error(error_text)
            raise RuntimeError(
                f"Failed to install Playwright Chromium automatically (exit={process.returncode})"
            )
        logger.info("Playwright Chromium installed successfully")

    async def _ensure_playwright_linux_deps(self):
        if self._is_windows():
            return
        logger.warning("Playwright Linux dependencies missing. Installing system packages...")
        install_cmd = [
            "apt-get",
            "update",
        ]
        process = await asyncio.create_subprocess_exec(
            *install_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                "Failed to run `apt-get update` for Playwright dependencies: "
                + stderr.decode("utf-8", errors="replace").strip()
            )

        package_cmd = [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "libglib2.0-0",
            "libnss3",
            "libnspr4",
            "libdbus-1-3",
            "libatk1.0-0",
            "libatk-bridge2.0-0",
            "libcups2",
            "libdrm2",
            "libxkbcommon0",
            "libatspi2.0-0",
            "libxcomposite1",
            "libxdamage1",
            "libxfixes3",
            "libxrandr2",
            "libgbm1",
            "libasound2",
            "libpango-1.0-0",
            "libcairo2",
            "libx11-6",
            "libxcb1",
            "libxext6",
            "libxshmfence1",
            "libxcursor1",
            "libxi6",
            "libxtst6",
            "libxrender1",
            "libfontconfig1",
            "libfreetype6",
            "ca-certificates",
            "fonts-liberation",
        ]
        process = await asyncio.create_subprocess_exec(
            *package_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if stdout:
            logger.info(stdout.decode("utf-8", errors="replace").strip())
        if process.returncode != 0:
            raise RuntimeError(
                "Failed to install Linux packages for Playwright: "
                + stderr.decode("utf-8", errors="replace").strip()
            )
        logger.info("Playwright Linux dependencies installed successfully")

    # ────────── Browser Management (Async) ──────────

    async def _start_browser(self, force_headless=None):
        """Start browser with bundled Chromium only."""
        if self._browser:
            await self._close_browser()

        async_playwright, engine_name = await self._load_browser_engine()
        self._playwright = await async_playwright().start()

        xvfb_active = False
        if not self._is_windows():
            try:
                from pyvirtualdisplay import Display
                logger.info("Starting virtual display (Xvfb)...")
                self._display = Display(visible=0, size=(1440, 900))
                self._display.start()
                xvfb_active = True
                logger.info(f"Xvfb started. DISPLAY={os.environ.get('DISPLAY')}")
            except Exception as e:
                logger.warning(f"Xvfb unavailable, continuing: {e}")

        if force_headless is not None:
            use_headless = force_headless
            logger.info(f"Running browser with forced headless={use_headless}")
        else:
            default_headless = "1"
            use_headless = os.getenv("IVASMS_HEADLESS", default_headless) == "1"
            logger.info(f"Running browser with headless={use_headless}")

        fingerprint = self._build_browser_fingerprint()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-infobars",
            "--lang=en-US",
            "--start-maximized",
            "--window-size=1440,900",
        ]
        if not self._is_windows():
            launch_args.extend(
                [
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                ]
            )

        launch_kwargs = {
            "headless": use_headless,
            "args": launch_args,
        }
        logger.info("Starting bundled Chromium without local browser path or channel")

        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" in message:
                await self._ensure_playwright_chromium()
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            elif "error while loading shared libraries" in message or "libglib-2.0.so.0" in message:
                await self._ensure_playwright_linux_deps()
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)
            else:
                raise
        self._context = await self._browser.new_context(
            viewport=fingerprint["viewport"],
            locale="en-US",
            timezone_id="Asia/Jakarta",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        self._page = await self._context.new_page()
        logger.info("Browser context ready")
        return True

    async def _close_browser(self):
        """Close browser cleanly."""
        for obj, method in [
            (self._page, "close"),
            (self._context, "close"),
            (self._browser, "close"),
            (self._playwright, "stop"),
        ]:
            if obj:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass
        if self._display:
            try:
                self._display.stop()
                logger.info("🖥️ Virtual display stopped")
            except Exception:
                pass
            self._display = None
        self.logged_in = False
        self.csrf_token = ""
        self._page = self._context = self._browser = self._playwright = None

    async def _wait_for_cf(self, timeout_s=90):
        """Tunggu Cloudflare challenge selesai (async) dengan auto-click Turnstile."""
        logger.info(f"⏳ Menunggu CF challenge (max {timeout_s}s)...")
        for i in range(timeout_s):
            if not self._page:
                logger.warning("⚠️ CF wait aborted because page is no longer available")
                return False
            title = await self._page.title()
            if "Just a moment" not in title and "Cloudflare" not in title:
                logger.info(f"✅ CF bypass {i}s — {title[:50]}")
                return True

            # Auto-click Turnstile checkbox if visible
            try:
                iframe = await self._page.query_selector("iframe[src*='challenges.cloudflare.com']")
                if iframe:
                    box = await iframe.bounding_box()
                    if box:
                        logger.debug("Cloudflare Turnstile detected, auto-clicking checkbox")
                        # Try slightly different offsets to make click reliable (30px, 40px, 50px)
                        offset_x = 35 + (i % 3) * 10
                        offset_y = box['height'] / 2
                        await self._page.mouse.click(box['x'] + offset_x, box['y'] + offset_y)
                        logger.debug(f"Clicked inside Turnstile iframe (offset: x={offset_x}, y={offset_y})")
                        await asyncio.sleep(2.0)
            except Exception as e:
                logger.warning(f"⚠️ Turnstile click skip: {e}")

            if i % 20 == 0 and i > 0:
                logger.debug(f"CF still active... {i}s")
            await asyncio.sleep(1)
        logger.warning(f"⚠️ CF aktif setelah {timeout_s}s")
        return False

    async def _wait_for_login_result(self, timeout_s=180):
        """Wait for post-login redirect without forcing reloads while Cloudflare is still working."""

        logger.info(f"⏳ Menunggu hasil login (max {timeout_s}s)...")
        for i in range(timeout_s):
            if not self._page:
                logger.warning("⚠️ Login result wait aborted because page is no longer available")
                return False
            url = self._page.url
            title = await self._page.title()

            if self._is_portal_like_url(url):
                logger.info(f"✅ Login redirect detected after {i}s: {url}")
                return True

            if i % 15 == 0:
                logger.info(f"  ⏳ Login still pending... {i}s | {title[:60]} | {url}")

            await asyncio.sleep(1)

        logger.warning(f"⚠️ Login belum redirect setelah {timeout_s}s")
        return False

    async def _click_turnstile_widget(self):
        """Click the inline Cloudflare Turnstile widget if it is visible on the login form."""
        try:
            iframe = await self._page.query_selector("iframe[src*='challenges.cloudflare.com']")
            if not iframe:
                return False

            box = await iframe.bounding_box()
            if not box:
                return False

            offset_x = min(max(box["width"] * 0.22, 28), box["width"] - 8)
            offset_y = box["height"] / 2
            await self._page.mouse.click(box["x"] + offset_x, box["y"] + offset_y)
            logger.info(f"👉 Clicked Turnstile widget at x={offset_x:.1f}, y={offset_y:.1f}")
            return True
        except Exception as exc:
            logger.warning(f"⚠️ Turnstile widget click failed: {exc}")
            return False

    async def _read_login_form_state(self):
        """Read lightweight login/turnstile state from the page."""
        return await self._page.evaluate(
            """
            () => {
                const bodyText = (document.body?.innerText || "").toLowerCase();
                const email = document.querySelector("input[type='email'], input[name='email']");
                const password = document.querySelector("input[type='password'], input[name='password']");
                const cfResponse = document.querySelector("input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response']");
                return {
                    url: window.location.href,
                    bodyText,
                    emailValue: email ? email.value : "",
                    passwordValue: password ? password.value : "",
                    hasVerificationError: bodyText.includes("security verification failed"),
                    isVerifying: bodyText.includes("verifying..."),
                    hasTurnstileToken: !!(cfResponse && cfResponse.value && cfResponse.value.trim()),
                };
            }
            """
        )

    async def _ensure_login_credentials(self, email, password):
        """Refill login fields when Cloudflare or validation clears them."""
        email_selector = "input[type='email'], input[name='email']"
        password_selector = "input[type='password'], input[name='password']"

        await self._page.wait_for_selector(email_selector, timeout=30000, state="visible")
        await self._page.wait_for_selector(password_selector, timeout=30000, state="visible")
        state = await self._read_login_form_state()

        if state.get("emailValue") != email:
            await self._page.click(email_selector)
            await self._page.press(email_selector, "Control+A")
            await self._page.press(email_selector, "Backspace")
            await self._page.type(email_selector, email, delay=60)

        if state.get("passwordValue") != password:
            await self._page.click(password_selector)
            await self._page.press(password_selector, "Control+A")
            await self._page.press(password_selector, "Backspace")
            await self._page.type(password_selector, password, delay=60)

        verified = await self._read_login_form_state()
        logger.info(
            "Login form state after fill: email_set=%s password_set=%s",
            verified.get("emailValue") == email,
            verified.get("passwordValue") == password,
        )

    async def _dismiss_portal_overlays(self):
        """Close guided tours / modals in portal pages so background scraping stays stable."""
        try:
            await self._page.keyboard.press("Escape")
        except Exception:
            pass

        scripts = [
            """
            () => {
                const wanted = ["done", "close", "skip", "ok", "got it", "finish"];
                const overlayRoots = Array.from(
                    document.querySelectorAll(
                        ".swal2-container, .modal.show, .modal.in, .introjs-overlay, .introjs-tooltipReferenceLayer, .introjs-tooltipbuttons"
                    )
                );
                const roots = overlayRoots.length ? overlayRoots : [];
                let clicked = 0;
                for (const root of roots) {
                    const buttons = Array.from(root.querySelectorAll("button, a, [role='button']"));
                    for (const button of buttons) {
                        const text = (button.innerText || button.textContent || "").trim().toLowerCase();
                        const rect = button.getBoundingClientRect();
                        const visible = rect.width > 0 && rect.height > 0;
                        if (visible && wanted.some(label => text === label || text.includes(label))) {
                            button.click();
                            clicked += 1;
                        }
                    }
                }
                return clicked;
            }
            """,
            """
            () => {
                const selectors = [
                    ".introjs-skipbutton",
                    ".introjs-donebutton",
                    ".modal [data-dismiss='modal']",
                    ".modal .close",
                    ".swal2-confirm",
                    ".swal2-close"
                ];
                let clicked = 0;
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        const rect = node.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            node.click();
                            clicked += 1;
                        }
                    }
                }
                return clicked;
            }
            """,
        ]

        for script in scripts:
            try:
                clicked = await self._page.evaluate(script)
                if clicked:
                    logger.info(f"Closed {clicked} portal overlay button(s)")
            except Exception as exc:
                logger.warning(f"Overlay dismiss skipped: {exc}")

    async def _ensure_sms_received_page(self, timeout=30000):
        if not self._page:
            return False
        if not self._is_sms_received_url(self._page.url):
            await self._goto_ivasms(
                "/portal/sms/received",
                timeout=timeout,
                wait_until="domcontentloaded",
                retries=1,
            )
            await self._wait_for_cf(30)
        await self._dismiss_portal_overlays()
        csrf_ready = await self._extract_csrf()
        if not csrf_ready:
            try:
                status_result = await self._run_node_bridge("status")
                csrf_ready = bool(status_result.get("token"))
                if csrf_ready:
                    self.csrf_token = status_result["token"]
            except Exception:
                csrf_ready = False
        self.logged_in = bool(csrf_ready and self._is_sms_received_url(self._page.url))
        return self.logged_in

    async def _bootstrap_headless_session_from_cookies(self):
        """Restart the browser in headless mode using saved cookies for runtime OTP polling."""
        try:
            await self._close_browser()
            await self._start_browser(force_headless=True)
            if not await self._load_cookies():
                logger.warning("Unable to reload cookies into headless session")
                return False

            await self._goto_ivasms("/portal/sms/received", timeout=30000, wait_until="domcontentloaded")
            await self._wait_for_cf(30)
            await self._dismiss_portal_overlays()
            csrf_ready = await self._extract_csrf()
            if not csrf_ready:
                try:
                    status_result = await self._run_node_bridge("status")
                    csrf_ready = bool(status_result.get("token"))
                    if csrf_ready:
                        self.csrf_token = status_result["token"]
                except Exception:
                    csrf_ready = False
            self.logged_in = bool(
                csrf_ready and self._is_sms_received_url(self._page.url)
            )
            logger.info(f"Headless runtime session ready={self.logged_in}")
            return self.logged_in
        except Exception as e:
            logger.error(f"Headless runtime bootstrap failed: {e}")
            return False

    async def _wait_for_turnstile_ready(self, timeout_s=180):
        """Wait until the inline Turnstile widget stops verifying and provides a token."""
        logger.info(f"⏳ Menunggu Turnstile login siap (max {timeout_s}s)...")
        last_click_at = -999
        for i in range(timeout_s):
            if not self._page:
                logger.warning("⚠️ Turnstile wait aborted because page is no longer available")
                return False
            title = await self._page.title()
            if "Just a moment" in title or "Cloudflare" in title:
                await self._wait_for_cf(min(30, timeout_s - i if timeout_s - i > 0 else 1))

            state = await self._read_login_form_state()
            if self._is_portal_like_url(state["url"]):
                return True

            if state["hasTurnstileToken"] and not state["isVerifying"] and not state["hasVerificationError"]:
                logger.info(f"✅ Turnstile token ready after {i}s")
                return True

            if state["hasVerificationError"] and i - last_click_at >= 5:
                logger.warning("⚠️ Security verification failed. Re-clicking Turnstile and waiting.")
                if await self._click_turnstile_widget():
                    last_click_at = i

            elif not state["isVerifying"] and i - last_click_at >= 8:
                if await self._click_turnstile_widget():
                    last_click_at = i

            if i % 15 == 0:
                logger.info(
                    f"  ⏳ Turnstile pending... {i}s | verifying={state['isVerifying']} "
                    f"| error={state['hasVerificationError']} | token={state['hasTurnstileToken']}"
                )

            await asyncio.sleep(1)

        logger.warning(f"⚠️ Turnstile belum siap setelah {timeout_s}s")
        return False

    async def _extract_csrf(self):
        """Extract CSRF token."""
        try:
            token = await self._page.eval_on_selector(
                "input[name='_token']", "el => el.value"
            )
            if token:
                self.csrf_token = token
                logger.info("🔑 CSRF token OK")
                return True
        except Exception:
            pass
        try:
            token = await self._page.eval_on_selector(
                "meta[name='csrf-token']", "el => el.getAttribute('content')"
            )
            if token:
                self.csrf_token = token
                logger.info("🔑 CSRF token OK (meta)")
                return True
        except Exception:
            pass
        logger.warning("⚠️ CSRF token tidak ditemukan")
        return False

    async def _goto_ivasms(self, path, timeout=30000, wait_until="domcontentloaded", retries=2):
        """Navigate using root/www fallback and retry on transient browser/network failures."""
        import asyncio

        last_error = None
        normalized_path = path if path.startswith("/") else f"/{path}"
        for attempt in range(retries + 1):
            for base_url in self._base_urls:
                target = f"{base_url}{normalized_path}"
                try:
                    logger.info(f"Navigating to {target} (attempt {attempt + 1})")
                    response = await self._page.goto(
                        target,
                        timeout=timeout,
                        wait_until=wait_until,
                    )
                    self.base_url = base_url
                    return response
                except Exception as exc:
                    last_error = exc
                    logger.warning(f"Navigation failed for {target}: {exc}")

            if attempt < retries:
                await asyncio.sleep(2)

        if last_error:
            raise last_error
        raise RuntimeError(f"Unable to navigate to IvaSms path: {normalized_path}")

    async def _save_cookies(self):
        """Save cookies ke file (Playwright format)."""
        try:
            self._ensure_state_dir()
            cookies = await self._context.cookies()
            # Normalize sameSite values for Playwright
            for c in cookies:
                if c.get('sameSite') not in ('Strict', 'Lax', 'None'):
                    c['sameSite'] = 'Lax'
            with open(self._cookies_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"💾 Saved {len(cookies)} cookies")
        except Exception as e:
            logger.error(f"❌ Save cookies error: {e}")

    def save_credentials(self, email, password):
        """Persist login credentials for autonomous session recovery."""
        try:
            self._ensure_state_dir()
            with open(self._credentials_file, "w", encoding="utf-8") as f:
                json.dump({"email": email, "password": password}, f, indent=2)
            logger.info(f"Saved IvaSms credentials to {self._credentials_file}")
            return True
        except Exception as e:
            logger.error(f"❌ Save credentials error: {e}")
            return False

    def _load_cookie_map(self):
        if not os.path.exists(self._cookies_file):
            return {}
        try:
            with open(self._cookies_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            if not isinstance(cookies, list):
                return {}
            cookie_map = {}
            for item in cookies:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
                if name and value:
                    cookie_map[name] = value
            return cookie_map
        except Exception as exc:
            logger.warning(f"⚠️ Failed to read cookie map: {exc}")
            return {}

    def _cookie_header(self, cookie_map):
        return "; ".join(f"{name}={value}" for name, value in cookie_map.items())

    def _save_cookie_map(self, cookie_map):
        self._ensure_state_dir()
        cookies = [
            {
                "name": name,
                "value": value,
                "domain": ".ivasms.com",
                "path": "/",
                "secure": True,
                "httpOnly": name == "ivas_sms_session",
                "sameSite": "Lax",
            }
            for name, value in cookie_map.items()
            if name and value
        ]
        with open(self._cookies_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)

    def _update_cookies_from_response_headers(self, headers):
        if not headers:
            return
        set_cookie_headers = []
        try:
            set_cookie_headers = headers.get_all("Set-Cookie") or []
        except Exception:
            single_cookie = headers.get("Set-Cookie") if hasattr(headers, "get") else None
            if single_cookie:
                set_cookie_headers = [single_cookie]

        if not set_cookie_headers:
            return

        cookie_map = self._load_cookie_map()
        changed = False
        for cookie_header in set_cookie_headers:
            head = str(cookie_header).split(";", 1)[0].strip()
            if "=" not in head:
                continue
            name, value = head.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name in {"XSRF-TOKEN", "ivas_sms_session"} and value:
                if cookie_map.get(name) != value:
                    cookie_map[name] = value
                    changed = True

        if changed:
            self._save_cookie_map(cookie_map)
            logger.info("Updated saved cookies from HTTP response headers")

    def _is_session_expired_response(self, status_code, response_text):
        body = (response_text or "").lower()
        return (
            status_code in {401, 419}
            or '"message":"unauthenticated"' in body
            or "unauthenticated" in body
        )

    def _run_node_bridge_sync(self, action, **payload):
        cookie_map = self._load_cookie_map()
        if not cookie_map:
            raise RuntimeError("No saved cookies available for HTTP scraper")

        bridge_payload = {
            "action": action,
            "base_url": "https://www.ivasms.com",
            "cookies": cookie_map,
            "user_agent": self._http_user_agent,
            **payload,
        }
        env = os.environ.copy()
        env["IVAS_NODE_BRIDGE_PAYLOAD"] = json.dumps(bridge_payload)
        result = subprocess.run(
            ["node", "-e", IVAS_NODE_BRIDGE_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "Node bridge failed").strip())

        try:
            data = json.loads(result.stdout.strip() or "{}")
        except Exception as exc:
            raise RuntimeError(f"Invalid node bridge response: {result.stdout[:200]}") from exc

        bridge_cookies = data.get("cookies") or {}
        if isinstance(bridge_cookies, dict) and bridge_cookies:
            self._save_cookie_map(bridge_cookies)

        if not data.get("ok"):
            raise RuntimeError(str(data.get("error", "Node bridge failed")))

        result_payload = data.get("result") or {}
        if isinstance(result_payload, dict) and result_payload.get("token"):
            self.csrf_token = result_payload["token"]
        return result_payload

    async def _run_node_bridge(self, action, **payload):
        return await asyncio.to_thread(self._run_node_bridge_sync, action, **payload)

    def _xsrf_from_cookie(self, cookie_map):
        raw = cookie_map.get("XSRF-TOKEN", "")
        if not raw:
            return ""
        try:
            return urllib_parse.unquote(raw)
        except Exception:
            return raw

    def _http_request_sync(self, method, path, body=None, content_type=None, extra_headers=None):
        cookie_map = self._load_cookie_map()
        if not cookie_map:
            raise RuntimeError("No saved cookies available for HTTP scraper")

        headers = {
            "User-Agent": self._http_user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": self._cookie_header(cookie_map),
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/portal/sms/received",
            "X-Requested-With": "XMLHttpRequest",
        }
        xsrf = self._xsrf_from_cookie(cookie_map)
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
            headers["X-CSRF-TOKEN"] = xsrf
        if content_type:
            headers["Content-Type"] = content_type
        if extra_headers:
            headers.update(extra_headers)

        payload = None
        if body is not None:
            payload = body.encode("utf-8") if isinstance(body, str) else body

        last_error = None
        for base_url in self._base_urls:
            request = urllib_request.Request(
                url=f"{base_url}{path}",
                data=payload,
                headers={**headers, "Origin": base_url, "Referer": f"{base_url}/portal/sms/received"},
                method=method,
            )
            try:
                with urllib_request.urlopen(request, timeout=30) as response:
                    self.base_url = base_url
                    self._update_cookies_from_response_headers(response.headers)
                    response_text = response.read().decode("utf-8", errors="ignore")
                    if self._is_session_expired_response(getattr(response, "status", 200), response_text):
                        raise RuntimeError("SESSION_EXPIRED")
                    return response_text
            except urllib_error.HTTPError as exc:
                self._update_cookies_from_response_headers(exc.headers)
                response_text = exc.read().decode("utf-8", errors="ignore")
                if self._is_session_expired_response(exc.code, response_text):
                    raise RuntimeError("SESSION_EXPIRED") from exc
                last_error = RuntimeError(f"HTTP {exc.code} for {path}: {response_text[:200]}")
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        raise RuntimeError(f"HTTP request failed for {path}")

    async def _http_request(self, method, path, body=None, content_type=None, extra_headers=None):
        return await asyncio.to_thread(
            self._http_request_sync,
            method,
            path,
            body,
            content_type,
            extra_headers,
        )

    async def _fetch_portal_token_http(self):
        result = await self._run_node_bridge("status")
        token = str(result.get("token", "") or "")
        if token:
            self.csrf_token = token
        return token

    def _today_str(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _normalize_ivasms_date(self, value):
        raw_value = str(value or "").strip()
        if not raw_value:
            return ""
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw_value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return raw_value

    def _strip_html(self, value):
        text = re.sub(r"<[^>]+>", " ", value or "")
        return html.unescape(re.sub(r"\s+", " ", text)).strip()

    def _build_numbers_query_path(self, start=0, length=5000):
        ts = int(time.time() * 1000)
        return (
            "/portal/numbers?draw=1"
            "&columns[0][data]=number_id&columns[0][name]=id&columns[0][orderable]=false"
            "&columns[1][data]=Number"
            "&columns[2][data]=range"
            "&columns[3][data]=A2P"
            "&columns[4][data]=LimitA2P"
            "&columns[5][data]=limit_cli_a2p"
            "&columns[6][data]=limit_cli_did_a2p"
            "&columns[7][data]=action&columns[7][searchable]=false&columns[7][orderable]=false"
            "&order[0][column]=1&order[0][dir]=desc"
            f"&start={start}&length={length}&search[value]=&_={ts}"
        )

    def _parse_numbers_payload(self, response_text):
        try:
            payload = json.loads(response_text)
        except Exception:
            numbers = sorted(set(re.findall(r"\b\d{7,15}\b", response_text)))
            aa_data = [["", "", number, "Weekly", ""] for number in numbers]
            return {
                "sEcho": 2,
                "iTotalRecords": str(len(aa_data)),
                "iTotalDisplayRecords": str(len(aa_data)),
                "aaData": aa_data,
                "rows": [
                    {"range": "", "number": number, "plan": "Weekly"}
                    for number in numbers
                ],
            }

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            data = []

        aa_data = []
        rows = []
        for row in data:
            if not isinstance(row, dict):
                continue
            range_name = str(row.get("range", "") or "")
            number = str(row.get("Number", "") or "")
            aa_data.append([range_name, "", number, "Weekly", ""])
            rows.append(
                {
                    "range": range_name,
                    "number": number,
                    "plan": "Weekly",
                    "raw": row,
                }
            )

        return {
            "sEcho": 2,
            "iTotalRecords": str(payload.get("recordsTotal", len(aa_data))),
            "iTotalDisplayRecords": str(payload.get("recordsFiltered", len(aa_data))),
            "aaData": aa_data,
            "rows": rows,
        }

    def _parse_sms_rows_html(self, response_text, range_name, number, default_date):
        rows = []
        for match in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", response_text, flags=re.IGNORECASE):
            if "<th" in match.lower():
                continue
            sender_match = re.search(r'class="cli-tag"[^>]*>([^<]+)<', match, flags=re.IGNORECASE)
            message_match = re.search(r'class="msg-text"[^>]*>([\s\S]*?)</div>', match, flags=re.IGNORECASE)
            time_match = re.search(r'class="time-cell"[^>]*>\s*([0-9:]+)\s*<', match, flags=re.IGNORECASE)
            message = self._strip_html(message_match.group(1)) if message_match else ""
            if not message:
                continue
            sender = sender_match.group(1).strip() if sender_match else "SMS"
            time_text = time_match.group(1).strip() if time_match else "00:00:00"
            rows.append(
                {
                    "date": f"{default_date} {time_text}",
                    "range": range_name,
                    "number": str(number),
                    "sender": sender,
                    "message": message,
                    "currency": "$",
                    "amount": 0,
                    "aa_row": [
                        f"{default_date} {time_text}",
                        range_name,
                        str(number),
                        sender,
                        message,
                        "$",
                        0,
                    ],
                }
            )
        return rows

    async def update_http_session(self, xsrf, session):
        cookie_map = self._load_cookie_map()
        cookie_map["XSRF-TOKEN"] = str(xsrf or "").strip()
        cookie_map["ivas_sms_session"] = str(session or "").strip()
        if not cookie_map["XSRF-TOKEN"] or not cookie_map["ivas_sms_session"]:
            raise ValueError("Both xsrf and session are required")
        self._save_cookie_map(cookie_map)
        self.csrf_token = ""
        return {
            "ok": True,
            "message": "Session cookies updated",
            "cookies_path": str(self.cookies_path),
        }

    async def get_session_status_http(self):
        try:
            result = await self._run_node_bridge("status")
            token = str(result.get("token", "") or "")
            return {
                "ok": bool(token),
                "active": bool(token),
                "token_ready": bool(token),
                "cookies_path": str(self.cookies_path),
            }
        except Exception as exc:
            return {
                "ok": False,
                "active": False,
                "token_ready": False,
                "error": str(exc),
                "cookies_path": str(self.cookies_path),
            }

    async def get_numbers_http(self, start=0, length=5000):
        result = await self._run_node_bridge("numbers", start=start, length=length)
        return result

    async def get_sms_http(self, from_date="", to_date="", limit=500):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        result = await self._run_node_bridge("sms", from_date=from_date, to_date=to_date)
        rows = []
        for row in (result.get("rows") or [])[:limit]:
            rows.append(
                {
                    "date": row.get("date", ""),
                    "range": row.get("range", ""),
                    "number": row.get("number", ""),
                    "phone_number": row.get("number", ""),
                    "sender": row.get("sender", "SMS"),
                    "message": row.get("message", ""),
                    "otp_message": row.get("message", ""),
                    "currency": row.get("currency", "$"),
                    "amount": row.get("amount", 0),
                    "aa_row": [
                        row.get("date", ""),
                        row.get("range", ""),
                        row.get("number", ""),
                        row.get("sender", "SMS"),
                        row.get("message", ""),
                        row.get("currency", "$"),
                        row.get("amount", 0),
                    ],
                }
            )
        return {
            "sEcho": 1,
            "iTotalRecords": str(len(rows)),
            "iTotalDisplayRecords": str(len(rows)),
            "aaData": [row["aa_row"] for row in rows],
            "rows": rows,
            "ranges": result.get("ranges", []),
        }

    async def get_raw_sms_html(self, from_date="", to_date="", range_name="", number=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        result = await self._run_node_bridge(
            "raw_sms",
            from_date=from_date,
            to_date=to_date,
            raw_options={"range": range_name, "number": number},
        )
        return {
            "ok": True,
            "range": result.get("rawRange", ""),
            "number": str(result.get("rawNumber", "")),
            "html": result.get("rawHtml", ""),
        }

    async def get_sms_details_http(self, phone_range, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        token = await self._fetch_portal_token_http()
        if not token:
            raise RuntimeError("SESSION_EXPIRED")
        body = urllib_parse.urlencode(
            {
                "_token": token,
                "start": from_date,
                "end": to_date,
                "range": phone_range,
            }
        )
        html_text = await self._http_request(
            "POST",
            "/portal/sms/received/getsms/number",
            body=body,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
            extra_headers={"Accept": "text/html, */*; q=0.01"},
        )
        return self._parse_number_details_html(html_text)

    async def get_otp_message_http(self, phone_number, phone_range, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        token = await self._fetch_portal_token_http()
        if not token:
            raise RuntimeError("SESSION_EXPIRED")
        body = urllib_parse.urlencode(
            {
                "_token": token,
                "start": from_date,
                "end": to_date,
                "Number": str(phone_number),
                "Range": str(phone_range),
            }
        )
        html_text = await self._http_request(
            "POST",
            "/portal/sms/received/getsms/number/sms",
            body=body,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
            extra_headers={"Accept": "text/html, */*; q=0.01"},
        )
        return self._parse_otp_message_html(html_text)

    async def get_sms_details_page(self, phone_range, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        if not await self._ensure_browser_sms_session_ready():
            raise RuntimeError("BROWSER_SESSION_NOT_READY")
        html_text = await self._fetch_sms_fragment_via_browser(
            "numbers",
            self.csrf_token,
            from_date,
            to_date,
            range_name=phone_range,
        )
        return self._parse_number_details_html(html_text)

    async def get_otp_message_page(self, phone_number, phone_range, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        if not await self._ensure_browser_sms_session_ready():
            raise RuntimeError("BROWSER_SESSION_NOT_READY")
        html_text = await self._fetch_sms_fragment_via_browser(
            "sms",
            self.csrf_token,
            from_date,
            to_date,
            range_name=phone_range,
            number=str(phone_number),
        )
        return self._parse_otp_message_html(html_text)

    async def refresh_http_session(self):
        """Use the active browser session to refresh persisted cookies for HTTP scraping."""
        if not self._page:
            return False
        try:
            refreshed = await self.refresh_session()
            if not refreshed:
                return False
            await self._save_cookies()
            result = await self._run_node_bridge("status")
            token = str(result.get("token", "") or "")
            if token:
                self.csrf_token = token
            return bool(token)
        except Exception as exc:
            logger.warning(f"HTTP session refresh failed: {exc}")
            return False

    def load_credentials(self):
        """Load login credentials from env first, then local credentials file."""
        email = os.getenv("IVASMS_EMAIL", "") or IVASMS_EMAIL
        password = os.getenv("IVASMS_PASSWORD", "") or IVASMS_PASSWORD
        if email and password:
            return {"email": email, "password": password}

        if not self._credentials_file.exists():
            return None

        try:
            with open(self._credentials_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            email = str(data.get("email", "")).strip()
            password = str(data.get("password", "")).strip()
            if email and password:
                return {"email": email, "password": password}
        except Exception as e:
            logger.error(f"❌ Load credentials error: {e}")
        return None

    async def _load_cookies(self):
        """Load cookies dari file."""
        if not os.path.exists(self._cookies_file):
            return False
        try:
            with open(self._cookies_file, encoding="utf-8") as f:
                cookies = json.load(f)
            if not cookies or not isinstance(cookies, list):
                return False

            # Normalize & filter valid cookies
            valid = []
            for c in cookies:
                if not isinstance(c, dict) or not c.get('name') or not c.get('value'):
                    continue
                entry = {
                    'name': c['name'],
                    'value': c['value'],
                    'domain': c.get('domain', '.ivasms.com'),
                    'path': c.get('path', '/'),
                }
                if c.get('sameSite') in ('Strict', 'Lax', 'None'):
                    entry['sameSite'] = c['sameSite']
                if c.get('httpOnly'):
                    entry['httpOnly'] = True
                if c.get('secure'):
                    entry['secure'] = True
                valid.append(entry)

            if not valid:
                return False

            await self._context.add_cookies(valid)
            logger.info(f"🍪 Loaded {len(valid)} cookies")
            return True
        except Exception as e:
            logger.warning(f"⚠️ Load cookies error: {e}")
            try:
                os.remove(self._cookies_file)
            except Exception:
                pass
            return False

    # ────────── Login ──────────

    def has_cookies_file(self, cookies_file=None):
        target = cookies_file if cookies_file is not None else self._cookies_file
        return os.path.exists(target)

    def has_saved_credentials(self):
        return self.load_credentials() is not None

    def clear_saved_session(self):
        cleared = {"cookies": False, "credentials": False}
        try:
            if os.path.exists(self._cookies_file):
                os.remove(self._cookies_file)
                cleared["cookies"] = True
        except Exception as exc:
            logger.error(f"❌ Failed to remove cookies file: {exc}")
        try:
            if self._credentials_file.exists():
                self._credentials_file.unlink()
                cleared["credentials"] = True
        except Exception as exc:
            logger.error(f"❌ Failed to remove credentials file: {exc}")
        return cleared

    async def login_with_cookies(self, cookies_file=None, startup_mode=False):
        """Login via saved cookies, fallback ke warning."""
        fallback_credentials = None
        async with self._session_lock:
            if cookies_file is not None:
                self._cookies_file = cookies_file
            credentials = self.load_credentials()

            if not self.has_cookies_file():
                logger.warning("No cookies file found. Gunakan /setlogin email password di Telegram")
                logger.warning("💡 Gunakan /setlogin email password di Telegram")
                return False

            logger.info("🔐 Mencoba login via cookies...")
            restore_modes = [True]

            for headless_mode in restore_modes:
                await self._start_browser(force_headless=headless_mode)

                if await self._load_cookies():
                    try:
                        await self._goto_ivasms(
                            "/portal/sms/received",
                            timeout=30000, wait_until="domcontentloaded"
                        )
                        await self._wait_for_cf(60)
                        url = self._page.url
                        if self._is_portal_like_url(url):
                            self.logged_in = await self._ensure_sms_received_page()
                            await self._save_cookies()
                            if self.logged_in:
                                logger.info("✅ Login via cookies berhasil!")
                                return True
                            logger.warning("Cookies loaded but CSRF/token portal belum siap")
                        logger.warning(f"⚠️ Cookies expired. URL: {url}")
                    except Exception as e:
                        logger.error(f"Cookie check error: {e}")

                await self._close_browser()
                logger.warning("Headless cookie restore gagal, tetap retry dalam mode headless")

            if credentials:
                if startup_mode:
                    logger.warning("Cookies invalid saat startup. Lanjut auto-login headless dari credential tersimpan.")
                else:
                    logger.warning("Cookies invalid. Trying autonomous login from stored credentials.")
                fallback_credentials = credentials
            else:
                logger.warning("Cookies invalid and no stored credentials available.")

            logger.warning("💡 Gunakan /setlogin email password di Telegram")

        if fallback_credentials:
            return await self.login_with_credentials(
                fallback_credentials["email"],
                fallback_credentials["password"],
            )
        return False

    async def login_with_credentials(self, email, password):
        """Login lewat form — Playwright async + stealth."""
        import asyncio
        async with self._session_lock:
            logger.info(f"🔐 Login: {email}")

            await self._start_browser()

            try:
                # Step 1 — Navigate login page
                logger.info("🌐 Opening login page...")
                await self._goto_ivasms(
                    "/login",
                    timeout=30000, wait_until="domcontentloaded"
                )

                # Step 2 — Tunggu CF selesai (termasuk di halaman login)
                await self._wait_for_cf(timeout_s=180)
                if not self._page:
                    logger.warning("Login aborted because browser page is no longer available after CF wait")
                    return False

                page_title = await self._page.title()
                page_url = self._page.url
                logger.info(f"📄 Page: {page_title} | {page_url}")
                if "Just a moment" in page_title or "Cloudflare" in page_title:
                    logger.warning("Cloudflare challenge masih aktif setelah timeout login headless")
                    return False

                # Step 3 — Tunggu form email muncul
                logger.info("⏳ Waiting for login form...")
                await self._page.wait_for_selector(
                    "input[type='email'], input[name='email']",
                    timeout=180000, state="visible"
                )

                # Step 4 — Isi form
                logger.info("✍️ Filling login form...")
                await self._ensure_login_credentials(email, password)
                await asyncio.sleep(0.5)
                await self._wait_for_turnstile_ready(180)

                for submit_attempt in range(1, 4):
                    await self._ensure_login_credentials(email, password)
                    state_before_submit = await self._read_login_form_state()

                    if state_before_submit["isVerifying"]:
                        logger.info("Turnstile still verifying. Waiting before submit.")
                        await self._wait_for_turnstile_ready(180)
                        await self._ensure_login_credentials(email, password)

                    logger.info(f"📤 Submitting form... attempt {submit_attempt}")
                    await self._page.click("button[type='submit'], input[type='submit']")
                    await asyncio.sleep(3)

                    url = self._page.url
                    logger.info(f"📍 After submit: {url}")

                    if self._is_portal_like_url(url):
                        await self._save_cookies()
                        self.logged_in = await self._ensure_sms_received_page()
                        if not self.logged_in:
                            logger.warning("Visible login reached portal, but SMS Received page is not ready yet.")
                            return False
                        logger.info("✅ Login berhasil!")
                        return True

                    state_after_submit = await self._read_login_form_state()
                    if state_after_submit["hasVerificationError"]:
                        logger.warning("⚠️ Cloudflare verification failed after submit. Re-filling credentials and waiting.")
                        await self._ensure_login_credentials(email, password)
                        await self._wait_for_turnstile_ready(180)
                        continue

                    if "/login" in url:
                        redirected = await self._wait_for_login_result(180)
                        if not self._page:
                            logger.warning("Login aborted because browser page closed while waiting for redirect")
                            return False
                        url = self._page.url
                        if redirected and self._is_portal_like_url(url):
                            await self._save_cookies()
                            self.logged_in = await self._ensure_sms_received_page()
                            if not self.logged_in:
                                logger.warning("Visible login reached portal, but SMS Received page is not ready yet.")
                                return False
                            logger.info("✅ Login berhasil!")
                            return True

                        await self._ensure_login_credentials(email, password)

                logger.error(f"❌ Login gagal. URL: {self._page.url if self._page else '-'}")
                return False

            except Exception as e:
                logger.error(f"❌ Login error: {e}")
                import traceback
                logger.error(traceback.format_exc())
                return False

    # ────────── AJAX / Data Fetch ──────────

    async def _post_ajax(self, url, payload):
        """Async AJAX POST via page.evaluate."""
        if not self._page:
            return None
        try:
            form_body = "&".join(
                f"{k}={v}" for k, v in payload.items()
            )
            result = await self._page.evaluate("""
                async ([url, body]) => {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: body,
                        credentials: 'same-origin'
                    });
                    return await resp.text();
                }
            """, [url, form_body])
            return result
        except Exception as e:
            logger.error(f"❌ AJAX error [{url}]: {e}")
            return None

    async def _ensure_browser_sms_session_ready(self):
        """Ensure the current browser page is usable for same-origin SMS fetches."""
        if not self._page:
            return False
        if self._is_sms_received_url(self._page.url) and self.csrf_token:
            await self._dismiss_portal_overlays()
            return True
        return await self._ensure_sms_received_page()

    async def _fetch_sms_fragment_via_browser(self, action, token, from_date, to_date, range_name="", number=""):
        if not self._page:
            raise RuntimeError("BROWSER_SESSION_UNAVAILABLE")
        result = await self._page.evaluate(
            """
            async ({ action, token, fromDate, toDate, rangeName, number }) => {
                const ajaxHeaders = {
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "text/html, */*; q=0.01"
                };
                let response;
                if (action === "ranges") {
                    const body = new URLSearchParams({
                        _token: token,
                        from: fromDate,
                        to: toDate,
                    });
                    response = await fetch("/portal/sms/received/getsms", {
                        method: "POST",
                        body,
                        headers: {
                            ...ajaxHeaders,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        credentials: "same-origin",
                    });
                } else if (action === "numbers") {
                    const body = new URLSearchParams({
                        _token: token,
                        start: fromDate,
                        end: toDate,
                        range: rangeName,
                    });
                    response = await fetch("/portal/sms/received/getsms/number", {
                        method: "POST",
                        body,
                        headers: {
                            ...ajaxHeaders,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        credentials: "same-origin",
                    });
                } else if (action === "sms") {
                    const body = new URLSearchParams({
                        _token: token,
                        start: fromDate,
                        end: toDate,
                        Number: number,
                        Range: rangeName,
                    });
                    response = await fetch("/portal/sms/received/getsms/number/sms", {
                        method: "POST",
                        body,
                        headers: {
                            ...ajaxHeaders,
                            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        },
                        credentials: "same-origin",
                    });
                } else {
                    throw new Error(`Unknown browser fetch action: ${action}`);
                }

                const text = await response.text();
                return {
                    ok: response.ok,
                    status: response.status,
                    text,
                };
            }
            """,
            {
                "action": action,
                "token": token,
                "fromDate": from_date,
                "toDate": to_date,
                "rangeName": range_name,
                "number": str(number or ""),
            },
        )
        status = int(result.get("status", 0) or 0)
        text = str(result.get("text", "") or "")
        if status in (401, 419) or '"message":"Unauthenticated"' in text:
            raise RuntimeError("SESSION_EXPIRED")
        if status >= 400:
            raise RuntimeError(f"HTTP {status} for browser {action}: {text[:200]}")
        return text

    async def _check_otps_page(self, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        if not await self._ensure_browser_sms_session_ready():
            raise RuntimeError("BROWSER_SESSION_NOT_READY")
        token = self.csrf_token
        html_text = await self._fetch_sms_fragment_via_browser("ranges", token, from_date, to_date)
        unique_ranges = self._parse_range_names_html(html_text)
        if not unique_ranges:
            logger.warning(
                "[IVAS BROWSER] no ranges for %s..%s, html_snippet=%s",
                from_date,
                to_date,
                (html_text or "")[:400].replace("\n", " ").replace("\r", " "),
            )
        logger.info(f"[IVAS BROWSER] ranges={len(unique_ranges)} for {from_date}..{to_date}")
        return {
            "count_sms": len(unique_ranges),
            "paid_sms": 0,
            "unpaid_sms": 0,
            "revenue": "0",
            "source": "browser",
            "sms_details": [{"country_number": range_name} for range_name in unique_ranges],
        }

    async def _get_all_otp_messages_page(self, sms_details, from_date="", to_date="", limit=100):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        allowed_ranges = {
            str(detail.get("country_number", "")).strip()
            for detail in (sms_details or [])
            if str(detail.get("country_number", "")).strip()
        }
        messages = []
        for range_name in allowed_ranges:
            number_details = await self.get_sms_details_page(range_name, from_date=from_date, to_date=to_date)
            for number_detail in number_details:
                phone_number = str(number_detail.get("phone_number", "")).strip()
                if not phone_number:
                    continue
                otp_message = await self.get_otp_message_page(
                    phone_number,
                    range_name,
                    from_date=from_date,
                    to_date=to_date,
                )
                if not otp_message:
                    continue
                messages.append(
                    {
                        "range": range_name,
                        "phone_number": phone_number,
                        "otp_message": otp_message,
                        "sender": "SMS",
                    }
                )
                if len(messages) >= limit:
                    return messages[:limit]
        return messages[:limit]

    async def _check_otps_http(self, from_date="", to_date=""):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        result = await self._run_node_bridge("ranges", from_date=from_date, to_date=to_date)
        ranges = self._parse_range_names_html(result.get("html", "")) or (result.get("ranges", []) or [])
        logger.info(f"[IVAS HTTP] ranges={len(ranges)} for {from_date}..{to_date}")
        return {
            "count_sms": len(ranges),
            "paid_sms": 0,
            "unpaid_sms": 0,
            "revenue": "0",
            "source": "http",
            "sms_details": [{"country_number": range_name} for range_name in ranges],
        }

    async def _get_all_otp_messages_http(self, sms_details, from_date="", to_date="", limit=100):
        from_date = self._normalize_ivasms_date(from_date or self._today_str())
        to_date = self._normalize_ivasms_date(to_date or from_date)
        allowed_ranges = {
            str(detail.get("country_number", "")).strip()
            for detail in (sms_details or [])
            if str(detail.get("country_number", "")).strip()
        }
        all_msgs = []
        for range_name in allowed_ranges:
            number_details = await self.get_sms_details_http(range_name, from_date=from_date, to_date=to_date)
            for number_detail in number_details:
                phone_number = str(number_detail.get("phone_number", "")).strip()
                if not phone_number:
                    continue
                otp_message = await self.get_otp_message_http(
                    phone_number,
                    range_name,
                    from_date=from_date,
                    to_date=to_date,
                )
                if not otp_message:
                    continue
                all_msgs.append(
                    {
                        "range": range_name,
                        "phone_number": phone_number,
                        "otp_message": otp_message,
                        "sender": "SMS",
                    }
                )
                if len(all_msgs) >= limit:
                    return all_msgs
        return all_msgs

    async def check_otps(self, from_date="", to_date="", allow_browser_refresh=True):
        """Fetch Static SMS statistics directly from iVaSMS."""
        if self._page and (self.logged_in or self.csrf_token):
            try:
                result = await self._check_otps_page(from_date=from_date, to_date=to_date)
                self._prefer_browser_fetch_until = time.time() + 60
                return result
            except Exception as exc:
                logger.error(f"Direct browser SMS scrape failed: {exc}")
        try:
            return await self._check_otps_http(from_date=from_date, to_date=to_date)
        except Exception as exc:
            if ("HTTP 403" in str(exc) or "SESSION_EXPIRED" in str(exc)) and self._page and (self.logged_in or self.csrf_token):
                try:
                    logger.warning(f"HTTP SMS scrape failed, trying active browser session: {exc}")
                    result = await self._check_otps_page(from_date=from_date, to_date=to_date)
                    self._prefer_browser_fetch_until = time.time() + 60
                    return result
                except Exception as browser_exc:
                    logger.error(f"Browser SMS scrape failed: {browser_exc}")
            if "HTTP 403" in str(exc) or "SESSION_EXPIRED" in str(exc):
                self.logged_in = False
                self.csrf_token = ""
            if allow_browser_refresh:
                logger.warning(f"HTTP SMS scrape failed, trying browser session refresh: {exc}")
            else:
                logger.error(f"HTTP SMS scrape failed: {exc}")
                return None
            if await self.refresh_http_session():
                try:
                    return await self._check_otps_http(from_date=from_date, to_date=to_date)
                except Exception as retry_exc:
                    logger.error(f"HTTP SMS scrape failed after browser refresh: {retry_exc}")
                    return None
            logger.error(f"HTTP SMS scrape failed: {exc}")
            return None

    async def ensure_logged_in(self, startup_mode=True):
        """Ensure a valid runtime session exists. Returns True when session is active."""
        if self.logged_in and self._page and self.csrf_token:
            return True
        return await self.login_with_cookies(startup_mode=startup_mode)

    async def refresh_session(self):
        """Refresh in-memory session state against the current page when possible."""
        if not self._page:
            self.logged_in = False
            self.csrf_token = ""
            return False
        try:
            url = self._page.url
            if self._is_portal_like_url(url):
                self.logged_in = await self._ensure_sms_received_page(timeout=20000)
                return self.logged_in
        except Exception:
            pass
        self.logged_in = False
        self.csrf_token = ""
        return False

    async def get_otp_for_number(self, phone_number, from_date="", to_date=""):
        """Check apakah ada OTP masuk untuk nomor ini di Static SMS."""
        if not self.logged_in and not self.cookies_path.exists():
            return None

        import asyncio
        from datetime import datetime, timedelta
        if not from_date:
            from_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.now().strftime("%Y-%m-%d")

        try:
            result = await self.check_otps(from_date=from_date, to_date=to_date)
            if not result or int(result.get('count_sms', 0) or 0) == 0:
                return None

            for detail in result.get('sms_details', []):
                otps = await self.get_all_otp_messages(
                    [detail], from_date=from_date, to_date=to_date, limit=30
                )
                for otp in (otps or []):
                    if str(otp.get('phone_number', '')) == str(phone_number):
                        return otp.get('otp_message')
            return None
        except Exception as e:
            logger.error(f"❌ get_otp_for_number error: {e}")
            return None

    async def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=100, allow_browser_refresh=True):
        """Fetch pesan OTP dari iVaSMS directly."""
        if self._page and (self.logged_in or self.csrf_token):
            try:
                result = await self._get_all_otp_messages_page(
                    sms_details,
                    from_date=from_date,
                    to_date=to_date,
                    limit=limit,
                )
                self._prefer_browser_fetch_until = time.time() + 60
                return result
            except Exception as exc:
                logger.error(f"Direct browser OTP message scrape failed: {exc}")
        try:
            return await self._get_all_otp_messages_http(
                sms_details,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
            )
        except Exception as exc:
            if ("HTTP 403" in str(exc) or "SESSION_EXPIRED" in str(exc)) and self._page and (self.logged_in or self.csrf_token):
                try:
                    logger.warning(f"HTTP OTP message scrape failed, trying active browser session: {exc}")
                    result = await self._get_all_otp_messages_page(
                        sms_details,
                        from_date=from_date,
                        to_date=to_date,
                        limit=limit,
                    )
                    self._prefer_browser_fetch_until = time.time() + 60
                    return result
                except Exception as browser_exc:
                    logger.error(f"Browser OTP message scrape failed: {browser_exc}")
            if "HTTP 403" in str(exc) or "SESSION_EXPIRED" in str(exc):
                self.logged_in = False
                self.csrf_token = ""
            if allow_browser_refresh:
                logger.warning(f"HTTP OTP message scrape failed, trying browser session refresh: {exc}")
            else:
                logger.error(f"HTTP OTP message scrape failed: {exc}")
                return []
            if await self.refresh_http_session():
                try:
                    return await self._get_all_otp_messages_http(
                        sms_details,
                        from_date=from_date,
                        to_date=to_date,
                        limit=limit,
                    )
                except Exception as retry_exc:
                    logger.error(f"HTTP OTP message scrape failed after browser refresh: {retry_exc}")
                    return []
            logger.error(f"HTTP OTP message scrape failed: {exc}")
            return []

    async def close(self):
        await self._close_browser()


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


class IvaApiHandler(BaseHTTPRequestHandler):
    server_version = "IvaSmsPythonAPI/1.0"

    def _send_json(self, status_code, payload):
        body = _json_bytes(payload)
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        raw_length = self.headers.get("Content-Length", "0").strip() or "0"
        length = int(raw_length)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _run(self, coro):
        return asyncio.run(coro)

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path or "/"
        client = IVASSMSClient()

        try:
            if route == "/" and query.get("type", [""])[0] == "numbers":
                payload = self._run(
                    client.get_numbers_http(
                        start=int(query.get("start", ["0"])[0]),
                        length=int(query.get("length", ["5000"])[0]),
                    )
                )
                return self._send_json(200, payload)

            if route == "/" and query.get("type", [""])[0] == "sms":
                payload = self._run(
                    client.get_sms_http(
                        from_date=query.get("from", [""])[0],
                        to_date=query.get("to", [""])[0],
                        limit=int(query.get("limit", ["500"])[0]),
                    )
                )
                return self._send_json(200, payload)

            if route == "/status":
                payload = self._run(client.get_session_status_http())
                return self._send_json(200 if payload.get("ok") else 401, payload)

            if route == "/raw-sms":
                payload = self._run(
                    client.get_raw_sms_html(
                        from_date=query.get("from", [""])[0],
                        to_date=query.get("to", [""])[0],
                        range_name=query.get("range", [""])[0],
                        number=query.get("number", [""])[0],
                    )
                )
                return self._send_json(200 if payload.get("ok") else 404, payload)

            return self._send_json(
                404,
                {
                    "ok": False,
                    "error": "Not found",
                    "routes": [
                        "GET /?type=numbers",
                        "GET /?type=sms",
                        "GET /status",
                        "GET /raw-sms",
                        "POST /update-session",
                    ],
                },
            )
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except RuntimeError as exc:
            return self._send_json(401 if "SESSION_EXPIRED" in str(exc) else 500, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled GET error")
            return self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        route = parsed.path or "/"
        client = IVASSMSClient()

        try:
            if route == "/update-session":
                body = self._read_json_body()
                payload = self._run(
                    client.update_http_session(
                        xsrf=body.get("xsrf", ""),
                        session=body.get("session", ""),
                    )
                )
                return self._send_json(200, payload)

            return self._send_json(404, {"ok": False, "error": "Not found"})
        except json.JSONDecodeError:
            return self._send_json(400, {"ok": False, "error": "Invalid JSON body"})
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except RuntimeError as exc:
            return self._send_json(401 if "SESSION_EXPIRED" in str(exc) else 500, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.exception("Unhandled POST error")
            return self._send_json(500, {"ok": False, "error": str(exc)})

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)


def run_http_api_server():
    host = os.getenv("IVAS_HTTP_HOST", "127.0.0.1")
    port = int(os.getenv("IVAS_HTTP_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), IvaApiHandler)
    logger.info("Iva HTTP API listening on http://%s:%s", host, port)
    server.serve_forever()


if __name__ == "__main__":
    run_http_api_server()
