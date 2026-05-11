/*!
 * Energovision Chatbot Widget
 * Inštalácia: vlož 1 riadok do <head> webu:
 *   <script src="https://energovision-cp-generator.onrender.com/static/widget.js" async></script>
 *
 * Widget sa pripojí ako bublina vpravo dole.
 * Volá Render endpoint /webhook/chat.
 */
(function() {
  if (window.__EVO_CHATBOT_LOADED__) return;
  window.__EVO_CHATBOT_LOADED__ = true;

  const RENDER_BASE = "https://energovision-cp-generator.onrender.com";
  const ENDPOINT = RENDER_BASE + "/webhook/chat";

  // ============== CSS ==============
  const css = `
    .evo-widget-launcher {
      position: fixed;
      bottom: 24px;
      right: 24px;
      width: 60px;
      height: 60px;
      border-radius: 50%;
      background: linear-gradient(135deg, #2d8a5f, #0a3d2e);
      color: #f4c542;
      box-shadow: 0 6px 24px rgba(10, 61, 46, 0.35);
      cursor: pointer;
      z-index: 999998;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 28px;
      transition: transform 0.2s, box-shadow 0.2s;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .evo-widget-launcher:hover {
      transform: scale(1.08);
      box-shadow: 0 8px 32px rgba(10, 61, 46, 0.5);
    }
    .evo-widget-launcher.hidden { display: none; }
    .evo-widget-badge {
      position: absolute;
      top: -4px;
      right: -4px;
      width: 18px; height: 18px;
      background: #e74c3c;
      color: white;
      font-size: 11px;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-weight: bold;
      border: 2px solid white;
    }

    .evo-widget-panel {
      position: fixed;
      bottom: 100px;
      right: 24px;
      width: 380px;
      max-width: calc(100vw - 32px);
      height: 600px;
      max-height: calc(100vh - 140px);
      background: white;
      border-radius: 16px;
      box-shadow: 0 12px 48px rgba(0, 0, 0, 0.18);
      z-index: 999999;
      display: none;
      flex-direction: column;
      overflow: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 14px;
      color: #1a2620;
    }
    .evo-widget-panel.open { display: flex; }

    .evo-w-header {
      background: #0a3d2e;
      color: white;
      padding: 14px 16px;
      display: flex; align-items: center; gap: 10px;
    }
    .evo-w-logo {
      width: 32px; height: 32px;
      background: #2d8a5f;
      color: #f4c542;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-weight: bold;
      font-size: 14px;
    }
    .evo-w-title { flex: 1; line-height: 1.2; }
    .evo-w-title strong { font-size: 14px; display: block; }
    .evo-w-title small { color: #5cb98a; font-size: 11px; }
    .evo-w-close {
      background: none;
      border: none;
      color: white;
      font-size: 22px;
      cursor: pointer;
      width: 28px; height: 28px;
      line-height: 1;
    }

    .evo-w-messages {
      flex: 1;
      overflow-y: auto;
      padding: 14px;
      background: #f7f9f8;
      display: flex; flex-direction: column; gap: 10px;
    }
    .evo-w-msg {
      max-width: 85%;
      padding: 9px 12px;
      border-radius: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
      word-wrap: break-word;
    }
    .evo-w-msg.bot {
      background: white;
      border: 1px solid #d8e3df;
      align-self: flex-start;
      border-bottom-left-radius: 4px;
    }
    .evo-w-msg.user {
      background: #2d8a5f;
      color: white;
      align-self: flex-end;
      border-bottom-right-radius: 4px;
    }
    .evo-w-lead-banner {
      background: linear-gradient(135deg, #2d8a5f, #5cb98a);
      color: white;
      padding: 10px 12px;
      border-radius: 10px;
      text-align: center;
      font-size: 13px;
    }
    .evo-w-typing {
      align-self: flex-start;
      display: flex; gap: 4px;
      padding: 10px 14px;
      background: white;
      border: 1px solid #d8e3df;
      border-radius: 12px;
      border-bottom-left-radius: 4px;
    }
    .evo-w-typing span {
      width: 5px; height: 5px;
      background: #2d8a5f;
      border-radius: 50%;
      animation: evo-bounce 1.4s infinite;
    }
    .evo-w-typing span:nth-child(2) { animation-delay: 0.2s; }
    .evo-w-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes evo-bounce {
      0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
      30% { transform: translateY(-4px); opacity: 1; }
    }

    .evo-w-quick {
      padding: 0 14px 8px;
      display: flex; flex-wrap: wrap; gap: 6px;
      background: #f7f9f8;
    }
    .evo-w-chip {
      background: white;
      border: 1px solid #d8e3df;
      color: #1a2620;
      padding: 5px 10px;
      border-radius: 14px;
      font-size: 12px;
      cursor: pointer;
    }
    .evo-w-chip:hover { background: #5cb98a; color: white; border-color: #5cb98a; }

    .evo-w-composer {
      border-top: 1px solid #d8e3df;
      padding: 10px;
      display: flex; gap: 6px;
      background: white;
    }
    .evo-w-composer textarea {
      flex: 1;
      border: 1px solid #d8e3df;
      border-radius: 10px;
      padding: 8px 10px;
      font-size: 13px;
      resize: none;
      font-family: inherit;
      max-height: 80px;
      outline: none;
    }
    .evo-w-composer textarea:focus { border-color: #2d8a5f; }
    .evo-w-composer button {
      background: #2d8a5f;
      color: white;
      border: none;
      border-radius: 10px;
      padding: 0 14px;
      font-weight: 600;
      cursor: pointer;
      font-size: 13px;
    }
    .evo-w-composer button:disabled { opacity: 0.5; cursor: not-allowed; }
    .evo-w-foot {
      padding: 5px 14px 8px;
      text-align: center;
      font-size: 10px;
      color: #5a6b65;
      background: white;
    }

    @media (max-width: 480px) {
      .evo-widget-panel {
        width: 100vw;
        height: 100vh;
        max-height: 100vh;
        bottom: 0;
        right: 0;
        border-radius: 0;
      }
      .evo-widget-launcher.hidden-mobile { display: none; }
    }
  `;

  // ============== HTML ==============
  const launcherHtml = `
    <div class="evo-widget-launcher" id="evo-w-launcher" title="Pýtajte sa">
      💬
      <span class="evo-widget-badge">1</span>
    </div>
  `;

  const panelHtml = `
    <div class="evo-widget-panel" id="evo-w-panel">
      <div class="evo-w-header">
        <div class="evo-w-logo">EV</div>
        <div class="evo-w-title">
          <strong>Energovision Asistent</strong>
          <small>Odpovedáme do 1 minúty</small>
        </div>
        <button class="evo-w-close" id="evo-w-close" aria-label="Zavrieť">×</button>
      </div>
      <div class="evo-w-messages" id="evo-w-messages">
        <div class="evo-w-msg bot">Dobrý deň, som virtuálny asistent Energovision. Pomôžem s otázkami o fotovoltike, batériach, wallboxoch, revíziach či trafostaniciach. Ako vám môžem pomôcť?</div>
      </div>
      <div class="evo-w-quick" id="evo-w-quick">
        <button class="evo-w-chip" data-text="Aká je cena fotovoltiky pre rodinný dom?">💰 Cena FVE</button>
        <button class="evo-w-chip" data-text="Koľko je dotácia Zelená domácnostiam?">🏛️ Dotácie</button>
        <button class="evo-w-chip" data-text="Aký výkon FVE pre spotrebu 5000 kWh ročne?">⚡ Dimenzovanie</button>
        <button class="evo-w-chip" data-text="Chcem cenovú ponuku">📋 Ponuka</button>
      </div>
      <form class="evo-w-composer" id="evo-w-form">
        <textarea id="evo-w-input" placeholder="Napíšte správu..." rows="1"></textarea>
        <button type="submit" id="evo-w-send">Poslať</button>
      </form>
      <div class="evo-w-foot">Asistent využíva AI · +421 917 424 564</div>
    </div>
  `;

  // ============== Init ==============
  function init() {
    const style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);

    const container = document.createElement("div");
    container.innerHTML = launcherHtml + panelHtml;
    document.body.appendChild(container);

    const launcher = document.getElementById("evo-w-launcher");
    const panel = document.getElementById("evo-w-panel");
    const closeBtn = document.getElementById("evo-w-close");
    const messages = document.getElementById("evo-w-messages");
    const quick = document.getElementById("evo-w-quick");
    const form = document.getElementById("evo-w-form");
    const input = document.getElementById("evo-w-input");
    const sendBtn = document.getElementById("evo-w-send");

    let history = [];

    launcher.addEventListener("click", () => {
      panel.classList.add("open");
      launcher.classList.add("hidden");
      const badge = launcher.querySelector(".evo-widget-badge");
      if (badge) badge.style.display = "none";
      setTimeout(() => input.focus(), 100);
    });

    closeBtn.addEventListener("click", () => {
      panel.classList.remove("open");
      launcher.classList.remove("hidden");
    });

    quick.addEventListener("click", (e) => {
      if (e.target.classList.contains("evo-w-chip")) {
        const text = e.target.getAttribute("data-text");
        input.value = text;
        send();
      }
    });

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      send();
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });

    function add(role, text) {
      const d = document.createElement("div");
      d.className = "evo-w-msg " + role;
      d.textContent = text;
      messages.appendChild(d);
      messages.scrollTop = messages.scrollHeight;
    }

    function typing(show) {
      let t = document.getElementById("evo-w-typing");
      if (show) {
        if (t) return;
        t = document.createElement("div");
        t.id = "evo-w-typing";
        t.className = "evo-w-typing";
        t.innerHTML = "<span></span><span></span><span></span>";
        messages.appendChild(t);
        messages.scrollTop = messages.scrollHeight;
      } else if (t) {
        t.remove();
      }
    }

    function leadBanner(text) {
      const d = document.createElement("div");
      d.className = "evo-w-lead-banner";
      d.textContent = text;
      messages.appendChild(d);
      messages.scrollTop = messages.scrollHeight;
    }

    async function send() {
      const text = input.value.trim();
      if (!text) return;
      add("user", text);
      input.value = "";
      input.disabled = true;
      sendBtn.disabled = true;
      quick.style.display = "none";
      typing(true);

      try {
        const resp = await fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ history: history, message: text })
        });
        const data = await resp.json();
        typing(false);

        if (data.error) {
          add("bot", "Prepáčte, vyskytla sa chyba. Kontaktujte priamo +421 917 424 564.");
        } else {
          add("bot", data.answer || "(prázdna odpoveď)");
          history.push({ role: "user", content: text });
          history.push({ role: "assistant", content: data.answer });
          if (data.lead_ready) {
            if (data.lead_saved) {
              leadBanner("✓ Vaše údaje sme odoslali Dominikovi z Energovision. Ozve sa do 24 hodín.");
            } else {
              leadBanner("Údaje sme zaznamenali. Pre rýchle kontaktovanie volajte +421 917 424 564.");
            }
          }
        }
      } catch (err) {
        typing(false);
        add("bot", "Chyba spojenia. Kontaktujte priamo Dominika: +421 917 424 564 alebo dominik.galaba@energovision.sk");
      }

      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
