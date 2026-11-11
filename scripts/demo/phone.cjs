// The demo's phone: the console in Chrome at phone size, filmed, driven by
// demo.py over stdin (one JSON command per line, one JSON reply each).
//
//   node phone.cjs pair <console-url> <pair-url> <confirm-code>
//   node phone.cjs film <console-url> <video-dir>
//   node phone.cjs frame <html> <png> <width> <height>
//
// Playwright isn't a bot-bottle dependency: `demo.py setup --phone` installs
// it under ~/.cache/bot-bottle-demo/phone, and demo.py runs this from there.
// It drives your installed Chrome (`channel: "chrome"`), or the browser at
// BB_PHONE_CHROME.
//
// The phone has its own passkey, on a virtual authenticator (Chrome's
// DevTools WebAuthn domain), so nobody touches a key: `pair` creates it while
// making the demo's own console account, and saves it to passkey.json (0600)
// beside the profile; `film` loads it back, so a run whose session has
// expired signs in again with it.
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { chromium } = require(path.join(process.env.BB_PHONE_DIR, "node_modules", "playwright"));

const PROFILE = path.join(process.env.BB_PHONE_DIR, "profile");
const PASSKEY = path.join(process.env.BB_PHONE_DIR, "passkey.json");
const PHONE = { width: 390, height: 844 };

function browser() {
  const exe = process.env.BB_PHONE_CHROME;
  return exe ? { executablePath: exe } : { channel: "chrome" };
}

function reply(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function context(headless, video) {
  return chromium.launchPersistentContext(PROFILE, {
    ...browser(),
    headless,
    viewport: PHONE,
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    colorScheme: "dark",
    ...(video ? { recordVideo: { dir: video, size: PHONE } } : {}),
  });
}

// A virtual authenticator on `page`, holding the saved passkey if there is
// one: it answers navigator.credentials on its own, user verified.
async function authenticator(page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2", transport: "internal", hasResidentKey: true,
      hasUserVerification: true, isUserVerified: true, automaticPresenceSimulation: true,
    },
  });
  if (fs.existsSync(PASSKEY)) {
    for (const credential of JSON.parse(fs.readFileSync(PASSKEY, "utf8")).credentials) {
      await cdp.send("WebAuthn.addCredential", { authenticatorId, credential });
    }
  }
  return { cdp, authenticatorId };
}

// Save the authenticator's credentials, signature counter included: the
// console refuses an assertion whose counter hasn't gone up (a cloned key),
// so a passkey reloaded with a stale counter would stop working.
async function savePasskey(cdp, authenticatorId, url) {
  const { credentials } = await cdp.send("WebAuthn.getCredentials", { authenticatorId });
  if (!credentials.length) return 0;
  fs.writeFileSync(PASSKEY, JSON.stringify({ console: url, credentials }), { mode: 0o600 });
  fs.chmodSync(PASSKEY, 0o600);
  return credentials.length;
}

// Landed on the sign-in page (the session expired): sign in with the passkey.
async function signIn(page, url) {
  // The app sends a signed-out visitor to /login itself, after the page loads.
  const onLogin = (u) => u.pathname.startsWith("/login");
  try {
    await page.waitForURL(onLogin, { timeout: 5000 });
  } catch {
    return; // signed in
  }
  await page.getByRole("button", { name: "Sign in with a passkey" }).click();
  await page.waitForURL((u) => !onLogin(u), { timeout: 30000 });
  await page.goto(url);
}

// Pair the demo host with a new account that is just this phone's passkey:
// the pair page's "No bot-bottle account yet?" path, with the confirm code
// the host's terminal printed. Saves the passkey for `film`.
async function pair(url, pairUrl, confirmCode) {
  const ctx = await context(true, null);
  const page = ctx.pages()[0] || (await ctx.newPage());
  const { cdp, authenticatorId } = await authenticator(page);
  await page.goto(pairUrl);
  await page.locator("details.new-account summary").click();
  await page.fill("#confirm_code", confirmCode);
  await page.click("#pair-passkey");
  await page.waitForURL(/\/pair\/done/, { timeout: 60000 });
  if (!(await savePasskey(cdp, authenticatorId, url))) throw new Error("the pair page made no passkey");
  const paired = await page.locator("body").innerText();
  await ctx.close();
  reply({ paired: true, account: /new passkey account/.test(paired) });
}

