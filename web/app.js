/* =============================================================================
   app.js — Lógica de la interfaz de predicción de retrasos.

   Sin framework y sin proceso de compilación: la API sirve estos ficheros tal
   cual, y desplegar es un git pull más un reinicio de servicio. En un proyecto
   de una sola persona con tres semanas de plazo, cada pieza de tooling que no
   se añade es una que no puede fallar el día de la defensa.
   ============================================================================= */

"use strict";

// El HTML lo sirve la propia API, así que las rutas son relativas al mismo origen.
const API = "";

const estado = {
  origen: null,        // {stop_id, nombre, lineas}
  destino: null,
  offsetMin: 0,        // minutos desde ahora, según la ficha elegida
  horaManual: null,    // "HH:MM" si el usuario fija una hora concreta
  lineasConsulta: [],   // líneas del último resultado, para filtrar alertas
  pantallaActiva: "llegada",
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------------

/** Instante de salida elegido, como Date. */
function instanteSalida() {
  if (estado.horaManual) {
    const [h, m] = estado.horaManual.split(":").map(Number);
    const cuando = new Date();
    cuando.setHours(h, m, 0, 0);
    if (cuando.getTime() < Date.now() - 3 * 3600 * 1000) {
      cuando.setDate(cuando.getDate() + 1);
    }
    return cuando;
  }
  return new Date(Date.now() + estado.offsetMin * 60 * 1000);
}

/** Date -> "07:12" en la hora local del navegador. */
function comoHora(fecha) {
  return fecha.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
}

/** ISO con zona -> "07:12". Sin parsear a Date para evitar problemas de TZ. */
function horaDeISO(iso) {
  return iso ? iso.slice(11, 16) : "";
}

/** Pinta la interfaz con el color de la línea del trayecto. */
function aplicarColorDeLinea(color) {
  document.documentElement.style.setProperty("--linea", color || "var(--tinta)");
}

function mostrar(id, visible) {
  $(id).hidden = !visible;
}

// ---------------------------------------------------------------------------
// Navegación entre pantallas
// ---------------------------------------------------------------------------

const TITULOS = {
  llegada: "Llegada estimada",
  alertas: "Incidencias",
  mapa: "Estado de la red",
};

function irA(pantalla) {
  if (pantalla === estado.pantallaActiva) return;

  // Ocultar la pantalla anterior, mostrar la nueva
  $(`pantalla-${estado.pantallaActiva}`).hidden = true;
  $(`pantalla-${pantalla}`).hidden = false;

  // Actualizar los tabs
  document.querySelectorAll(".nav__item").forEach((btn) => {
    btn.classList.toggle("nav__item--activo", btn.dataset.pantalla === pantalla);
  });

  // Actualizar el título
  $("titulo-pantalla").textContent = TITULOS[pantalla] || "";

  estado.pantallaActiva = pantalla;

  // Al entrar en alertas, cargar y arrancar el sondeo
  if (pantalla === "alertas") {
    cargarAlertas();
    iniciarSondeo();
  } else {
    pararSondeo();
  }
}

document.querySelector(".nav").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".nav__item");
  if (!btn || btn.disabled) return;
  irA(btn.dataset.pantalla);
});

// ---------------------------------------------------------------------------
// Autocompletado de estaciones
// ---------------------------------------------------------------------------

