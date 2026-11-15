/* 个性化初始化：必须跑在 CSS 之前（主题防闪白、纯图模式防闪正文）。
   pw_prefs = { theme, accent, bg, font, skin, author, pureimg, dblfav }；
   主题三态：light/dark/system；旧 pw_theme='dark' 迁移为深色，其余跟随系统。
   皮肤（skin）：swiss/brut/photo，空=默认苹果风；皮肤自带底色，theme-color 跟随。 */
(function () {
  var KEY = 'pw_prefs';
  var THEME_BG = { light: '#ffffff', dark: '#0d0d0f' };
  var SKIN_BG = {
    swiss: { light: '#f7f6f2', dark: '#0f0f11' },
    brut:  { light: '#f3ead6', dark: '#16130d' },
    photo: { light: '#fbfbfa', dark: '#0b0b0c' }
  };
  var root = document.documentElement;
  function prefs() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { return {}; }
  }
  function sysTheme() {
    return (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches)
      ? 'dark' : 'light';
  }
  function theme() {
    var p = prefs();
    if (p.theme === 'light' || p.theme === 'dark') return p.theme;
    if (p.theme === 'system') return sysTheme();
    try { if (localStorage.getItem('pw_theme') === 'dark') return 'dark'; } catch (e) {}
    return sysTheme();   // 从未配置过：跟随系统
  }
  function applyAll() {
    var p = prefs();
    var t = theme();
    root.setAttribute('data-theme', t);
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.setAttribute('content', (SKIN_BG[p.skin] || THEME_BG)[t]);
    ['accent', 'bg', 'font', 'skin'].forEach(function (k) {
      var v = p[k];
      if (k === 'accent' && v && ['indigo', 'green', 'plum'].indexOf(v) < 0) v = '';   // 琥珀已退役（专职收藏标记），旧档回落默认
      if (k === 'skin' && v && ['swiss', 'brut', 'photo'].indexOf(v) < 0) v = '';      // 未知皮肤档位回落默认
      if (v) root.setAttribute('data-' + k, v);
      else root.removeAttribute('data-' + k);
    });
    if (p.pureimg) root.setAttribute('data-pureimg', '1');
    else root.removeAttribute('data-pureimg');
    root.setAttribute('data-x-blur', p['x-blur'] === 'off' ? 'off' : 'on');
  }
  /* 供设置面板写入：'' 表示清除该配置（如 theme='' 回归跟随系统） */
  window.pwSetPref = function (key, val) {
    var p = prefs();
    if (val === '' || val === null || val === undefined) delete p[key];
    else p[key] = val;
    try { localStorage.setItem(KEY, JSON.stringify(p)); } catch (e) {}
    applyAll();
  };
  window.pwGetPrefs = prefs;
  applyAll();
  if (window.matchMedia) {
    var mq = matchMedia('(prefers-color-scheme: dark)');
    var follow = function () { if (!prefs().theme || prefs().theme === 'system') applyAll(); };
    mq.addEventListener ? mq.addEventListener('change', follow) : mq.addListener(follow);
  }
  window.addEventListener('storage', function (e) {
    if (e.key === KEY) applyAll();
  });
})();
