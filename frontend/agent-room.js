/* ============================================================================
   AGENT ROOM — grokbot-style parliament chat (grokbot-clone loop.py pattern).

   Live only: GET /agents (roster + mode badge), GET /meals (chips),
   POST /chat/stream (SSE: agent_start, token*, vote, tally, result, error).

   Everything is namespaced under AR / ar*, so it cannot collide with the
   site's own globals in script.js (PRODUCTS, MEALS, currentMeal, ...).
   ========================================================================= */
var AR = {
  agents: [], meals: [], mode: null, states: {}, last: {},
  focused: null, running: false, booted: false,
  turn: 0,          // bumped per prompt — bubble ids are turn-scoped so a new
                    // turn can never write into the previous turn's bubble
  bubbles: {},      // handle -> its newest bubble (scroll / rail target)
  dms: {},          // handle -> private-window transcript
  dm: null, dmBusy: false
};
var AR_MIXOLOGIST = {
  handle: "MIXOLOGIST", name: "Mixologist",
  role: "orchestrator — runs the pipeline, talks to you",
  emoji: "\ud83c\udf78", color: "#70B96B"
};

/* Served by the backend at :8000, "" is right. Opened straight off disk or on
   another local port, the API still lives at :8000. */
var AR_API = (location.protocol === "file:" ||
              (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname) && location.port !== "8000"))
  ? "http://localhost:8000" : "";

function arEl(id) { return document.getElementById(id); }
function arEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
  });
}
function arAvatar(a) {
  return '<div class="ar-av" style="background:' + (a.color || "#FFD52A") +
         ';color:#170052">' + (a.emoji || "•") + "</div>";
}

function openAgentRoom() {
  arEl("agent-room").setAttribute("data-open", "true");
  arEl("agent-room").setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  if (!AR.booted) arBoot();
  setTimeout(function () { var i = arEl("ar-input"); if (i) i.focus(); }, 250);
}
function closeAgentRoom() {
  arEl("agent-room").setAttribute("data-open", "false");
  arEl("agent-room").setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
}
document.addEventListener("keydown", function (e) {
  if (e.key !== "Escape" || arEl("agent-room").getAttribute("data-open") !== "true") return;
  if (AR.dm) arDmClose(); else closeAgentRoom();
});

async function arBoot() {
  AR.booted = true;
  try {
    var a = await (await fetch(AR_API + "/agents")).json();
    AR.agents = a.agents || [];
    AR.mode = a;
    arEl("ar-mode").textContent = a.mode === "live"
      ? "live · " + a.provider + " · " + a.model
      : "engine live · template voice (no api key)";
  } catch (e) { arEl("ar-mode").textContent = "backend unreachable"; }
  try {
    AR.meals = await (await fetch(AR_API + "/meals")).json();
    arEl("ar-meals").innerHTML = AR.meals.map(function (m) {
      return '<button class="ar-chip" type="button" onclick="arChip(' +
             JSON.stringify(m.name).replace(/"/g, "&quot;") + ')">' + arEsc(m.emoji + " " + m.name) + "</button>";
    }).join("");
  } catch (e) {}
  arReset();
}

function arRenderRail() {
  arEl("ar-rail").innerHTML = AR.agents.map(function (a) {
    var st = AR.states[a.handle] || { cls: "idle", label: "idle" };
    var sel = AR.focused === a.handle;
    return '<div class="ar-agent" role="button" tabindex="0" aria-pressed="' + sel + '" ' +
        'onclick="arFocus(\'' + a.handle + '\')" onkeydown="arRailKey(event,\'' + a.handle + '\')">' +
      arAvatar(a) +
      '<span style="min-width:0;flex:1">' +
        '<span class="ar-nm">' + arEsc(a.name) + "</span>" +
        '<span class="ar-role">' + arEsc(a.role) + "</span>" +
        '<span class="ar-prev">' + arEsc(AR.last[a.handle] || "listening for the next meal") + "</span>" +
      "</span>" +
      '<span style="display:flex;flex-direction:column;gap:.3rem;align-items:flex-end">' +
        '<span class="ar-st ' + st.cls + '" id="ar-st-' + a.handle + '">' + arEsc(st.label) + "</span>" +
        '<span class="ar-dm-btn" role="button" tabindex="0" title="Private window" ' +
          'onclick="event.stopPropagation();arDmOpen(\'' + a.handle + '\')" ' +
          'onkeydown="event.stopPropagation();if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();arDmOpen(\'' + a.handle + '\');}">\ud83d\udcac DM</span>' +
      "</span>" +
    "</div>";
  }).join("") || '<div class="ar-sys">No agents loaded.</div>';
}
function arRailKey(ev, handle) {
  if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); arFocus(handle); }
}
function arState(handle, cls, label) {
  AR.states[handle] = { cls: cls, label: label };   // survives arRenderRail()
  var n = document.getElementById("ar-st-" + handle);
  if (n) { n.className = "ar-st " + cls; n.textContent = label; }
  arRenderRail();
}
function arFocus(handle) {
  AR.focused = handle;
  arRenderRail();
  var b = AR.bubbles[handle];
  if (b) b.scrollIntoView({ behavior: "smooth", block: "center" });
  arDmOpen(handle);          // tapping an agent opens its private window
}
/* One chip per real database read: name, which DB, timing. The point of the tool
   loop is that you can SEE the agent went and looked, so this is not decoration. */