function montarBuscador(idEntrada, idLista, clave) {
  const entrada = $(idEntrada);
  const lista = $(idLista);
  let opciones = [];
  let resaltada = -1;
  let temporizador = null;

  function cerrar() {
    lista.hidden = true;
    entrada.setAttribute("aria-expanded", "false");
    resaltada = -1;
  }

  function resaltar(indice) {
    const items = lista.querySelectorAll(".sugerencia");
    items.forEach((li, i) => li.setAttribute("aria-selected", String(i === indice)));
    resaltada = indice;
  }

  function elegir(estacion) {
    estado[clave] = estacion;
    entrada.value = estacion.nombre;
    cerrar();
    const otro = clave === "origen" ? estado.destino : estado.origen;
    if (otro) {
      const comunes = estacion.lineas.filter((l) => otro.lineas.includes(l));
      if (comunes.length === 1) pintarSegunLinea(comunes[0]);
    }
    $("pie-consulta").textContent = "";
  }

  async function buscar(texto) {
    if (texto.trim().length < 2) return cerrar();
    try {
      const resp = await fetch(`${API}/api/estaciones?q=${encodeURIComponent(texto)}`);
      if (!resp.ok) throw new Error(resp.status);
      opciones = await resp.json();
    } catch {
      return cerrar();
    }

    if (!opciones.length) return cerrar();

    lista.innerHTML = opciones
      .map(
        (e, i) => `
        <li class="sugerencia" role="option" id="${idLista}-${i}" aria-selected="false">
          <span class="sugerencia__nombre">${e.nombre}</span>
          <span class="insignias">${e.lineas.map((l) => `<span class="insignia" style="background:${colorDeLinea(l)};color:#fff">${l}</span>`).join("")}</span>
        </li>`
      )
      .join("");

    lista.querySelectorAll(".sugerencia").forEach((li, i) => {
      li.addEventListener("mousedown", (ev) => {
        ev.preventDefault();
        elegir(opciones[i]);
      });
    });

    lista.hidden = false;
    entrada.setAttribute("aria-expanded", "true");
    resaltar(-1);
  }

  entrada.addEventListener("input", () => {
    estado[clave] = null;
    clearTimeout(temporizador);
    temporizador = setTimeout(() => buscar(entrada.value), 160);
  });

  entrada.addEventListener("keydown", (ev) => {
    if (lista.hidden) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      resaltar(Math.min(resaltada + 1, opciones.length - 1));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      resaltar(Math.max(resaltada - 1, 0));
    } else if (ev.key === "Enter" && resaltada >= 0) {
      ev.preventDefault();
      elegir(opciones[resaltada]);
    } else if (ev.key === "Escape") {
      cerrar();
    }
  });

  entrada.addEventListener("blur", () => setTimeout(cerrar, 120));
}

// ---------------------------------------------------------------------------
// Colores de línea (vienen del GTFS, no están escritos a mano)
// ---------------------------------------------------------------------------
const coloresLinea = {};

async function cargarColores() {
  try {
    const resp = await fetch(`${API}/api/lineas`);
    const datos = await resp.json();
    datos.lineas.forEach((l) => (coloresLinea[l.line_id] = l.color));
    $("pie-version").textContent = `Horarios GTFS · versión ${datos.gtfs_version}`;
  } catch {
    $("pie-version").textContent = "";
  }
}

function pintarSegunLinea(lineId) {
  aplicarColorDeLinea(coloresLinea[lineId]);
}

function colorDeLinea(lineId) {
  return coloresLinea[lineId] || "#5b6676";
}

// ---------------------------------------------------------------------------
// Consulta
// ---------------------------------------------------------------------------
async function consultar() {
  if (!estado.origen || !estado.destino) {
    $("pie-consulta").textContent = "Elige una estación de origen y otra de destino.";
    return;
  }
  if (estado.origen.stop_id === estado.destino.stop_id) {
    $("pie-consulta").textContent = "El origen y el destino son la misma estación.";
    return;
  }

  $("pie-consulta").textContent = "";
  mostrar("panel-resultado", false);
  mostrar("panel-aviso", false);
  mostrar("panel-cargando", true);
  $("buscar").disabled = true;

  try {
    const resp = await fetch(`${API}/api/consulta`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origen: estado.origen.stop_id,
        destino: estado.destino.stop_id,
        salida_desde_utc: instanteSalida().toISOString(),
      }),
    });

    if (!resp.ok) {
      const cuerpo = await resp.json().catch(() => ({}));
      throw new Error(cuerpo.detail || `Error ${resp.status}`);
    }

    pintarResultado(await resp.json());
  } catch (err) {
    avisar("No se ha podido consultar", String(err.message || err));
  } finally {
    mostrar("panel-cargando", false);
    $("buscar").disabled = false;
  }
}

