(function () {
  "use strict";

  var API = "";
  var TYPE_NAME = { daily: "日报", weekly: "周报", monthly: "月报", yearly: "年报" };
  var TYPE_PERIOD = { daily: "当日", weekly: "本周", monthly: "本月", yearly: "本年" };
  var state = {
    online: false,
    offline: false,
    athletes: [],
    athleteId: null,
    athleteDetail: null,
    sessions: [],
    sessionId: "",
    type: "weekly",
    windowKey: "",
    view: "coach",
    busy: false,
    sessionReq: 0,
    detailReq: 0
  };

  function $(id) { return document.getElementById(id); }
  var athleteSelect = $("athleteSelect");
  var sessionSelect = $("sessionSelect");
  var windowSelect = $("windowSelect");
  var generateBtn = $("generateBtn");
  var bowValue = $("bowValue");
  var statusBar = $("statusBar");
  var offlineStrip = $("offlineStrip");
  var toastEl = $("toast");
  var idleTitle = $("idleTitle");
  var idleLead = $("idleLead");
  var idleCta = $("idleCta");
  var idleHint = $("idleHint");
  var stepAthlete = $("stepAthlete");
  var stepBow = $("stepBow");
  var stepType = $("stepType");
  var railAvgLabel = $("railAvgLabel");

  /* ---------- utilities ---------- */
  function pad(n) { return String(n).padStart(2, "0"); }

  function localParts(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    var dtf = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23"
    });
    var out = {};
    dtf.formatToParts(d).forEach(function (p) {
      if (p.type !== "literal") out[p.type] = p.value;
    });
    return out;
  }

  function localDateTime(iso) {
    var p = localParts(iso);
    if (!p) return "时间未知";
    return p.month + "-" + p.day + " " + p.hour + ":" + p.minute;
  }

  function localDate(iso) {
    var p = localParts(iso);
    if (!p) return "日期未知";
    return p.month + "-" + p.day;
  }

  function isoWeekKey(parts) {
    var d = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day));
    var day = d.getUTCDay() || 7;
    d.setUTCDate(d.getUTCDate() + 4 - day);
    var yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
    var week = Math.ceil((((d - yearStart) / 86400000) + 1) / 7);
    return d.getUTCFullYear() + "-W" + pad(week);
  }

  function periodKey(session, type) {
    if (type === "daily") return "daily:" + session.session_id;
    var p = localParts(session.session_time_utc);
    if (!p) return null;
    if (type === "weekly") return isoWeekKey(p);
    if (type === "monthly") return p.year + "-" + p.month;
    if (type === "yearly") return p.year;
    return null;
  }

  function periodLabel(session, type) {
    if (type === "daily") return localDate(session.session_time_utc);
    return periodKey(session, type) || "—";
  }

  function windowDisplay(key, type) {
    if (!key) return "—";
    if (type === "daily" && key.indexOf("daily:") === 0) {
      var sid = key.slice(6);
      var session = state.sessions.find(function (s) { return s.session_id === sid; });
      return session ? localDate(session.session_time_utc) : "该场次";
    }
    return key;
  }

  function sessionLabel(s) {
    var bits = [localDateTime(s.session_time_utc)];
    if (typeof s.shot_count === "number") bits.push(s.shot_count + "箭");
    else bits.push("箭数未知");
    if (typeof s.distance_m === "number") bits.push(s.distance_m + "m");
    return bits.join(" · ");
  }

  function athleteLabel(a, all) {
    var name = a.name && String(a.name).trim() ? a.name : "未命名";
    var no = a.display_no == null ? "" : " · " + a.display_no;
    if (a.name && all) {
      var same = all.filter(function (x) { return x.name === a.name; }).length;
      if (same > 1) return name + no;
    }
    return name;
  }

  function setChildren(el, nodes) {
    el.replaceChildren.apply(el, nodes);
  }

  function span(text, className) {
    var s = document.createElement("span");
    if (className) s.className = className;
    s.textContent = text;
    return s;
  }

  function dot() { return span("", "dot"); }

  function setStatus(parts, withRetry) {
    var nodes = [];
    parts.forEach(function (part, i) {
      if (i) nodes.push(dot());
      if (typeof part === "string") {
        nodes.push(span(part));
      } else if (part && part.bold) {
        var b = document.createElement("b");
        b.textContent = part.text;
        nodes.push(b);
      } else if (part && part.text) {
        nodes.push(span(part.text));
      }
    });
    if (withRetry) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "重试";
      btn.addEventListener("click", init);
      nodes.push(btn);
    }
    setChildren(statusBar, nodes);
  }

  var toastTimer = null;
  function showToast(text) {
    toastEl.textContent = text;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 2400);
  }

  function disableControls(disabled) {
    athleteSelect.disabled = disabled;
    sessionSelect.disabled = disabled || !state.sessions.length;
    windowSelect.disabled = disabled || !state.sessionId;
    generateBtn.disabled = disabled || !state.sessionId;
    document.querySelectorAll("#typeGroup button").forEach(function (b) {
      b.disabled = disabled;
    });
  }

  function setOffline() {
    state.online = false;
    state.offline = true;
    state.athletes = [];
    state.sessions = [];
    state.sessionId = "";
    state.windowKey = "";
    state.athleteId = null;
    state.athleteDetail = null;
    offlineStrip.hidden = false;
    athleteSelect.replaceChildren(new Option("无法连接", ""));
    athleteSelect.disabled = true;
    sessionSelect.replaceChildren(new Option("无法连接", ""));
    sessionSelect.disabled = true;
    windowSelect.replaceChildren(new Option("无法连接", ""));
    windowSelect.disabled = true;
    generateBtn.disabled = true;
    bowValue.textContent = "—";
    stepAthlete.querySelector("span:last-child").textContent = "运动员，—";
    stepBow.querySelector("span:last-child").textContent = "弓种，—";
    stepBow.classList.remove("done");
    stepAthlete.classList.remove("done");
    setStatus(["引擎未连接 · 127.0.0.1:8000"], true);
    updateEmptyCopy();
  }

  async function api(path) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 6000);
    try {
      var res = await fetch(API + path, {
        method: "GET",
        headers: { "Accept": "application/json" },
        signal: controller.signal,
        cache: "no-store"
      });
      var text = await res.text();
      var data = null;
      if (text) {
        try { data = JSON.parse(text); } catch (err) { data = null; }
      }
      if (!res.ok) {
        var error = new Error("HTTP " + res.status);
        error.status = res.status;
        error.code = data && data.code;
        error.detail = data && data.detail;
        throw error;
      }
      return data;
    } finally {
      clearTimeout(timer);
    }
  }

  /* ---------- rendering ---------- */
  function renderAthletes(athletes) {
    var opts = [];
    athletes.forEach(function (a) {
      opts.push(new Option(athleteLabel(a, athletes), a.athlete_id));
    });
    athleteSelect.replaceChildren.apply(athleteSelect, opts);
    athleteSelect.disabled = false;
  }

  function renderSessions(sessions) {
    if (!sessions.length) {
      sessionSelect.replaceChildren(new Option("暂无场次", ""));
      sessionSelect.disabled = true;
      return;
    }
    var opts = [new Option("尚未选择", "")];
    sessions.forEach(function (s) {
      var o = new Option(sessionLabel(s), s.session_id);
      o.title = s.session_id;
      opts.push(o);
    });
    sessionSelect.replaceChildren.apply(sessionSelect, opts);
    sessionSelect.disabled = false;
    sessionSelect.value = "";
  }

  function updateWindowOptions(reset) {
    if (!state.sessionId) {
      windowSelect.replaceChildren(new Option("选择场次后可选", ""));
      windowSelect.disabled = true;
      state.windowKey = "";
      return;
    }
    var session = state.sessions.find(function (s) { return s.session_id === state.sessionId; });
    if (!session) {
      windowSelect.replaceChildren(new Option("选择场次后可选", ""));
      windowSelect.disabled = true;
      state.windowKey = "";
      return;
    }

    if (state.type === "daily") {
      var dailyKey = periodKey(session, "daily");
      windowSelect.replaceChildren(new Option(periodLabel(session, "daily"), dailyKey));
      windowSelect.disabled = true;
      state.windowKey = dailyKey;
      return;
    }

    var keys = [];
    var seen = {};
    state.sessions.forEach(function (s) {
      var k = periodKey(s, state.type);
      if (k && !seen[k]) { seen[k] = true; keys.push(k); }
    });
    keys.sort(function (a, b) { return a < b ? 1 : (a > b ? -1 : 0); });
    if (keys.length > 52) keys = keys.slice(0, 52);
    var sessionKey = periodKey(session, state.type);
    if (sessionKey && keys.indexOf(sessionKey) === -1) keys.unshift(sessionKey);

    var opts = keys.map(function (k) {
      var o = new Option(k, k);
      if (k === sessionKey) o.title = "所选场次所在周期";
      return o;
    });
    windowSelect.replaceChildren.apply(windowSelect, opts);
    windowSelect.disabled = false;
    if (reset || keys.indexOf(state.windowKey) === -1) state.windowKey = sessionKey;
    windowSelect.value = state.windowKey;
  }

  function updateEmptyCopy() {
    var period = TYPE_PERIOD[state.type];
    var typeName = TYPE_NAME[state.type];
    var hasAthlete = !!state.athleteId;
    var hasSessions = state.sessions.length > 0;
    var hasSession = !!state.sessionId;

    if (state.offline) {
      idleTitle.textContent = "报告引擎未连接";
      idleLead.textContent = "请确认引擎已在本机启动（127.0.0.1:8000），然后点击重试。";
      idleCta.textContent = "等待连接";
      idleCta.disabled = true;
      idleHint.textContent = "引擎恢复后可从命令栏选择场次并生成报告。";
      railAvgLabel.textContent = period + "均环";
      stepType.classList.remove("done");
      return;
    }

    idleTitle.textContent = "选择场次，点亮" + period + "记分板";
    idleLead.textContent = "报告引擎将按" + period + "汇总均环、水平对比、风档与建议。运动员与弓种已就位，还差一场有效射击数据。";
    stepType.querySelector("span:last-child").textContent = "确认要出的是" + typeName + "，或改选其它报告类型";
    railAvgLabel.textContent = period + "均环";

    if (!hasAthlete) {
      idleTitle.textContent = "尚未导入运动员档案";
      idleLead.textContent = "请先在引擎侧导入运动员与训练数据，再回到这里生成报告。";
    } else if (!hasSessions) {
      idleLead.textContent = "该运动员暂无场次。请先在引擎侧导入该运动员的训练数据。";
    }

    idleCta.textContent = hasSession ? "使用上方「生成报告」" : "等待场次";
    idleCta.disabled = true;
    idleHint.textContent = hasSession
      ? "场次已就绪，点击命令栏的「生成报告」。"
      : "先在命令栏选择场次；也可改选日报、周报、月报或年报。";

    stepType.classList.toggle("done", hasSession);
  }

  function updateSteps() {
    var athlete = state.athletes.find(function (a) { return a.athlete_id === state.athleteId; });
    var label = athlete ? athleteLabel(athlete, state.athletes) : "—";
    stepAthlete.querySelector("span:last-child").textContent = "运动员，" + label;
    stepAthlete.classList.toggle("done", !!athlete);

    var bow = state.athleteDetail && state.athleteDetail.profile && state.athleteDetail.profile.bow_type;
    var hasAnchor = !!(state.athleteDetail && state.athleteDetail.baseline && state.athleteDetail.baseline.anchor);
    if (bow) {
      stepBow.querySelector("span:last-child").textContent = "弓种，" + bow + (hasAnchor ? "（锚点已有）" : "（暂无锚点）");
      stepBow.classList.add("done");
      bowValue.textContent = bow;
    } else {
      stepBow.querySelector("span:last-child").textContent = "弓种，未设置";
      stepBow.classList.remove("done");
      bowValue.textContent = "未设置";
    }
  }

  function updateTypeButtons() {
    document.querySelectorAll("#typeGroup button").forEach(function (b) {
      b.classList.toggle("on", b.dataset.type === state.type);
    });
  }

  function updateViewButtons() {
    document.querySelectorAll("#viewSeg button").forEach(function (b) {
      var on = b.dataset.view === state.view;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  /* ---------- data flow ---------- */
  async function init() {
    state.busy = true;
    offlineStrip.hidden = true;
    athleteSelect.disabled = true;
    sessionSelect.disabled = true;
    windowSelect.disabled = true;
    generateBtn.disabled = true;
    athleteSelect.replaceChildren(new Option("正在载入…", ""));
    sessionSelect.replaceChildren(new Option("尚未选择", ""));
    windowSelect.replaceChildren(new Option("选择场次后可选", ""));
    setStatus(["正在连接报告引擎…"]);

    try {
      var health = await api("/api/v1/health");
      if (!health || health.status !== "ok") throw new Error("health not ok");
      state.online = true;
      state.offline = false;
      offlineStrip.hidden = true;

      var data = await api("/api/v1/athletes");
      var athletes = (data && data.athletes) || [];
      state.athletes = athletes;
      if (!athletes.length) {
        state.athleteId = null;
        state.sessions = [];
        athleteSelect.replaceChildren(new Option("暂无运动员", ""));
        athleteSelect.disabled = true;
        updateEmptyCopy();
        setStatus(["尚未导入运动员档案"]);
        return;
      }

      renderAthletes(athletes);
      var latest = athletes.slice().sort(function (a, b) {
        var ta = a.last_session_utc ? Date.parse(a.last_session_utc) : -1;
        var tb = b.last_session_utc ? Date.parse(b.last_session_utc) : -1;
        return tb - ta;
      })[0];
      if (!latest) {
        latest = athletes.slice().sort(function (a, b) {
          return (a.display_no || 0) - (b.display_no || 0);
        })[0];
      }
      state.athleteId = latest.athlete_id;
      athleteSelect.value = latest.athlete_id;
      await afterAthleteChange(true);
    } catch (err) {
      setOffline();
    } finally {
      state.busy = false;
      updateEmptyCopy();
    }
  }

  async function afterAthleteChange(initial) {
    state.sessionId = "";
    state.windowKey = "";
    state.sessions = [];
    state.athleteDetail = null;
    sessionSelect.replaceChildren(new Option("正在载入场次…", ""));
    sessionSelect.disabled = true;
    windowSelect.replaceChildren(new Option("选择场次后可选", ""));
    windowSelect.disabled = true;
    generateBtn.disabled = true;
    stepType.classList.remove("done");
    updateEmptyCopy();

    var athlete = state.athletes.find(function (a) { return a.athlete_id === state.athleteId; });
    if (athlete) {
      stepAthlete.querySelector("span:last-child").textContent = "运动员，" + athleteLabel(athlete, state.athletes);
      stepAthlete.classList.add("done");
    }
    bowValue.textContent = "—";

    var req = ++state.sessionReq;
    var detailReq = ++state.detailReq;
    try {
      var sessionsData = await api("/api/v1/athletes/" + encodeURIComponent(state.athleteId) + "/sessions");
      if (req !== state.sessionReq) return;
      var sessions = (sessionsData && sessionsData.sessions) || [];
      sessions.sort(function (a, b) {
        return String(b.session_time_utc).localeCompare(String(a.session_time_utc));
      });
      state.sessions = sessions;
      renderSessions(sessions);
      if (!sessions.length) {
        setStatus(["等待场次", "该运动员暂无场次"]);
      } else {
        setStatus(["等待场次", "尚未生成报告", "运动员与弓种已就位"]);
      }
      updateEmptyCopy();
    } catch (err) {
      if (req !== state.sessionReq) return;
      sessionSelect.replaceChildren(new Option("场次读取失败", ""));
      sessionSelect.disabled = true;
      showToast("场次读取失败，请重试");
      updateEmptyCopy();
    }

    try {
      var detail = await api("/api/v1/athletes/" + encodeURIComponent(state.athleteId));
      if (detailReq !== state.detailReq) return;
      state.athleteDetail = detail;
      updateSteps();
    } catch (err) {
      if (detailReq !== state.detailReq) return;
      state.athleteDetail = null;
      updateSteps();
    }
  }

  /* ---------- events ---------- */
  athleteSelect.addEventListener("change", function () {
    if (!athleteSelect.value || state.busy) return;
    state.athleteId = athleteSelect.value;
    showToast("已切换运动员，场次与报告将重新载入");
    afterAthleteChange(false);
  });

  sessionSelect.addEventListener("change", function () {
    state.sessionId = sessionSelect.value || "";
    updateWindowOptions(true);
    generateBtn.disabled = !state.sessionId;
    stepType.classList.toggle("done", !!state.sessionId);
    updateEmptyCopy();
    updateSteps();
    if (state.sessionId) {
      var session = state.sessions.find(function (s) { return s.session_id === state.sessionId; });
      setStatus(["已选场次" + (session ? " " + localDate(session.session_time_utc) : ""), "尚未生成报告", "可生成" + TYPE_NAME[state.type]]);
    } else {
      setStatus(["等待场次", "尚未生成报告", "运动员与弓种已就位"]);
    }
  });

  windowSelect.addEventListener("change", function () {
    state.windowKey = windowSelect.value;
    if (state.sessionId) {
      setStatus(["已选窗口 " + TYPE_NAME[state.type] + " " + windowDisplay(state.windowKey, state.type), "尚未生成报告", "可生成" + TYPE_NAME[state.type]]);
    }
  });

  document.querySelectorAll("#typeGroup button").forEach(function (b) {
    b.addEventListener("click", function () {
      if (state.busy) return;
      state.type = b.dataset.type;
      updateTypeButtons();
      updateWindowOptions(true);
      updateEmptyCopy();
      updateSteps();
      if (state.sessionId) {
        setStatus(["已选窗口 " + TYPE_NAME[state.type] + " " + windowDisplay(state.windowKey, state.type), "尚未生成报告", "可生成" + TYPE_NAME[state.type]]);
      }
    });
  });

  generateBtn.addEventListener("click", function () {
    if (!state.sessionId) {
      showToast("请先选择场次");
      return;
    }
    showToast("WP2 接入：将生成「" + TYPE_NAME[state.type] + " " + windowDisplay(state.windowKey, state.type) + "」；当前工作包先完成壳与空态。");
  });

  $("idleCta").addEventListener("click", function () {
    if (!state.sessionId) {
      showToast("请先在命令栏选择场次");
    }
  });

  document.querySelectorAll("#viewSeg button").forEach(function (b) {
    b.addEventListener("click", function () {
      if (state.busy) return;
      state.view = b.dataset.view;
      updateViewButtons();
      showToast(state.view === "athlete"
        ? "已切换到运动员视图；报告生成后按该视角读取（WP3 接入刷新）"
        : "已切换到教练视图；报告生成后按该视角读取（WP3 接入刷新）");
    });
  });

  document.querySelectorAll(".app-rail button").forEach(function (b) {
    b.addEventListener("click", function () {
      if (b.dataset.rail === "report") return;
      var label = b.querySelector("span") ? b.querySelector("span").textContent : "该页面";
      showToast(label + "即将接入");
    });
  });

  document.querySelectorAll(".wb").forEach(function (b) {
    b.addEventListener("click", function () {
      var action = b.dataset.win === "min" ? "minimize"
        : (b.dataset.win === "max" ? "toggle_max" : "close");
      var api = window.pywebview && window.pywebview.api;
      if (api && typeof api[action] === "function") {
        api[action]();
        return;
      }
      showToast("窗口控制由桌面宿主提供（WP0 验证）");
    });
  });

  // 无边框宿主：在标题栏空白处按下时，交给宿主执行 Windows 拖拽
  var titlebar = document.querySelector(".titlebar");
  if (titlebar) {
    titlebar.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      if (e.target.closest("button,select,input,a")) return;
      var api = window.pywebview && window.pywebview.api;
      if (api && typeof api.start_drag === "function") {
        api.start_drag();
      }
    });
  }

  $("offlineRetry").addEventListener("click", init);

  /* ---------- boot ---------- */
  updateTypeButtons();
  updateViewButtons();
  updateEmptyCopy();
  init();
})();