// What each command does on the page.
const COMMANDS = {
  async go(page, url, { path: where }) {
    await page.goto(new URL(where, url).toString());
    return {};
  },
  async open_agent(page, url, { id, label }) {
    await page.goto(new URL(`/bottled-agents/${id}`, url).toString());
    // An id the console doesn't know: the agent's row on the home list.
    if (!(await page.locator(".agent-bar").first().isVisible().catch(() => false))) {
      await page.goto(url);
      // The host's id isn't the console's: find the live row by label and open
      // its link. Clicking it timed out: the live list re-renders every few
      // seconds, so the row never held still long enough to be clicked. Not
      // "running": a row can show its host's health there ("host
      // intermittent") instead. The running group comes first.
      const row = page.locator("a.sb-agent:visible", { hasText: label })
        .filter({ hasNotText: "stopped" }).first();
      await row.waitFor({ timeout: 60000 });
      await page.goto(new URL(await row.getAttribute("href"), url).toString());
    }
    return { at: page.url() };
  },
  async approve(page, _url, { text, timeout }) {
    const card = page.locator("section.approval-card", { hasText: text });
    await card.waitFor({ timeout });
    await page.waitForTimeout(1500); // the card, read before it's tapped
    // force: the live page re-renders, and a tap waiting for the button to
    // hold still can time out (as the agent row's click did).
    await card.locator('button[value="allow"]').first().tap({ force: true });
    await page.waitForTimeout(500);
    return {};
  },
  async wait(page, _url, { ms }) {
    await page.waitForTimeout(ms);
    return {};
  },
  // The advanced demo (advanced.py): the phone away from the agent's page.
  async home(page, url) {
    await page.goto(url);
    return {};
  },
  // Wait, wherever the phone is, for the agent at `agent` (its page's path)
  // to ask for approval; the card's message.
  async wait_approval(page, url, { agent, timeout }) {
    const id = agent.split("/").filter(Boolean).pop();
    const api = new URL(`/api/v1/page/bottled-agents/${id}`, url).toString();
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const r = await page.request.get(api).catch(() => null);
      if (r && r.ok()) {
        const data = await r.json();
        if (data.approval) return { message: data.approval.message || "" };
      }
      await page.waitForTimeout(1000);
    }
    throw new Error("no approval came");
  },
  // The push notification the console sends (bb_console/push.py: the
  // agent's label, and what it wants), drawn as the phone would show it:
  // headless Chrome can't show a real one. Held, then tapped: it opens the
  // agent's page, as the real one's url does.
  async notify(page, url, { title, body, open, hold }) {
    await page.evaluate(({ title, body }) => {
      const el = document.createElement("div");
      el.id = "demo-notification";
      el.setAttribute("style", [
        "position:fixed", "z-index:2147483647", "left:8px", "right:8px", "top:10px",
        "padding:12px 14px", "border-radius:22px", "display:flex", "gap:12px",
        "align-items:flex-start", "background:rgba(44,44,48,0.94)",
        "backdrop-filter:blur(24px)", "-webkit-backdrop-filter:blur(24px)",
        "box-shadow:0 12px 32px rgba(0,0,0,0.55)", "color:#f5f5f7",
        "font:15px/1.3 -apple-system,'SF Pro Text',Inter,system-ui,sans-serif",
        "transform:translateY(-140%)", "transition:transform 420ms cubic-bezier(.2,.9,.3,1.1)",
      ].join(";"));
      const icon = document.createElement("img");
      icon.src = "/brand/favicon.svg";
      icon.setAttribute("style", "width:38px;height:38px;border-radius:9px;background:#0b825d;flex:none");
      const text = document.createElement("div");
      text.setAttribute("style", "flex:1;min-width:0");
      const head = document.createElement("div");
      head.setAttribute("style", "display:flex;justify-content:space-between;font-weight:600");
      head.append(Object.assign(document.createElement("span"), { textContent: title }));
      const now = Object.assign(document.createElement("span"), { textContent: "now" });
      now.setAttribute("style", "font-weight:400;opacity:.6;font-size:13px");
      head.append(now);
      const msg = Object.assign(document.createElement("div"), { textContent: body });
      msg.setAttribute("style", "opacity:.92;margin-top:2px");
      text.append(head, msg);
      el.append(icon, text);
      document.body.append(el);
      requestAnimationFrame(() => requestAnimationFrame(() => { el.style.transform = "none"; }));
    }, { title, body });
    await page.waitForTimeout(hold);
    const banner = page.locator("#demo-notification");
    await banner.tap({ force: true });
    await page.waitForTimeout(250);
    await page.goto(new URL(open, url).toString());
    return {};
  },
  // A block alert on the agent's page (the console's shield): wait for its
  // count, let it be seen, then open the panel and hold on the alert that
  // mentions `text`.
  async alerts(page, _url, { text, timeout, hold }) {
    // Amber: earlier blocks (a host off the list) already show a grey count.
    const count = page.locator(".shield__count--urgent");
    await count.waitFor({ timeout });
    await page.waitForTimeout(1200);
    await page.locator("button.agent-reply__approvals").tap({ force: true });
    await page.locator("#shield-pop .shield-pop__item", { hasText: text }).first()
      .waitFor({ timeout: 10000 });
    await page.waitForTimeout(hold);
    return {};
  },
  // A message typed into the agent's message box and sent.
  async say(page, _url, { text, delay }) {
    const box = page.locator("#agent-reply-text");
    await box.waitFor({ timeout: 30000 });
    await box.tap();
    await box.pressSequentially(text, { delay });
    await page.waitForTimeout(600);
    await page.locator("button.agent-reply__send").tap({ force: true });
    return {};
  },
};