function avisar(titulo, texto) {
  $("aviso-titulo").textContent = titulo;
  $("aviso-texto").textContent = texto;
  mostrar("panel-aviso", true);
}

function clasificarRetraso(segundos) {
  const min = Math.round(segundos / 60);
  if (min <= 2) return { clase: "puntual", icono: "●", texto: "En hora", min };
  if (min <= 10) return { clase: "leve", icono: "▲", texto: `+${min} min`, min };
  return { clase: "alto", icono: "■", texto: `+${min} min`, min };
}

function pintarMargen(tramo) {
  if (!tramo.retraso_s.con_intervalo) return "";

  const { p10, p50, p90 } = tramo.retraso_s;
  const tope = Math.max(p90 * 1.15, 300);
  const pct = (v) => Math.max(0, Math.min(100, (v / tope) * 100));

  const teorica = new Date(tramo.destino.hora_teorica_utc).getTime();
  const hora = (s) => comoHora(new Date(teorica + s * 1000));

  return `
    <div class="margen">
      <div class="margen__pista" role="img"
           aria-label="Se espera la llegada entre las ${hora(p10)} y las ${hora(p90)}">
        <span class="margen__banda" style="left:${pct(p10)}%;width:${pct(p90) - pct(p10)}%"></span>
        <span class="margen__mediana" style="left:${pct(p50)}%"></span>
      </div>
      <div class="margen__etiquetas">
        <span>Se espera entre las ${hora(p10)}</span>
        <span>y las ${hora(p90)}</span>
      </div>
    </div>`;
}

function pintarResultado(datos) {
  if (!datos.opciones.length) {
    avisar("Sin trenes para ese trayecto", datos.aviso || "Prueba con otra hora.");
    return;
  }

  const lineas = new Set(datos.opciones.map((op) => op.tramos[0].line_id));
  aplicarColorDeLinea(lineas.size === 1 ? colorDeLinea([...lineas][0]) : null);

  // Guardar las líneas del resultado para conectar con alertas
  estado.lineasConsulta = [...lineas];

  $("resultados").innerHTML = datos.opciones
    .map((op) => {
      const tramo = op.tramos[0];
      const salida = comoHora(new Date(op.salida_teorica_utc));
      const llegada = comoHora(new Date(op.llegada_estimada_utc));
      const paradas = tramo.paradas_intermedias;
      const r = clasificarRetraso(op.retraso_total_s);

      return `
        <article class="tren" style="--linea:${colorDeLinea(tramo.line_id)}">
          <div class="tren__origen">
            <span class="tren__linea">${tramo.line_id}</span>
            <span>Sale a las <span class="tren__salida">${salida}</span></span>
            <span>· ${paradas} ${paradas === 1 ? "parada" : "paradas"}</span>
          </div>

          <div class="tren__principal">
            <div>
              <span class="tren__llegada">${llegada}</span>
              <span class="tren__llegada-etiqueta">Llegada estimada</span>
            </div>
            <span class="retraso retraso--${r.clase}">
              <span class="retraso__icono" aria-hidden="true">${r.icono}</span>${r.texto}
            </span>
          </div>

          ${pintarMargen(tramo)}
        </article>`;
    })
    .join("");

  const bloques = new Set();
  datos.opciones.forEach((op) =>
    op.tramos.forEach((t) => (t.degraded_blocks || []).forEach((b) => bloques.add(b)))
  );
  const nombres = { meteo: "meteorología", estado_red: "estado de la red",
                    alertas: "incidencias", estado_propio: "posición del tren" };
  const nota = $("nota-degradada");
  if (bloques.size) {
    nota.textContent =
      "Predicción calculada sin " +
      [...bloques].map((b) => nombres[b] || b).join(", ") +
      ": esa información no estaba disponible en este momento.";
    nota.hidden = false;
  } else {
    nota.hidden = true;
  }

  // Enlace contextual a alertas si hay incidencias en las líneas del trayecto
  actualizarEnlaceAlertas();

  mostrar("panel-resultado", true);
}

