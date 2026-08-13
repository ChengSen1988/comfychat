// ComfyChat v0.3 — workflow list, param form, chat history, SSE realtime stream
(function () {
  "use strict";

  let workflows = [];
  let currentWorkflow = null;
  let currentSkill = null;        // native skill mode (e.g. rmbg), bypasses ComfyUI
  let skillFiles = [];            // skill-mode selected image files
  let paramEls = {};
  let currentConvId = null;       // current conversation id (null = unsaved)
  let sending = false;

  const $ = (id) => document.getElementById(id);
  const chatContainer = $("chatContainer");
  const workflowList = $("workflowList");
  const convList = $("convList");
  const paramsRow = $("paramsRow");
  const wfName = $("wfName");
  const wfPath = $("wfPath");
  const userInput = $("userInput");
  const sendButton = $("sendButton");
  const countInput = $("countInput");
  const comfyStatusText = $("comfyStatusText");
  const comfyStatus = $("comfyStatus");
  const paramPanel = $("paramPanel");
  const presetBar = $("presetBar");
  const presetSelect = $("presetSelect");
  const presetSaveBtn = $("presetSave");
  const presetDelBtn = $("presetDel");
  const skillList = $("skillList");
  const skillUploadBar = $("skillUploadBar");
  const skillTag = $("skillTag");
  const countGroup = $("countGroup");

  // in-flight generation (cross-conversation keep-alive): moved into a hidden container when switching away, restored on return
  const hiddenRuns = document.createElement("div");
  hiddenRuns.id = "hiddenRuns";
  hiddenRuns.style.display = "none";
  document.body.appendChild(hiddenRuns);
  let activeRun = null;   // {convId, ai, live, results}

  // ── utils ──
  const scrollBtn = $("scrollToBottom");
  function isNearBottom() {
    return chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight < 120;
  }
  function updateScrollBtn() {
    if (scrollBtn) scrollBtn.hidden = isNearBottom();
  }
  function scrollBottom(force) {
    // auto-follow only while the user is near the bottom; otherwise do not disturb (show "back to bottom" button)
    if (force || isNearBottom()) chatContainer.scrollTop = chatContainer.scrollHeight;
    updateScrollBtn();
  }
  chatContainer.addEventListener("scroll", updateScrollBtn);
  if (scrollBtn) {
    scrollBtn.addEventListener("click", () => {
      chatContainer.scrollTop = chatContainer.scrollHeight;
      updateScrollBtn();
    });
  }
  function fmtTs(ts) {
    const d = new Date(ts);
    return (d.getMonth() + 1) + "/" + d.getDate() + " " +
      String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  function addMsg(text, cls) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + (cls === "user" ? "user" : "ai");
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = cls === "user" ? "👤" : "🤖";
    const bw = document.createElement("div");
    bw.className = "bubble-wrap";
    const bubble = document.createElement("div");
    bubble.className = "bubble" + (cls === "error" ? " error" : "");
    bubble.textContent = text || "";
    bw.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(bw);
    chatContainer.appendChild(wrap);
    scrollBottom();
    return { wrap, bubble, bw };
  }

  function addAiBubble() {
    return addMsg("", "ai");
  }

  // ── dark mode ──
  function applyTheme(dark) {
    document.documentElement.setAttribute("data-theme", dark ? "dark" : "light");
    $("themeBtn").textContent = dark ? "☀️" : "🌙";
    try { localStorage.setItem("comfychat_theme", dark ? "dark" : "light"); } catch (e) {}
  }
  $("themeBtn").addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") !== "dark";
    applyTheme(dark);
  });
  try {
    applyTheme(localStorage.getItem("comfychat_theme") === "dark");
  } catch (e) { applyTheme(false); }

  // ── ComfyUI status ──
  async function refreshStatus() {
    try {
      const r = await fetch("/api/comfy_status");
      const d = await r.json();
      const on = !!d.online;
      comfyStatusText.textContent = on ? "ComfyUI online" : "ComfyUI offline";
      comfyStatus.classList.toggle("online", on);
      comfyStatus.classList.toggle("offline", !on);
    } catch (e) {
      comfyStatusText.textContent = "Status check failed";
      comfyStatus.classList.add("offline");
    }
  }

  // ── workflow list ──
  async function loadWorkflows() {
    workflowList.innerHTML = '<li class="empty">Loading...</li>';
    try {
      const r = await fetch("/api/workflows");
      workflows = await r.json();
    } catch (e) { workflows = []; }
    if (!workflows.length) {
      workflowList.innerHTML = '<li class="empty">No workflows</li>';
      return;
    }
    workflowList.innerHTML = "";
    const icons = ["🎨", "🖼️", "✨", "🧩", "🎭", "🛠️", "📐", "🌈", "🎬", "🔊"];
    workflows.forEach((wf, i) => {
      const li = document.createElement("li");
      const ico = document.createElement("span");
      ico.className = "ico";
      ico.textContent = icons[i % icons.length];
      const name = document.createElement("span");
      name.className = "it-name";
      name.textContent = wf.name;
      li.appendChild(ico);
      li.appendChild(name);
      li.addEventListener("click", () => selectWorkflow(wf));
      workflowList.appendChild(li);
      if (i === 0) selectWorkflow(wf);
    });
    $("wfCount").textContent = workflows.length + " workflows";
  }

  // ── conversation history ──
  async function loadConversations(autoOpen = false) {
    try {
      const r = await fetch("/api/conversations");
      const list = await r.json();
      convList.innerHTML = "";
      if (!list.length) {
        convList.innerHTML = '<li class="empty">No conversations</li>';
        // auto-create a conversation on first entry so messages always have somewhere to persist
        if (autoOpen) await newConversation();
        return;
      }
      list.forEach((c) => {
        const li = document.createElement("li");
        li.dataset.cid = c.id;
        if (c.id === currentConvId) li.classList.add("active");
        const ico = document.createElement("span");
        ico.className = "ico";
        ico.textContent = "💬";
        const name = document.createElement("span");
        name.className = "it-name";
        name.textContent = c.title || "New Chat";
        const sub = document.createElement("span");
        sub.className = "it-sub";
        sub.textContent = fmtTs(c.updated);
        const del = document.createElement("span");
        del.className = "del";
        del.textContent = "🗑";
        del.title = "Delete conversation";
        del.addEventListener("click", async (e) => {
          e.stopPropagation();
          await fetch("/api/conversations/" + c.id, { method: "DELETE" });
          if (currentConvId === c.id) { currentConvId = null; clearChat(); }
          loadConversations();
        });
        li.appendChild(ico);
        li.appendChild(name);
        li.appendChild(sub);
        li.appendChild(del);
        li.addEventListener("click", () => openConversation(c.id));
        convList.appendChild(li);
      });
      // restore state after refresh: reopen the last-viewed conversation (localStorage), else latest
      if (autoOpen && !currentConvId) {
        let target = null;
        try {
          const saved = localStorage.getItem("comfychat_lastConv");
          if (saved) target = list.find((c) => c.id === saved) || null;
        } catch (e) {}
        openConversation(target ? target.id : list[0].id);
      }
    } catch (e) {}
  }

  async function openConversation(id) {
    try {
      const r = await fetch("/api/conversations/" + id);
      const conv = await r.json();
      if (conv.error) return;
      currentConvId = id;
      try { localStorage.setItem("comfychat_lastConv", id); } catch (e) {}
      clearChat();
      let pendingUser = null;   // nearest user bubble, used to pair with assistant exec and attach workflow name + 🔄
      (conv.messages || []).forEach((m) => {
        if (m.role === "user" && m.kind === "text") {
          pendingUser = addMsg(m.content || "", "user");
          if (m.resources && m.resources.length) renderUserImages(pendingUser.bw, m.resources);
        } else if (m.role === "assistant") {
          const ai = addAiBubble();
          // bubble shows a completion summary (image count only; duration is unreliable after refresh)
          if (m.kind === "resources" && m.resources && m.resources.length) {
            ai.bubble.textContent = "✓ Generated " + m.resources.length + " image(s)";
          }
          if (m.kind === "resources" && m.resources && m.resources.length) {
            renderResourceGrid(ai.bw, m.resources, m.exec);
          } else if (m.kind === "text") {
            ai.bubble.textContent = m.content || "";
            if ((m.content || "").startsWith("✗")) ai.bubble.classList.add("error");
          }
          if (pendingUser && m.exec) {
            addUserTools(pendingUser, m.exec);   // attach workflow name + batch Regenerate under the user bubble
            pendingUser = null;
          }
        }
      });
      await loadConversations();   // wait for the list rebuild so the highlight is timely
      restoreActiveRun();          // restore display if this conversation has in-flight generation
      checkActiveTasks();          // refresh recovery: poll background tasks, auto-refresh when done
    } catch (e) {
      console.error("openConversation error:", e);
    }
  }

  // ── refresh recovery: background task still running (SSE gone), poll /api/active_tasks until done ──
  let _activeTaskTimer = null;
  async function checkActiveTasks() {
    if (!currentConvId) return;
    let tasks = [];
    try {
      const r = await fetch("/api/active_tasks?conv_id=" + encodeURIComponent(currentConvId));
      tasks = await r.json();
    } catch (e) { return; }
    if (!tasks || !tasks.length) { stopActiveTaskPoll(); return; }
    // insert one hint per task (dedup by started_at, no duplicates)
    tasks.forEach((t) => {
      const key = "task_" + t.started_at;
      if (document.querySelector("[data-task='" + key + "']")) return;
      const ai = addAiBubble();
      ai.wrap.dataset.task = key;
      ai.bubble.textContent = "⚙️ Background task running" +
        (t.prompt ? ": " + t.prompt.slice(0, 40) + "" : "") + " ... will auto-refresh when done";
      ai.bubble.style.borderLeft = "3px solid var(--accent)";
    });
    // start polling: reload conversation messages when all tasks finish
    if (!_activeTaskTimer) {
      _activeTaskTimer = setInterval(async () => {
        let t2 = [];
        try {
          const r2 = await fetch("/api/active_tasks?conv_id=" + encodeURIComponent(currentConvId));
          t2 = await r2.json();
        } catch (e) { return; }
        if (!t2.length) {
          stopActiveTaskPoll();
          openConversation(currentConvId);   // background results written to the conversation, reload to display
        }
      }, 2000);
    }
  }
  function stopActiveTaskPoll() {
    if (_activeTaskTimer) { clearInterval(_activeTaskTimer); _activeTaskTimer = null; }
  }

  async function newConversation() {
    try {
      const r = await fetch("/api/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      const d = await r.json();
      currentConvId = d.id;
      clearChat();
      loadConversations();
    } catch (e) {}
  }

  $("newChatBtn").addEventListener("click", newConversation);
  $("clearConvs").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/conversations");
      const list = await r.json();
      for (const c of list) await fetch("/api/conversations/" + c.id, { method: "DELETE" });
      currentConvId = null;
      clearChat();
      loadConversations();
    } catch (e) {}
  });

  function clearChat() {
    // in-flight generation is not destroyed: move only this run's messages (user+AI) into the hidden container
    if (activeRun && activeRun.msgWraps) {
      activeRun.msgWraps.forEach((m) => {
        if (m && m.parentNode === chatContainer) {
          hiddenRuns.appendChild(m);
        }
      });
    }
    chatContainer.innerHTML = "";
    gallery = [];
  }

  // restore in-flight generation when switching back (if any), including user messages
  function restoreActiveRun() {
    if (!activeRun || activeRun.convId !== currentConvId) return;
    // move this run's messages back (user + AI messages, keep order)
    (activeRun.msgWraps || []).forEach((m) => {
      if (m && m.parentNode !== chatContainer) {
        chatContainer.appendChild(m);   // moved back from hiddenRuns
      }
    });
    // rebuild gallery (images back to visible)
    gallery = Array.from(chatContainer.querySelectorAll(".result-card img"))
      .map((i) => i.src);
    // hint that generation is still running (once only, avoid duplicate lines when switching)
    if (!activeRun._hintShown) {
      activeRun._hintShown = true;
      const line = document.createElement("div");
      line.className = "status-line";
      line.textContent = "· This conversation is still generating...";
      activeRun.ai.bubble.appendChild(line);
      activeRun._hintLine = line;
    }
    scrollBottom();
  }

  // ── param form ──
  // workflow health-check banner (top of panel): turn "unexpected errors" into early warnings
  function renderHealth(health) {
    const old = document.querySelector(".wf-health");
    if (old) old.remove();
    if (!health || !health.length) return;
    const box = document.createElement("div");
    box.className = "wf-health";
    health.forEach((h) => {
      const row = document.createElement("div");
      row.className = "wf-health-row " + (h.level || "info");
      row.textContent = (h.level === "error" ? "✗ " : h.level === "warn" ? "⚠ " : "ℹ ") + h.msg;
      box.appendChild(row);
    });
    const panel = document.getElementById("paramPanel");
    const body = document.getElementById("paramsRow");
    if (panel && body) panel.insertBefore(box, body);
  }

  function renderParams() {
    paramsRow.innerHTML = "";
    paramEls = {};
    // when "expand all": show every workflow param (ignoring the PIN whitelist), otherwise only pinned node params
    const source = (showAllParams && currentWorkflow && currentWorkflow.allParams)
      ? currentWorkflow.allParams : (currentWorkflow ? currentWorkflow.params : []);
    if (!currentWorkflow || !source.length) {
      const hint = document.createElement("div");
      hint.style.cssText = "font-size:12px;color:var(--muted);width:100%";
      hint.textContent = "This workflow has no adjustable params (will run with defaults)";
      paramsRow.appendChild(hint);
      return;
    }
    // the prompt is carried by the bottom input (backend already excludes C2A1 node text from params),
    // render all remaining params here (incl. negative prompt etc.)
    const groups = {};
    source.forEach((p) => {
      (groups[p.node_label] = groups[p.node_label] || []).push(p);
    });
    for (const [label, items] of Object.entries(groups)) {
      const t = document.createElement("div");
      t.className = "group-title";
      t.style.cssText = "width:100%;font-size:11px;color:var(--accent);font-weight:700;margin-top:6px;letter-spacing:0.5px";
      t.textContent = "◆ " + label;
      paramsRow.appendChild(t);
      items.forEach((p) => paramsRow.appendChild(buildParam(p)));
    }
  }

  function buildParam(p) {
    const group = document.createElement("div");
    group.className = "param-group";
    if (p.type === "text" && (p.multiline || ["text", "prompt", "negative"].includes(p.name))) {
      group.classList.add("is-long-text");   // long text takes its own row (incl. C2AParam TEXTAREA)
    }
    const label = document.createElement("label");
    let lt = p.label;
    if (["seed", "noise_seed"].includes(p.name)) lt += " (empty = random)";
    label.textContent = lt;
    group.appendChild(label);

    let el;
    if (p.type === "text") {
      const isLong = p.multiline || ["text", "prompt", "negative"].includes(p.name);
      if (isLong) {
        el = document.createElement("textarea"); el.rows = 2;
        // auto-height (prompt edited inside the panel)
        const auto = () => { el.style.height = "auto"; el.style.height = Math.min(el.scrollHeight, 120) + "px"; };
        el.addEventListener("input", auto);
        setTimeout(auto, 0);
      }
      else { el = document.createElement("input"); el.type = "text"; el.style.minWidth = "180px"; }
      el.value = p.default ?? "";
    } else if (p.type === "int") {
      el = document.createElement("input"); el.type = "number"; el.step = "1";
      // seed params default empty (= random); other ints use workflow defaults
      el.value = ["seed", "noise_seed"].includes(p.name) ? "" : (p.default ?? 0);
    } else if (p.type === "float") {
      el = document.createElement("input"); el.type = "number"; el.step = "any"; el.value = p.default ?? 0;
    } else if (p.type === "combo") {
      el = document.createElement("select");
      const opts = (p.options || []).filter((o) => o !== null && o !== undefined);
      const src = opts.length ? opts : [p.default].filter((o) => o !== null && o !== undefined);
      src.forEach((o) => {
        const opt = document.createElement("option");
        opt.value = o; opt.textContent = o;
        if (o === p.default) opt.selected = true;
        el.appendChild(opt);
      });
    } else if (p.type === "bool") {
      el = document.createElement("input"); el.type = "checkbox"; el.checked = !!p.default;
    } else if (p.type === "image") {
      group.classList.add("is-image");   // triggers .thumb-list full-row + horizontal scroll styles
      el = document.createElement("input");
      el.type = "file"; el.accept = "image/*"; el.multiple = true;   // supports multi-select: processed per image in order
      const list = document.createElement("div");
      list.className = "thumb-list";
      const cnt = document.createElement("span");
      cnt.className = "img-count";
      const itemState = { el, type: "image", uploadFilename: null, uploadFiles: null };
      const renderThumbs = () => {
        list.innerHTML = "";
        const fs = el.files ? Array.from(el.files) : [];
        // create the placeholder item immediately (spinner)
        const mk = (src, onDel) => {
          const it = document.createElement("div");
          it.className = "thumb-item";
          const img = document.createElement("img");
          const spin = document.createElement("div");
          spin.className = "thumb-spin";
          const del = document.createElement("span");
          del.className = "thumb-del";
          del.textContent = "✕";
          del.title = "Remove this image";
          del.addEventListener("click", (e) => {
            e.stopPropagation();
            onDel();
            it.remove();
            const n = (el.files ? el.files.length : 0) + (itemState.uploadFiles || []).length;
            cnt.textContent = n > 1 ? "" + n + " selected (processed in order)" : "";
          });
          if (src) {
            img.src = src;
            it.appendChild(img);
          } else {
            it.appendChild(spin);   // while waiting: show only the spinner, no blank img
          }
          it.appendChild(del);
          list.appendChild(it);
          return { it, img, spin };
        };
        // batched concurrent compression (small concurrency + yield main thread per image so the browser paints each frame, one by one)
        const LIMIT = 3;
        const jobs = fs.map((f) => ({ f, el: mk(null, () => {
          const dt = new DataTransfer();
          Array.from(el.files).forEach((x) => { if (x !== f) dt.items.add(x); });
          el.files = dt.files;
        }) }));
        let idx = 0, active = 0;
        const pump = () => {
          while (active < LIMIT && idx < jobs.length) {
            const job = jobs[idx++];
            active++;
            makeThumbUrl(job.f, 160, (src) => {
              active--;
              job.el.img.src = src;
              // replace the spinner placeholder with img (img inserted only now; no blank space while waiting)
              job.el.it.insertBefore(job.el.img, job.el.spin);
              if (job.el.spin.parentNode) job.el.spin.parentNode.removeChild(job.el.spin);
              setTimeout(pump, 0);   // yield the main thread: browser paints this image first, then processes the next
            });
          }
        };
        setTimeout(pump, 0);   // paint the placeholder (spinner) first, then start compression
        // uploaded images: server-compressed urls, same placeholder + batched loading
        const ups = (itemState.uploadFiles || []).map((fn) => ({ fn, el: mk(null, () => {
          itemState.uploadFiles = (itemState.uploadFiles || []).filter((x) => x !== fn);
        }) }));
        let ui = 0, ua = 0;
        const pumpU = () => {
          while (ua < LIMIT && ui < ups.length) {
            const j = ups[ui++];
            ua++;
            const url = comfyThumbUrl(j.fn, "input");
            const im = new Image();
            im.onload = () => {
              ua--;
              j.el.img.src = url;
              j.el.it.insertBefore(j.el.img, j.el.spin);
              if (j.el.spin.parentNode) j.el.spin.parentNode.removeChild(j.el.spin);
              setTimeout(pumpU, 0);
            };
            im.onerror = () => {
              ua--;
              j.el.img.src = url;
              j.el.it.insertBefore(j.el.img, j.el.spin);
              if (j.el.spin.parentNode) j.el.spin.parentNode.removeChild(j.el.spin);
              setTimeout(pumpU, 0);
            };
            im.src = url;
          }
        };
        setTimeout(pumpU, 0);
        const n = fs.length + (itemState.uploadFiles || []).length;
        cnt.textContent = n > 1 ? "" + n + " selected (processed in order)" : "";
      };
      el.addEventListener("change", renderThumbs);
      itemState.renderThumbs = renderThumbs;
      // order: label → count → file input → thumbs (thumbs own a row via CSS flex-basis:100%)
      group.appendChild(cnt);
      group.appendChild(el);
      group.appendChild(list);
      paramEls[p.key] = itemState;
      return group;
    }
    group.appendChild(el);
    paramEls[p.key] = { el, type: p.type, uploadFilename: null };
    return group;
  }

  // ── native skills (in-process inference, no ComfyUI) ──
  async function loadSkills() {
    try {
      const r = await fetch("/api/skills");
      const d = await r.json();
      skillList.innerHTML = "";
      const skills = Object.entries(d.skills || {});
      // hide the whole "skills" block when none are available (backend switch off)
      document.getElementById("skillSection").style.display = skills.length ? "" : "none";
      skills.forEach(([id, s]) => {
        const li = document.createElement("li");
        const it = document.createElement("span");
        it.className = "it-name";
        it.textContent = s.name;
        li.title = s.desc;
        li.appendChild(it);
        li.addEventListener("click", () => selectSkill(id, s));
        skillList.appendChild(li);
      });
    } catch (e) {}
  }

  function selectSkill(id, info) {
    if (sending) return;
    // clear workflow selection and param panel
    currentWorkflow = null;
    currentSkill = { id, name: info.name, desc: info.desc };
    Array.from(workflowList.children).forEach((li) => li.classList.remove("active"));
    Array.from(skillList.children).forEach((li) => {
      li.classList.toggle("active", li.querySelector(".it-name")?.textContent === info.name);
    });
    wfName.textContent = "Skill: " + info.name;
    wfPath.textContent = info.desc || "";
    paramsRow.innerHTML = "";
    presetBar.hidden = true;
    paramPanel.classList.add("collapsed");   // collapse the param panel in skill mode
    // skill mode: show the upload bar, hide count
    skillUploadBar.style.display = "flex";
    skillTag.textContent = info.name;
    countGroup.style.display = "none";
    userInput.placeholder = "Optional: add a note for this run";
    sendButton.disabled = skillFiles.length === 0;
  }

  function exitSkillMode() {
    currentSkill = null;
    skillUploadBar.style.display = "none";
    countGroup.style.display = "";
    userInput.placeholder = "Type a prompt, Enter to send, Shift+Enter for newline";
  }

  async function selectWorkflow(wf) {
    if (sending) return;   // lock switching while running
    exitSkillMode();
    Array.from(skillList.children).forEach((li) => li.classList.remove("active"));
    currentWorkflow = null;
    Array.from(workflowList.children).forEach((li) => {
      li.classList.toggle("active", li.querySelector(".it-name")?.textContent === wf.name);
    });
    wfName.textContent = "Loading " + wf.name + "…";
    wfPath.textContent = "";
    paramsRow.innerHTML = "";
    sendButton.disabled = true;
    try {
      const r = await fetch("/api/workflow_config?path=" + encodeURIComponent(wf.path));
      const d = await r.json();
      if (d.error) throw new Error(d.error);
      currentWorkflow = { name: d.name, path: d.path, params: d.params, allParams: d.all_params || d.params, prompt_key: d.prompt_key || null };
      wfName.textContent = d.name;
      wfPath.textContent = d.path;
      renderHealth(d.health);   // workflow health hints (unpinned text / disabled node links / no seed)
      renderParams();
      // panel collapse is controlled by header click (default closed); selecting a workflow does not auto-expand
      sendButton.disabled = false;
      presetBar.hidden = false;
      loadPresets();
    } catch (e) {
      wfName.textContent = "Failed to load: " + e.message;
    }
  }

  // ── presets (prompt+param combos, one-click load) ──
  function toast(msg) {
    let t = document.getElementById("toastTip");
    if (!t) {
      t = document.createElement("div");
      t.id = "toastTip";
      t.style.cssText = "position:fixed;bottom:130px;left:50%;transform:translateX(-50%);" +
        "background:rgba(0,0,0,.78);color:#fff;padding:8px 16px;border-radius:8px;font-size:13px;" +
        "z-index:99;transition:opacity .3s;pointer-events:none";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = "1";
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.style.opacity = "0"; }, 2200);
  }

  async function loadPresets() {
    if (!currentWorkflow) return;
    try {
      const r = await fetch("/api/presets?path=" + encodeURIComponent(currentWorkflow.path));
      const list = await r.json();
      presetSelect.innerHTML = '<option value="">(Select a preset to load)</option>';
      (list || []).forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = p.name + (p.prompt ? " — " + p.prompt.slice(0, 24) : "");
        presetSelect.appendChild(opt);
      });
      if (!list || !list.length) {
        presetSelect.options[0].textContent = "(No presets — workflow may have been renamed)";
        presetSelect.title = "This workflow has no presets. If you recently renamed the workflow file, presets may not have migrated — check the ComfyChat console for auto-repair logs.";
      }
    } catch (e) { /* ignore */ }
  }

  // collect current form params (upload local image files to get ComfyUI filenames)
  async function collectFormParams() {
    const params = {};
    const promptKey = currentWorkflow.prompt_key || null;   // main prompt carried by the bottom input
    for (const [key, item] of Object.entries(paramEls)) {
      if (key === promptKey) continue;
      if (item.type === "image") {
        const fns = [];
        if (item.el.files && item.el.files.length) {
          for (const f of item.el.files) {
            try { fns.push(await uploadImage(f)); }
            catch (e) { toast("Image upload failed: " + e.message); break; }
          }
        }
        (item.uploadFiles || []).forEach((fn) => fns.push(fn));
        if (!fns.length && item.uploadFilename) fns.push(item.uploadFilename);
        if (fns.length === 1) params[key] = fns[0];
        else if (fns.length > 1) params[key] = fns;
        continue;
      }
      const v = item.type === "bool" ? (item.el.checked ? "1" : "0") : String(item.el.value);
      if (v !== "") params[key] = v;
    }
    return params;
  }

  async function saveCurrentPreset() {
    if (!currentWorkflow) return;
    const prompt = (userInput.value || "").trim();
    const name = (prompt || "Preset " + new Date().toLocaleTimeString("zh-CN", { hour12: false })).slice(0, 60);
    const params = await collectFormParams();
    const r = await fetch("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, workflow_path: currentWorkflow.path, prompt, params }),
    });
    const d = await r.json();
    if (!d.success) { toast("Failed to save preset: " + (d.error || "")); return; }
    await loadPresets();
    presetSelect.value = d.id;
    toast("Preset saved: " + name);
  }

  async function applyPreset(presetId) {
    let list;
    try {
      const r = await fetch("/api/presets");
      list = await r.json();
    } catch (e) { return; }
    const p = (list || []).find((x) => x.id === presetId);
    if (!p) return;
    if (p.prompt) {
      userInput.value = p.prompt;
      userInput.style.height = "auto";
      userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";
    }
    for (const [key, val] of Object.entries(p.params || {})) {
      const item = paramEls[key];
      if (!item) continue;
      if (item.type === "image") {
        const arr = Array.isArray(val) ? val.filter(Boolean) : (String(val) ? [String(val)] : []);
        item.uploadFilename = arr[0] || null;
        item.uploadFiles = arr;
        if (item.renderThumbs) item.renderThumbs();
      } else if (item.type === "bool") {
        item.el.checked = (String(val) === "1" || val === true);
      } else {
        item.el.value = String(val);
      }
    }
    toast("Preset loaded: " + p.name);
  }

  presetSelect.addEventListener("change", () => {
    if (presetSelect.value) applyPreset(presetSelect.value);
  });
  presetSaveBtn.addEventListener("click", saveCurrentPreset);
  presetDelBtn.addEventListener("click", async () => {
    const id = presetSelect.value;
    if (!id) return;
    await fetch("/api/presets/" + id, { method: "DELETE" });
    presetSelect.value = "";
    loadPresets();
    toast("Preset deleted");
  });

  // ── image viewer ──
  let gallery = [];       // image URL list of the current chat
  let lbIdx = 0;

  const lightbox = $("lightbox");
  const lightboxImg = $("lightboxImg");
  const lbCount = $("lbCount");

  function openLightbox(idx) {
    if (!gallery.length) return;
    lbIdx = ((idx % gallery.length) + gallery.length) % gallery.length;
    lightboxImg.src = gallery[lbIdx];
    lbCount.textContent = (lbIdx + 1) + " / " + gallery.length;
    lightbox.hidden = false;
    document.body.style.overflow = "hidden";
  }
  function closeLightbox() {
    lightbox.hidden = true;
    document.body.style.overflow = "";
    lightboxImg.src = "";
  }
  function stepLightbox(d) {
    if (!gallery.length) return;
    openLightbox(lbIdx + d);
  }
  $("lbPrev").addEventListener("click", () => stepLightbox(-1));
  $("lbNext").addEventListener("click", () => stepLightbox(1));
  $("lbClose").addEventListener("click", closeLightbox);
  lightbox.addEventListener("click", (e) => {
    if (e.target === lightbox) closeLightbox();
  });
  document.addEventListener("keydown", (e) => {
    if (lightbox.hidden) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); stepLightbox(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepLightbox(1); }
    else if (e.key === "Escape") { closeLightbox(); }
  });

  // ── resource rendering (image/animation/video/audio) ──
  // kind fallback: infer from filename extension (old history may have a wrong kind, e.g. .mp4 saved as image)
  function resKind(res) {
    if (res.kind === "video" || res.kind === "gif" || res.kind === "audio") return res.kind;
    const fn = String(res.filename || res.url || "").toLowerCase();
    if (/\.(mp4|webm|mov|mkv)$/.test(fn)) return "video";
    if (/\.gif$/.test(fn)) return "gif";
    if (/\.(wav|mp3|flac|ogg|m4a)$/.test(fn)) return "audio";
    return res.kind || "image";
  }

  function resourceEl(res, execRef, idx) {
    const card = document.createElement("div");
    card.className = "result-card";
    const kind = resKind(res);
    if (kind === "video") {
      const v = document.createElement("video");
      v.src = res.url; v.controls = true; v.preload = "metadata";
      v.style.maxWidth = "100%";
      card.appendChild(v);
    } else if (kind === "audio") {
      const a = document.createElement("audio");
      a.src = res.url; a.controls = true; a.style.width = "100%";
      card.appendChild(a);
    } else {
      // image/animation: click to open the fullscreen viewer (← → to switch)
      const idx = gallery.push(res.url) - 1;
      const img = document.createElement("img");
      img.src = res.url; img.alt = res.filename || "";
      img.title = "Click to view (← → switch, Esc close)";
      img.style.cursor = "zoom-in";
      img.addEventListener("click", () => openLightbox(idx));
      card.appendChild(img);
    }
    const meta = document.createElement("div");
    meta.className = "result-meta";
    const actions = document.createElement("span");
    actions.className = "result-actions";
    // Open containing folder
    const openFolder = document.createElement("a");
    openFolder.href = "javascript:void(0)";
    openFolder.className = "folder-btn";
    openFolder.textContent = "📂";
    openFolder.title = "Open containing folder";
    openFolder.addEventListener("click", async (e) => {
      e.preventDefault();
      try {
        // old history may lack subfolder/type: parse from the resource URL as fallback (?subfolder=&type=)
        let sf = res.subfolder || "";
        let tp = res.type || "output";
        if (!sf && res.url) {
          try {
            const u = new URL(res.url);
            sf = u.searchParams.get("subfolder") || "";
            tp = u.searchParams.get("type") || tp;
          } catch (err) {}
        }
        await fetch("/api/open_folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: res.filename || "", type: tp, subfolder: sf }),
        });
      } catch (err) { /* ignore */ }
    });
    actions.appendChild(openFolder);
    if (execRef) addReproBtn(actions, execRef, idx);
    meta.appendChild(actions);
    card.appendChild(meta);
    // this image's actual params (each image has its own random seed)
    if (res.param_summary) {
      const ps = document.createElement("div");
      ps.className = "card-param-summary";
      ps.textContent = res.param_summary;
      card.appendChild(ps);
    }
    return card;
  }

  // Regenerate button: result card = this image only; user bubble = whole batch
  function addReproBtn(actions, exec, idx) {
    if (actions.querySelector(".repro-btn")) return;   // prevent duplicates
    const repro = document.createElement("a");
    repro.href = "javascript:void(0)";
    repro.className = "folder-btn repro-btn";
    repro.textContent = "🔄";
    repro.title = idx !== undefined ? "Regenerate this image (same params)" : "Regenerate with same prompt & params";
    repro.addEventListener("click", async (e) => {
      e.preventDefault();
      reproduce(exec, idx);
    });
    actions.appendChild(repro);
  }

  function renderResourceGrid(container, resources, execRef) {
    // container should be bubble-wrap (full width) so the grid tiles horizontally
    const grid = document.createElement("div");
    grid.className = "resource-grid";
    resources.forEach((r, i) => grid.appendChild(resourceEl(r, execRef, i)));
    container.appendChild(grid);
    scrollBottom();
  }
  // ── execution ──
  async function uploadImage(file) {
    const fd = new FormData();
    fd.append("image", file);
    const r = await fetch("/api/upload_image", { method: "POST", body: fd });
    const d = await r.json();
    if (!d.success) throw new Error(d.error || "Image upload failed");
    return d.filename;
  }

  // ── skill execution: in-process PyTorch inference (no ComfyUI) ──
  function renderSkillThumbs() {
    const box = $("skillThumbs");
    box.innerHTML = "";
    skillFiles.forEach((f) => {
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f);
      img.title = f.name;
      box.appendChild(img);
    });
  }

  async function runSkill(skillId) {
    if (!skillFiles.length || sending) return;
    sending = true;
    sendButton.disabled = true;
    const note = userInput.value.trim();
    const userBubble = addMsg(note || ("[" + skillTag.textContent + "] " + skillFiles.length + " image(s)"), "user");
    const ai = addAiBubble();
    scrollBottom(true);
    ai.bubble.textContent = "Skill running (first run loads the model, please wait)...";
    const fd = new FormData();
    skillFiles.forEach((f) => fd.append("images", f, f.name));
    if (currentConvId) fd.append("convId", currentConvId);
    try {
      const r = await fetch("/api/skill/" + skillId, { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok || !d.success) throw new Error(d.error || "Skill execution failed");
      ai.bubble.textContent = "✓ Done " + d.results.length + " image(s)";
      if (d.results.length) renderResourceGrid(ai.bw, d.results, null);
      skillFiles = [];
      renderSkillThumbs();
      sendButton.disabled = true;
    } catch (e) {
      ai.bubble.textContent = "✗ " + e.message;
      ai.bubble.classList.add("error");
    } finally {
      sending = false;
      userInput.value = "";
      sendButton.disabled = skillFiles.length === 0;
    }
  }

  async function sendMessage() {
    if ((!currentWorkflow && !currentSkill) || sending) return;
    // auto-create a conversation when none exists, so messages always persist
    if (!currentConvId) {
      await newConversation();
    }
    // ── native skill mode: in-process inference (no ComfyUI) ──
    if (currentSkill) {
      return runSkill(currentSkill.id);
    }
    const promptText = userInput.value.trim();
    // prompt target: declared by the workflow's C2A1-prefixed node (prompt_key), no auto-matching
    const promptKey = currentWorkflow.prompt_key || null;
    const hasImageParam = currentWorkflow.params.some((p) => p.type === "image");
    if (!promptText && !promptKey && !hasImageParam) { userInput.focus(); return; }

    const fd = new FormData();
    fd.append("workflowPath", currentWorkflow.path);
    fd.append("count", countInput.value || "1");
    if (currentConvId) fd.append("convId", currentConvId);

    // multi-image param → image_loop (each image runs count times)
    let imageLoop = null;
    const userImages = [];   // uploaded image filenames (shown under the user bubble + saved to history)
    for (const [key, item] of Object.entries(paramEls)) {
      if (item.type !== "image") continue;
      const localFns = [];
      if (item.el.files && item.el.files.length) {
        for (const f of item.el.files) {
          try { localFns.push(await uploadImage(f)); }
          catch (e) { addMsg("Image upload failed: " + e.message, "error"); return; }
        }
      }
      const remoteFns = (item.uploadFiles || []).filter(Boolean);
      const fns = localFns.concat(remoteFns);
      userImages.push(...fns);
      if (fns.length > 1) {
        imageLoop = { key, list: fns };
      } else if (fns.length === 1) {
        fd.append("param:" + key, fns[0]);
      } else if (item.uploadFilename) {
        // legacy single image: reuse the filename directly (file already in ComfyUI input dir)
        fd.append("param:" + key, item.uploadFilename);
        userImages.push(item.uploadFilename);
      }
    }
    if (imageLoop) {
      fd.append("image_loop_key", imageLoop.key);
      fd.append("image_loop_list", JSON.stringify(imageLoop.list));
    }
    if (promptKey) fd.append("promptKey", promptKey);   // explicitly declare the prompt target (backend reads it)
    if (promptKey && promptText) fd.append("param:" + promptKey, promptText);
    if (userImages.length) fd.append("user_images", JSON.stringify(userImages));
    for (const [key, item] of Object.entries(paramEls)) {
      if (item.type === "image" || key === promptKey) continue;
      const v = item.type === "bool" ? (item.el.checked ? "1" : "0") : item.el.value;
      const sv = v === undefined || v === null ? "" : String(v);
      if (sv === "") continue;   // empty values are not submitted; workflow defaults used (empty seed → backend random)
      fd.append("param:" + key, sv);
    }

    const summary = currentWorkflow.params
      .filter((p) => p.type !== "image" && p.key !== promptKey)
      .map((p) => {
        const it = paramEls[p.key];
        if (!it) return null;
        const v = it.type === "bool" ? (it.el.checked ? "On" : "Off") : String(it.el.value).trim();
        if (v === "" || v === "0") return null;
        return p.label + "=" + v;
      }).filter(Boolean).join("，");
    await runProcess(fd, (promptText || "") + (summary ? "\n【" + summary + "】" : ""), userImages);
  }

  // reuse the recorded execution: same workflow + params + prompt to regenerate
  // singleIdx !== undefined → regenerate only that image (same params, new random seed)
  async function reproduce(exec, singleIdx) {
    if (!exec || sending) return;
    if (!exec.workflow_path) return;
    if (!currentConvId) await newConversation();
    const single = singleIdx !== undefined;
    const fd = new FormData();
    fd.append("workflowPath", exec.workflow_path);
    fd.append("count", single ? "1" : (exec.count || "1"));
    if (currentConvId) fd.append("convId", currentConvId);
    for (const [k, v] of Object.entries(exec.params || {})) {
      if (single && /seed/.test(k)) continue;   // single image: exclude seed so regeneration uses a fresh random seed
      fd.append("param:" + k, v);
    }
    if (single && exec.image_loop && Array.isArray(exec.image_loop.list) && exec.image_loop.list.length) {
      // locate the source image for this resource: resources ordered as image→count
      const cnt = Math.max(1, parseInt(exec.count, 10) || 1);
      const imgIdx = Math.floor(singleIdx / cnt) % exec.image_loop.list.length;
      fd.append("image_loop_key", exec.image_loop.key);
      fd.append("image_loop_list", JSON.stringify([exec.image_loop.list[imgIdx]]));
    } else if (!single && exec.image_loop) {
      fd.append("image_loop_key", exec.image_loop.key);
      fd.append("image_loop_list", JSON.stringify(exec.image_loop.list));
    }
    if (exec.prompt_key) fd.append("promptKey", exec.prompt_key);
    if (exec.prompt && exec.prompt_key) fd.append("param:" + exec.prompt_key, exec.prompt);
    const reproImgs = (exec.user_images || []).map((u) => u.filename).filter(Boolean);
    if (reproImgs.length) fd.append("user_images", JSON.stringify(reproImgs));
    await runProcess(fd, exec.prompt || "Regenerate (same params)", reproImgs);
  }

  // user bubble toolbar: workflow name + batch "Regenerate" button
  function addUserTools(userEl, exec) {
    if (!userEl || !exec || userEl._tools) return;
    userEl._tools = true;
    const bar = document.createElement("div");
    bar.className = "user-tools";
    const wfName = String(exec.workflow_path || "").split(/[\\/]/).pop().replace(/\.json$/, "") || "Unknown workflow";
    const wf = document.createElement("span");
    wf.className = "wf-name";
    wf.textContent = "Workflow: " + wfName;
    const btn = document.createElement("a");
    btn.href = "javascript:void(0)";
    btn.className = "folder-btn repro-btn";
    btn.textContent = "🔄";
    btn.title = "Regenerate with same prompt & params";
    btn.addEventListener("click", (e) => { e.preventDefault(); reproduce(exec); });
    bar.appendChild(wf);
    bar.appendChild(btn);
    userEl.bw.appendChild(bar);
  }

  // unified entry: submit /process and stream-render the AI bubble (shared by sendMessage & reproduce)
  async function runProcess(fd, userText, userImages) {
    const userBubble = addMsg(userText || "Regenerate", "user");
    // uploaded images shown under the user bubble (ComfyUI input dir, still accessible after refresh)
    if (userImages && userImages.length) {
      renderUserImages(userBubble.bw, userImages.map((f) => ({ filename: f })));
    }
    const ai = addAiBubble();
    scrollBottom(true);   // force-scroll to the bottom after sending so the latest content is visible (independent of prior scroll)
    ai.bubble.textContent = "Submitting...";
    sending = true;
    sendButton.disabled = true;
    userInput.value = "";
    const live = { line: null, txt: null, fill: null, track: null, spin: null, userEl: userBubble };
    const results = [];
    // record this run's messages (user+AI) for cross-conversation keep-alive
    activeRun = { convId: currentConvId, ai, live, results, msgWraps: [userBubble.wrap, ai.wrap],
                  _hintShown: false, _hintLine: null };

    try {
      const resp = await fetch("/process", { method: "POST", body: fd });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        throw new Error(d.error || ("HTTP " + resp.status));
      }
      ai.bubble.textContent = "";
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!chunk.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(chunk.slice(6)); } catch (e) { continue; }
          handleEvent(ev, ai, live, results);
        }
      }
      if (currentConvId) loadConversations();
    } catch (e) {
      const err = document.createElement("div");
      err.className = "bubble error";
      err.textContent = "Execution failed: " + e.message;
      ai.bubble.appendChild(err);
    } finally {
      sending = false;
      sendButton.disabled = false;
      activeRun = null;   // done/failed: release cross-conversation keep-alive
    }
  }

  // realtime status line: executing/progress reuse one line (updated in place), finished on done
  function handleEvent(ev, ai, live, results) {
    if (ev.status && ev.message) {
      const msg = ev.message;
      // batch line: `Image X/N...` and `Submitted (xxx)...` merged into one line, numbers updated in place
      if (/^Image \d+\/\d+/.test(msg) || /^Submitted \(/.test(msg)) {
        if (!live.batch) {
          const line = document.createElement("div");
          line.className = "status-line";
          ai.bubble.appendChild(line);
          live.batch = line;
        }
        const m = msg.match(/^Image (\d+)\/(\d+)/);
        if (m) { live.batchCur = m[1]; live.batchTotal = m[2]; }
        // multi-image loop: keep the "(fig X/Y)" tag, update in place, do not re-list
        const imgM = msg.match(/\(fig (\d+)\/(\d+)\)/);
        if (imgM) live.batchImg = imgM[1] + "/" + imgM[2];
        let txt = "· Image " + (live.batchCur || "?") + "/" + (live.batchTotal || "?") + "";
        if (live.batchImg) txt += " (fig " + live.batchImg + ")";
        if (/^Submitted \(/.test(msg)) {
          const pm = msg.match(/Submitted \(([0-9a-f]+)\)/);
          txt += " · Submitted (" + (pm ? pm[1] : "...") + ")...";
        }
        live.batch.textContent = txt;
      } else if (msg.startsWith("Executing") || msg.includes("%")) {
        // realtime line: executing/progress reuse one line (updated in place)
        if (!live.line) {
          const line = document.createElement("div");
          line.className = "status-line";
          const spin = document.createElement("span");
          spin.className = "spinner";
          const txt = document.createElement("span");
          const track = document.createElement("div");
          track.className = "progress-track";
          track.style.display = "none";
          const fill = document.createElement("div");
          fill.className = "progress-fill";
          track.appendChild(fill);
          line.appendChild(spin);
          line.appendChild(txt);
          line.appendChild(track);
          ai.bubble.appendChild(line);
          live.line = line; live.txt = txt; live.fill = fill;
          live.track = track; live.spin = spin;
        }
        if (live.spin) live.spin.style.display = "";
        live.txt.textContent = msg;
        if (msg.includes("%")) {
          live.track.style.display = "";
          live.fill.style.width = (parseInt(msg, 10) || 0) + "%";
        } else {
          live.track.style.display = "none";
        }
      } else {
        // other one-shot statuses (start etc.)
        const line = document.createElement("div");
        line.className = "status-line";
        line.textContent = "· " + msg;
        ai.bubble.appendChild(line);
      }
      scrollBottom();
    }
    if (ev.resource) {
      const r = ev.resource;
      if (ev.seed !== undefined) r.seed = ev.seed;
      results.push(r);
      // realtime images also use the horizontal grid (tiled in order, wraps automatically)
      if (!ai._grid) {
        const grid = document.createElement("div");
        grid.className = "resource-grid";
        ai.bw.appendChild(grid);   // append to bubble-wrap (full width)
        ai._grid = grid;
      }
      ai._grid.appendChild(resourceEl(r));
      scrollBottom();
    }
    if (ev.error) {
      // task ended (stopped/failed): clear progress lines (start/batch/progress), keep only the error message
      ai.bubble.querySelectorAll(".status-line").forEach((el) => el.remove());
      const err = document.createElement("div");
      err.className = "bubble error";
      err.textContent = "✗ " + ev.error;
      ai.bubble.appendChild(err);
      if (ev.error_detail) {
        const det = document.createElement("div");
        det.style.cssText = "margin-top:6px";
        const btn = document.createElement("span");
        btn.className = "link-btn";
        btn.textContent = "View error details ▾";
        let open = false;
        btn.addEventListener("click", () => {
          open = !open;
          detail.style.display = open ? "block" : "none";
          btn.textContent = open ? "Hide details ▴" : "View error details ▾";
        });
        const detail = document.createElement("pre");
        detail.style.cssText = "display:none;margin:6px 0 0;padding:8px;background:var(--code-bg);" +
          "border-radius:8px;font-size:11px;overflow:auto;max-height:200px;white-space:pre-wrap;color:var(--text)";
        detail.textContent = ev.error_detail;
        det.appendChild(btn);
        det.appendChild(detail);
        ai.bubble.appendChild(det);
      }
      scrollBottom();
    }
    if (ev.done) {
      // debounce: handle done once per batch (avoid duplicate cleanup)
      if (live._done) { scrollBottom(); return; }
      live._done = true;
      // remove the batch line (Image X/N · Submitted served its purpose)
      if (live.batch && live.batch.parentNode) {
        live.batch.parentNode.removeChild(live.batch);
        live.batch = null;
      }
      // done: remove the "still generating" hint line (avoid contradicting the completion)
      if (activeRun && activeRun._hintLine && activeRun._hintLine.parentNode) {
        activeRun._hintLine.parentNode.removeChild(activeRun._hintLine);
        activeRun._hintLine = null;
      }
      // done: finish the realtime status line (no leftover "running..."; duration not shown)
      if (live.line) {
        live.spin.style.display = "none";
        live.track.style.display = "none";
        live.txt.textContent = "✓ " + (ev.message || "Done");
      } else {
        const line = document.createElement("div");
        line.className = "status-line";
        line.textContent = "✓ " + (ev.message || "Done");
        ai.bubble.appendChild(line);
      }
      // attach buttons in realtime (no refresh): user bubble = workflow name + batch 🔄; result card = single 🔄
      if (ev.exec) {
        if (live.userEl) addUserTools(live.userEl, ev.exec);
        ai.bw.querySelectorAll(".result-card").forEach((card, i) => {
          const a = card.querySelector(".result-actions");
          if (a) addReproBtn(a, ev.exec, i);
        });
      }
      scrollBottom();
    }
  }

  // show uploaded image thumbs under the user bubble (resources: [{filename, url}])
  function renderUserImages(bw, images) {
    if (!images || !images.length) return;
    const grid = document.createElement("div");
    grid.className = "user-images";
    images.forEach((img) => {
      const thumb = document.createElement("div");
      thumb.className = "user-thumb";
      const el = document.createElement("img");
      // all thumbs go through the ComfyChat thumbnail API (256px, disk cached); the model still gets the original image
      el.src = "api/thumb?filename=" + encodeURIComponent(img.filename || "");
      el.title = img.filename || "";
      el.loading = "lazy";
      thumb.appendChild(el);
      grid.appendChild(thumb);
    });
    bw.appendChild(grid);
  }

  // ComfyUI server-side compressed thumbnail URL (/view supports preview=webp)
  function comfyThumbUrl(filename, type) {
    return "http://127.0.0.1:8188/view?filename=" + encodeURIComponent(filename) +
           "&type=" + (type || "input") + "&preview=webp";
  }

  // local File → compressed thumbnail (canvas downscale to maxSide, webp) to avoid lag from full-size images
  function makeThumbUrl(file, maxSide, cb) {
    const raw = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      try {
        const scale = Math.min(1, (maxSide || 160) / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(raw);
        canvas.toBlob((blob) => cb(blob ? URL.createObjectURL(blob) : raw), "image/webp", 0.8);
      } catch (e) { cb(raw); }
    };
    img.onerror = () => cb(raw);
    img.src = raw;
  }

  // ── drag & drop upload ──
  const dragOverlay = $("dragOverlay");
  let dragDepth = 0;
  document.addEventListener("dragenter", (e) => {
    e.preventDefault();
    if (!currentWorkflow || !currentWorkflow.params.some((p) => p.type === "image")) return;
    dragDepth++;
    dragOverlay.classList.add("show");
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("dragleave", (e) => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) dragOverlay.classList.remove("show");
  });
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    dragOverlay.classList.remove("show");
    if (!currentWorkflow) return;
    const imgParam = currentWorkflow.params.find((p) => p.type === "image");
    if (!imgParam || !e.dataTransfer.files.length) return;
    const f = e.dataTransfer.files[0];
    if (!f.type.startsWith("image/")) return;
    const it = paramEls[imgParam.key];
    if (!it) return;
    it.el.files = e.dataTransfer.files;
    if (it.renderThumbs) it.renderThumbs();   // drag multiple images: render all thumbs
  });

  // ── event binding ──
  userInput.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" && !e.shiftKey) || (e.key === "Enter" && e.ctrlKey)) {
      if (!e.shiftKey) { e.preventDefault(); sendMessage(); }
    }
  });
  userInput.addEventListener("input", () => {
    userInput.style.height = "auto";
    userInput.style.height = Math.min(userInput.scrollHeight, 120) + "px";
  });
  sendButton.addEventListener("click", sendMessage);
  $("refreshBtn").addEventListener("click", () => { loadWorkflows(); refreshStatus(); });
  // skill mode: pick images → preview + can send
  $("skillFileInput").addEventListener("change", (e) => {
    skillFiles = Array.from(e.target.files || []);
    renderSkillThumbs();
    sendButton.disabled = skillFiles.length === 0 || sending;
  });
  loadSkills();
  $("paramPanelHead").addEventListener("click", (e) => {
    if (e.target.closest(".pn-actions")) return;   // header buttons do not toggle the panel collapse
    paramPanel.classList.toggle("collapsed");
  });

  // expand/collapse all params (temporarily ignore the PIN whitelist, show every param of the workflow)
  let showAllParams = false;
  $("expandAllBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    showAllParams = !showAllParams;
    $("expandAllBtn").classList.toggle("active", showAllParams);
    renderParams();
  });

  // ── startup ──
  refreshStatus();
  setInterval(refreshStatus, 10000);
  loadConversations(true);   // auto-restore the most recent conversation
  loadWorkflows();
})();