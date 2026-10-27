/* Perspective calligraphic currents, independent glints, and an inversion lens. */
(() => {
  "use strict";
  const canvas = document.querySelector("#typeField"), ctx = canvas.getContext("2d");
  const opening = document.querySelector("#opening"), shell = document.querySelector("#studioShell");
  const halo = document.querySelector("#cursorHalo");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const fine = matchMedia("(pointer: fine)");
  const glyphs = Array.from("循文见知山川日月天地玄黄宇宙洪荒风雨江海云水流墨书卷学问求索思辨证据来源观察论述知识光阴万象时空深远宁静致远格物致知思想奔涌百川入海源远流长上下古今春秋冬夏");
  const bundledFaces = ['"Trace Ink Running"', '"Trace Ink Kai"', '"Trace Ink Grass"', '"Trace Ink Display"'];
  const fonts = [], loadedFaces = new Set();
  const cell = 80, columns = 16, atlas = document.createElement("canvas");
  atlas.width = cell * columns;
  const ink = atlas.getContext("2d");
  function buildAtlas() {
    const available = bundledFaces.filter(font => loadedFaces.has(font));
    if (!available.length || available.length === fonts.length) return;
    fonts.splice(0, fonts.length, ...available);
    atlas.height = Math.ceil(glyphs.length * fonts.length / columns) * cell;
    ink.clearRect(0, 0, atlas.width, atlas.height);
    ink.fillStyle = "#eeeeee"; ink.textAlign = "center"; ink.textBaseline = "middle";
    fonts.forEach((font, f) => {
      ink.font = `62px ${font}`;
      glyphs.forEach((letter, i) => {
        const n = f * glyphs.length + i;
        ink.fillText(letter, (n % columns + .5) * cell, (Math.floor(n / columns) + .53) * cell);
      });
    });
    canvas.dataset.typefaces = String(fonts.length);
    document.body.classList.add("type-ready");
    resize(); restart();
  }
  let width = 1, height = 1, ratio = 1, frame = 0, last = 0, time = 0, streams = [];
  let entered = sessionStorage.getItem("trace-entered") === "yes";
  let paused = localStorage.getItem("trace-motion-paused") === "yes" || reduce.matches;
  let intensity = entered ? .28 : 1;
  let departureTimer = 0;
  let workspaceReady = false, assetsReady = false, presented = false, entryPending = false;
  const boot = document.querySelector("#bootScreen");
  const slowBoot = setTimeout(() => {
    if (presented) return;
    document.querySelector("#bootStatus").textContent = "连接较慢，正在恢复研究空间";
    document.querySelector("#bootRetry").hidden = false;
  }, 8000);
  document.querySelector("#bootRetry").onclick = () => location.reload();
  function finishBoot() {
    if (presented || !workspaceReady || !assetsReady) return;
    presented = true; clearTimeout(slowBoot);
    document.body.classList.remove("studio-loading");
    shell.setAttribute("aria-busy", "false");
    if (entered) shell.inert = false;
    boot.inert = true; boot.classList.add("is-complete");
    setTimeout(() => { boot.hidden = true; }, reduce.matches ? 0 : 450);
    if (entryPending) { entryPending = false; openStudio(); }
  }
  window.TracePresentation = {ready() { workspaceReady = true; finishBoot(); }};
  const pointer = {x: -1000, y: -1000, smoothX: -1000, smoothY: -1000, active: false};
  const camera = {x: 0, y: 0};
  const trail = [];
  const perspective = 760;
  // Each sheet has its own distance, flow rate and wave amplitude.
  // Rear characters stay numerous and small; nearby strokes move past the viewer.
  const layers = [
    {z: 1050, relief: 170, gapX: 29, gapY: 34, size: 18, speed: 10, sway: .012, alpha: .13},
    {z: 380, relief: 200, gapX: 42, gapY: 48, size: 31, speed: 25, sway: .026, alpha: .18},
    {z: 20, relief: 115, gapX: 108, gapY: 108, size: 56, speed: 42, sway: .038, alpha: .2}
  ];
  const hash = n => {
    const value = Math.sin(n * 127.1 + 311.7) * 43758.5453;
    return value - Math.floor(value);
  };
  function project(u, v, layer) {
    const phase = v * 5.2 - time * .35 + u * 1.8;
    const wave = Math.sin(phase) + .2 * Math.sin(v * 10.5 + time * .13);
    const span = 1 + layer.z / perspective;
    const x = (u - .5 + wave * layer.sway) * width * span;
    const y = (v - .5) * height * span;
    const fold = Math.sin(u * 6 + v * 3.4 - time * .16) * layer.relief;
    const yaw = Math.sin(time * .08) * .015 + camera.x * .025;
    const pitch = Math.cos(time * .06) * .009 + camera.y * .014;
    const rotatedX = x * Math.cos(yaw) + fold * Math.sin(yaw);
    const rotatedZ = fold * Math.cos(yaw) - x * Math.sin(yaw);
    const rotatedY = y * Math.cos(pitch) - rotatedZ * Math.sin(pitch);
    const depth = layer.z + rotatedZ * Math.cos(pitch) + y * Math.sin(pitch);
    const scale = perspective / Math.max(420, perspective + depth);
    const near = 1 - layer.z / 1400;
    return {
      x: width * .5 + rotatedX * scale + camera.x * 26 * near,
      y: height * .5 + rotatedY * scale + camera.y * 14 * near,
      scale: scale * span,
      tilt: Math.cos(phase) * layer.sway * 1.5
    };
  }
  function drawRibbons() {
    // Travelling waves and independent twists keep the silk visibly in motion.
    const steps = 92;
    for (let ribbon = 0; ribbon < 2; ribbon++) {
      const side = ribbon === 0 ? 1 : -1;
      const phase = ribbon * 1.9 - time * (ribbon === 0 ? .95 : .81);
      const flutter = ribbon * 2.4 - time * 1.35;
      const points = [];
      for (let i = 0; i <= steps; i++) {
        const t = i / steps * 1.3 - .15;
        const bend = Math.sin(t * 5.6 + phase) * .065 + Math.sin(t * 9 + flutter) * .025;
        const dx = side * width * (Math.cos(t * 5.6 + phase) * .364 + Math.cos(t * 9 + flutter) * .225);
        const length = Math.hypot(dx, height);
        const twist = Math.sin(t * 7.4 + ribbon * 1.5 - time * 1.4);
        points.push({
          x: width * ((ribbon === 0 ? .16 : .84) + side * bend) + camera.x * 8,
          y: t * height,
          nx: height / length, ny: -dx / length,
          half: Math.min(52, width * .047) * (.13 + .87 * Math.abs(twist))
        });
      }
      const edge = (p, f) => [p.x + p.nx * p.half * f, p.y + p.ny * p.half * f];
      const gradient = ctx.createLinearGradient(0, 0, width * .2, height);
      gradient.addColorStop(0, "rgba(220,228,239,0)");
      gradient.addColorStop(.28, "rgba(220,228,239,.085)");
      gradient.addColorStop(.65, "rgba(220,228,239,.035)");
      gradient.addColorStop(.82, "rgba(220,228,239,.09)");
      gradient.addColorStop(1, "rgba(220,228,239,0)");
      ctx.globalAlpha = intensity;
      ctx.fillStyle = gradient;
      ctx.beginPath();
      points.forEach((p, i) => { const [x, y] = edge(p, -1); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
      for (let i = points.length - 1; i >= 0; i--) ctx.lineTo(...edge(points[i], 1));
      ctx.closePath(); ctx.fill();
      // Fine longitudinal fibres give each band a satin-like surface.
      for (const strand of [-1, -.65, -.3, 0, .3, .65, 1]) {
        ctx.strokeStyle = `rgba(226,232,242,${Math.abs(strand) === 1 ? .105 : .024})`;
        ctx.lineWidth = Math.abs(strand) === 1 ? .8 : .5;
        ctx.beginPath();
        points.forEach((p, i) => { const [x, y] = edge(p, strand); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
  }
  function resize() {
    width = innerWidth; height = innerHeight;
    ratio = Math.min(devicePixelRatio || 1, 1.6);
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    streams = fonts.length ? layers.flatMap((layer, index) => {
      const count = Math.ceil(width / layer.gapX) + 10;
      const rows = Math.ceil(height * 1.6 / layer.gapY);
      return Array.from({length: count}, (_, i) => {
        const seed = i * 13.7 + index * 291;
        return {
          layer, index, rows, u: (i - 4.5) / (count - 10),
          offset: hash(seed + 5), speed: layer.speed * (.8 + hash(seed) * .4),
          characters: Array.from({length: rows}, (_, row) => {
            const key = seed + row * 71.3;
            const glyph = i % fonts.length * glyphs.length + Math.floor(hash(key + 8) * glyphs.length);
            return {
              sx: glyph % columns * cell, sy: Math.floor(glyph / columns) * cell,
              size: .8 + hash(key + 9) * .4, phase: hash(key) * Math.PI * 2,
              frequency: 2.1 + hash(key + 2) * 1.8, glint: hash(key + 3) > .6,
              strength: .5 + hash(key + 4) * .5
            };
          })
        };
      });
    }) : [];
    if (paused) draw(performance.now(), true);
  }
  function draw(now, single = false) {
    const dt = Math.min((now - (last || now)) / 1000, .04); last = now;
    if (!paused) time += dt;
    intensity += ((entered ? .28 : 1) - intensity) * Math.min(1, dt * 3);
    ctx.clearRect(0, 0, width, height);
    const lens = pointer.active && fine.matches && !reduce.matches;
    if (lens) {
      const follow = single ? 1 : 1 - Math.exp(-dt * 18);
      pointer.smoothX += (pointer.x - pointer.smoothX) * follow;
      pointer.smoothY += (pointer.y - pointer.smoothY) * follow;
      halo.style.transform = `translate3d(${pointer.smoothX}px,${pointer.smoothY}px,0)`;
    }
    const cameraFollow = single ? 1 : 1 - Math.exp(-dt * 2.3);
    const targetX = lens ? Math.max(-1, Math.min(1, pointer.smoothX / width * 2 - 1)) : 0;
    const targetY = lens ? Math.max(-1, Math.min(1, pointer.smoothY / height * 2 - 1)) : 0;
    camera.x += (targetX - camera.x) * cameraFollow;
    camera.y += (targetY - camera.y) * cameraFollow;
    drawRibbons();
    const radius = entered ? 52 : 128;
    for (const stream of streams) {
      const {layer, rows} = stream;
      const travel = time * stream.speed / (height * 1.6) + stream.offset;
      for (let row = 0; row < rows; row++) {
        const letter = stream.characters[row];
        const v = ((row / rows + travel) % 1) * 1.6 - .3;
        const point = project(stream.u, v, layer);
        const size = layer.size * letter.size * point.scale;
        const {x, y} = point;
        if (x < -size || x > width + size || y < -size || y > height + size) continue;
        const edge = Math.min(1, Math.max(0, (y + size) / 100), Math.max(0, (height + size - y) / 100));
        const breath = .74 + .26 * Math.sin(time * letter.frequency + letter.phase);
        // Individual, smooth light pulses, never a full-screen flashing frame.
        const glint = letter.glint ? Math.pow(Math.max(0, Math.sin(time * (2.4 + letter.frequency * .35) + letter.phase)), 10) : 0;
        const waveLight = .72 + .28 * Math.sin(v * 6.5 - time * .24 + stream.u * 3);
        const centre = Math.exp(-((x / width - .5) ** 2 * 17 + (y / height - .46) ** 2 * 12));
        const titleSpace = 1 - centre * (stream.index === 2 ? .8 : .38);
        let alpha = (layer.alpha * breath * waveLight + glint * .31 * letter.strength) * edge * titleSpace * intensity;
        if (lens) {
          const distance = Math.hypot(x - pointer.smoothX, y - pointer.smoothY);
          alpha += Math.max(0, 1 - distance / (radius * 1.4)) * .15 * intensity;
        }
        const tilt = point.tilt + Math.sin(time * .12 + letter.phase) * .015;
        const cos = Math.cos(tilt), sin = Math.sin(tilt);
        ctx.setTransform(ratio * cos, ratio * sin, -ratio * sin, ratio * cos, x * ratio, y * ratio);
        // A stretched, faint brush echo follows the nearby moving strokes.
        if (stream.index === 2) {
          ctx.globalAlpha = alpha * .09;
          ctx.drawImage(atlas, letter.sx, letter.sy, cell, cell, -size * .5, -size * .8, size, size * 1.35);
        }
        ctx.globalAlpha = Math.min(.75, alpha);
        ctx.drawImage(atlas, letter.sx, letter.sy, cell, cell, -size * .5, -size * .5, size, size);
      }
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    if (lens && !paused) {
      trail.unshift({x: pointer.smoothX, y: pointer.smoothY});
      if (trail.length > 16) trail.pop();
      for (let i = trail.length - 1; i > 2; i -= 3) {
        const point = trail[i];
        if (Math.hypot(point.x - pointer.smoothX, point.y - pointer.smoothY) < 3) continue;
        ctx.globalAlpha = (1 - i / trail.length) * (entered ? .035 : .09);
        ctx.strokeStyle = "#eeeeee"; ctx.lineWidth = .8;
        ctx.beginPath(); ctx.arc(point.x, point.y, radius - i * .35, 0, Math.PI * 2); ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    if (!single && !paused && !document.hidden) frame = requestAnimationFrame(draw);
  }
  function restart() {
    cancelAnimationFrame(frame); last = 0;
    if (!document.hidden) frame = requestAnimationFrame(now => draw(now, paused));
  }
  function motionUI() {
    document.body.classList.toggle("motion-paused", paused);
    document.querySelector("#motionToggle").textContent = paused ? "播放动效" : "暂停动效";
    document.querySelector("#ambientToggle").textContent = paused ? "播放背景" : "暂停背景";
    document.querySelectorAll("#motionToggle,#ambientToggle").forEach(b => b.setAttribute("aria-pressed", String(paused)));
  }
  function toggleMotion() {
    paused = !paused; localStorage.setItem("trace-motion-paused", paused ? "yes" : "no");
    motionUI(); restart();
  }
  function openStudio() {
    if (entered) return;
    if (!presented) { entryPending = true; return; }
    clearTimeout(departureTimer);
    entered = true; sessionStorage.setItem("trace-entered", "yes");
    wheelDistance = 0; touchStart = null;
    if (paused) intensity = .28;
    document.body.classList.remove("landing-open"); opening.classList.add("departing");
    window.scrollTo({top: 0, behavior: "instant"});
    opening.inert = true; shell.inert = false;
    departureTimer = setTimeout(() => {
      opening.hidden = true; document.querySelector("#createThread").focus({preventScroll: true});
    }, reduce.matches ? 0 : 700);
    restart();
  }
  function replay() {
    clearTimeout(departureTimer);
    entered = false; opening.hidden = false; opening.inert = false; shell.inert = true;
    wheelDistance = 0; touchStart = null;
    if (paused) intensity = 1;
    opening.classList.remove("departing"); document.body.classList.add("landing-open");
    document.querySelector("#enterStudio").focus({preventScroll: true}); restart();
  }
  document.querySelector("#enterStudio").onclick = openStudio;
  document.querySelector("#replayOpening").onclick = replay;
  document.querySelector(".brand").onclick = e => { e.preventDefault(); replay(); };
  document.querySelector("#motionToggle").onclick = toggleMotion;
  document.querySelector("#ambientToggle").onclick = toggleMotion;
  let wheelDistance = 0, lastWheelAt = 0, touchStart = null;
  document.addEventListener("wheel", e => {
    if (opening.hidden || e.ctrlKey) return;
    // Hold the page still until the opening has finished leaving.
    e.preventDefault();
    if (entered) return;
    const now = performance.now();
    if (now - lastWheelAt > 250) wheelDistance = 0;
    lastWheelAt = now;
    if (e.deltaY <= 0 || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      wheelDistance = 0;
      return;
    }
    const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? innerHeight : 1;
    wheelDistance += e.deltaY * unit;
    if (wheelDistance >= 60) openStudio();
  }, {passive: false});
  opening.addEventListener("touchstart", e => {
    touchStart = !entered && e.touches.length === 1
      ? {x: e.touches[0].clientX, y: e.touches[0].clientY} : null;
  }, {passive: true});
  document.addEventListener("touchmove", e => {
    if (opening.hidden) return;
    if (e.touches.length !== 1) { touchStart = null; return; }
    if (entered) { e.preventDefault(); return; }
    if (!touchStart) return;
    const dx = e.touches[0].clientX - touchStart.x;
    const dy = touchStart.y - e.touches[0].clientY;
    if (Math.abs(dy) > Math.abs(dx)) {
      e.preventDefault();
      if (dy >= 50) openStudio();
    }
  }, {passive: false});
  document.addEventListener("touchend", () => { touchStart = null; }, {passive: true});
  document.addEventListener("touchcancel", () => { touchStart = null; }, {passive: true});
  document.addEventListener("keydown", e => {
    if (entered || opening.hidden || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.target.closest("input,textarea,select,[contenteditable]")) return;
    const downKey = e.key === "ArrowDown" || e.key === "PageDown";
    const pageSpace = e.key === " " && !e.target.closest("button,a,summary");
    if (downKey || pageSpace) { e.preventDefault(); openStudio(); }
  });
  document.addEventListener("pointermove", e => {
    if (!fine.matches || e.pointerType === "touch" || reduce.matches) return;
    pointer.x = e.clientX; pointer.y = e.clientY;
    if (!pointer.active) { pointer.smoothX = pointer.x; pointer.smoothY = pointer.y; }
    pointer.active = true; halo.classList.add("visible");
    halo.classList.toggle("over-control", !!e.target.closest("button,a,input,textarea,summary"));
    if (paused) draw(performance.now(), true);
  }, {passive: true});
  document.addEventListener("pointerout", e => {
    if (!e.relatedTarget) { pointer.active = false; trail.length = 0; halo.classList.remove("visible"); if (paused) draw(performance.now(), true); }
  });
  document.addEventListener("visibilitychange", restart);
  reduce.addEventListener("change", () => { paused = reduce.matches; motionUI(); restart(); });
  addEventListener("resize", resize);
  if (entered) { opening.hidden = true; opening.inert = true; document.body.classList.remove("landing-open"); }
  resize(); motionUI(); restart();
  // Canvas captures pixels, so a later CSS font swap cannot repair a fallback atlas.
  // Paint only faces whose bundled files have actually loaded; late arrivals rebuild it.
  const faceLoads = bundledFaces.map(font => document.fonts.load(`62px ${font}`, glyphs.join(""))
    .then(faces => { if (faces.length) loadedFaces.add(font); }));
  const typography = Promise.allSettled([...faceLoads, document.fonts.load('700 48px "Trace Title Sans"', "让每一句话都有来处")])
    .then(buildAtlas);
  const visualDeadline = setTimeout(() => { buildAtlas(); assetsReady = true; finishBoot(); }, 2500);
  typography.then(() => { clearTimeout(visualDeadline); assetsReady = true; finishBoot(); });
})();
