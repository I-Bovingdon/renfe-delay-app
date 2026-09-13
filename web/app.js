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

  // Al entrar en alertas o en mapa, cargar y arrancar su sondeo. Solo sondea la
  // pantalla visible: dos temporizadores a la vez serían dos GET por minuto para
  // enseñar una sola cosa.
  pararSondeo();
  pararSondeoMapa();
  if (pantalla === "alertas") {
    cargarAlertas();
    iniciarSondeo();
  } else if (pantalla === "mapa") {
    iniciarMapa();
    cargarMapa();
    iniciarSondeoMapa();
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
// El trazado de cada línea ya viene del catálogo GTFS en /api/lineas. El mapa lo
// dibuja desde aquí: no hay ningún fichero de geometría aparte que mantener.
const trazadosLinea = {};

async function cargarColores() {
  try {
    const resp = await fetch(`${API}/api/lineas`);
    const datos = await resp.json();
    datos.lineas.forEach((l) => {
      coloresLinea[l.line_id] = l.color;
      trazadosLinea[l.line_id] = l.trazado || [];
    });
    $("pie-version").textContent = `Horarios GTFS · versión ${datos.gtfs_version}`;
  } catch {
    $("pie-version").textContent = "";
  }
}

/** Enseña el enlace al panel de análisis solo si tiene una URL de verdad.
 *
 *  WEB-HOLDER: mientras el href de #enlace-analisis siga valiendo la cadena
 *  WEB-HOLDER (o esté vacío), el enlace permanece oculto. En cuanto se ponga
 *  la URL definitiva en index.html, aparece solo. No hay que tocar este
 *  fichero.
 *
 *  Por qué ocultarlo en lugar de dejarlo puesto: un enlace visible que lleva
 *  a una página de error delante del tribunal es peor que no tener enlace.
 */
function mostrarEnlaceAnalisis() {
  const enlace = $("enlace-analisis");
  if (!enlace) return;
  const url = enlace.getAttribute("href") || "";
  enlace.hidden = !(url && url !== "WEB-HOLDER" && /^https?:\/\//i.test(url));
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
        <article class="tren" data-linea="${tramo.line_id}"
                 style="--linea:${colorDeLinea(tramo.line_id)}">
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

  // Dos causas distintas, dos mensajes distintos.
  //
  // Estructural: la fuente NO está integrada todavía y no lo estará hoy. Las
  // incidencias esperan a que se conecte el clasificador al cálculo de
  // variables. Estos bloques faltan en TODAS las consultas, así que anunciarlos
  // como una avería puntual es falso y además enseña al usuario a ignorar el
  // aviso.
  //
  // F8 (13/09): salen de esta lista las dos que quedaban. La meteorología
  // alimenta al modelo con la observación de AEMET más reciente de la estación
  // asignada al destino; las incidencias, con la ventana de 30 minutos de la
  // línea calculada sobre el mismo índice que pinta la pantalla de alertas. Las
  // cuatro fuentes del proyecto llegan ya al modelo, así que cualquier bloque
  // que aparezca en degraded_blocks es por definición un fallo de hoy.
  //
  // El mecanismo se conserva vacío a propósito: si mañana se añade una quinta
  // fuente y entra por fases, este es su sitio.
  //
  // Transitoria: la fuente existe y hoy ha fallado. Eso sí es una degradación
  // y tiene que decirse como tal.
  const ESTRUCTURALES = new Set();

  const estructurales = [...bloques].filter((b) => ESTRUCTURALES.has(b));
  const transitorios = [...bloques].filter((b) => !ESTRUCTURALES.has(b));

  const frases = [];
  if (transitorios.length) {
    frases.push(
      "No se ha podido usar " +
      transitorios.map((b) => nombres[b] || b).join(", ") +
      ": esa fuente no respondía al calcular la predicción."
    );
  }
  if (estructurales.length) {
    frases.push(
      "La predicción no incorpora " +
      estructurales.map((b) => nombres[b] || b).join(" ni ") +
      ": son fuentes que todavía no alimentan al modelo."
    );
  }

  const nota = $("nota-degradada");
  nota.textContent = frases.join(" ");
  nota.hidden = frases.length === 0;

  // Aviso de corte de servicio en la propia ficha del tren, y enlace contextual
  // a la pantalla de alertas si hay cualquier otra incidencia en el trayecto.
  aplicarAvisosInterrupcion();
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
  // "Vuelta a la normalidad" y no "Incidencia resuelta": el tipo describe lo
  // que dice el TEXTO del aviso, y el estado ACTIVA/RESUELTA describe si RENFE
  // lo sigue publicando. Son ejes ortogonales y compartir la palabra "resuelta"
  // hacía que una alerta activa de tipo RESOLUCION pareciese mal colocada.
  RESOLUCION: "Vuelta a la normalidad",
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

// ----------------------------------------------------- corte de servicio ---
// Tipos de incidencia que implican que el tren puede NO circular. Son los dos que
// usa RENFE para cortes de servicio. El resto (averías, obras, retrasos) afectan a
// la puntualidad, que es justo lo que el modelo ya predice: avisar de ellos sería
// duplicar la predicción con peor información.
const TIPOS_INTERRUPCION = new Set(["SUPRESION", "SERVICIO_BUS"]);

// El feed publica "C4" sin distinguir la rama, mientras que el catálogo tiene C4a
// y C4b por separado. Se comparan los códigos base para que una supresión en la C4
// avise también en sus dos ramas. Misma aproximación que en fuente_raw.py.
const codigoBase = (l) => (l || "").toLowerCase().replace(/[ab]$/, "");

function interrupcionDeLinea(lineId) {
  if (!datosAlertasCrudos || !datosAlertasCrudos.incidencias) return null;
  const objetivo = codigoBase(lineId);
  return (
    datosAlertasCrudos.incidencias.find(
      (i) =>
        i.estado === "ACTIVA" &&
        TIPOS_INTERRUPCION.has(i.tipo) &&
        (i.lineas || []).some((l) => codigoBase(l) === objetivo)
    ) || null
  );
}

// Se opera sobre el DOM ya pintado en lugar de volver a pintarlo: la consulta y las
// alertas llegan por caminos distintos y en cualquier orden (la consulta es puntual,
// las alertas se sondean cada 60 s), así que esta función tiene que poder ejecutarse
// las veces que haga falta sin efectos acumulados. De ahí que lo primero sea limpiar.
function aplicarAvisosInterrupcion() {
  document.querySelectorAll("#resultados .tren").forEach((ficha) => {
    const previo = ficha.querySelector(".interrupcion");
    if (previo) previo.remove();
    ficha.classList.remove("tren--interrumpido");

    const incidencia = interrupcionDeLinea(ficha.dataset.linea);
    if (!incidencia) return;

    const linea = ficha.dataset.linea;
    // "en parte de la línea" a propósito: la alerta nombra el tramo cortado en
    // texto libre ("entre Cercedilla, Puerto de Navacerrada y Los Cotos") y eso no
    // se puede casar con las paradas del trayecto de forma fiable. Afirmar que
    // ESTE tren concreto está suprimido sería ir más allá de lo que dice el dato.
    const cabeza =
      incidencia.tipo === "SERVICIO_BUS"
        ? `Servicio suspendido en parte de la ${linea}. RENFE ha establecido autobuses.`
        : `Hay supresiones de trenes en la ${linea}.`;

    const aviso = document.createElement("p");
    aviso.className = "interrupcion";
    // El texto de la alerta NO se inyecta aquí: solo se compone a partir de su
    // tipo y del identificador de línea del catálogo, que son datos propios.
    aviso.innerHTML =
      `<span class="interrupcion__icono" aria-hidden="true">■</span>` +
      `<span>${cabeza} Este tren puede no circular; la predicción no tiene en ` +
      `cuenta la incidencia.</span>`;

    ficha.classList.add("tren--interrumpido");
    ficha.prepend(aviso);
  });
}

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
  // Si el sondeo trae una incidencia nueva mientras hay un resultado en pantalla,
  // el aviso aparece sin que el usuario tenga que volver a consultar.
  aplicarAvisosInterrupcion();
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
    // OJO con la forma corta `activas.map(pintarAlerta)`: map pasa
    // (elemento, índice, array), así que el ÍNDICE entraba como segundo
    // argumento de pintarAlerta, que es `resuelta`. Resultado: la primera
    // alerta se pintaba normal (índice 0, falso) y TODAS las demás atenuadas
    // al 55 % como si estuvieran resueltas. Se envuelve en una flecha para que
    // llegue un único argumento.
    $("lista-activas").innerHTML = activas.map((i) => pintarAlerta(i)).join("");
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
    ? `<span class="alerta__etiqueta alerta__etiqueta--planificada">Planificada</span>` : "";

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

  // Las líneas que se nombran son las AFECTADAS por esas incidencias, no las
  // del trayecto. Antes se listaban las del trayecto entero, así que una
  // única avería en C10 y C7 se anunciaba como "en C10, C4a, C3, C7".
  const afectadas = [...new Set(
    relevantes.flatMap((i) => i.lineas).filter((l) => set.has(l.toLowerCase()))
  )].sort();

  $("enlace-alertas-texto").textContent =
    `${relevantes.length} ${plural} en ${afectadas.join(", ")}`;
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
  const visible = document.visibilityState === "visible";
  if (visible && estado.pantallaActiva === "alertas") {
    cargarAlertas();
    iniciarSondeo();
  } else if (visible && estado.pantallaActiva === "mapa") {
    cargarMapa();
    iniciarSondeoMapa();
  } else {
    pararSondeo();
    pararSondeoMapa();
  }
});

// ---------------------------------------------------------------------------
// Pantalla de mapa
// ---------------------------------------------------------------------------

const PERIODO_MAPA_MS = 30 * 1000;

const mapaEstado = {
  mapa: null,
  capaLineas: null,
  capaEstaciones: null,
  capaTrenes: null,
  marcadores: new Map(),   // id de vehículo -> marcador de Leaflet
  estaciones: new Map(),   // stop_id -> círculo de estación
  polilineas: new Map(),   // line_id -> polilínea
  filtro: "",              // "" = todas las líneas
  datos: null,
  timer: null,
};

/** Construye el mapa la primera vez que se entra en la pantalla.
 *  No se puede construir antes: Leaflet mide el contenedor al crearlo y el div está
 *  oculto hasta ese momento, así que saldría con 0 px de alto. */
function iniciarMapa() {
  if (mapaEstado.mapa) {
    // Al volver a la pestaña, el contenedor pudo cambiar de tamaño mientras estaba
    // oculto (rotar el móvil). Sin esto, el mapa queda recortado.
    setTimeout(() => mapaEstado.mapa.invalidateSize(), 0);
    return;
  }
  if (typeof L === "undefined") {
    $("aviso-mapa-texto").textContent =
      "No se ha podido cargar la librería del mapa. Recarga la página.";
    mostrar("aviso-mapa", true);
    return;
  }

  const mapa = L.map("mapa", { zoomControl: true }).setView([40.42, -3.7], 10);

  // Teselas de OpenStreetMap. `referrerPolicy` es obligatorio: Leaflet 1.9.4 es de
  // 2023 y no la fija solo, y sin cabecera Referer el servidor de OSM bloquea las
  // teselas sin devolver ningún error visible.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    referrerPolicy: "strict-origin-when-cross-origin",
    attribution:
      '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · ' +
      "Datos de RENFE (CC BY 4.0)",
  }).addTo(mapa);

  // El orden de creación es el orden de apilado: trazado abajo, estaciones en
  // medio y trenes arriba. Un tren nunca puede quedar tapado por una estación.
  mapaEstado.capaLineas = L.layerGroup().addTo(mapa);
  mapaEstado.capaEstaciones = L.layerGroup().addTo(mapa);
  mapaEstado.capaTrenes = L.layerGroup().addTo(mapa);
  mapaEstado.mapa = mapa;

  pintarTrazados();
  pintarEstaciones();
  pintarFiltrosMapa();
  setTimeout(() => mapa.invalidateSize(), 0);
}