// ---------------------------------------------------------------------------
// Alertas
// ---------------------------------------------------------------------------

// Datos de la última respuesta del endpoint, sin filtrar
let datosAlertasCrudos = null;
let filtroLineaActual = "";   // "" = todas
let timerSondeo = null;
const PERIODO_SONDEO_MS = 60 * 1000;

const TIPO_LEGIBLE = {
  SUPRESION: "Supresión",
  AVERIA: "Avería",
  RETRASO: "Retraso",
  SERVICIO_BUS: "Servicio alternativo",
  OBRAS: "Obras",
  OTRO: "Otra incidencia",
};

const IMPACTO_CLASE = {
  ALTO: "alto",
  MEDIO: "leve",
  BAJO: "puntual",
};

const IMPACTO_ICONO = {
  ALTO: "■",
  MEDIO: "▲",
  BAJO: "●",
};

async function cargarAlertas() {
  try {
    const resp = await fetch(`${API}/api/alertas`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    datosAlertasCrudos = await resp.json();
  } catch {
    datosAlertasCrudos = null;
  }
  pintarPantallaAlertas();
  actualizarBadge();
  actualizarEnlaceAlertas();
}

function pintarPantallaAlertas() {
  // Ocultar todo primero
  mostrar("panel-alertas-cargando", false);
  mostrar("panel-alertas-activas", false);
  mostrar("panel-alertas-resueltas", false);
  mostrar("panel-accesibilidad", false);
  mostrar("panel-sin-alertas", false);
  mostrar("aviso-feed", false);
  mostrar("filtros-linea", false);

  if (!datosAlertasCrudos) {
    mostrar("panel-alertas-cargando", true);
    return;
  }

  const datos = datosAlertasCrudos;

  // Estado del feed: "datos no disponibles" nunca debe confundirse con
  // "sin incidencias". Son cosas distintas y esta distinción es el argumento
  // más importante de la pantalla.
  if (datos.feed.estado === "CADUCO" || datos.feed.estado === "SIN_DATOS") {
    $("aviso-feed-texto").textContent =
      "Los datos de incidencias no están disponibles en este momento. " +
      "La última actualización fue a las " + horaDeISO(datos.feed.ultima_captura || "") + ".";
    mostrar("aviso-feed", true);
  } else if (datos.feed.estado === "EMISOR_VACIO") {
    $("aviso-feed-texto").textContent =
      "El feed de incidencias de RENFE responde pero sin contenido. " +
      "Es posible que haya un problema en la fuente.";
    mostrar("aviso-feed", true);
  }

  // Filtrar por línea
  const incidencias = filtrarPorLinea(datos.incidencias);
  const activas = incidencias.filter((i) => i.estado === "ACTIVA");
  const resueltas = incidencias.filter((i) => i.estado === "RESUELTA");

  // Filtros: recoger todas las líneas con alertas hoy (sin filtrar)
  const todasLineas = new Set();
  datos.incidencias.forEach((i) => i.lineas.forEach((l) => todasLineas.add(l)));
  if (todasLineas.size > 0) {
    pintarFiltros([...todasLineas].sort());
    mostrar("filtros-linea", true);
  }

  // Estado vacío
  if (!activas.length && !resueltas.length && !datos.accesibilidad.length) {
    if (filtroLineaActual) {
      $("sin-alertas-texto").textContent =
        `Sin incidencias hoy en la línea ${filtroLineaActual.toUpperCase()}.`;
    } else {
      $("sin-alertas-texto").textContent = "No se han registrado incidencias hoy en la red.";
    }
    mostrar("panel-sin-alertas", true);
    return;
  }

  // Activas
  if (activas.length) {
    $("lista-activas").innerHTML = activas.map(pintarAlerta).join("");
    mostrar("panel-alertas-activas", true);
    conectarExpandibles($("lista-activas"));
  }

  // Resueltas
  if (resueltas.length) {
    $("lista-resueltas").innerHTML = resueltas.map((i) => pintarAlerta(i, true)).join("");
    mostrar("panel-alertas-resueltas", true);
    conectarExpandibles($("lista-resueltas"));
  }

  // Accesibilidad (sin filtro de línea: siempre se muestran todas)
  const acc = datos.accesibilidad || [];
  if (acc.length) {
    $("lista-accesibilidad").innerHTML = acc
      .map((a) => pintarAlertaAccesibilidad(a))
      .join("");
    mostrar("panel-accesibilidad", true);
    conectarExpandibles($("lista-accesibilidad"));
  }
}

function pintarAlerta(item, resuelta = false) {
  const cls = resuelta ? "alerta alerta--resuelta" : "alerta";
  const lineas = item.lineas
    .map((l) => `<span class="insignia" style="background:${colorDeLinea(l)};color:#fff">${l}</span>`)
    .join("");
  const sinLinea = item.lineas.length === 0
    ? `<span class="insignia">Red</span>` : "";

  const tipo = TIPO_LEGIBLE[item.tipo] || item.tipo;
  const planificada = item.planificada
    ? `<span class="alerta__etiqueta alerta__etiqueta--planificada">Programada</span>` : "";

  const impClase = IMPACTO_CLASE[item.impacto] || "puntual";
  const impIcono = IMPACTO_ICONO[item.impacto] || "●";

  let tiempo = `Desde las ${horaDeISO(item.desde)}`;
  if (item.hasta) tiempo += ` — resuelta a las ${horaDeISO(item.hasta)}`;

  return `
    <article class="${cls}">
      <div class="alerta__cabecera">
        ${lineas}${sinLinea}
        <span class="alerta__tipo">${tipo}</span>
        ${planificada}
      </div>
      <p class="alerta__texto">${item.texto}</p>
      <button class="alerta__leer-mas" type="button">leer más</button>
      <div class="alerta__meta">
        <span class="alerta__hora">${tiempo}</span>
        <span class="retraso retraso--${impClase}">
          <span class="retraso__icono" aria-hidden="true">${impIcono}</span>${item.impacto.toLowerCase()}
        </span>
      </div>
    </article>`;
}

function pintarAlertaAccesibilidad(item) {
  const resuelta = item.estado === "RESUELTA";
  const cls = resuelta ? "alerta alerta--resuelta" : "alerta";
  const estaciones = (item.estaciones || []).join(", ");

  let tiempo = `Desde las ${horaDeISO(item.desde)}`;
  if (item.hasta) tiempo += ` — resuelta a las ${horaDeISO(item.hasta)}`;

  return `
    <article class="${cls}">
      <p class="alerta__texto">${item.texto}</p>
      <button class="alerta__leer-mas" type="button">leer más</button>
      ${estaciones ? `<p class="alerta__estaciones">${estaciones}</p>` : ""}
      <div class="alerta__meta">
        <span class="alerta__hora">${tiempo}</span>
      </div>
    </article>`;
}

/** Conecta los botones "leer más" / "leer menos" de las alertas.
 *  Si el texto no está truncado (cabe en 3 líneas), se oculta el botón. */
/* --- Detección de texto truncado -----------------------------------------

   El texto se recorta con `max-height` (ver estilos.css), así que la caja es
   un bloque normal y la comparación estándar funciona:

       scrollHeight  = altura de TODO el contenido
       clientHeight  = altura visible, topada por max-height
       truncado      = scrollHeight > clientHeight

   La medida se engancha a un ResizeObserver en vez de ejecutarse una vez.
   El observador se dispara cuando la caja cambia de tamaño, y eso cubre los
   tres momentos en que el resultado puede cambiar: el primer layout, un
   cambio de ancho de la ventana, y la sustitución de la fuente de reserva
   por Barlow al terminar de cargarse. Sin él hacía falta acertar el instante
   exacto de la medición, que es lo que fallaba.

   El resultado se escribe en `data-truncado` del <article>, y es el CSS el
   que decide si el botón se muestra. Una sola fuente de verdad.            */

const observadorTruncado = new ResizeObserver((entradas) => {
  for (const entrada of entradas) evaluarTruncado(entrada.target);
});

function evaluarTruncado(texto) {
  const articulo = texto.closest(".alerta");
  if (!articulo) return;

  // Expandido no se mide: sin max-height, scrollHeight y clientHeight
  // coinciden y la alerta se marcaría como no truncada, escondiendo el
  // botón "leer menos" con el texto ya desplegado.
  if (texto.classList.contains("alerta__texto--expandido")) return;

  const truncado = texto.scrollHeight > texto.clientHeight + 1;
  articulo.dataset.truncado = truncado ? "si" : "no";
}

function conectarExpandibles(contenedor) {
  contenedor.querySelectorAll(".alerta").forEach((articulo) => {
    const texto = articulo.querySelector(".alerta__texto");
    const boton = articulo.querySelector(".alerta__leer-mas");
    if (!texto || !boton) return;

    // El observador emite una primera vez en cuanto empieza a observar, ya
    // con el layout hecho: no hace falta medir aquí a mano.
    observadorTruncado.observe(texto);

    boton.addEventListener("click", () => {
      const expandido = texto.classList.toggle("alerta__texto--expandido");
      boton.textContent = expandido ? "leer menos" : "leer más";
    });
  });
}

// --- Filtros por línea ---------------------------------------------------

function pintarFiltros(lineas) {
  const cont = $("filtros-linea");
  // Conservar el filtro "Todas" que ya está en el HTML
  const html = lineas.map((l) => {
    const activo = filtroLineaActual.toLowerCase() === l.toLowerCase() ? " filtro--activo" : "";
    const color = colorDeLinea(l);
    return `<button class="filtro${activo}" data-linea="${l}" type="button"
              style="--filtro-color:${color}">${l}</button>`;
  }).join("");

  // Botón "Todas" + botón "Tu trayecto" si hay líneas de consulta + líneas individuales
  let prefijos = "";
  const todasActivo = !filtroLineaActual ? " filtro--activo" : "";
  prefijos += `<button class="filtro${todasActivo}" data-linea="" type="button">Todas</button>`;

  if (estado.lineasConsulta.length > 0) {
    const trayectoActivo = filtroLineaActual === "__trayecto__" ? " filtro--activo" : "";
    prefijos += `<button class="filtro${trayectoActivo}" data-linea="__trayecto__" type="button">Tu trayecto</button>`;
  }

  cont.innerHTML = prefijos + html;
}

function filtrarPorLinea(incidencias) {
  if (!filtroLineaActual) return incidencias;

  if (filtroLineaActual === "__trayecto__") {
    const set = new Set(estado.lineasConsulta.map((l) => l.toLowerCase()));
    return incidencias.filter(
      (i) => i.lineas.length === 0 || i.lineas.some((l) => set.has(l.toLowerCase()))
    );
  }

  const objetivo = filtroLineaActual.toLowerCase();
  return incidencias.filter(
    (i) => i.lineas.length === 0 || i.lineas.some((l) => l.toLowerCase() === objetivo)
  );
}

$("filtros-linea").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".filtro");
  if (!btn) return;
  filtroLineaActual = btn.dataset.linea;
  pintarPantallaAlertas();
});

