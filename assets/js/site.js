// tnodes.org: small progressive enhancements. Everything works without this file.
(function () {
  "use strict";
  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // YouTube: the player (privacy-enhanced youtube-nocookie.com) loads only after a tap.
  // Several IDs play one after another.
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[data-yt]");
    if (!link || e.ctrlKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    var ids = link.getAttribute("data-yt").split(",");
    var src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(ids[0]) +
      "?autoplay=1&rel=0&playsinline=1";
    if (ids.length > 1) src += "&playlist=" + ids.slice(1).map(encodeURIComponent).join(",");
    var frame = document.createElement("iframe");
    frame.src = src;
    frame.title = link.getAttribute("aria-label").replace(/^Play video: /, "");
    frame.allow = "accelerometer; autoplay; encrypted-media; gyroscope; picture-in-picture; fullscreen";
    frame.allowFullscreen = true;
    var box = document.createElement("div");
    box.className = link.className;
    box.appendChild(frame);
    link.replaceWith(box);
    frame.focus();
  });

  // Gameplay loops: play only while visible; never autoplay with Reduce motion (the still image stays).
  var loops = document.querySelectorAll("video[data-loop]");
  if (!loops.length || reduceMotion || !("IntersectionObserver" in window)) return;
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      var v = entry.target;
      if (entry.isIntersecting) {
        var p = v.play();
        if (p && p.catch) p.catch(function () {});
      } else {
        v.pause();
      }
    });
  }, { threshold: 0.25 });
  loops.forEach(function (v) { io.observe(v); });
})();
