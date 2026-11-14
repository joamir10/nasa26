/* Planet X: client for the backend API. If the server is unreachable the site keeps working in local-only mode. */
(function () {
  'use strict';
  var KEY = 'spxc'; // cache محلي للمرشحين (الواجهة بتقرأ منه)
  var PX = (window.PX = { user: null, online: null });

  function req(method, path, body) {
    var o = { method: method, credentials: 'same-origin', headers: {} };
    if (body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
    return fetch(path, o).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || 'HTTP ' + r.status); e.status = r.status; e.api = !!j.error; throw e; }
        return j;
      });
    });
  }
  PX.get = function (p) { return req('GET', p); };
  PX.post = function (p, b) { return req('POST', p, b || {}); };
  PX.del = function (p) { return req('DELETE', p); };
  // الشبكة واقعة أو مفيش باك اند (استضافة static): نرجع للوضع المحلي
  PX.offline = function (e) { return !e.status || (e.status === 404 && !e.api) || e.status === 405; };

  function emit(name) { try { window.dispatchEvent(new Event(name || 'px:auth')); } catch (e) {} }
  function readCache() { try { return JSON.parse(localStorage.getItem(KEY) || '[]'); } catch (e) { return []; } }
  function writeCache(a) { try { localStorage.setItem(KEY, JSON.stringify(a)); } catch (e) {} }
  function toServer(c) {
    return { name: c.name || c.id || 'candidate', field: c.n || 'Field', source: c.id || '',
             ra: +c.ra, dec: +c.de, status: c.st || '', data: c };
  }

  PX.me = function () {
    return PX.get('/api/auth/me').then(function (j) { PX.online = true; PX.user = j.user; return j.user; })
      .catch(function (e) { PX.online = !PX.offline(e); PX.user = null; return null; })
      .then(function (u) { emit('px:auth'); return u; });
  };
  PX.ready = PX.me();

  // مزامنة المرشحين: نرفع اللي اتحفظ محليًا (من غير sid) ثم نسحب نسخة السيرفر في الـ cache
  PX.sync = function () {
    if (!PX.user) return Promise.resolve();
    var local = readCache().filter(function (c) { return !c.sid; }).slice(0, 100);
    var pre = local.length ? PX.post('/api/candidates/import', { items: local.map(toServer) }) : Promise.resolve();
    return pre.then(function () { return PX.get('/api/candidates'); }).then(function (j) {
      writeCache(j.candidates.map(function (c) {
        return Object.assign({}, c.data, { sid: c.id, n: c.field, id: c.source, name: c.name, st: c.status,
                                           ra: (+c.ra).toFixed(4), de: (+c.dec).toFixed(4) });
      }));
      emit('px:data');
    }).catch(function () {});
  };
  PX.push = function (c) {
    if (!PX.user) return;
    PX.post('/api/candidates', toServer(c)).then(PX.sync).catch(function () {});
  };
  PX.clearLocal = function () { try { localStorage.removeItem(KEY); } catch (e) {} };

  PX.analyze = function (payload) { return PX.post('/api/analyze', payload); };

  // ---- المفضلة (Save field): بتاخد الحالة من رابط المشاركة الحالي
  PX.addFav = function () {
    var h = location.hash;
    if (!/^#explore\?/.test(h)) return Promise.reject(new Error('Open a sky field first'));
    var p = new URLSearchParams(h.split('?')[1]), t = document.getElementById('mt');
    return PX.post('/api/favorites', { label: ((t && t.textContent) || 'Sky field').slice(0, 120),
      ra: +p.get('ra'), dec: +p.get('dec'), fov: +p.get('fov') || 240, hash: h });
  };
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('#fav');
    if (!b) return;
    var orig = '☆ Save field', say = function (m) { b.textContent = m; setTimeout(function () { b.textContent = orig; }, 2200); };
    if (PX.online === false) return say('Needs the server');
    if (!PX.user) return say('Sign in to save');
    PX.addFav().then(function () { say('★ Saved'); PX.loadData(); })
      .catch(function (x) { say(x.status === 409 ? 'Limit reached' : 'Could not save'); });
  });

  // ---- لوحة "حسابي" في صفحة Sign in (DOM بدل innerHTML عشان مفيش XSS)
  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === 'text') n.textContent = attrs[k]; else n.setAttribute(k, attrs[k]); });
    (kids || []).forEach(function (c) { n.appendChild(c); });
    return n;
  }
  function section(title, rows, empty) {
    var ul = el('ul', {}, rows);
    return el('div', { 'class': 'my-sec' }, [el('h4', { text: title }), rows.length ? ul : el('p', { 'class': 'my-empty', text: empty })]);
  }
  PX.loadData = function () {
    var box = document.getElementById('myData');
    if (!box || !PX.user) { if (box) box.textContent = ''; return Promise.resolve(); }
    return Promise.all([PX.get('/api/favorites'), PX.get('/api/candidates')]).then(function (r) {
      var favs = r[0].favorites.map(function (f) {
        var a = el('a', { text: f.label, href: /^#explore/.test(f.hash) ? f.hash : '#explore' });
        return el('li', {}, [a, el('small', { text: ' ' + (+f.ra).toFixed(3) + '°, ' + (+f.dec).toFixed(3) + '°' }),
          el('button', { type: 'button', 'data-fav': f.id, 'aria-label': 'Remove ' + f.label, text: '✕' })]);
      });
      var cands = r[1].candidates.map(function (c) {
        return el('li', {}, [el('b', { text: c.name }), el('small', { text: ' ' + c.field + ' · ' + (+c.ra).toFixed(4) + '°, ' + (+c.dec).toFixed(4) + '° · ' + c.status }),
          el('button', { type: 'button', 'data-cand': c.id, 'aria-label': 'Remove ' + c.name, text: '✕' })]);
      });
      box.textContent = '';
      box.appendChild(section('Saved fields', favs, 'None yet. Open a field in Explore and press “Save field”.'));
      box.appendChild(section('Your candidates', cands, 'None yet. Find the unmatched mover in Explore and name it.'));
    }).catch(function () {});
  };
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('#myData button');
    if (!b) return;
    var url = b.dataset.fav ? '/api/favorites/' + encodeURIComponent(b.dataset.fav) : '/api/candidates/' + encodeURIComponent(b.dataset.cand);
    b.disabled = true;
    PX.del(url).then(function () { return b.dataset.cand ? PX.sync() : null; }).then(PX.loadData).catch(function () { b.disabled = false; });
  });
  window.addEventListener('px:auth', function () { PX.loadData(); });
  window.addEventListener('px:data', function () { PX.loadData(); });
  window.addEventListener('hashchange', function () { if (location.hash === '#signin') PX.loadData(); });
})();