async function film(url, videoDir) {
  const ctx = await context(true, videoDir);
  const page = ctx.pages()[0] || (await ctx.newPage());
  // The video starts with the page; demo.py lines it up with the terminal's.
  const started = Date.now() / 1000;
  const { cdp, authenticatorId } = await authenticator(page);
  await page.goto(url);
  await signIn(page, url);
  await savePasskey(cdp, authenticatorId, url);
  reply({ ready: true, started });
  const lines = readline.createInterface({ input: process.stdin });
  for await (const line of lines) {
    const cmd = JSON.parse(line);
    if (cmd.stop) break;
    try {
      reply({ ok: true, ...(await COMMANDS[cmd.do](page, url, cmd)) });
    } catch (e) {
      reply({ ok: false, error: String(e && e.message || e).split("\n")[0] });
    }
  }
  const video = page.video();
  await ctx.close();
  reply({ video: video ? await video.path() : null });
  process.exit(0); // stdin is still open
}

// A PNG of `html` with a transparent background: the device outlines the
// recordings are laid under.
async function frame(html, png, width, height) {
  const chrome = await chromium.launch({ ...browser(), headless: true });
  const page = await chrome.newPage({ viewport: { width: +width, height: +height } });
  await page.setContent(fs.readFileSync(html, "utf8"), { waitUntil: "networkidle" });
  // Web fonts load after the page does: drawn before they arrive, a
  // caption falls back to the system font.
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: png, omitBackground: true });
  await chrome.close();
}

const [mode, ...args] = process.argv.slice(2);
const run = { pair, film, frame }[mode];
if (!run) {
  console.error("usage: phone.cjs pair <url> <pair-url> <confirm-code> | film <url> <video-dir>"
                + " | frame <html> <png> <w> <h>");
  process.exit(2);
}
run(...args).catch((e) => {
  console.error(`phone: ${e && e.message || e}`);
  process.exit(1);
});
