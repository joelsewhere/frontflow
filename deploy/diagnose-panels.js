// Paste into the DevTools console on the workspace page.
// Reports each dock group, its tab strip, and any content overlay
// painting over it.
(() => {
  const rect = (el) => { const r = el.getBoundingClientRect();
    return { top: Math.round(r.top), height: Math.round(r.height), width: Math.round(r.width) }; };

  const groups = [...document.querySelectorAll(".dv-groupview")];
  console.log(`%c${groups.length} groups`, "font-weight:bold");
  groups.forEach((g, i) => {
    const strip = g.querySelector(".dv-tabs-and-actions-container");
    const tabs = [...g.querySelectorAll(".dv-tab")].map((t) => t.textContent.trim() || "(blank)");
    const s = strip && getComputedStyle(strip);
    console.log(`group ${i}`, {
      group: rect(g),
      tabStrip: strip ? rect(strip) : "MISSING",
      tabs,
      stripDisplay: s && s.display,
      stripZIndex: s && s.zIndex,
    });
  });

  const overlays = [...document.querySelectorAll(".dv-render-overlay")];
  console.log(`%c${overlays.length} content overlays`, "font-weight:bold");
  overlays.forEach((o, i) => {
    const r = rect(o);
    const covered = groups
      .map((g, gi) => ({ gi, strip: g.querySelector(".dv-tabs-and-actions-container") }))
      .filter(({ strip }) => strip && (() => {
        const sr = strip.getBoundingClientRect();
        return r.top < sr.bottom && r.top + r.height > sr.top && r.width > 0 && r.height > 0;
      })())
      .map(({ gi }) => gi);
    console.log(`overlay ${i}`, { ...r, inlineHeight: o.style.height || "(none - CSS 100% applies)",
      display: getComputedStyle(o).display, coversTabStripOfGroups: covered });
  });
})();
