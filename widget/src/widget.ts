/**
 * FirstVoid chat widget.
 *
 * Vanilla TS + Shadow DOM on purpose: this loads on strangers' WordPress and
 * Squarespace sites. No framework runtime, no CSS leaking either direction.
 * Fails silently - never render an error on a client's homepage.
 */

type Cfg = {
  apiBase: string;
  publicKey: string;
  channel: string;
  accent: string;
  position: "right" | "left";
};

const script = document.currentScript as HTMLScriptElement | null;

const cfg: Cfg = {
  apiBase: script?.dataset.api || "http://localhost:8000",
  publicKey: script?.dataset.key || "",
  channel: script?.dataset.channel || "web",
  accent: script?.dataset.accent || "#0F62FE",
  position: (script?.dataset.position as "right" | "left") || "right",
};

const STORE = `fv_session_${cfg.publicKey.slice(-8)}`;

const CSS = `
:host { all: initial; }
*, *::before, *::after { box-sizing: border-box; }
.root {
  position: fixed; ${cfg.position}: 20px; bottom: 20px; z-index: 2147483000;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #16181d;
}
.launcher {
  width: 56px; height: 56px; border-radius: 50%; border: 0; cursor: pointer;
  background: var(--accent); color: #fff; font-size: 22px;
  box-shadow: 0 8px 24px rgba(0,0,0,.22); transition: transform .15s ease;
}
.launcher:hover { transform: scale(1.06); }
.panel {
  position: absolute; bottom: 70px; ${cfg.position}: 0; width: 370px; max-width: calc(100vw - 40px);
  height: 540px; max-height: calc(100vh - 120px); background: #fff; border-radius: 14px;
  box-shadow: 0 18px 48px rgba(0,0,0,.24); display: none; flex-direction: column; overflow: hidden;
}
.panel[data-open="true"] { display: flex; }
.head { background: var(--accent); color: #fff; padding: 14px 16px; display: flex; justify-content: space-between; align-items: center; }
.head strong { font-size: 15px; font-weight: 600; }
.head button { background: transparent; border: 0; color: #fff; font-size: 20px; cursor: pointer; line-height: 1; opacity: .85; }
.log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px; background: #f7f8fa; }
.msg { max-width: 84%; padding: 9px 13px; border-radius: 14px; white-space: pre-wrap; word-wrap: break-word; }
.msg.bot { background: #fff; border: 1px solid #e6e8ec; align-self: flex-start; border-bottom-left-radius: 4px; }
.msg.me { background: var(--accent); color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
.dots { display: inline-flex; gap: 4px; }
.dots i { width: 6px; height: 6px; border-radius: 50%; background: #b6bcc6; animation: b 1.2s infinite; }
.dots i:nth-child(2) { animation-delay: .2s } .dots i:nth-child(3) { animation-delay: .4s }
@keyframes b { 0%,60%,100% { opacity:.3 } 30% { opacity:1 } }
.foot { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #e6e8ec; background: #fff; }
.foot input { flex: 1; border: 1px solid #d7dbe0; border-radius: 9px; padding: 10px 12px; font: inherit; outline: none; }
.foot input:focus { border-color: var(--accent); }
.consent { background: #fff; border: 1px solid #e6e8ec; border-radius: 10px; padding: 12px; font-size: 12.5px; }
.consent p { margin: 0 0 8px; color: #454b54; line-height: 1.45; }
.consent a { color: var(--accent); display: inline-block; margin-bottom: 8px; }
.consent button { display: block; width: 100%; border: 0; background: var(--accent); color: #fff; border-radius: 8px; padding: 9px; font: inherit; font-weight: 600; cursor: pointer; }
.foot button { border: 0; background: var(--accent); color: #fff; border-radius: 9px; padding: 0 16px; cursor: pointer; font-weight: 600; }
.foot button:disabled { opacity: .5; cursor: default; }
@media (max-width: 420px) { .panel { width: calc(100vw - 32px); height: calc(100vh - 110px); } }
`;