// --- Plegable de accesibilidad -------------------------------------------

$("toggle-accesibilidad").addEventListener("click", () => {
  const expandido = $("toggle-accesibilidad").getAttribute("aria-expanded") === "true";
  $("toggle-accesibilidad").setAttribute("aria-expanded", String(!expandido));
  $("lista-accesibilidad").hidden = expandido;
});

// --- Badge y enlace contextual -------------------------------------------

function actualizarBadge() {
  const badge = $("badge-alertas");
  if (!datosAlertasCrudos) {
    badge.hidden = true;
    return;
  }
  const n = datosAlertasCrudos.resumen.activas;
  badge.textContent = String(n);
  badge.hidden = n === 0;
}

/** Enlace "Ver alertas de tu trayecto" en el panel de resultado. */
function actualizarEnlaceAlertas() {
  const btn = $("ver-alertas-trayecto");
  if (!datosAlertasCrudos || estado.lineasConsulta.length === 0) {
    btn.hidden = true;
    return;
  }

  // ¿Hay alertas activas en las líneas del trayecto?
  const set = new Set(estado.lineasConsulta.map((l) => l.toLowerCase()));
  const relevantes = datosAlertasCrudos.incidencias.filter(
    (i) => i.estado === "ACTIVA" && i.lineas.some((l) => set.has(l.toLowerCase()))
  );

  if (relevantes.length === 0) {
    btn.hidden = true;
    return;
  }

  const plural = relevantes.length === 1 ? "incidencia activa" : "incidencias activas";
  const lineasTexto = estado.lineasConsulta.join(", ");
  $("enlace-alertas-texto").textContent =
    `${relevantes.length} ${plural} en ${lineasTexto}`;
  btn.hidden = false;
}