function arToolChips(list) {
  if (!list || !list.length) return "";
  return '<div class="ar-tools">' + list.map(function (t) {
    var db = t.offline ? "offline" : (t.source || "db");
    var arg = t.args ? Object.keys(t.args).map(function (k) {
      return t.args[k];
    }).join(" / ") : "";
    return '<span class="ar-tool' + (t.ok ? "" : " bad") + '" title="' +
      arEsc((t.error || "ok") + (t.ms != null ? " \u00b7 " + t.ms + "ms" : "")) + '">' +
      "\ud83d\udd27 " + arEsc(t.tool || "tool") +
      (arg ? "(" + arEsc(String(arg).slice(0, 28)) + ")" : "") +
      " \u00b7 " + arEsc(db) + "</span>";
  }).join("") + "</div>";
}

function arSys(html) {
  var d = document.createElement("div");
  d.className = "ar-sys";
  d.innerHTML = html;
  arEl("ar-inner").appendChild(d);
  arScroll();
}
function arDots(handles) {
  return handles.map(function (h) {
    var a = AR.agents.filter(function (x) { return x.handle === h; })[0] || {};
    return '<i style="background:' + (a.color || "#FFD52A") + '"></i>';
  }).join("");
}
function arUser(text) {
  var d = document.createElement("div");
  d.className = "ar-msg me";
  d.innerHTML = '<div><div class="ar-who" style="text-align:right">You</div>' +
    '<div class="ar-bub">' + arEsc(text) + "</div></div>";
  arEl("ar-inner").appendChild(d);
  arScroll();
}
function arAgentBy(handle) {
  // roster handles are upper-case (KAVACH); the DM API path is lower-case, so
  // match case-insensitively instead of falling back to a nameless agent.
  var h = String(handle || "").toUpperCase();
  if (h === "MIXOLOGIST") return AR_MIXOLOGIST;
  return AR.agents.filter(function (a) { return String(a.handle).toUpperCase() === h; })[0] ||
         { handle: h, name: h, role: "council agent", emoji: "\u2022", color: "#FFD52A" };
}
/* One bubble per agent PER TURN. Ids used to be "<kind>-<handle>", so from the
   second prompt on getElementById() returned turn 1's node and every new token
   was appended to the old bubble while the fresh bubble stayed empty. live[handle]
   now holds the element refs, and the ids carry the turn number. */
function arBubble(a, live) {
  var d = document.createElement("div");
  var turn = live.turn;
  d.className = "ar-msg";
  d.id = "ar-bubble-" + turn + "-" + a.handle;
  d.innerHTML = arAvatar(a) +
    '<div style="min-width:0">' +
      '<div class="ar-who">' + arEsc(a.name) + " \u00b7 " + arEsc(a.role) + "</div>" +
      '<div class="ar-bub typing" id="ar-text-' + turn + "-" + a.handle + '" data-text=""></div>' +
      '<div class="ar-tags" id="ar-tags-' + turn + "-" + a.handle + '"></div>' +
    "</div>";
  arEl("ar-inner").appendChild(d);
  live[a.handle] = {
    bubble: d,
    text: d.querySelector(".ar-bub"),
    tags: d.querySelector(".ar-tags")
  };
  AR.bubbles[a.handle] = d;
  arScroll();
  return live[a.handle];
}
function arEnsure(handle, live) {
  if (!live[handle]) arBubble(arAgentBy(handle), live);
  return live[handle];
}
function arAppend(live, handle, delta) {
  var ref = live[handle];
  if (!ref || !ref.text) return;
  ref.text.dataset.text = (ref.text.dataset.text || "") + delta;   // never read back innerText
  ref.text.textContent = ref.text.dataset.text;
  arScroll();
}
function arSettleTyping(live) {
  Object.keys(live).forEach(function (h) {
    if (live[h] && live[h].text) live[h].text.classList.remove("typing");
  });
}
function arScroll() { var t = arEl("ar-thread"); t.scrollTop = t.scrollHeight; }