function mount() {
  if (!cfg.publicKey) return;

  const host = document.createElement("div");
  host.setAttribute("data-firstvoid", "");
  document.body.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = `:host{--accent:${cfg.accent}}` + CSS;

  const wrap = document.createElement("div");
  wrap.className = "root";
  wrap.innerHTML = `
    <div class="panel" data-open="false">
      <div class="head"><strong data-name>Assistant</strong><button data-close aria-label="Close">&times;</button></div>
      <div class="log" data-log></div>
      <form class="foot"><input data-input placeholder="Type your message" autocomplete="off" /><button data-send>Send</button></form>
    </div>
    <button class="launcher" aria-label="Open chat">&#128172;</button>`;

  root.append(style, wrap);

  const panel = wrap.querySelector<HTMLDivElement>(".panel")!;
  const log = wrap.querySelector<HTMLDivElement>("[data-log]")!;
  const input = wrap.querySelector<HTMLInputElement>("[data-input]")!;
  const send = wrap.querySelector<HTMLButtonElement>("[data-send]")!;
  const nameEl = wrap.querySelector<HTMLElement>("[data-name]")!;
  const form = wrap.querySelector<HTMLFormElement>(".foot")!;

  let sessionId = "";
  let booted = false;
  let busy = false;
  let consentNeeded = false;
  let consentGiven = false;

  const bubble = (cls: "bot" | "me", text = "") => {
    const el = document.createElement("div");
    el.className = `msg ${cls}`;
    el.textContent = text;
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  };

  function renderConsent(c: any) {
    const box = document.createElement("div");
    box.className = "consent";

    // textContent, never innerHTML: this copy comes from tenant config and is
    // injected into someone else's page. Parsing it as HTML would be stored XSS.
    const text = document.createElement("p");
    text.textContent = c.notice || "";
    box.appendChild(text);

    if (c.policy_url) {
      const link = document.createElement("a");
      link.href = c.policy_url;          // href is attribute-assigned, not interpolated
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = c.policy_label || "Privacy policy";
      box.appendChild(link);
    }

    const accept = document.createElement("button");
    accept.type = "button";
    accept.textContent = c.accept_label || "Start chat";
    accept.addEventListener("click", () => {
      consentGiven = true;
      box.remove();
      form.hidden = false;
      input.focus();
    });
    box.appendChild(accept);

    form.hidden = true;
    log.appendChild(box);
  }

  async function boot() {
    if (booted) return;
    booted = true;
    try {
      const res = await fetch(
        `${cfg.apiBase}/v1/chat/bootstrap?public_key=${encodeURIComponent(cfg.publicKey)}`
      );
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      nameEl.textContent = data.agent_name || "Assistant";
      sessionId = sessionStorage.getItem(STORE) || data.session_id;
      sessionStorage.setItem(STORE, sessionId);
      if (data.greeting) bubble("bot", data.greeting);

      consentNeeded = !!data.consent_required;
      if (consentNeeded && data.consent) renderConsent(data.consent);
    } catch {
      // silent: a broken widget must never look like a broken website
      booted = false;
    }
  }

  async function ask(text: string) {
    if (busy || !text.trim()) return;
    busy = true;
    send.disabled = true;
    bubble("me", text);

    const out = bubble("bot");
    out.innerHTML = `<span class="dots"><i></i><i></i><i></i></span>`;

    try {
      // Replies are held until guard_out clears them unless this tenant opted into
      // streaming, so the plain JSON endpoint is the default path.
      const res = await fetch(`${cfg.apiBase}/v1/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          public_key: cfg.publicKey,
          session_id: sessionId,
          text: text.trim(),
          channel: cfg.channel,
          consent: consentNeeded ? consentGiven : false,
        }),
      });
      const data = await res.json();
      if (res.status === 451) {
        out.textContent = "Please accept the notice above to start the chat.";
        return;
      }
      out.textContent =
        (res.ok && data.reply) || "I'm having trouble right now. Please try again in a moment.";
    } catch {
      out.textContent = "I'm having trouble connecting. Please try again in a moment.";
    } finally {
      busy = false;
      send.disabled = false;
      log.scrollTop = log.scrollHeight;
      input.focus();
    }
  }

  wrap.querySelector(".launcher")!.addEventListener("click", async () => {
    const open = panel.dataset.open === "true";
    panel.dataset.open = String(!open);
    if (!open) { await boot(); input.focus(); }
  });
  wrap.querySelector("[data-close]")!.addEventListener("click", () => {
    panel.dataset.open = "false";
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value;
    input.value = "";
    ask(text);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