function pintarTrazados() {
  const puntosTodos = [];
  Object.entries(trazadosLinea).forEach(([lineId, puntos]) => {
    if (!puntos || !puntos.length) return;
    const linea = L.polyline(puntos, {
      color: colorDeLinea(lineId),
      weight: 4,
      opacity: 0.9,
    }).addTo(mapaEstado.capaLineas);
    mapaEstado.polilineas.set(lineId, linea);
    puntosTodos.push(...puntos);
  });
  if (puntosTodos.length) {
    mapaEstado.mapa.fitBounds(L.latLngBounds(puntosTodos), { padding: [20, 20] });
  }
}

/** Puntos de estación, para que el usuario sitúe los trenes respecto a paradas
 *  concretas y no solo respecto al trazado. Se piden una sola vez: las 95 del
 *  núcleo no cambian mientras la pestaña está abierta. */
async function pintarEstaciones() {
  let estaciones = [];
  try {
    const resp = await fetch(`${API}/api/estaciones`);
    estaciones = await resp.json();
  } catch {
    return; // sin estaciones el mapa sigue siendo útil: no se avisa de nada
  }
  if (!mapaEstado.capaEstaciones) return;

  estaciones.forEach((e) => {
    if (e.lat === null || e.lon === null) return;
    const punto = L.circleMarker([e.lat, e.lon], {
      radius: 3.5,
      color: "#101826",
      weight: 1.5,
      fillColor: "#ffffff",
      fillOpacity: 1,
      // Sin interacción de teclado: son 95 puntos y colarlos en el orden de
      // tabulación dejaría el mapa inmanejable con teclado.
      keyboard: false,
    }).bindTooltip(e.nombre, { direction: "top" });

    // Se guardan las líneas de la estación en el propio marcador para poder
    // atenuarla cuando el filtro deja fuera todas sus líneas.
    punto.lineasEstacion = e.lineas || [];
    punto.addTo(mapaEstado.capaEstaciones);
    mapaEstado.estaciones.set(e.stop_id, punto);
  });

  atenuarEstaciones();
}