function arReset() {
  arEl("ar-inner").innerHTML = "";
  AR.states = {}; AR.last = {}; AR.focused = null; AR.bubbles = {};
  AR.dms = {}; arDmClose(); arEl("ar-dm-thread").innerHTML = "";
  arRenderRail();
  arSys("Room ready · " + AR.agents.length + " agents on the roster. Tell them what you ate, or pick a meal below.");
}
function arChip(name) { arEl("ar-input").value = name; arSend(null); }

async function arSend(ev) {
  if (ev) ev.preventDefault();
  if (AR.running) return;
  var text = (arEl("ar-input").value || "").trim();
  if (!text) return;
  AR.running = true;
  arEl("ar-send").disabled = true;
  arEl("ar-input").value = "";
  arUser(text);
  AR.agents.forEach(function (a) { arState(a.handle, "idle", "waiting"); });
  var live = { turn: ++AR.turn };

  try {
    var res = await fetch(AR_API + "/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text })
    });
    if (!res.ok || !res.body) throw new Error("HTTP " + res.status);
    var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
    for (;;) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buf += dec.decode(chunk.value, { stream: true });
      var frames = buf.split("\n\n");
      buf = frames.pop();
      for (var i = 0; i < frames.length; i++) {
        var line = frames[i].split("\n").filter(function (l) { return l.indexOf("data:") === 0; })[0];
        if (!line) continue;
        var obj;
        try { obj = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        arHandle(obj, live);
      }
    }
  } catch (err) {
    arSys('<span style="color:var(--ar-pink)">Could not reach the backend (' +
          arEsc(err.message) + "). Is the server running on :8000?</span>");
  } finally {
    AR.running = false;
    arEl("ar-send").disabled = false;
    arSettleTyping(live);
    arEl("ar-input").focus();
  }
}

function arHandle(ev, live) {
  switch (ev.event) {
    case "agent_start":
      arBubble(ev.agent, live);
      arState(ev.agent.handle, "thinking", "thinking");
      break;
    case "token": {
      var fresh = !live[ev.agent];
      arEnsure(ev.agent, live);
      if (fresh) arState(ev.agent, "thinking", "speaking");
      arAppend(live, ev.agent, ev.delta);
      break;
    }
    case "vote": {
      var ref = live[ev.agent];
      if (ref) {
        ref.text.classList.remove("typing");
        var warm = ev.vote === "no";
        ref.tags.innerHTML = '<span class="ar-tag ' + (warm ? "no" : "yes") + '">' +
          (warm ? "objects" : "approves") + '</span><span class="ar-tag voice">' +
          arEsc(ev.source === "template" ? "template voice" : ev.source) + "</span>";
      }
      AR.last[ev.agent] = ev.reason;
      arState(ev.agent, ev.vote === "no" ? "no" : "yes", ev.vote === "no" ? "objects" : "approves");
      break;
    }
    case "error": {
      var ref2 = live[ev.agent];
      if (ref2) {
        var line = document.createElement("div");
        line.className = "ar-err";
        line.textContent = "model call failed — " + (ev.provider || "provider") +
          (ev.status ? " " + ev.status : "") + ", fell back to template voice";
        ref2.bubble.lastElementChild.appendChild(line);
      }
      break;
    }
    case "tool": {
      /* The mixologist read a real database — show it in the transcript. */
      if (ev.tool && ev.tool.indexOf("gut_engine") === 0) break;
      arSys(arToolChips([ev]));
      break;
    }
    case "done":
      arSettleTyping(live);       // the running caret stops when the turn ends
      break;
    case "tally": {
      var yes = ev.votes.filter(function (v) { return v.vote === "yes"; });
      var no = ev.votes.filter(function (v) { return v.vote === "no"; });
      arSys("<b>" + ev.yes + " of " + ev.total + "</b> approve " +
            arDots(yes.map(function (v) { return v.agent; })) +
            (no.length ? " · " + no.length + " object " + arDots(no.map(function (v) { return v.agent; })) : ""));
      break;
    }
    case "result":
      AR.agents.forEach(function (a) { arState(a.handle, "idle", "deliberated"); });
      arRx(ev.result);
      break;
    case "unknown_meal":
      arSys("That meal isn't in the demo set — the Mixologist listed what it knows above.");
      break;
  }
}

