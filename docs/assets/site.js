/* Marionette site. Vanilla, no libraries. Every number and event below comes
   from job_338f29ad552d, the swarm recorded in the hero video. */
(function () {
  "use strict";

  var REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var REPO = "professorpalmer/marionette";
  var RELEASES = "https://github.com/" + REPO + "/releases/latest";

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function onView(node, fn, opts) {
    if (!node) return;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) { fn(en.isIntersecting, en); });
    }, opts || { threshold: 0.2 });
    io.observe(node);
    return io;
  }

  /* ------------------------------------------------------------------ */
  /* Real job data                                                        */
  /* ------------------------------------------------------------------ */
  // [seconds since job.created, event, role, detail]
  var EVENTS = [[0,"job.created","",""],[0,"job.mode","",""],[2,"job.brief","",""],[2,"job.status","","running"],[2,"router.registry_reconciled","",""],[2,"task.saved","conflict-auditor","queued"],[2,"task.saved","explore","queued"],[2,"task.saved","test-coverage-reviewer","queued"],[2,"artifact.saved","conflict-auditor",{"type":"routing","conf":0.9,"sha":"a7706ec8b4","claim":"","model":"openai/gpt-6.1-sol"}],[2,"artifact.saved","explore",{"type":"routing","conf":0.9,"sha":"b1ac28c8cd","claim":"","model":"openai/gpt-6.1-sol"}],[2,"artifact.saved","test-coverage-reviewer",{"type":"routing","conf":0.9,"sha":"586807aa94","claim":"","model":"openai/gpt-6.1-sol"}],[2,"task.claimed","conflict-auditor","03:45:58"],[2,"task.claimed","test-coverage-reviewer","03:45:58"],[2,"task.claimed","explore","03:45:58"],[4,"task.lease_renewed","explore",""],[4,"run.heartbeat","explore",""],[4,"task.lease_renewed","test-coverage-reviewer",""],[4,"run.heartbeat","test-coverage-reviewer",""],[4,"task.lease_renewed","conflict-auditor",""],[4,"run.heartbeat","conflict-auditor",""],[6,"task.lease_renewed","explore",""],[6,"run.heartbeat","explore",""],[6,"task.lease_renewed","conflict-auditor",""],[6,"run.heartbeat","conflict-auditor",""],[7,"task.lease_renewed","explore",""],[7,"run.heartbeat","explore",""],[7,"task.lease_renewed","test-coverage-reviewer",""],[7,"run.heartbeat","test-coverage-reviewer",""],[7,"task.lease_renewed","conflict-auditor",""],[7,"run.heartbeat","conflict-auditor",""],[9,"task.lease_renewed","explore",""],[9,"run.heartbeat","explore",""],[9,"task.lease_renewed","conflict-auditor",""],[9,"run.heartbeat","conflict-auditor",""],[11,"task.lease_renewed","explore",""],[11,"run.heartbeat","explore",""],[11,"task.lease_renewed","test-coverage-reviewer",""],[11,"run.heartbeat","test-coverage-reviewer",""],[11,"task.lease_renewed","conflict-auditor",""],[11,"run.heartbeat","conflict-auditor",""],[12,"task.lease_renewed","explore",""],[12,"run.heartbeat","explore",""],[12,"task.lease_renewed","test-coverage-reviewer",""],[12,"run.heartbeat","test-coverage-reviewer",""],[12,"task.lease_renewed","conflict-auditor",""],[12,"run.heartbeat","conflict-auditor",""],[14,"task.lease_renewed","test-coverage-reviewer",""],[14,"run.heartbeat","test-coverage-reviewer",""],[14,"task.lease_renewed","conflict-auditor",""],[14,"run.heartbeat","conflict-auditor",""],[16,"task.lease_renewed","explore",""],[16,"run.heartbeat","explore",""],[16,"task.lease_renewed","test-coverage-reviewer",""],[16,"run.heartbeat","test-coverage-reviewer",""],[16,"task.lease_renewed","conflict-auditor",""],[16,"run.heartbeat","conflict-auditor",""],[17,"task.lease_renewed","test-coverage-reviewer",""],[17,"run.heartbeat","test-coverage-reviewer",""],[17,"task.lease_renewed","explore",""],[17,"run.heartbeat","explore",""],[17,"task.lease_renewed","conflict-auditor",""],[17,"run.heartbeat","conflict-auditor",""],[19,"task.lease_renewed","explore",""],[19,"run.heartbeat","explore",""],[19,"task.lease_renewed","test-coverage-reviewer",""],[19,"run.heartbeat","test-coverage-reviewer",""],[19,"task.lease_renewed","conflict-auditor",""],[19,"run.heartbeat","conflict-auditor",""],[21,"task.lease_renewed","explore",""],[21,"run.heartbeat","explore",""],[21,"task.lease_renewed","conflict-auditor",""],[21,"run.heartbeat","conflict-auditor",""],[22,"task.lease_renewed","explore",""],[22,"run.heartbeat","explore",""],[22,"task.lease_renewed","test-coverage-reviewer",""],[22,"run.heartbeat","test-coverage-reviewer",""],[22,"task.lease_renewed","conflict-auditor",""],[22,"run.heartbeat","conflict-auditor",""],[24,"task.lease_renewed","explore",""],[24,"run.heartbeat","explore",""],[24,"task.lease_renewed","test-coverage-reviewer",""],[24,"run.heartbeat","test-coverage-reviewer",""],[24,"task.lease_renewed","conflict-auditor",""],[24,"run.heartbeat","conflict-auditor",""],[26,"task.lease_renewed","explore",""],[26,"run.heartbeat","explore",""],[26,"task.lease_renewed","test-coverage-reviewer",""],[26,"run.heartbeat","test-coverage-reviewer",""],[26,"task.lease_renewed","conflict-auditor",""],[26,"run.heartbeat","conflict-auditor",""],[27,"task.lease_renewed","explore",""],[27,"run.heartbeat","explore",""],[27,"task.lease_renewed","test-coverage-reviewer",""],[27,"run.heartbeat","test-coverage-reviewer",""],[27,"task.lease_renewed","conflict-auditor",""],[27,"run.heartbeat","conflict-auditor",""],[29,"task.lease_renewed","explore",""],[29,"run.heartbeat","explore",""],[29,"task.lease_renewed","test-coverage-reviewer",""],[29,"run.heartbeat","test-coverage-reviewer",""],[29,"task.lease_renewed","conflict-auditor",""],[29,"run.heartbeat","conflict-auditor",""],[31,"task.lease_renewed","explore",""],[31,"run.heartbeat","explore",""],[31,"task.lease_renewed","test-coverage-reviewer",""],[31,"run.heartbeat","test-coverage-reviewer",""],[31,"task.lease_renewed","conflict-auditor",""],[31,"run.heartbeat","conflict-auditor",""],[32,"task.lease_renewed","explore",""],[32,"run.heartbeat","explore",""],[32,"task.lease_renewed","test-coverage-reviewer",""],[32,"run.heartbeat","test-coverage-reviewer",""],[32,"task.lease_renewed","conflict-auditor",""],[32,"run.heartbeat","conflict-auditor",""],[34,"task.lease_renewed","explore",""],[34,"run.heartbeat","explore",""],[34,"task.lease_renewed","test-coverage-reviewer",""],[34,"run.heartbeat","test-coverage-reviewer",""],[34,"task.lease_renewed","conflict-auditor",""],[34,"run.heartbeat","conflict-auditor",""],[36,"task.lease_renewed","test-coverage-reviewer",""],[36,"run.heartbeat","test-coverage-reviewer",""],[36,"task.lease_renewed","conflict-auditor",""],[36,"run.heartbeat","conflict-auditor",""],[37,"task.lease_renewed","explore",""],[37,"run.heartbeat","explore",""],[37,"task.lease_renewed","test-coverage-reviewer",""],[37,"run.heartbeat","test-coverage-reviewer",""],[37,"task.lease_renewed","conflict-auditor",""],[37,"run.heartbeat","conflict-auditor",""],[39,"task.lease_renewed","explore",""],[39,"run.heartbeat","explore",""],[39,"task.lease_renewed","test-coverage-reviewer",""],[39,"run.heartbeat","test-coverage-reviewer",""],[39,"task.lease_renewed","conflict-auditor",""],[39,"run.heartbeat","conflict-auditor",""],[41,"task.lease_renewed","explore",""],[41,"run.heartbeat","explore",""],[41,"task.lease_renewed","test-coverage-reviewer",""],[41,"run.heartbeat","test-coverage-reviewer",""],[41,"task.lease_renewed","conflict-auditor",""],[41,"run.heartbeat","conflict-auditor",""],[42,"task.lease_renewed","explore",""],[42,"run.heartbeat","explore",""],[42,"task.lease_renewed","test-coverage-reviewer",""],[42,"run.heartbeat","test-coverage-reviewer",""],[44,"task.lease_renewed","explore",""],[44,"run.heartbeat","explore",""],[44,"task.lease_renewed","conflict-auditor",""],[44,"run.heartbeat","conflict-auditor",""],[46,"task.lease_renewed","explore",""],[46,"run.heartbeat","explore",""],[46,"task.lease_renewed","test-coverage-reviewer",""],[46,"run.heartbeat","test-coverage-reviewer",""],[46,"task.lease_renewed","conflict-auditor",""],[46,"run.heartbeat","conflict-auditor",""],[47,"task.lease_renewed","explore",""],[47,"run.heartbeat","explore",""],[47,"task.lease_renewed","test-coverage-reviewer",""],[47,"run.heartbeat","test-coverage-reviewer",""],[47,"task.lease_renewed","conflict-auditor",""],[47,"run.heartbeat","conflict-auditor",""],[49,"task.lease_renewed","explore",""],[49,"run.heartbeat","explore",""],[49,"task.lease_renewed","test-coverage-reviewer",""],[49,"run.heartbeat","test-coverage-reviewer",""],[49,"task.lease_renewed","conflict-auditor",""],[49,"run.heartbeat","conflict-auditor",""],[51,"task.lease_renewed","explore",""],[51,"run.heartbeat","explore",""],[51,"task.lease_renewed","test-coverage-reviewer",""],[51,"run.heartbeat","test-coverage-reviewer",""],[52,"task.lease_renewed","explore",""],[52,"run.heartbeat","explore",""],[53,"task.lease_renewed","test-coverage-reviewer",""],[53,"run.heartbeat","test-coverage-reviewer",""],[54,"artifact.saved","conflict-auditor",{"type":"verification","conf":0.9,"sha":"93b0b98d44","claim":""}],[54,"artifact.saved","conflict-auditor",{"type":"finding","conf":0.99,"sha":"3a59a5b157","claim":"[High] The recursive-force delete gate misses ordinary equivalent spellings: rm -r -f /, rm --recurs"}],[54,"artifact.saved","conflict-auditor",{"type":"finding","conf":0.99,"sha":"f991c6f695","claim":"[Medium] Cancellation is not actually edge-triggered as documented: stale_cancel is captured once, s"}],[54,"artifact.saved","conflict-auditor",{"type":"finding","conf":0.99,"sha":"db6dbd3be1","claim":"[Medium] MAX_CAPTURED_OUTPUT is described as a byte cap, but capture and truncation count Unicode ch"}],[54,"artifact.saved","conflict-auditor",{"type":"finding","conf":1.0,"sha":"973fe87b79","claim":"[Low] The context-switch latch in guard_destructive_command is dead for all current classifier outpu"}],[54,"artifact.saved","conflict-auditor",{"type":"decision","conf":0.8,"sha":"8be9ba4cb4","claim":""}],[54,"artifact.saved","conflict-auditor",{"type":"verification","conf":1.0,"sha":"d1a14cf5e3","claim":""}],[54,"task.saved","conflict-auditor","complete"],[54,"worker.completed_task","conflict-auditor",""],[54,"task.lease_renewed","explore",""],[54,"run.heartbeat","explore",""],[54,"task.lease_renewed","test-coverage-reviewer",""],[54,"run.heartbeat","test-coverage-reviewer",""],[56,"task.lease_renewed","explore",""],[56,"run.heartbeat","explore",""],[56,"task.lease_renewed","test-coverage-reviewer",""],[56,"run.heartbeat","test-coverage-reviewer",""],[58,"task.lease_renewed","explore",""],[58,"run.heartbeat","explore",""],[58,"task.lease_renewed","test-coverage-reviewer",""],[58,"run.heartbeat","test-coverage-reviewer",""],[59,"task.lease_renewed","explore",""],[59,"run.heartbeat","explore",""],[59,"task.lease_renewed","test-coverage-reviewer",""],[59,"run.heartbeat","test-coverage-reviewer",""],[61,"task.lease_renewed","explore",""],[61,"run.heartbeat","explore",""],[61,"task.lease_renewed","test-coverage-reviewer",""],[61,"run.heartbeat","test-coverage-reviewer",""],[62,"artifact.saved","explore",{"type":"verification","conf":0.9,"sha":"1b6d7c3f7d","claim":""}],[62,"artifact.saved","explore",{"type":"finding","conf":0.99,"sha":"72bec46696","claim":"[High] Full-auto recursive-delete gate misses normal equivalent flag spellings and quoted executable"}],[62,"artifact.saved","explore",{"type":"risk","conf":0.99,"sha":"cb8016affc","claim":""}],[62,"artifact.saved","explore",{"type":"finding","conf":0.99,"sha":"2d738f0d13","claim":"[Medium] Explicit process-tree survival coverage is platform-limited: test_process_group_kill_no_orp"}],[62,"artifact.saved","explore",{"type":"finding","conf":0.99,"sha":"113b80eb89","claim":"[Low] The output cap documented as bytes is implemented in decoded characters. UTF-8 text decoding p"}],[62,"artifact.saved","explore",{"type":"finding","conf":1.0,"sha":"ea7895bdf7","claim":"[Informational] The module separates policy from execution: resolve_timeout/resolve_hard_ceiling fee"}],[62,"artifact.saved","explore",{"type":"decision","conf":0.8,"sha":"ed4a5b1473","claim":""}],[62,"artifact.saved","explore",{"type":"verification","conf":1.0,"sha":"27e160c567","claim":""}],[62,"task.saved","explore","complete"],[62,"worker.completed_task","explore",""],[63,"task.lease_renewed","test-coverage-reviewer",""],[63,"run.heartbeat","test-coverage-reviewer",""],[64,"task.lease_renewed","test-coverage-reviewer",""],[64,"run.heartbeat","test-coverage-reviewer",""],[66,"task.lease_renewed","test-coverage-reviewer",""],[66,"run.heartbeat","test-coverage-reviewer",""],[67,"artifact.saved","test-coverage-reviewer",{"type":"verification","conf":0.9,"sha":"860474817e","claim":""}],[67,"artifact.saved","test-coverage-reviewer",{"type":"finding","conf":0.98,"sha":"acfe6a2262","claim":"[High] The main classifier regression matrix leaves device-redirect, dynamic-code-exec, shell-exec-f"}],[67,"artifact.saved","test-coverage-reviewer",{"type":"finding","conf":0.94,"sha":"0f46972efe","claim":"[Medium] The explicitly forced threaded-pipe test proves only normal completion. The high-risk queue"}],[67,"artifact.saved","test-coverage-reviewer",{"type":"finding","conf":0.99,"sha":"d26d2c7abc","claim":"[Medium] Timeout precedence has a mutation-insensitive assertion: timeout=600 and ceiling=900 passes"}],[67,"artifact.saved","test-coverage-reviewer",{"type":"risk","conf":0.98,"sha":"81a94a1c08","claim":""}],[67,"artifact.saved","test-coverage-reviewer",{"type":"decision","conf":0.8,"sha":"410e018b1d","claim":""}],[67,"artifact.saved","test-coverage-reviewer",{"type":"verification","conf":1.0,"sha":"4efbae4e81","claim":""}],[67,"task.saved","test-coverage-reviewer","complete"],[67,"worker.completed_task","test-coverage-reviewer",""],[67,"job.status","","stitching"],[67,"summary.written","",""],[67,"job.status","","complete"]];

  var ROLES = [
    {"id": "conflict-auditor", "need": 85, "tin": 98028, "tout": 1570, "task": "task_4e2cec0ad97a", "model": "gpt-6.1-sol", "bill": "api-billed &#183; openrouter"},
    {"id": "explore", "need": 73, "tin": 79158, "tout": 1662, "task": "task_33a48684d4b9", "model": "gpt-6.1-sol", "bill": "api-billed &#183; openrouter"},
    {"id": "test-coverage-reviewer", "need": 75, "tin": 94625, "tout": 1722, "task": "task_cd6f9879a3df", "model": "gpt-6.1-sol", "bill": "api-billed &#183; openrouter"}
  ];

  /* ------------------------------------------------------------------ */
  /* Downloads, release tag, stars                                        */
  /* ------------------------------------------------------------------ */
  var os = (function () {
    var ua = (navigator.userAgent || "") + " " + (navigator.platform || "");
    if (/Win/i.test(ua)) return "win";
    if (/Mac/i.test(ua) && !/iPhone|iPad|iPod/i.test(ua)) return "mac";
    if (/Linux|X11/i.test(ua) && !/Android/i.test(ua)) return "linux";
    return "mac";
  })();
  var LABEL = { mac: "Download for macOS", win: "Download for Windows", linux: "Download for Linux" };
  var OTHER = {
    mac: 'Universal build for Apple silicon and Intel. Also for <a href="' + RELEASES + '">Windows and Linux</a>.',
    win: 'Windows installer (.exe). Also for <a href="' + RELEASES + '">macOS and Linux</a>.',
    linux: 'AppImage for x86_64. Also for <a href="' + RELEASES + '">macOS and Windows</a>.'
  };
  var hrefs = {
    mac: RELEASES.replace("/latest", "/latest/download/Marionette-0.9.596-universal.dmg"),
    win: RELEASES.replace("/latest", "/latest/download/Marionette-0.9.596-Setup.exe"),
    linux: RELEASES.replace("/latest", "/latest/download/Marionette-0.9.596.AppImage")
  };
  function paintDownloads() {
    ["dl1", "dl2"].forEach(function (id) {
      var a = document.getElementById(id);
      if (!a) return;
      a.href = hrefs[os] || RELEASES;
      $(".dl-label", a).textContent = LABEL[os];
    });
    var o = $("#dlOther"); if (o) o.innerHTML = OTHER[os];
  }
  paintDownloads();

  fetch("https://api.github.com/repos/" + REPO + "/releases/latest", { headers: { Accept: "application/vnd.github+json" } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (rel) {
      if (!rel) return;
      if (rel.tag_name) { var t = $("#relTag"); if (t) t.textContent = rel.tag_name; }
      (rel.assets || []).forEach(function (a) {
        var n = a.name.toLowerCase();
        if (n.endsWith(".dmg")) hrefs.mac = a.browser_download_url;
        else if (n.endsWith(".exe")) hrefs.win = a.browser_download_url;
        else if (n.endsWith(".appimage")) hrefs.linux = a.browser_download_url;
      });
      paintDownloads();
    })
    .catch(function () {});
  fetch("https://api.github.com/repos/" + REPO)
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) { if (d && typeof d.stargazers_count === "number") $("#stars").textContent = d.stargazers_count.toLocaleString(); })
    .catch(function () {});

  /* ------------------------------------------------------------------ */
  /* Nav                                                                  */
  /* ------------------------------------------------------------------ */
  var nav = $("#nav");
  function onScrollNav() { nav.classList.toggle("scrolled", window.scrollY > 12); }
  onScrollNav();

  /* ------------------------------------------------------------------ */
  /* Hero strings: puppet strings with signals running down them          */
  /* ------------------------------------------------------------------ */
  (function strings() {
    var cv = $("#strings"); if (!cv) return;
    var ctx = cv.getContext("2d");
    var W = 0, H = 0, dpr = 1, lines = [], pulses = [], mouse = { x: -9999, y: -9999 }, running = false, raf = 0, last = 0;

    function rand(a, b) { return a + Math.random() * (b - a); }
    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      W = cv.clientWidth; H = cv.clientHeight;
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var gap = W < 700 ? 30 : 38;
      lines = [];
      for (var x = gap / 2; x < W; x += gap) {
        var centerBias = 1 - Math.abs(x / W - 0.5) * 1.3;
        lines.push({ x: x + rand(-6, 6), len: H * rand(0.42, 0.62) + H * 0.32 * Math.max(0, centerBias), amp: rand(4, 14), ph: rand(0, Math.PI * 2), sp: rand(0.25, 0.6), a: rand(0.035, 0.075), bend: 0 });
      }
      pulses = [];
      draw(0);
    }
    function spawn() {
      var l = lines[(Math.random() * lines.length) | 0]; if (!l) return;
      pulses.push({ l: l, y: -40, v: rand(140, 320), len: rand(40, 110) });
    }
    function point(l, t, time) {
      var sway = Math.sin(time * l.sp + l.ph) * l.amp * t * t + l.bend * Math.sin(Math.PI * t);
      return { x: l.x + sway, y: t * l.len };
    }
    function draw(time) {
      ctx.clearRect(0, 0, W, H);
      ctx.lineWidth = 1;
      for (var i = 0; i < lines.length; i++) {
        var l = lines[i];
        var dx = mouse.x - l.x, dy = mouse.y;
        var near = Math.abs(dx) < 140 && dy > 0 && dy < l.len ? (1 - Math.abs(dx) / 140) : 0;
        l.bend += ((near ? -Math.sign(dx) * near * 26 : 0) - l.bend) * 0.08;
        ctx.strokeStyle = "rgba(236,236,239," + (l.a + near * 0.1).toFixed(3) + ")";
        ctx.beginPath();
        for (var s = 0; s <= 16; s++) {
          var p = point(l, s / 16, time);
          if (s === 0) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
        var end = point(l, 1, time);
        ctx.fillStyle = "rgba(236,236,239," + (l.a * 1.6).toFixed(3) + ")";
        ctx.beginPath(); ctx.arc(end.x, end.y, 1.6, 0, Math.PI * 2); ctx.fill();
      }
      for (var j = pulses.length - 1; j >= 0; j--) {
        var q = pulses[j];
        var t1 = clamp(q.y / q.l.len, 0, 1), t0 = clamp((q.y - q.len) / q.l.len, 0, 1);
        if (t1 > t0) {
          var a = point(q.l, t0, time), b = point(q.l, t1, time);
          var g = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
          g.addColorStop(0, "rgba(224,164,90,0)");
          g.addColorStop(1, "rgba(244,200,143,0.95)");
          ctx.strokeStyle = g; ctx.lineWidth = 1.6;
          ctx.beginPath(); ctx.moveTo(a.x, a.y);
          for (var k = 1; k <= 6; k++) { var pp = point(q.l, t0 + (t1 - t0) * k / 6, time); ctx.lineTo(pp.x, pp.y); }
          ctx.stroke();
          ctx.lineWidth = 1;
        }
        if (q.y - q.len > q.l.len) {
          var e = point(q.l, 1, time);
          ctx.fillStyle = "rgba(224,164,90,0.9)";
          ctx.beginPath(); ctx.arc(e.x, e.y, 2.6, 0, Math.PI * 2); ctx.fill();
          pulses.splice(j, 1);
        }
      }
    }
    function frame(ts) {
      if (!running) return;
      var dt = last ? Math.min(0.05, (ts - last) / 1000) : 0.016; last = ts;
      var time = ts / 1000;
      if (pulses.length < Math.max(6, lines.length / 4) && Math.random() < 0.08) spawn();
      for (var i = 0; i < pulses.length; i++) pulses[i].y += pulses[i].v * dt;
      draw(time);
      raf = requestAnimationFrame(frame);
    }
    function start() { if (running || REDUCED) return; running = true; last = 0; raf = requestAnimationFrame(frame); }
    function stop() { running = false; cancelAnimationFrame(raf); }
    window.addEventListener("resize", function () { resize(); });
    $(".hero").addEventListener("pointermove", function (e) { var r = cv.getBoundingClientRect(); mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top; });
    $(".hero").addEventListener("pointerleave", function () { mouse.x = -9999; });
    document.addEventListener("visibilitychange", function () { if (document.hidden) stop(); else if (heroVisible) start(); });
    var heroVisible = true;
    resize();
    onView($(".hero"), function (vis) { heroVisible = vis; if (vis) start(); else stop(); }, { threshold: 0 });
  })();

  /* ------------------------------------------------------------------ */
  /* Hero window: tilt flattens as you scroll, video pauses offscreen      */
  /* ------------------------------------------------------------------ */
  var win = $("#heroWindow"), stage = $("#stage"), vid = $("#heroVideo");
  function tilt() {
    if (!win || REDUCED) return;
    var r = stage.getBoundingClientRect();
    var p = clamp(1 - (r.top - window.innerHeight * 0.1) / (window.innerHeight * 0.55), 0, 1);
    var e = 1 - Math.pow(1 - p, 3);
    win.style.setProperty("--tilt", (18 * (1 - e)).toFixed(2) + "deg");
    win.style.setProperty("--zoom", (0.94 + 0.06 * e).toFixed(4));
  }
  tilt();
  if (vid) {
    vid.playbackRate = 1;
    onView(vid, function (v) { if (v) { var p = vid.play(); if (p && p.catch) p.catch(function () {}); } else vid.pause(); }, { threshold: 0.05 });
  }

  // Each card annotates the stretch of the video it describes (data-at/data-to,
  // seconds). None covers the composer while the prompt is typed. Without a
  // playing video (reduced motion, no autoplay) every card simply shows.
  var cards = $$(".float[data-at]");
  function syncCards() {
    var t = vid.currentTime;
    cards.forEach(function (c) { c.classList.toggle("on", t >= +c.dataset.at && t < +c.dataset.to); });
  }
  if (vid && cards.length && !REDUCED) {
    // timeupdate fires only ~4x a second; a frame clock keeps the cues tight.
    (function loop() { if (!vid.paused) syncCards(); requestAnimationFrame(loop); })();
    vid.addEventListener("seeked", syncCards);
  } else {
    cards.forEach(function (c) { c.classList.add("on"); });
  }

  /* ------------------------------------------------------------------ */
  /* Reveal + counters                                                    */
  /* ------------------------------------------------------------------ */
  $$(".reveal").forEach(function (n) {
    var io = new IntersectionObserver(function (ents) {
      ents.forEach(function (en) { if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); } });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    io.observe(n);
  });

  function fmt(v, f) {
    if (f === "int") return Math.round(v).toLocaleString("en-US");
    if (f === "usd2") return "$" + v.toFixed(2);
    if (f === "dec1") return v.toFixed(1);
    return String(v);
  }
  $$("[data-count]").forEach(function (n) {
    var target = parseFloat(n.getAttribute("data-count")), f = n.getAttribute("data-fmt");
    if (!target || REDUCED) { n.textContent = fmt(target, f); return; }
    n.textContent = fmt(0, f);
    var io = new IntersectionObserver(function (ents) {
      ents.forEach(function (en) {
        if (!en.isIntersecting) return;
        io.unobserve(n);
        var t0 = performance.now(), dur = 1600;
        (function step(ts) {
          var k = clamp((ts - t0) / dur, 0, 1), e = 1 - Math.pow(1 - k, 4);
          n.textContent = fmt(target * e, f);
          if (k < 1) requestAnimationFrame(step);
        })(t0);
      });
    }, { threshold: 0.4 });
    io.observe(n);
  });
  $$("[data-bars]").forEach(function (card) {
    onView(card, function (vis) {
      if (!vis) return;
      $$("i[data-w]", card).forEach(function (i, k) { setTimeout(function () { i.style.width = i.getAttribute("data-w") + "%"; }, 200 + k * 220); });
    }, { threshold: 0.35 });
  });

  /* Tile spotlight */
  $$(".tile").forEach(function (t) {
    t.addEventListener("pointermove", function (e) {
      var r = t.getBoundingClientRect();
      t.style.setProperty("--mx", (e.clientX - r.left) + "px");
      t.style.setProperty("--my", (e.clientY - r.top) + "px");
    });
  });

  /* ------------------------------------------------------------------ */
  /* Shared: log line formatting from real events                         */
  /* ------------------------------------------------------------------ */
  function tplus(s) { var m = Math.floor(s / 60), x = s % 60; return "T+" + (m < 10 ? "0" : "") + m + ":" + (x < 10 ? "0" : "") + x; }
  function describe(e) {
    var ev = e[1], role = e[2], d = e[3];
    switch (ev) {
      case "job.created": return ["e", "3 roles &middot; audit harness/command_policy.py"];
      case "job.mode": return ["", "analysis, read-only. No files will be edited."];
      case "job.brief": return ["", "repo brief written &middot; 8,133 bytes"];
      case "job.status": return [d === "complete" ? "g" : "a", d];
      case "router.agentic_catalog_merged": return ["", "curated agentic models merged for 4 providers"];
      case "router.registry_reconciled": return ["", "dropped models with no usable credentials"];
      case "task.saved": return [d === "complete" ? "g" : "", role + " &middot; " + d];
      case "task.claimed": return ["a", role + " &middot; lease until " + d];
      case "run.heartbeat": return ["", role];
      case "task.lease_renewed": return ["", role + " &middot; ttl 5s"];
      case "worker.completed_task": return ["g", role + " &middot; done"];
      case "worker.failed_task": return ["r", role + " &middot; " + d];
      case "router.auto_fallback": return ["a", role + " &middot; " + d.reason + " &rarr; " + d.to];
      case "job.auto_fallback_round": return ["", d + " task re-routed"];
      case "summary.written": return ["e", "stitched summary ready"];
      case "artifact.saved":
        var cls = d.type === "finding" ? "a" : d.type === "risk" ? "r" : d.type === "verification" ? "g" : "";
        var extra = d.claim ? " &middot; " + esc(d.claim) : " &middot; sha " + d.sha;
        if (d.type === "routing" && d.model) extra = " &middot; " + d.model;
        return [cls, d.type + " &middot; " + (role || "router") + extra];
    }
    return ["", ev];
  }

  /* ------------------------------------------------------------------ */
  /* Scroll story: camera over real frames                                */
  /* ------------------------------------------------------------------ */
  (function story() {
    var frame = $("#camFrame"); if (!frame) return;
    var cam = $("#cam"), ring = $("#focusRing"), imgs = $$("img", cam), steps = $$(".step"), log = $("#camLog");
    var hudStep = $("#hudStep"), hudText = $("#hudText"), bars = $$("#storyProgress span");
    // Focus rectangles in normalized frame coordinates (frame is 1440x900 css px).
    var SHOTS = [
      { img: 0, f: [0.318, 0.035, 0.29, 0.125], hud: "composer &rarr; pilot" },
      { img: 1, f: [0.772, 0.005, 0.226, 0.37], hud: "swarm tracker &middot; expanded" },
      { img: 1, f: null, hud: "state.sqlite3 &middot; live" },
      { img: 2, f: [0.272, 0.125, 0.3, 0.25], hud: "transcript &middot; answer" },
      { img: 2, f: [0.0, 0.976, 0.272, 0.022], z: 3.4, hud: "status bar &middot; receipts" }
    ];
    var cur = -1;
    function apply(i) {
      var s = SHOTS[i], W = frame.clientWidth, H = frame.clientHeight;
      imgs.forEach(function (im, k) { im.classList.toggle("on", k === s.img); });
      var sc = 1, tx = 0, ty = 0;
      if (s.f) {
        var fx = s.f[0], fy = s.f[1], fw = s.f[2], fh = s.f[3];
        sc = clamp(Math.min(0.84 / fw, 0.84 / fh), 1, s.z || 2.6);
        tx = clamp(W * (0.5 - sc * (fx + fw / 2)), W * (1 - sc), 0);
        // The status bar is the capture's last row: let the camera lift the image a
        // little past its bottom edge so the ring centers on it instead of hugging
        // (and clipping against) the frame.
        ty = clamp(H * (0.5 - sc * (fy + fh / 2)), H * (1 - sc) - H * 0.07, 0);
        var pad = 6, inset = 4;
        var L = Math.max(inset, tx + sc * fx * W - pad), T = Math.max(inset, ty + sc * fy * H - pad);
        var R = Math.min(W - inset, tx + sc * (fx + fw) * W + pad), B = Math.min(H - inset, ty + sc * (fy + fh) * H + pad);
        ring.style.opacity = "1";
        ring.style.left = L + "px"; ring.style.top = T + "px";
        ring.style.width = (R - L) + "px"; ring.style.height = (B - T) + "px";
      } else {
        ring.style.opacity = "0";
      }
      cam.style.transform = "translate(" + tx.toFixed(1) + "px," + ty.toFixed(1) + "px) scale(" + sc.toFixed(4) + ")";
      hudStep.textContent = "0" + (i + 1);
      hudText.innerHTML = s.hud;
      bars.forEach(function (b, k) { b.classList.toggle("done", k <= i); });
      steps.forEach(function (st, k) { st.classList.toggle("active", k === i); });
      if (i === 2) startLog(); else stopLog();
    }
    function pick() {
      var mid = window.innerHeight * (window.innerWidth <= 900 ? 0.72 : 0.55), idx = 0;
      steps.forEach(function (st, k) { if (st.getBoundingClientRect().top < mid) idx = k; });
      if (idx !== cur) { cur = idx; apply(idx); }
    }
    var logTimer = 0, logIdx = 0;
    var stream = EVENTS.filter(function (e) { return e[1] !== "job.brief"; });
    function pushLine() {
      var e = stream[logIdx % stream.length]; logIdx++;
      var d = describe(e);
      var row = el("div", "row", '<span class="t">' + tplus(e[0]) + '</span><span><span class="' + (d[0] || "e") + '">' + e[1] + "</span> " + d[1] + "</span>");
      log.appendChild(row);
      while (log.children.length > 7) log.removeChild(log.firstChild);
    }
    function startLog() {
      log.classList.add("on");
      if (logTimer) return;
      if (!log.children.length) for (var k = 0; k < 4; k++) pushLine();
      logTimer = setInterval(pushLine, REDUCED ? 1600 : 520);
    }
    function stopLog() { log.classList.remove("on"); clearInterval(logTimer); logTimer = 0; }
    window.addEventListener("resize", function () { if (cur >= 0) apply(cur); });
    window.addEventListener("scroll", pick, { passive: true });
    pick();
  })();

  /* ------------------------------------------------------------------ */
  /* Kernel replay                                                        */
  /* ------------------------------------------------------------------ */
  (function kernel() {
    var host = $("#kernelSvgHost"); if (!host) return;
    var NS = "http://www.w3.org/2000/svg";
    var WY = [64, 224, 384];
    var svg =
      '<svg class="kernel-svg" viewBox="0 0 1200 540" role="img" aria-label="Animated diagram of the Puppetmaster kernel replaying job_338f29ad552d">' +
      '<defs><linearGradient id="kg" x1="0" x2="1"><stop offset="0" stop-color="#e0a45a" stop-opacity="0"/><stop offset="1" stop-color="#e0a45a" stop-opacity=".9"/></linearGradient></defs>' +
      // pilot
      '<rect class="box" x="24" y="196" width="232" height="156" rx="14"/>' +
      '<text class="lbl" x="46" y="226">PILOT</text>' +
      '<text class="big" x="46" y="256">claude-sonnet-5.5</text>' +
      '<text x="46" y="280">openrouter &#183; depth DEEP</text>' +
      '<text class="acc" x="46" y="322" id="kPilotAct">waiting</text>' +
      // pilot to kernel
      '<path class="wire" id="wP" d="M256 274 C300 274 310 274 352 274"/>' +
      // kernel
      '<rect class="box kern" x="352" y="28" width="404" height="488" rx="18"/>' +
      '<text class="lbl acc" x="376" y="60">PUPPETMASTER</text>' +
      '<text class="big" x="376" y="88">kernel</text>' +
      '<text x="736" y="60" text-anchor="end" id="kClock">T+00:00</text>' +
      row(108, "ROUTER", "kRouter", "policy=balanced") +
      row(204, "LEASES", "kLeases", "0 held &#183; ttl 5s") +
      row(300, "STORE &#183; state.sqlite3", "kStore", "0 artifacts") +
      row(408, "GRAPH", "kGraph", "0 edges") +
      '<g id="kSlots"></g>' +
      // workers
      worker(0) + worker(1) + worker(2) +
      '<g id="kParticles"></g>' +
      "</svg>";
    function row(y, label, id, val) {
      var h = label.indexOf("STORE") === 0 ? 92 : 80;
      return '<rect class="row" id="' + id + 'R" x="372" y="' + y + '" width="364" height="' + h + '" rx="10"/>' +
        '<text class="lbl" x="392" y="' + (y + 28) + '">' + label + "</text>" +
        '<text class="big" style="font-size:14px" x="392" y="' + (y + 54) + '" id="' + id + '">' + val + "</text>";
    }
    function worker(i) {
      var y = WY[i], r = ROLES[i];
      return '<path class="wire" id="wW' + i + '" d="M756 ' + (y + 60) + " C790 " + (y + 60) + " 790 " + (y + 60) + " 820 " + (y + 60) + '"/>' +
        '<g class="worker" id="kW' + i + '">' +
        '<rect class="box" x="820" y="' + y + '" width="356" height="120" rx="14"/>' +
        '<text class="lbl" x="844" y="' + (y + 30) + '">' + r.id.toUpperCase() + "</text>" +
        '<text class="big" x="844" y="' + (y + 58) + '" id="kWm' + i + '">' + r.model + '</text>' +
        '<text x="844" y="' + (y + 82) + '" id="kWp' + i + '">needs ' + r.need + " &#183; " + r.bill + "</text>" +
        '<text x="844" y="' + (y + 104) + '" id="kWs' + i + '">queued</text>' +
        '<circle class="ring-track" cx="1136" cy="' + (y + 46) + '" r="18"/>' +
        '<circle class="ring" id="kWr' + i + '" cx="1136" cy="' + (y + 46) + '" r="18"/>' +
        '<circle class="hb" id="kWh' + i + '" cx="1136" cy="' + (y + 46) + '" r="4"/>' +
        "</g>";
    }
    host.innerHTML = svg;
    var S = host.firstChild;
    var statusPill = $("#jobStatus"), logBox = $("#kLog");
    var counts = { finding: 0, risk: 0, verification: 0, decision: 0, routing: 0 };
    var COLOR = { finding: "#e0a45a", risk: "#e0796b", verification: "#4ec08a", decision: "#cfd3d9", routing: "#6b7178" };
    var slotN = 0, edges = 0, held = 0, renewals = 0;
    var timers = [], workerState = [{}, {}, {}], tokenRaf = 0, gen = 0;

    function byRole(r) { for (var i = 0; i < ROLES.length; i++) if (ROLES[i].id === r) return i; return -1; }
    function setText(id, v) { var n = S.getElementById ? S.getElementById(id) : document.getElementById(id); if (n) n.innerHTML = v; }
    function setStatus(s) { statusPill.textContent = s; statusPill.setAttribute("data-s", s); }
    function hot(id, on) { var n = document.getElementById(id + "R"); if (n) n.classList.toggle("hot", on); }
    function flash(id) { hot(id, true); setTimeout(function () { hot(id, false); }, 420); }
    function slotXY(k) { var col = k % 16, rw = Math.floor(k / 16); return { x: 562 + col * 10.5, y: 362 + rw * 11 }; }
    function addSlot(type) {
      var k = slotN++, p = slotXY(k);
      var r = document.createElementNS(NS, "rect");
      r.setAttribute("x", p.x); r.setAttribute("y", p.y); r.setAttribute("width", 8); r.setAttribute("height", 8); r.setAttribute("rx", 2);
      r.setAttribute("fill", COLOR[type] || "#9aa1ab"); r.style.opacity = "0"; r.style.transition = "opacity 300ms";
      document.getElementById("kSlots").appendChild(r);
      requestAnimationFrame(function () { r.style.opacity = "1"; });
    }
    function particle(fromRole, type) {
      var g = gen, i = byRole(fromRole), p = slotXY(slotN);
      var sx = i >= 0 ? 820 : 556, sy = i >= 0 ? WY[i] + 60 : 150;
      var c = document.createElementNS(NS, "circle");
      c.setAttribute("r", 4.5); c.setAttribute("fill", COLOR[type] || "#9aa1ab"); c.setAttribute("class", "particle");
      c.style.color = COLOR[type] || "#9aa1ab";
      document.getElementById("kParticles").appendChild(c);
      var t0 = performance.now(), dur = REDUCED ? 1 : 700, cx = (sx + p.x) / 2, cy = Math.min(sy, p.y) - 60;
      (function step(ts) {
        var k = clamp((ts - t0) / dur, 0, 1), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        var x = (1 - e) * (1 - e) * sx + 2 * (1 - e) * e * cx + e * e * (p.x + 4);
        var y = (1 - e) * (1 - e) * sy + 2 * (1 - e) * e * cy + e * e * (p.y + 4);
        c.setAttribute("cx", x); c.setAttribute("cy", y);
        if (k < 1) requestAnimationFrame(step); else { c.remove(); if (g === gen) addSlot(type); }
      })(t0);
    }
    function legend() { $$("#kLegend b[data-c]").forEach(function (b) { b.textContent = counts[b.getAttribute("data-c")]; }); }
    function logLine(e) {
      var d = describe(e);
      var row = el("div", "row", '<span class="t">' + tplus(e[0]) + '</span><span class="' + (d[0] || "e") + '">' + e[1] + "</span><span>" + d[1] + "</span>");
      logBox.appendChild(row);
      while (logBox.children.length > 12) logBox.removeChild(logBox.firstChild);
    }
    function wire(id, on) { var w = document.getElementById(id); if (w) w.classList.toggle("flow", on); }
    function ringRun(i, on) {
      var r = document.getElementById("kWr" + i); if (!r) return;
      if (on && !REDUCED) { r.style.animation = "none"; r.getBoundingClientRect(); r.style.animation = "kring 1500ms linear infinite"; }
      else { r.style.animation = "none"; r.style.strokeDashoffset = on ? "20" : "0"; }
    }
    function heartbeat(i) {
      var h = document.getElementById("kWh" + i); if (!h || REDUCED) return;
      h.animate([{ opacity: 0.9, r: 4 }, { opacity: 0, r: 26 }], { duration: 700, easing: "ease-out" });
    }
    function tokens() {
      var now = performance.now(), any = false;
      for (var i = 0; i < 3; i++) {
        var w = workerState[i];
        if (w.start && !w.done) {
          any = true;
          var k = clamp((now - w.start) / Math.max(1, w.dur), 0, 0.97);
          setText("kWs" + i, ROLES[i].tin ? "running &#183; " + Math.round(ROLES[i].tin * k).toLocaleString("en-US") + " tokens in" : "running");
        }
      }
      if (any) tokenRaf = requestAnimationFrame(tokens);
    }

    var injected = false;
    function injectKeyframes() {
      if (injected) return; injected = true;
      var st = document.createElement("style");
      st.textContent = "@keyframes kring{0%{stroke-dashoffset:0}90%{stroke-dashoffset:100}100%{stroke-dashoffset:0}}";
      document.head.appendChild(st);
    }

    function reset() {
      timers.forEach(clearTimeout); timers = []; gen++;
      cancelAnimationFrame(tokenRaf);
      Object.keys(counts).forEach(function (k) { counts[k] = 0; });
      slotN = 0; edges = 0; held = 0; renewals = 0;
      document.getElementById("kSlots").innerHTML = "";
      document.getElementById("kParticles").innerHTML = "";
      logBox.innerHTML = "";
      legend();
      setStatus("queued");
      setText("kRouter", "policy=balanced"); setText("kLeases", "0 held &#183; ttl 5s");
      setText("kStore", "0 artifacts"); setText("kGraph", "0 edges"); setText("kClock", "T+00:00");
      setText("kPilotAct", "waiting");
      ["wP", "wW0", "wW1", "wW2"].forEach(function (w) { wire(w, false); });
      for (var i = 0; i < 3; i++) {
        workerState[i] = {};
        var g = document.getElementById("kW" + i); g.classList.remove("running", "done", "failed");
        setText("kWs" + i, "queued"); setText("kWm" + i, ROLES[i].model); setText("kWp" + i, "needs " + ROLES[i].need + " &#183; " + ROLES[i].bill); ringRun(i, false);
      }
    }

    // Replay schedule: compress the 269 real seconds to roughly 22, keep bursts readable.
    function schedule() {
      var out = [], prev = -1;
      EVENTS.forEach(function (e) { var r = Math.max(prev + 0.13, e[0] * 0.075 + 0.6); out.push(r); prev = r; });
      return out;
    }
    var SCHED = schedule();
    var claimAt = {}, doneAt = {};
    EVENTS.forEach(function (e, k) {
      if (e[1] === "task.claimed") claimAt[e[2]] = SCHED[k];
      if (e[1] === "worker.completed_task") doneAt[e[2]] = SCHED[k];
    });

    function handle(e) {
      var ev = e[1], role = e[2], d = e[3], i = byRole(role);
      setText("kClock", tplus(e[0]));
      logLine(e);
      switch (ev) {
        case "job.created": setText("kPilotAct", "run_swarm &#8594; 3 roles"); wire("wP", true); break;
        case "job.status":
          setStatus(d);
          if (d === "complete") { wire("wP", false); setText("kPilotAct", "stitching answer &#183; done"); }
          break;
        case "router.agentic_catalog_merged": case "router.registry_reconciled": flash("kRouter"); break;
        case "task.saved": if (d === "queued") flash("kRouter"); break;
        case "task.claimed":
          held++; setText("kLeases", held + " held &#183; ttl 5s"); flash("kLeases");
          if (i >= 0) {
            var g = document.getElementById("kW" + i); g.classList.add("running");
            wire("wW" + i, true); ringRun(i, true);
            workerState[i] = { start: performance.now(), dur: ((doneAt[role] || 0) - (claimAt[role] || 0)) * 1000 };
            cancelAnimationFrame(tokenRaf); tokenRaf = requestAnimationFrame(tokens);
          }
          break;
        case "task.lease_renewed": renewals++; setText("kLeases", held + " held &#183; " + renewals + " renewals"); flash("kLeases"); break;
        case "run.heartbeat": if (i >= 0) heartbeat(i); break;
        case "artifact.saved":
          counts[d.type] = (counts[d.type] || 0) + 1; legend();
          edges++; setText("kGraph", edges + " edges &#183; task &#8594; artifact");
          particle(role, d.type);
          var total = Object.keys(counts).reduce(function (n, k) { return n + counts[k]; }, 0);
          if (REDUCED) setText("kStore", total + " artifacts &#183; sha256");
          else timers.push(setTimeout(function () { setText("kStore", total + " artifacts &#183; sha256"); flash("kStore"); }, 760));
          if (d.type === "routing" && counts.routing <= 3) setText("kRouter", ROLES[0].model + " &#215; " + counts.routing + " &#183; cap " + ROLES.map(function (r) { return r.need; }).join("/"));
          break;
        case "worker.completed_task":
          if (i >= 0) {
            workerState[i].done = true; held = Math.max(0, held - 1); setText("kLeases", held + " held &#183; " + renewals + " renewals");
            var gw = document.getElementById("kW" + i); gw.classList.remove("running"); gw.classList.add("done");
            wire("wW" + i, false); ringRun(i, false);
            setText("kWs" + i, "complete &#183; " + ROLES[i].tin.toLocaleString("en-US") + " in &#183; " + ROLES[i].tout.toLocaleString("en-US") + " out");
          }
          break;
        case "worker.failed_task":
          if (i >= 0) {
            workerState[i].done = true; held = Math.max(0, held - 1); setText("kLeases", held + " held &#183; " + renewals + " renewals");
            var gf = document.getElementById("kW" + i); gf.classList.remove("running"); gf.classList.add("failed");
            wire("wW" + i, false); ringRun(i, false);
            setText("kWs" + i, "failed &#183; " + String(d).replace("_", " "));
          }
          break;
        case "router.auto_fallback":
          if (i >= 0) {
            setText("kWm" + i, esc(d.to)); setText("kWp" + i, "needs " + ROLES[i].need + " &#183; api-billed &#183; openrouter");
            setText("kRouter", "auto-fallback &#8594; " + esc(d.to)); flash("kRouter");
            var gr = document.getElementById("kW" + i); gr.classList.remove("failed"); gr.classList.add("running");
            wire("wW" + i, true); ringRun(i, true);
            workerState[i] = { start: performance.now(), dur: 1500 };
            setText("kWs" + i, "re-routed &#183; attempt " + d.attempt);
          }
          break;
        case "summary.written": setText("kPilotAct", "summary &#8592; kernel"); break;
      }
    }

    var playing = false;
    function play() {
      injectKeyframes(); reset(); playing = true;
      if (REDUCED) { EVENTS.forEach(handle); playing = false; return; }
      EVENTS.forEach(function (e, k) { timers.push(setTimeout(function () { handle(e); }, SCHED[k] * 1000)); });
      timers.push(setTimeout(function () { playing = false; if (inView) timers.push(setTimeout(play, 5000)); }, (SCHED[SCHED.length - 1] + 1) * 1000));
    }
    var inView = false, started = false;
    onView($("#kernelCard"), function (vis) {
      inView = vis;
      if (vis && !started) { started = true; play(); }
    }, { threshold: 0.25 });
    $("#kReplay").addEventListener("click", function () { play(); });
    reset();
  })();

  /* ------------------------------------------------------------------ */
  /* Router: real routing records                                         */
  /* ------------------------------------------------------------------ */
  (function router() {
    var chart = $("#chart"); if (!chart) return;
    var MODELS = [
      {"id": "gemini-3.8-flash", "p": "google &#183; openrouter", "cap": 80},
      {"id": "gemini-3.7-flash", "p": "google &#183; openrouter", "cap": 80},
      {"id": "deepseek-v4.1-flash", "p": "deepseek &#183; openrouter", "cap": 80},
      {"id": "gpt-6-luna", "p": "openai &#183; openrouter", "cap": 80},
      {"id": "mimo-v2.6-flash", "p": "xiaomi &#183; openrouter", "cap": 80},
      {"id": "mimo-v2.6-pro", "p": "xiaomi &#183; openrouter", "cap": 80},
      {"id": "claude-sonnet-5.5", "p": "anthropic &#183; openrouter", "cap": 80},
      {"id": "glm-5.3-flash", "p": "z-ai &#183; openrouter", "cap": 80},
      {"id": "gpt-6.1-sol", "p": "openai &#183; openrouter", "cap": 85, "win": true, "cost": "$0.0186"},
      {"id": "kimi-k3", "p": "moonshotai &#183; openrouter", "cost": "$0.0204", "cap": 98},
      {"id": "claude-opus-5.5", "p": "anthropic &#183; openrouter", "cost": "$0.0372", "cap": 99}
    ];
    var REASONS = {
      "conflict-auditor": "policy=balanced: cheapest sufficient model whose capability_score (85) >= needed (85)",
      "explore": "cache affinity: prefer_model_id=agentic/openai/gpt-6.1-sol still meets capability need (73); keep shared job-brief prefix",
      "test-coverage-reviewer": "cache affinity: prefer_model_id=agentic/openai/gpt-6.1-sol still meets capability need (75); keep shared job-brief prefix"
    };
    var WIN = "$0.0186";
    var tabs = $("#routerTabs"), reasonEl = $("#reasonText"), timers = [];
    ROLES.forEach(function (r, i) {
      var b = el("button", "", '<span class="role">' + r.id + '</span><span class="need">needs capability ' + r.need + "</span>");
      b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", i === 0 ? "true" : "false");
      b.addEventListener("click", function () { select(i); });
      tabs.appendChild(b);
    });
    chart.innerHTML = '<div class="chart-head"><span>model &#183; provider</span><span>capability score</span><span>verdict</span></div>' +
      MODELS.map(function (m) {
        return '<div class="crow"><div class="name">' + m.id + "<small>" + m.p + '</small></div><div class="track"><div class="bar"></div><span class="cap">' + m.cap + '</span></div><div class="why"></div></div>';
      }).join("") + '<div class="threshold" id="thresh"><span></span></div>';
    var rows = $$(".crow", chart), th = $("#thresh");

    function trackLeft() { var t = $(".track", rows[0]); return t.offsetLeft; }
    function trackWidth() { return $(".track", rows[0]).offsetWidth; }
    function placeThreshold(need) {
      th.style.left = (trackLeft() + trackWidth() * need / 100) + "px";
      $("span", th).textContent = "needs " + need;
    }
    function typeReason(text) {
      reasonEl.textContent = "";
      if (REDUCED) { reasonEl.textContent = text; return; }
      var k = 0;
      (function tick() { reasonEl.textContent = text.slice(0, k); k += 2; if (k <= text.length + 1) timers.push(setTimeout(tick, 14)); })();
    }
    function select(i) {
      timers.forEach(clearTimeout); timers = [];
      var role = ROLES[i], affinity = i > 0;
      $$("button", tabs).forEach(function (b, k) { b.setAttribute("aria-selected", k === i ? "true" : "false"); });
      chart.classList.remove("grown", "thresh");
      rows.forEach(function (r) { r.className = "crow"; $(".bar", r).style.width = "0"; $(".why", r).textContent = ""; });
      reasonEl.textContent = "";
      var D = REDUCED ? 0 : 1;
      timers.push(setTimeout(function () {
        rows.forEach(function (r, k) { timers.push(setTimeout(function () { $(".bar", r).style.width = MODELS[k].cap + "%"; }, k * 50 * D)); });
        chart.classList.add("grown");
      }, 60 * D));
      timers.push(setTimeout(function () { placeThreshold(role.need); chart.classList.add("thresh"); }, 800 * D));
      timers.push(setTimeout(function () {
        rows.forEach(function (r, k) {
          var m = MODELS[k], why = $(".why", r);
          if (m.win) return;
          if (affinity) {
            why.textContent = "cache affinity kept sibling";
            r.classList.add(m.cap < role.need ? "below" : "pricier", "judged");
          } else if (m.cap < role.need) {
            why.textContent = "capability_score " + m.cap + " < needed " + role.need;
            r.classList.add("below", "judged");
          }
        });
      }, 1500 * D));
      timers.push(setTimeout(function () {
        if (affinity) return;
        rows.forEach(function (r, k) {
          var m = MODELS[k];
          if (!m.win && m.cap >= role.need) { $(".why", r).textContent = "pricier: " + m.cost + " vs " + WIN; r.classList.add("pricier", "judged"); }
        });
      }, 2200 * D));
      timers.push(setTimeout(function () {
        rows.forEach(function (r, k) {
          if (MODELS[k].win) { r.classList.add("win", "judged"); $(".why", r).textContent = affinity ? "selected, kept sibling" : "selected, " + WIN + " est., API-billed"; }
        });
        typeReason(REASONS[role.id]);
      }, (affinity ? 1900 : 2900) * D));
    }
    window.addEventListener("resize", function () {
      var sel = $$("button", tabs).findIndex(function (b) { return b.getAttribute("aria-selected") === "true"; });
      if (chart.classList.contains("thresh")) placeThreshold(ROLES[Math.max(0, sel)].need);
    });
    var started = false;
    onView($("#routerCard"), function (vis) { if (vis && !started) { started = true; select(0); } }, { threshold: 0.3 });
  })();

  /* ------------------------------------------------------------------ */
  /* Install terminal (real installer output)                             */
  /* ------------------------------------------------------------------ */
  (function terminal() {
    var body = $("#termBody"); if (!body) return;
    var CMDS = {
      unix: "curl -fsSL https://professorpalmer.github.io/marionette/install.sh | bash",
      win: "irm https://professorpalmer.github.io/marionette/install.ps1 | iex"
    };
    var OUT = {
      unix: [
        "== Installing Marionette for Darwin/arm64 ==",
        "== Cloning https://github.com/professorpalmer/marionette.git -> ~/.marionette/marionette ==",
        "== Provisioning Python via uv (reads .python-version) ==",
        "== Installing Marionette (editable) + Puppetmaster into .venv ==",
        "== Installing node deps + building the renderer ==",
        "== Installing the 'marionette' launcher into ~/.local/bin ==",
        "== Verifying the install ==",
        "",
        "Marionette is installed at: ~/.marionette/marionette"
      ],
      win: [
        "== Installing Marionette for Windows/x64 ==",
        "== Cloning https://github.com/professorpalmer/marionette.git -> %LOCALAPPDATA%\\marionette\\marionette ==",
        "== Provisioning Python via uv (reads .python-version) ==",
        "== Installing Marionette (editable) + Puppetmaster into .venv ==",
        "== Installing node deps + building the renderer ==",
        "== Installing launchers into %LOCALAPPDATA%\\marionette\\bin ==",
        "== Verifying the install =="
      ]
    };
    var which = os === "win" ? "win" : "unix", timers = [];
    var tabU = $("#tabUnix"), tabW = $("#tabWin"), copy = $("#copyBtn");
    function run() {
      timers.forEach(clearTimeout); timers = [];
      body.innerHTML = "";
      var cmd = el("div", "cmd"), cur = el("span", "cursor");
      body.appendChild(cmd); cmd.appendChild(document.createTextNode("")); cmd.appendChild(cur);
      var text = CMDS[which], k = 0;
      if (REDUCED) { cmd.firstChild.textContent = text; finish(); return; }
      (function type() {
        cmd.firstChild.textContent = text.slice(0, k++);
        if (k <= text.length) timers.push(setTimeout(type, 16 + Math.random() * 30)); else timers.push(setTimeout(finish, 450));
      })();
      function finish() {
        cur.remove();
        var lines = OUT[which], out = el("div", "out");
        body.appendChild(out);
        lines.forEach(function (ln, i) {
          timers.push(setTimeout(function () {
            var span = el("div", ln.indexOf("installed at") > -1 ? "ok" : ln.indexOf("==") === 0 ? "hl" : "", esc(ln) || "&nbsp;");
            out.appendChild(span);
            if (i === lines.length - 1) {
              var last = el("div", "cmd", which === "win" ? "marionette" : "marionette");
              last.appendChild(el("span", "cursor"));
              timers.push(setTimeout(function () { body.appendChild(last); }, 500));
            }
          }, REDUCED ? 0 : 380 * (i + 1)));
        });
      }
    }
    function select(w) {
      which = w;
      tabU.setAttribute("aria-selected", String(w === "unix"));
      tabW.setAttribute("aria-selected", String(w === "win"));
      run();
    }
    tabU.addEventListener("click", function () { select("unix"); });
    tabW.addEventListener("click", function () { select("win"); });
    copy.addEventListener("click", function () {
      var done = function () { copy.textContent = "Copied"; setTimeout(function () { copy.textContent = "Copy"; }, 1400); };
      if (navigator.clipboard) navigator.clipboard.writeText(CMDS[which]).then(done, function () {});
    });
    tabU.setAttribute("aria-selected", String(which === "unix"));
    tabW.setAttribute("aria-selected", String(which === "win"));
    var started = false;
    onView(body, function (vis) { if (vis && !started) { started = true; run(); } }, { threshold: 0.4 });
  })();

  /* ------------------------------------------------------------------ */
  /* Scroll loop                                                          */
  /* ------------------------------------------------------------------ */
  var ticking = false;
  window.addEventListener("scroll", function () {
    if (ticking) return; ticking = true;
    requestAnimationFrame(function () { ticking = false; onScrollNav(); tilt(); });
  }, { passive: true });
  window.addEventListener("resize", tilt);
})();