/** Deja en primer plano solo las estaciones de la línea filtrada. */
function atenuarEstaciones() {
  mapaEstado.estaciones.forEach((punto) => {
    const activa =
      !mapaEstado.filtro || (punto.lineasEstacion || []).includes(mapaEstado.filtro);
    punto.setStyle({ opacity: activa ? 1 : 0.25, fillOpacity: activa ? 1 : 0.25 });
  });
}

/** Triángulo girado al rumbo, o cuadrado si el tren está detenido.
 *  Un tren parado no se gira: el rumbo de un vehículo que no se mueve sería una
 *  dirección inventada. */
function iconoTren(tren) {
  const color = colorDeLinea(tren.linea);
  const forma = tren.parado
    ? `<rect x="5" y="5" width="12" height="12" rx="2" fill="${color}"
             stroke="#101826" stroke-width="1.5"/>`
    : `<path d="M11 2 L18 19 L11 15 L4 19 Z" fill="${color}" stroke="#101826"
             stroke-width="1.5" stroke-linejoin="round"
             transform="rotate(${tren.rumbo ?? 0} 11 11)"/>`;
  return L.divIcon({
    className: "marcador-tren",
    html: `<svg width="22" height="22" viewBox="0 0 22 22">${forma}</svg>`,
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  });
}