$("ver-alertas-trayecto").addEventListener("click", () => {
  filtroLineaActual = "__trayecto__";
  irA("alertas");
});

// --- Sondeo con visibilitychange -----------------------------------------

function iniciarSondeo() {
  pararSondeo();
  timerSondeo = setInterval(cargarAlertas, PERIODO_SONDEO_MS);
}

function pararSondeo() {
  if (timerSondeo) {
    clearInterval(timerSondeo);
    timerSondeo = null;
  }
}

// Pausar el sondeo cuando la pestaña no está visible. Sin esto, una pestaña
// olvidada lanza un GET cada 60 s al servidor para siempre.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && estado.pantallaActiva === "alertas") {
    cargarAlertas();
    iniciarSondeo();
  } else {
    pararSondeo();
  }
});

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------
montarBuscador("origen", "sugerencias-origen", "origen");
montarBuscador("destino", "sugerencias-destino", "destino");

$("fichas").addEventListener("click", (ev) => {
  const ficha = ev.target.closest(".ficha");
  if (!ficha) return;
  document.querySelectorAll(".ficha").forEach((f) => f.classList.remove("ficha--activa"));
  ficha.classList.add("ficha--activa");
  estado.offsetMin = Number(ficha.dataset.min);
  estado.horaManual = null;
  $("hora-manual").value = "";
});

$("hora-manual").addEventListener("change", (ev) => {
  estado.horaManual = ev.target.value || null;
  if (estado.horaManual) {
    document.querySelectorAll(".ficha").forEach((f) => f.classList.remove("ficha--activa"));
  }
});

$("intercambiar").addEventListener("click", () => {
  [estado.origen, estado.destino] = [estado.destino, estado.origen];
  const a = $("origen"), b = $("destino");
  [a.value, b.value] = [b.value, a.value];
});

$("buscar").addEventListener("click", consultar);

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.target.closest(".sugerencias") &&
      $("sugerencias-origen").hidden && $("sugerencias-destino").hidden) {
    consultar();
  }
});

cargarColores();

// Carga inicial de alertas en segundo plano para tener el badge listo.
// No arranca el sondeo: solo carga una vez.
cargarAlertas();