function arRx(r) {
  var d = r.drink;
  var card = document.createElement("div");
  card.className = "ar-rx";
  card.innerHTML =
    '<div class="kick">Prescription</div>' +
    "<h3>" + arEsc(d.emoji) + " " + arEsc(d.name) + "</h3>" +
    "<p><b>Gut score " + r.gut_score + "/10</b> · gaps: " + arEsc((r.gaps || []).join(", ") || "none") + "</p>" +
    "<p>" + arEsc(d.prep) + "</p>" +
    '<p style="opacity:.8">' + arEsc(d.explanation) + "</p>" +
    '<div class="ar-kv">' +
      "<div><span>Ingredients</span><b>" + arEsc((d.ingredients || []).join(", ")) + "</b></div>" +
      "<div><span>Shared molecules</span><b>" + d.shared_molecules + "</b></div>" +
      "<div><span>Pairing score</span><b>" + d.similarity_score + "</b></div>" +
      "<div><span>Agents approving</span><b>" + (r.tally ? r.tally.yes + "/" + r.tally.total : "?") + "</b></div>" +
    "</div>" +
    '<p style="opacity:.7;font-size:.76rem">Concept mockup · scores are rule-based heuristics and the votes are simulated — not medical advice.</p>';
  arEl("ar-inner").appendChild(card);
  arScroll();
}

/* ---- private 1:1 window ---------------------------------------------------
   One agent, one thread, no keyword routing: POST /api/agents/<handle>/chat.
   The answer comes from the LLM in that agent's own persona; when the model is
   unreachable the persona's template line answers and the tag says so. */
function arDmOpen(handle) {
  var a = arAgentBy(handle);
  AR.dm = a.handle;
  AR.focused = a.handle;
  if (!AR.dms[a.handle]) AR.dms[a.handle] = [];
  arEl("ar-dm-av").textContent = a.emoji || "\u2022";
  arEl("ar-dm-av").style.background = a.color || "#FFD52A";
  arEl("ar-dm-nm").textContent = a.name;
  arEl("ar-dm-role").textContent = a.role;
  arEl("ar-dm").setAttribute("data-open", "true");
  arEl("ar-dm").setAttribute("aria-hidden", "false");
  arRenderRail();
  arDmRender();
  setTimeout(function () { var i = arEl("ar-dm-input"); if (i) i.focus(); }, 120);
}
function arDmClose() {
  AR.dm = null;
  var p = arEl("ar-dm");
  if (p) { p.setAttribute("data-open", "false"); p.setAttribute("aria-hidden", "true"); }
}
function arDmRender() {
  var box = arEl("ar-dm-thread"), h = AR.dm;
  if (!box) return;
  if (!h) { box.innerHTML = ""; return; }
  var a = arAgentBy(h), msgs = AR.dms[h] || [];
  if (!msgs.length) {
    box.innerHTML = '<div class="ar-sys">Private line to ' + arEsc(a.name) +
      " \u2014 the rest of the parliament cannot see this.</div>";
    return;
  }
  box.innerHTML = msgs.map(function (m) {
    if (m.who === "you") {
      return '<div class="ar-dm-msg me"><div class="ar-bub">' + arEsc(m.text) + "</div></div>";
    }
    var tag = m.source === "template" ? " \u00b7 template voice"
            : m.source === "error" ? " \u00b7 offline" : "";
    return '<div class="ar-dm-msg"><div class="ar-who">' + arEsc(a.name) + tag + "</div>" +
      '<div class="ar-bub">' + arEsc(m.text) + "</div>" + arToolChips(m.tools) + "</div>";
  }).join("");
  box.scrollTop = box.scrollHeight;
}
async function arDmSend(ev) {
  if (ev) ev.preventDefault();
  if (AR.dmBusy || !AR.dm) return;
  var handle = AR.dm, input = arEl("ar-dm-input");
  var text = (input.value || "").trim();
  if (!text) return;
  AR.dmBusy = true;
  arEl("ar-dm-send").disabled = true;
  input.value = "";
  var thread = AR.dms[handle];
  var history = thread.slice(-8).map(function (m) {
    return { role: m.who === "you" ? "user" : "assistant", content: m.text };
  });
  thread.push({ who: "you", text: text });
  thread.push({ who: "agent", text: "\u2026", pending: true });
  arDmRender();
  try {
    var res = await fetch(AR_API + "/api/agents/" + encodeURIComponent(handle) + "/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, history: history })
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    var data = await res.json();
    thread.pop();
    thread.push({ who: "agent", text: data.text || "(no answer)", source: data.source,
                  tools: data.tools_used });
  } catch (err) {
    thread.pop();
    thread.push({
      who: "agent",
      text: "I can't reach my side of the room right now (" + err.message +
            "). Is the backend running on :8000?",
      source: "error"
    });
  } finally {
    AR.dmBusy = false;
    arEl("ar-dm-send").disabled = false;
    arDmRender();
    input.focus();
  }
}