function textoRetraso(tren) {
  if (tren.retraso_s === null || tren.retraso_s === undefined) {
    return "Sin estimación de retraso";
  }
  const min = Math.round(tren.retraso_s / 60);
  if (min <= 0) return "En hora, según RENFE";
  return `${min} min de retraso, según RENFE`;
}

function fichaTren(tren) {
  const situacion = tren.parado
    ? `Parado en ${tren.parada ?? "una estación"}`
    : `En marcha hacia ${tren.parada ?? "la siguiente parada"}`;
  return `
    <div class="tren-popup">
      <p class="tren-popup__linea" style="--linea:${colorDeLinea(tren.linea)}">
        ${tren.linea ?? "Línea sin identificar"}
      </p>
      <p class="tren-popup__destino">Dirección ${tren.destino ?? "desconocida"}</p>
      <p class="tren-popup__dato">${situacion}</p>
      <p class="tren-popup__dato">${textoRetraso(tren)}</p>
    </div>`;
}

async function cargarMapa() {
  try {
    const resp = await fetch(`${API}/api/mapa`, { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    mapaEstado.datos = await resp.json();
  } catch {
    mapaEstado.datos = null;
  }
  pintarMapa();
}

function pintarMapa() {
  if (!mapaEstado.mapa) return;
  const datos = mapaEstado.datos;
  mostrar("aviso-mapa", false);

  if (!datos) {
    $("aviso-mapa-texto").textContent =
      "No se han podido cargar las posiciones. Se reintenta en unos segundos.";
    mostrar("aviso-mapa", true);
    $("mapa-pie").textContent = "";
    return;
  }

  // Estado del feed. "No hay trenes" y "no tengo datos" son cosas distintas, y el
  // mapa tiene que decir cuál de las dos: si no, un fallo del emisor parecerá nuestro.
  if (datos.feed.estado === "CADUCO" || datos.feed.estado === "SIN_DATOS") {
    $("aviso-mapa-texto").textContent =
      "Las posiciones no están disponibles en este momento. Última actualización a las " +
      horaDeISO(datos.feed.ultima_captura || "") + ".";
    mostrar("aviso-mapa", true);
  } else if (datos.feed.estado === "EMISOR_VACIO") {
    $("aviso-mapa-texto").textContent =
      "El feed de posiciones de RENFE responde pero sin contenido. " +
      "Es posible que haya un problema en la fuente.";
    mostrar("aviso-mapa", true);
  }

  const visibles = datos.trenes.filter(
    (t) => !mapaEstado.filtro || t.linea === mapaEstado.filtro
  );

  const vistos = new Set();
  visibles.forEach((tren) => {
    vistos.add(tren.id);
    const existente = mapaEstado.marcadores.get(tren.id);
    if (existente) {
      existente.setLatLng([tren.lat, tren.lon]);
      existente.setIcon(iconoTren(tren));
      existente.setPopupContent(fichaTren(tren));
    } else {
      const m = L.marker([tren.lat, tren.lon], {
        icon: iconoTren(tren),
        keyboard: true,
        title: `${tren.linea ?? ""} dirección ${tren.destino ?? ""}`,
      })
        .bindPopup(fichaTren(tren))
        .addTo(mapaEstado.capaTrenes);
      mapaEstado.marcadores.set(tren.id, m);
    }
  });

  mapaEstado.marcadores.forEach((m, id) => {
    if (!vistos.has(id)) {
      mapaEstado.capaTrenes.removeLayer(m);
      mapaEstado.marcadores.delete(id);
    }
  });

  // Con filtro, las demás líneas se atenúan en lugar de desaparecer: la red tiene
  // que seguir reconociéndose para saber dónde está lo que sí se mira.
  mapaEstado.polilineas.forEach((linea, lineId) => {
    const activa = !mapaEstado.filtro || lineId === mapaEstado.filtro;
    linea.setStyle({ opacity: activa ? 0.85 : 0.12, weight: activa ? 4 : 2 });
  });
  atenuarEstaciones();

  pintarFiltrosMapa();

  const edad = datos.feed.antiguedad_s;
  const cuando = edad === null || edad === undefined ? "" : ` · actualizado hace ${edad} s`;
  if (visibles.length) {
    $("mapa-pie").textContent =
      `${visibles.length} ${visibles.length === 1 ? "tren" : "trenes"} en circulación${cuando}`;
  } else if (datos.n_trenes === 0) {
    $("mapa-pie").textContent = `No hay trenes de Cercanías Madrid en circulación${cuando}`;
  } else {
    $("mapa-pie").textContent =
      `Ningún tren de la línea ${mapaEstado.filtro} ahora mismo${cuando}`;
  }
}

function pintarFiltrosMapa() {
  const lineas = Object.keys(trazadosLinea).sort();
  if (!lineas.length) return;

  const conTrenes = new Set((mapaEstado.datos?.trenes || []).map((t) => t.linea));
  const todas = !mapaEstado.filtro ? " filtro--activo" : "";
  const botones = lineas
    .map((l) => {
      const activo = mapaEstado.filtro === l ? " filtro--activo" : "";
      const vacio = conTrenes.has(l) ? "" : " filtro--vacio";
      return `<button class="filtro${activo}${vacio}" data-linea="${l}" type="button"
                style="--filtro-color:${colorDeLinea(l)}">${l}</button>`;
    })
    .join("");

  $("filtros-mapa").innerHTML =
    `<button class="filtro${todas}" data-linea="" type="button">Todas</button>` + botones;
  mostrar("filtros-mapa", true);
}

$("filtros-mapa").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".filtro");
  if (!btn) return;
  mapaEstado.filtro = btn.dataset.linea;
  pintarMapa();
});

