// Progressive enhancement: mobile nav, scroll reveals, FAQ accordion, sticky-nav shadow.
// Flag the document ASAP so reveal CSS only hides content when JS is actually running
// (no-JS visitors and reduced-motion users always see everything).
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!reduce) document.documentElement.classList.add('reveal-ready');
})();

document.addEventListener('DOMContentLoaded', function () {
  // --- mobile nav toggle ---
  var toggle = document.querySelector('.nav-toggle');
  var links = document.querySelector('.nav-links');
  if (toggle && links) {
    toggle.addEventListener('click', function () {
      var open = links.classList.toggle('open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    links.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') { links.classList.remove('open'); toggle.setAttribute('aria-expanded', 'false'); }
    });
  }

  // --- sticky-nav shadow on scroll ---
  var nav = document.querySelector('.site-nav');
  if (nav) {
    var onScroll = function () { nav.classList.toggle('scrolled', window.scrollY > 8); };
    onScroll(); window.addEventListener('scroll', onScroll, { passive: true });
  }

  // --- scroll-reveal (the "Framer Motion feel", vanilla) ---
  var targets = document.querySelectorAll('[data-reveal], [data-reveal-group]');
  if (!('IntersectionObserver' in window) || document.documentElement.classList.contains('reveal-ready') === false) {
    // no observer support (or reduced motion): just show everything
    targets.forEach(function (el) { el.classList.add('in-view'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add('in-view'); io.unobserve(entry.target); }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -8% 0px' });
    targets.forEach(function (el) { io.observe(el); });
  }

  // --- FAQ accordion ---
  document.querySelectorAll('.faq-q').forEach(function (q) {
    q.addEventListener('click', function () {
      var open = q.getAttribute('aria-expanded') === 'true';
      q.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
  });
});

// Cinematic scroll scrubber — drives each pinned scene's zoom + headline enter/exit.
(function () {
  var cinema = document.querySelector('[data-cinema]');
  if (!cinema) return;
  var scenes = Array.prototype.slice.call(cinema.querySelectorAll('[data-scene]'));
  if (!scenes.length) return;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) { scenes.forEach(function (s) { s.style.setProperty('--p', 0); s.style.setProperty('--enter', 1); }); return; }
  var clamp = function (v) { return v < 0 ? 0 : v > 1 ? 1 : v; };
  var ticking = false;
  function update() {
    ticking = false;
    var vh = window.innerHeight;
    for (var i = 0; i < scenes.length; i++) {
      var track = scenes[i];
      var r = track.getBoundingClientRect();
      var span = Math.max(1, track.offsetHeight - vh);
      var p = clamp(-r.top / span);                 // 0 -> 1 across the scene's scroll track
      track.style.setProperty('--p', p.toFixed(4));
      var enter = 1;                                 // headline eases in, holds, eases out
      if (p < 0.16) enter = p / 0.16;
      else if (p > 0.80) enter = clamp((1 - p) / 0.20);
      track.style.setProperty('--enter', enter.toFixed(4));
    }
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(update); } }
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  update();
})();
