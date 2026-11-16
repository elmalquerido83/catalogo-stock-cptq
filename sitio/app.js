/* Catálogo de contenidos: búsqueda en el navegador sobre datos/catalogo.json */
(function () {
  'use strict';

  const POR_PAGINA = 60;
  const VACIAS = new Set(['de', 'la', 'el', 'en', 'y', 'con', 'los', 'las', 'del', 'un', 'una', 'al', 'a', 'por', 'para', 'e', 'o']);
  const $ = (id) => document.getElementById(id);

  const estado = { q: '', tipo: '', destino: '', formato: '', toma: '', momento: '', orden: 'rel' };
  let items = [];
  let porId = new Map();
  let buscador = null;
  let resultados = [];
  let mostrados = 0;

  const sinAcentos = (s) => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  // "Tulum_Cenote_PuertaAlCielo62.MP4" -> "Tulum Cenote Puerta Al Cielo 62"
  function palabrasDeNombre(nombre) {
    return nombre
      .replace(/\.[^.]+$/, '')
      .replace(/([a-záéíóúñ])([A-ZÁÉÍÓÚÑ])/g, '$1 $2')
      .replace(/([A-Za-zÁÉÍÓÚÑáéíóúñ])(\d)/g, '$1 $2')
      .replace(/[_\-.()]+/g, ' ');
  }

  function procesaTermino(t) {
    const n = sinAcentos(t);
    return VACIAS.has(n) ? null : n;
  }

  function construyeIndice() {
    buscador = new MiniSearch({
      idField: 'id',
      fields: ['nombreTxt', 'lugar', 'destino', 'carpetaTxt', 'descripcion', 'etiquetasTxt', 'extra'],
      processTerm: procesaTermino,
      searchOptions: {
        boost: { etiquetasTxt: 3, lugar: 3, nombreTxt: 2.5, destino: 2, descripcion: 1.5, carpetaTxt: 1 },
        prefix: (t) => t.length >= 3,
        fuzzy: (t) => (t.length >= 5 ? 0.2 : false),
        combineWith: 'AND',
        processTerm: procesaTermino,
      },
    });
    buscador.addAll(items.map((x) => ({
      id: x.id,
      nombreTxt: palabrasDeNombre(x.nombre),
      lugar: x.lugar || '',
      destino: x.destino || '',
      carpetaTxt: palabrasDeNombre(x.carpeta || '').replace(/\//g, ' '),
      descripcion: x.descripcion || '',
      etiquetasTxt: (x.etiquetas || []).join(' '),
      extra: [x.tipo === 'video' ? 'video clip' : 'foto fotografia imagen', x.toma, x.momento,
        x.personas ? 'personas gente' : '', esVertical(x) ? 'vertical' : 'horizontal',
        sinonimosDe([x.nombre, palabrasDeNombre(x.nombre), x.carpeta, x.lugar, x.descripcion, (x.etiquetas || []).join(' ')].join(' '))].join(' '),
    })));
  }

  const esVertical = (x) => x.alto > x.ancho;

  // Grupos de sinónimos: si una pieza menciona cualquiera de las palabras de un grupo,
  // también se encuentra buscando las demás (p. ej. Chacchoben → "sitio arqueológico").
  const SINONIMOS = [
    ['sitio arqueologico', 'zona arqueologica', 'arqueologia', 'arqueologico', 'arqueologica', 'ruinas', 'ruina', 'piramide', 'templo maya', 'maya', 'mayas', 'vestigios'],
    ['hotel', 'resort', 'hospedaje', 'alojamiento'],
    ['alberca', 'piscina'],
    ['restaurante', 'gastronomia', 'comida', 'platillo', 'cocina'],
    ['dron', 'drone', 'aerea', 'aereo', 'vista aerea'],
    ['atardecer', 'puesta de sol', 'ocaso'],
    ['fauna', 'animal', 'animales', 'aves', 'flamencos', 'tortuga', 'pez', 'peces', 'tiburon ballena'],
    ['submarino', 'submarina', 'buceo', 'snorkel'],
  ].map((g) => g.map(sinAcentos));

  function sinonimosDe(texto) {
    const t = ' ' + sinAcentos(texto).replace(/[^a-z0-9ñ]+/g, ' ') + ' ';
    const extra = [];
    SINONIMOS.forEach((g) => { if (g.some((w) => t.includes(' ' + w + ' '))) extra.push(...g); });
    return extra.join(' ');
  }

  function pasaFiltros(x) {
    if (estado.tipo && x.tipo !== estado.tipo) return false;
    if (estado.destino && x.destino !== estado.destino) return false;
    if (estado.formato === 'v' && !esVertical(x)) return false;
    if (estado.formato === 'h' && esVertical(x)) return false;
    if (estado.toma && x.toma !== estado.toma) return false;
    if (estado.momento && x.momento !== estado.momento) return false;
    return true;
  }

  function actualiza() {
    let lista;
    const q = estado.q.trim();
    if (q) {
      let r = buscador.search(q);
      if (!r.length) r = buscador.search(q, { combineWith: 'OR' }); // si no hay coincidencia exacta, ampliar
      lista = r.map((h) => porId.get(h.id));
    } else {
      lista = items.slice();
    }
    lista = lista.filter(pasaFiltros);
    if (estado.orden === 'reciente' || (!q && estado.orden === 'rel')) {
      lista.sort((a, b) => (b.fecha || '').localeCompare(a.fecha || '') || a.ruta.localeCompare(b.ruta));
    } else if (estado.orden === 'nombre') {
      lista.sort((a, b) => a.nombre.localeCompare(b.nombre, 'es', { numeric: true }));
    }
    resultados = lista;
    mostrados = 0;
    $('rejilla').textContent = '';
    const n = lista.length;
    $('estado').textContent = n
      ? `${n.toLocaleString('es')} ${n === 1 ? 'resultado' : 'resultados'}${q ? ` para «${q}»` : ''}`
      : (q ? `No encontré nada para «${q}». Prueba con otras palabras o quita filtros.` : 'No hay piezas con estos filtros.');
    pintaMas();
    guardaEnUrl();
  }

  function duracion(s) {
    if (!s && s !== 0) return '';
    const m = Math.floor(s / 60);
    const ss = String(Math.round(s % 60)).padStart(2, '0');
    return `${m}:${ss}`;
  }

  function tarjeta(x) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tarjeta';
    b.title = x.descripcion || x.nombre;
    const mini = document.createElement('div');
    mini.className = 'miniatura' + (esVertical(x) ? ' vertical' : '');
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = x.descripcion || x.nombre;
    img.src = x.mini;
    mini.appendChild(img);
    const ins = document.createElement('span');
    ins.className = 'insignia';
    ins.textContent = x.tipo === 'video' ? `▶ ${duracion(x.duracion)}` : 'FOTO';
    mini.appendChild(ins);
    if (esVertical(x)) {
      const v = document.createElement('span');
      v.className = 'insignia izq';
      v.textContent = 'VERTICAL';
      mini.appendChild(v);
    }
    const info = document.createElement('div');
    info.className = 't-info';
    const nom = document.createElement('span');
    nom.className = 't-nombre';
    nom.textContent = x.nombre;
    const lug = document.createElement('span');
    lug.className = 't-lugar';
    lug.textContent = x.lugar || x.destino || '';
    info.append(nom, lug);
    b.append(mini, info);
    b.addEventListener('click', () => abreDetalle(x));
    return b;
  }

  function pintaMas() {
    const frag = document.createDocumentFragment();
    const fin = Math.min(mostrados + POR_PAGINA, resultados.length);
    for (let i = mostrados; i < fin; i++) frag.appendChild(tarjeta(resultados[i]));
    mostrados = fin;
    $('rejilla').appendChild(frag);
  }

  const mb = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`);

  function enlaceDescarga(url) {
    if (!url) return '';
    const u = new URL(url);
    u.searchParams.set('dl', '1');
    return u.toString();
  }

  function abreDetalle(x) {
    $('d-img').src = x.mini;
    $('d-img').alt = x.descripcion || x.nombre;
    $('d-nombre').textContent = x.nombre;
    $('d-lugar').textContent = x.lugar || x.destino || '';
    $('d-desc').textContent = x.descripcion || '';
    const tags = $('d-tags');
    tags.textContent = '';
    (x.etiquetas || []).forEach((t) => {
      const li = document.createElement('li');
      const bt = document.createElement('button');
      bt.type = 'button';
      bt.textContent = t;
      bt.title = `Buscar «${t}»`;
      bt.addEventListener('click', () => { $('detalle').close(); $('q').value = t; estado.q = t; actualiza(); });
      li.appendChild(bt);
      tags.appendChild(li);
    });
    const datos = [
      ['Tipo', x.tipo === 'video' ? `Vídeo · ${duracion(x.duracion)}` : 'Foto'],
      ['Formato', esVertical(x) ? 'Vertical' : 'Horizontal'],
      ['Toma', x.toma && x.toma !== 'no se sabe' ? x.toma : ''],
      ['Momento', x.momento && x.momento !== 'no se sabe' ? x.momento : ''],
      ['Tamaño', x.tamano ? mb(x.tamano) : ''],
      ['Fecha', x.fecha || ''],
    ].filter((d) => d[1]);
    const dl = $('d-datos');
    dl.textContent = '';
    datos.forEach(([k, v]) => {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      dl.append(dt, dd);
    });
    $('d-ruta').textContent = x.ruta;
    const ver = $('d-ver'), bajar = $('d-bajar'), copiar = $('d-copiar');
    if (x.enlace) {
      ver.href = x.enlace; bajar.href = enlaceDescarga(x.enlace);
      ver.removeAttribute('aria-disabled'); bajar.removeAttribute('aria-disabled'); copiar.disabled = false;
    } else {
      ver.removeAttribute('href'); bajar.removeAttribute('href');
      ver.setAttribute('aria-disabled', 'true'); bajar.setAttribute('aria-disabled', 'true'); copiar.disabled = true;
    }
    copiar.textContent = 'Copiar enlace';
    copiar.onclick = async () => {
      try { await navigator.clipboard.writeText(x.enlace); copiar.textContent = '¡Copiado!'; } catch (e) { copiar.textContent = 'No se pudo copiar'; }
    };
    $('detalle').showModal();
  }

  // ---- estado en la URL (para compartir búsquedas)
  function guardaEnUrl() {
    const p = new URLSearchParams();
    Object.entries(estado).forEach(([k, v]) => { if (v && !(k === 'orden' && v === 'rel')) p.set(k, v); });
    const s = p.toString();
    history.replaceState(null, '', s ? `?${s}` : location.pathname);
  }
  function leeUrl() {
    const p = new URLSearchParams(location.search);
    Object.keys(estado).forEach((k) => { if (p.has(k)) estado[k] = p.get(k); });
  }
  function reflejaControles() {
    $('q').value = estado.q;
    document.querySelectorAll('.segmentado button').forEach((b) => b.classList.toggle('activo', b.dataset.tipo === estado.tipo));
    $('f-destino').value = estado.destino;
    $('f-formato').value = estado.formato;
    $('f-toma').value = estado.toma;
    $('f-momento').value = estado.momento;
    $('f-orden').value = estado.orden;
  }

  function llenaDestinos() {
    const cuenta = new Map();
    items.forEach((x) => { if (x.destino) cuenta.set(x.destino, (cuenta.get(x.destino) || 0) + 1); });
    const sel = $('f-destino');
    [...cuenta.entries()].sort((a, b) => a[0].localeCompare(b[0], 'es')).forEach(([d, n]) => {
      const o = document.createElement('option');
      o.value = d; o.textContent = `${d} (${n})`;
      sel.appendChild(o);
    });
  }

  function conecta() {
    let t;
    $('q').addEventListener('input', (e) => {
      clearTimeout(t);
      t = setTimeout(() => { estado.q = e.target.value; actualiza(); }, 180);
    });
    document.querySelectorAll('.segmentado button').forEach((b) => b.addEventListener('click', () => {
      estado.tipo = b.dataset.tipo; reflejaControles(); actualiza();
    }));
    [['f-destino', 'destino'], ['f-formato', 'formato'], ['f-toma', 'toma'], ['f-momento', 'momento'], ['f-orden', 'orden']]
      .forEach(([id, k]) => $(id).addEventListener('change', (e) => { estado[k] = e.target.value; actualiza(); }));
    $('limpiar').addEventListener('click', () => {
      Object.assign(estado, { q: '', tipo: '', destino: '', formato: '', toma: '', momento: '', orden: 'rel' });
      reflejaControles(); actualiza(); $('q').focus();
    });
    $('cerrar').addEventListener('click', () => $('detalle').close());
    $('detalle').addEventListener('click', (e) => { if (e.target === $('detalle')) $('detalle').close(); });
    new IntersectionObserver((ents) => {
      if (ents.some((e) => e.isIntersecting) && mostrados < resultados.length) pintaMas();
    }, { rootMargin: '800px' }).observe($('centinela'));
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && document.activeElement !== $('q') && !$('detalle').open) { e.preventDefault(); $('q').focus(); }
    });
  }

  async function inicia() {
    conecta();
    try {
      const r = await fetch('datos/catalogo.json', { cache: 'no-cache' });
      if (!r.ok) throw new Error(r.status);
      const datos = await r.json();
      items = datos.items || [];
      porId = new Map(items.map((x) => [x.id, x]));
      const f = datos.generado ? new Date(datos.generado).toLocaleString('es', { dateStyle: 'medium', timeStyle: 'short' }) : '';
      $('total').textContent = `${items.length.toLocaleString('es')} piezas${f ? ` · actualizado ${f}` : ''}`;
    } catch (e) {
      $('total').textContent = 'sin datos todavía';
      $('estado').textContent = 'El catálogo todavía se está generando. Vuelve a cargar la página en un rato.';
      return;
    }
    construyeIndice();
    llenaDestinos();
    leeUrl();
    reflejaControles();
    actualiza();
  }

  inicia();
})();