function iniciarSondeoMapa() {
  pararSondeoMapa();
  mapaEstado.timer = setInterval(cargarMapa, PERIODO_MAPA_MS);
}

function pararSondeoMapa() {
  if (mapaEstado.timer) {
    clearInterval(mapaEstado.timer);
    mapaEstado.timer = null;
  }
}

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
mostrarEnlaceAnalisis();

// Carga inicial de alertas en segundo plano para tener el badge listo.
// No arranca el sondeo: solo carga una vez.
cargarAlertas();

// ===========================================================================
// ASISTENTE CONVERSACIONAL
//
// Tres decisiones que gobiernan este bloque:
//
// 1. NO TOCA irA(). El panel vive en un <aside> hermano de <main>, y irA()
//    solo conmuta el hidden de los tres #pantalla-*. La conversación persiste
//    entre Llegada, Alertas y Mapa porque nadie la oculta, no porque haya
//    lógica que la preserve. Menos código que pueda fallar el día 18.
//
// 2. EL HISTORIAL VIVE AQUÍ, NO EN EL SERVIDOR. El navegador envía los dos
//    últimos turnos en cada petición. El servidor no guarda conversaciones de
//    nadie, y el límite de turnos se aplica además en la validación de entrada,
//    así que no depende de que el cliente se porte bien.
//
// 3. LA CLAVE NUNCA ESTÁ AQUÍ. Este fichero lo descarga cualquiera que abra la
//    página. El navegador solo habla con /api/chat; quien tiene la credencial
//    es el servicio, y la lee del entorno. Si en algún momento apareciera una
//    clave en app.js, el diseño estaría mal.
// ===========================================================================

const chatEstado = {
  abierto: false,
  enviando: false,
  // Identificador de sesión del navegador. Solo sirve para que el servidor
  // pueda explicar LA ÚLTIMA predicción de esta pestaña. No identifica a nadie
  // y muere al recargar.
  sesion: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now())),
  historial: [],   // [{rol, texto}], recortado a los 4 últimos turnos
};

const SUGERENCIAS_INICIO = [
  "¿A qué hora llego a Alcalá saliendo de Atocha?",
  "¿Qué incidencias hay ahora?",
  "¿Qué línea va peor en este momento?",
  "¿Qué puedes hacer?",
];

/** Añade una burbuja al hilo y devuelve el elemento, para poder sustituirlo. */
function chatBurbuja(texto, clase) {
  const div = document.createElement("div");
  div.className = `burbuja burbuja--${clase}`;
  div.textContent = texto;
  $("chat-hilo").appendChild(div);
  $("chat-hilo").scrollTop = $("chat-hilo").scrollHeight;
  return div;
}

/** Indicador de espera. La mediana es medio segundo, pero hay cola hasta 2,3 s:
 *  sin esto, una respuesta lenta parece una caída. */
function chatPensando() {
  const div = document.createElement("div");
  div.className = "burbuja burbuja--asistente burbuja--pensando";
  div.innerHTML = "<span></span><span></span><span></span>";
  $("chat-hilo").appendChild(div);
  $("chat-hilo").scrollTop = $("chat-hilo").scrollHeight;
  return div;
}

/** Botón de salto sugerido. Solo aparece con acciones de tipo "sugerir": una
 *  pregunta por incidencias no debe cambiarte de pantalla sin permiso. */
function chatAccion(accion) {
  if (!accion || accion.tipo !== "sugerir") return;
  const boton = document.createElement("button");
  boton.type = "button";
  boton.className = "chat__accion";
  boton.textContent = accion.etiqueta || "Ver más";
  boton.addEventListener("click", () => {
    irA(accion.pantalla);
    boton.disabled = true;
  });
  $("chat-hilo").appendChild(boton);
  $("chat-hilo").scrollTop = $("chat-hilo").scrollHeight;
}

function chatPintarSugerencias(lista) {
  const caja = $("chat-sugerencias");
  caja.innerHTML = "";
  (lista || []).forEach((texto) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chat__sugerencia";
    b.textContent = texto;
    b.addEventListener("click", () => chatEnviar(texto));
    caja.appendChild(b);
  });
}

async function chatEnviar(texto) {
  texto = (texto || "").trim();
  if (!texto || chatEstado.enviando) return;

  chatEstado.enviando = true;
  $("chat-enviar").disabled = true;
  $("chat-entrada").value = "";
  chatPintarSugerencias([]);          // las sugerencias solo guían el arranque
  chatBurbuja(texto, "usuario");
  const espera = chatPensando();

  try {
    const resp = await fetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        texto,
        sesion: chatEstado.sesion,
        historial: chatEstado.historial.slice(-4),
      }),
    });

    espera.remove();

    if (resp.status === 503) {
      // El asistente se ha apagado mientras la página estaba abierta.
      chatBurbuja("El asistente no está disponible ahora mismo. Las pantallas " +
                  "de llegada, alertas y mapa siguen funcionando.", "aviso");
      $("asistente").hidden = true;
      return;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const datos = await resp.json();
    const bloqueada = ["DEGRADADO", "LIMITADO", "SIN_CUOTA"].includes(datos.intencion);
    chatBurbuja(datos.respuesta, bloqueada ? "aviso" : "asistente");

    // Solo NAVEGAR cambia de pantalla por su cuenta: ahí el usuario lo ha
    // pedido de forma explícita. El resto se ofrece como botón.
    if (datos.accion && datos.accion.tipo === "navegar") {
      irA(datos.accion.pantalla);
    } else {
      chatAccion(datos.accion);
    }
    if (datos.sugerencias && datos.sugerencias.length) {
      chatPintarSugerencias(datos.sugerencias);
    }

    chatEstado.historial.push({ rol: "usuario", texto });
    chatEstado.historial.push({ rol: "asistente", texto: datos.respuesta });
    chatEstado.historial = chatEstado.historial.slice(-4);
  } catch (err) {
    espera.remove();
    // Degradar explícito, nunca inventar: el mismo criterio que el resto del
    // sistema. Y se dice qué SÍ funciona, que es lo útil para quien lo lee.
    chatBurbuja("No he podido responder ahora mismo. Las pantallas de llegada, " +
                "alertas y mapa siguen funcionando con normalidad.", "aviso");
  } finally {
    chatEstado.enviando = false;
    $("chat-enviar").disabled = false;
    $("chat-entrada").focus();
  }
}

function chatAbrir() {
  chatEstado.abierto = true;
  $("asistente").classList.add("asistente--abierto");
  $("chat-panel").hidden = false;
  $("chat-lanzador").setAttribute("aria-expanded", "true");
  if (!$("chat-hilo").childElementCount) {
    chatBurbuja("Puedo consultar tu trayecto, las incidencias de la red y el " +
                "estado de cada línea. Pregúntame.", "asistente");
    chatPintarSugerencias(SUGERENCIAS_INICIO);
  }
  $("chat-entrada").focus();
}

function chatCerrar() {
  chatEstado.abierto = false;
  $("asistente").classList.remove("asistente--abierto");
  $("chat-panel").hidden = true;
  $("chat-lanzador").setAttribute("aria-expanded", "false");
}

$("chat-lanzador").addEventListener("click", chatAbrir);
$("chat-cerrar").addEventListener("click", chatCerrar);

$("chat-formulario").addEventListener("submit", (ev) => {
  ev.preventDefault();
  chatEnviar($("chat-entrada").value);
});

// Escape cierra el panel, pero solo si está abierto: si no, dejaría de
// funcionar el Escape de los desplegables de estaciones.
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && chatEstado.abierto) chatCerrar();
});

/** Muestra el lanzador solo si el servicio dice que el asistente está activo.
 *
 *  Con CHAT_HABILITADO a falso, esta comprobación falla en silencio y el
 *  <aside> se queda oculto: la aplicación es indistinguible de la anterior sin
 *  necesidad de desplegar una versión distinta de la web. */
async function chatComprobarDisponible() {
  try {
    const resp = await fetch(`${API}/api/salud`);
    if (!resp.ok) return;
    const salud = await resp.json();
    if (salud.chat && salud.chat.habilitado) $("asistente").hidden = false;
  } catch (err) {
    /* sin asistente; el resto de la aplicación no se entera */
  }
}

chatComprobarDisponible();
