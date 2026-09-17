/* =============================================================================
   i18n.js — Textos de la interfaz en español e inglés.

   Se carga ANTES que app.js, que usa t() para todo texto visible.

   DECISIONES
   - El idioma por defecto es SIEMPRE el español. No se detecta el idioma del
     navegador: un tribunal con el navegador en inglés vería la aplicación en
     inglés sin haberlo pedido, en mitad de una defensa en español.
   - El usuario elige con el selector ES/EN de la cabecera, o con ?lang=en en la
     dirección (útil para enlazar desde el README). La elección se recuerda en
     localStorage.
   - Cambiar de idioma RECARGA la página. Repintar en caliente obligaría a que
     cada pantalla supiera volver a dibujarse con otro idioma; recargar cuesta
     perder la consulta en curso y no puede dejar media pantalla sin traducir.
   - Nombres de estación, códigos de línea y texto de las incidencias NO se
     traducen: son datos del operador, no textos de la aplicación.

   Los bloques ES y EN tienen que tener las mismas claves. verificar.py lo
   comprueba, y también que cada clave usada en app.js e index.html exista.
   ============================================================================= */

"use strict";

const TEXTOS = {
  // ---- ES ----
  es: {
    "doc.titulo": "Cercanías Madrid · Hora estimada de llegada",
    "doc.descripcion": "Predicción de la hora de llegada de los trenes de Cercanías de Madrid.",
    "idioma.grupo": "Idioma",
    "idioma.cambiar": "Cambiar el idioma a",

    "nav.grupo": "Secciones",
    "nav.llegada": "Llegada",
    "nav.alertas": "Alertas",
    "nav.mapa": "Mapa",

    "titulo.llegada": "Llegada estimada",
    "titulo.alertas": "Incidencias",
    "titulo.mapa": "Estado de la red",

    "form.titulo": "Elige tu trayecto",
    "form.desde": "Desde",
    "form.hasta": "Hasta",
    "form.origen": "Estación de origen",
    "form.destino": "Estación de destino",
    "form.intercambiar": "Intercambiar origen y destino",
    "form.buscar": "Ver trenes",
    "form.elige": "Elige una estación de origen y otra de destino.",
    "form.misma": "El origen y el destino son la misma estación.",

    "res.titulo": "Próximos trenes",
    "res.leyenda": "La hora grande es la <strong>llegada estimada</strong> a tu destino: horario oficial más el retraso previsto.",
    "res.cargando": "Consultando trenes y calculando retrasos",
    "res.error": "No se ha podido consultar",
    "res.sin_trenes": "Sin trenes para ese trayecto",
    "res.sin_directos": "No hay trenes directos disponibles ahora mismo.",
    "res.en_hora": "En hora",
    "res.sale": "Sale a las",
    "res.paradas_1": "· 1 parada hasta tu destino",
    "res.paradas_n": "· {n} paradas hasta tu destino",
    "res.llegada": "Llegada estimada",
    "res.margen_aria": "Se espera la llegada entre las {a} y las {b}",
    "res.margen_desde": "Se espera entre las {a}",
    "res.margen_hasta": "y las {b}",
    "res.degradado_transitorio": "No se ha podido usar {x}: esa fuente no respondía al calcular la predicción.",
    "res.degradado_estructural": "La predicción no incorpora {x}: son fuentes que todavía no alimentan al modelo.",
    "res.conjuncion_ni": " ni ",
    "bloque.meteo": "meteorología",
    "bloque.estado_red": "estado de la red",
    "bloque.alertas": "incidencias",
    "bloque.estado_propio": "posición del tren",
    "res.corte_bus": "Servicio suspendido en parte de la {l}. RENFE ha establecido autobuses.",
    "res.corte_supresion": "Hay supresiones de trenes en la {l}.",
    "res.corte_cola": "Este tren puede no circular; la predicción no tiene en cuenta la incidencia.",
    "res.enlace_alertas_1": "{n} incidencia activa en {l}",
    "res.enlace_alertas_n": "{n} incidencias activas en {l}",

    "al.todas": "Todas",
    "al.tu_trayecto": "Tu trayecto",
    "al.activas": "Incidencias activas",
    "al.activas_leyenda": "RENFE las sigue publicando ahora mismo. <strong>Planificada</strong> significa que procede de trabajos previstos, no de un fallo inesperado; está activa igual.",
    "al.resueltas": "Resueltas hoy",
    "al.resueltas_leyenda": "Aparecieron hoy y RENFE ha dejado de publicarlas. Se conservan para ver cómo ha ido el día.",
    "al.accesibilidad": "Accesibilidad",
    "al.cargando": "Cargando incidencias",
    "al.sin": "Sin incidencias",
    "al.sin_red": "No se han registrado incidencias hoy en la red.",
    "al.sin_linea": "Sin incidencias hoy en la línea {l}.",
    "al.caduco": "Los datos de incidencias no están disponibles en este momento. La última actualización fue a las {h}.",
    "al.vacio": "El feed de incidencias de RENFE responde pero sin contenido. Es posible que haya un problema en la fuente.",
    "al.red": "Red",
    "al.planificada": "Planificada",
    "al.desde": "Desde las {h}",
    "al.resuelta": " — resuelta a las {h}",
    "al.leer_mas": "leer más",
    "al.leer_menos": "leer menos",
    "tipo.RESOLUCION": "Vuelta a la normalidad",
    "tipo.SUPRESION": "Supresión",
    "tipo.AVERIA": "Avería",
    "tipo.RETRASO": "Retraso",
    "tipo.SERVICIO_BUS": "Servicio alternativo",
    "tipo.OBRAS": "Obras",
    "tipo.OTRO": "Otra incidencia",
    "impacto.ALTO": "alto",
    "impacto.MEDIO": "medio",
    "impacto.BAJO": "bajo",

    "mapa.titulo": "Mapa de la red en tiempo real",
    "mapa.aria": "Mapa de las líneas de Cercanías y los trenes en circulación",
    "mapa.sin_libreria": "No se ha podido cargar la librería del mapa. Recarga la página.",
    "mapa.atribucion": "Datos de RENFE (CC BY 4.0)",
    "mapa.error": "No se han podido cargar las posiciones. Se reintenta en unos segundos.",
    "mapa.caduco": "Las posiciones no están disponibles en este momento. Última actualización a las {h}.",
    "mapa.vacio": "El feed de posiciones de RENFE responde pero sin contenido. Es posible que haya un problema en la fuente.",
    "mapa.edad": " · actualizado hace {s} s",
    "mapa.trenes_1": "1 tren en circulación{c}",
    "mapa.trenes_n": "{n} trenes en circulación{c}",
    "mapa.ninguno": "No hay trenes de Cercanías Madrid en circulación{c}",
    "mapa.ninguno_linea": "Ningún tren de la línea {l} ahora mismo{c}",
    "tren.sin_estimacion": "Sin estimación de retraso",
    "tren.en_hora": "En hora, según RENFE",
    "tren.retraso": "{m} min de retraso, según RENFE",
    "tren.parado": "Parado en {p}",
    "tren.una_estacion": "una estación",
    "tren.en_marcha": "En marcha hacia {p}",
    "tren.siguiente": "la siguiente parada",
    "tren.sin_linea": "Línea sin identificar",
    "tren.direccion": "Dirección {d}",
    "tren.desconocida": "desconocida",
    "tren.titulo": "{l} dirección {d}",

    "chat.preguntar": "Preguntar",
    "chat.titulo": "Asistente",
    "chat.cerrar": "Cerrar",
    "chat.cerrar_largo": "Cerrar el asistente",
    "chat.placeholder": "Pregunta sobre tu trayecto o la red",
    "chat.escribe": "Escribe tu pregunta",
    "chat.enviar": "Enviar",
    "chat.nota": "Responde solo con datos de esta aplicación. No sustituye a la información oficial de Renfe.",
    "chat.bienvenida": "Puedo consultar tu trayecto, las incidencias de la red y el estado de cada línea. Pregúntame.",
    "chat.no_disponible": "El asistente no está disponible ahora mismo. Las pantallas de llegada, alertas y mapa siguen funcionando.",
    "chat.error": "No he podido responder ahora mismo. Las pantallas de llegada, alertas y mapa siguen funcionando con normalidad.",
    "chat.ver_mas": "Ver más",
    "chat.sug_1": "¿A qué hora llego a Alcalá saliendo de Atocha?",
    "chat.sug_2": "¿Qué incidencias hay ahora?",
    "chat.sug_3": "¿Qué línea va peor en este momento?",
    "chat.sug_4": "¿Qué puedes hacer?",

    "pie.tfm": "Trabajo Fin de Máster · Data Science, Big Data & Business Analytics · UCM",
    "pie.analisis": "Análisis histórico de retrasos (PDF)",
    "pie.alcance_aria": "Alcance de la predicción",
    "pie.alcance": "Se muestran los trenes que ya circulan o salen en los próximos 30\u00a0minutos. Es el margen con el que se entrenó el modelo. Más allá, la predicción no sería fiable.",
    "pie.version": "Horarios GTFS · versión {v}",
  },
  // ---- EN ----
  en: {
    "doc.titulo": "Cercanías Madrid · Estimated arrival time",
    "doc.descripcion": "Arrival time prediction for Madrid Cercanías commuter trains.",
    "idioma.grupo": "Language",
    "idioma.cambiar": "Switch language to",

    "nav.grupo": "Sections",
    "nav.llegada": "Arrival",
    "nav.alertas": "Alerts",
    "nav.mapa": "Map",

    "titulo.llegada": "Estimated arrival",
    "titulo.alertas": "Incidents",
    "titulo.mapa": "Network status",

    "form.titulo": "Choose your journey",
    "form.desde": "From",
    "form.hasta": "To",
    "form.origen": "Origin station",
    "form.destino": "Destination station",
    "form.intercambiar": "Swap origin and destination",
    "form.buscar": "Show trains",
    "form.elige": "Choose an origin and a destination station.",
    "form.misma": "Origin and destination are the same station.",

    "res.titulo": "Next trains",
    "res.leyenda": "The large time is the <strong>estimated arrival</strong> at your destination: official timetable plus predicted delay.",
    "res.cargando": "Looking up trains and calculating delays",
    "res.error": "The query could not be completed",
    "res.sin_trenes": "No trains for this journey",
    "res.sin_directos": "No direct trains available right now.",
    "res.en_hora": "On time",
    "res.sale": "Departs at",
    "res.paradas_1": "· 1 stop to your destination",
    "res.paradas_n": "· {n} stops to your destination",
    "res.llegada": "Estimated arrival",
    "res.margen_aria": "Arrival expected between {a} and {b}",
    "res.margen_desde": "Expected between {a}",
    "res.margen_hasta": "and {b}",
    "res.degradado_transitorio": "Could not use {x} data: that source was not responding when the prediction was calculated.",
    "res.degradado_estructural": "The prediction does not include {x} data: these sources do not feed the model yet.",
    "res.conjuncion_ni": " or ",
    "bloque.meteo": "weather",
    "bloque.estado_red": "network status",
    "bloque.alertas": "incident",
    "bloque.estado_propio": "train position",
    "res.corte_bus": "Service suspended on part of line {l}. RENFE has arranged replacement buses.",
    "res.corte_supresion": "Trains are being cancelled on line {l}.",
    "res.corte_cola": "This train may not run; the prediction does not take the incident into account.",
    "res.enlace_alertas_1": "{n} active incident on {l}",
    "res.enlace_alertas_n": "{n} active incidents on {l}",

    "al.todas": "All",
    "al.tu_trayecto": "Your journey",
    "al.activas": "Active incidents",
    "al.activas_leyenda": "RENFE is still publishing these right now. <strong>Planned</strong> means it comes from scheduled works, not an unexpected failure; it is active all the same.",
    "al.resueltas": "Resolved today",
    "al.resueltas_leyenda": "They appeared today and RENFE has stopped publishing them. They are kept to show how the day has gone.",
    "al.accesibilidad": "Accessibility",
    "al.cargando": "Loading incidents",
    "al.sin": "No incidents",
    "al.sin_red": "No incidents have been recorded on the network today.",
    "al.sin_linea": "No incidents today on line {l}.",
    "al.caduco": "Incident data is not available right now. Last update at {h}.",
    "al.vacio": "RENFE's incident feed is responding but has no content. There may be a problem at the source.",
    "al.red": "Network",
    "al.planificada": "Planned",
    "al.desde": "Since {h}",
    "al.resuelta": " · resolved at {h}",
    "al.leer_mas": "read more",
    "al.leer_menos": "read less",
    "tipo.RESOLUCION": "Service restored",
    "tipo.SUPRESION": "Cancellation",
    "tipo.AVERIA": "Breakdown",
    "tipo.RETRASO": "Delay",
    "tipo.SERVICIO_BUS": "Replacement service",
    "tipo.OBRAS": "Engineering works",
    "tipo.OTRO": "Other incident",
    "impacto.ALTO": "high",
    "impacto.MEDIO": "medium",
    "impacto.BAJO": "low",

    "mapa.titulo": "Real-time network map",
    "mapa.aria": "Map of the Cercanías lines and the trains currently running",
    "mapa.sin_libreria": "The map library could not be loaded. Reload the page.",
    "mapa.atribucion": "RENFE data (CC BY 4.0)",
    "mapa.error": "Train positions could not be loaded. Retrying in a few seconds.",
    "mapa.caduco": "Train positions are not available right now. Last update at {h}.",
    "mapa.vacio": "RENFE's position feed is responding but has no content. There may be a problem at the source.",
    "mapa.edad": " · updated {s} s ago",
    "mapa.trenes_1": "1 train running{c}",
    "mapa.trenes_n": "{n} trains running{c}",
    "mapa.ninguno": "No Cercanías Madrid trains running{c}",
    "mapa.ninguno_linea": "No trains on line {l} right now{c}",
    "tren.sin_estimacion": "No delay estimate",
    "tren.en_hora": "On time, according to RENFE",
    "tren.retraso": "{m} min late, according to RENFE",
    "tren.parado": "Stopped at {p}",
    "tren.una_estacion": "a station",
    "tren.en_marcha": "Heading to {p}",
    "tren.siguiente": "the next stop",
    "tren.sin_linea": "Unidentified line",
    "tren.direccion": "Towards {d}",
    "tren.desconocida": "unknown",
    "tren.titulo": "{l} towards {d}",

    "chat.preguntar": "Ask",
    "chat.titulo": "Assistant",
    "chat.cerrar": "Close",
    "chat.cerrar_largo": "Close the assistant",
    "chat.placeholder": "Ask about your journey or the network",
    "chat.escribe": "Type your question",
    "chat.enviar": "Send",
    "chat.nota": "Answers only with data from this app. It does not replace Renfe's official information.",
    "chat.bienvenida": "I can check your journey, incidents on the network and the status of each line. Ask me.",
    "chat.no_disponible": "The assistant is not available right now. The arrival, alerts and map screens are still working.",
    "chat.error": "I couldn't answer right now. The arrival, alerts and map screens are still working normally.",
    "chat.ver_mas": "See more",
    "chat.sug_1": "When do I get to Alcalá if I leave from Atocha?",
    "chat.sug_2": "Are there any incidents right now?",
    "chat.sug_3": "Which line is doing worst right now?",
    "chat.sug_4": "What can you do?",

    "pie.tfm": "Master's Thesis · Data Science, Big Data & Business Analytics · UCM",
    "pie.analisis": "Historical delay analysis (PDF, in Spanish)",
    "pie.alcance_aria": "Prediction scope",
    "pie.alcance": "Only trains that are already running or depart within the next 30\u00a0minutes are shown. That is the window the model was trained on. Beyond it, the prediction would not be reliable.",
    "pie.version": "GTFS timetable · version {v}",
  },
  // ---- FIN ----
};

const IDIOMAS_DISPONIBLES = Object.keys(TEXTOS);
const CLAVE_ALMACEN = "idioma";

/** Idioma elegido: ?lang= en la URL, si no el recordado, si no español. */
const IDIOMA = (() => {
  const deUrl = new URLSearchParams(location.search).get("lang");
  if (deUrl && IDIOMAS_DISPONIBLES.includes(deUrl)) {
    recordarIdioma(deUrl);
    return deUrl;
  }
  let guardado = null;
  try {
    guardado = localStorage.getItem(CLAVE_ALMACEN);
  } catch {
    /* navegación privada o almacenamiento bloqueado: se queda en español */
  }
  return IDIOMAS_DISPONIBLES.includes(guardado) ? guardado : "es";
})();

function recordarIdioma(idioma) {
  try {
    localStorage.setItem(CLAVE_ALMACEN, idioma);
  } catch {
    /* sin almacenamiento, el idioma dura lo que dure la página */
  }
}

/** Texto de la clave en el idioma activo, con {variables} sustituidas.
 *  Si falta en inglés cae al español, y si falta en los dos devuelve la clave:
 *  un texto ausente se ve en pantalla en vez de romper la página. */
function t(clave, variables = {}) {
  const plantilla = TEXTOS[IDIOMA][clave] ?? TEXTOS.es[clave] ?? clave;
  return plantilla.replace(/\{(\w+)\}/g, (_, nombre) =>
    variables[nombre] !== undefined ? String(variables[nombre]) : `{${nombre}}`
  );
}

/** Configuración regional para formatear horas. en-GB mantiene las 24 horas. */
const LOCALE = IDIOMA === "en" ? "en-GB" : "es-ES";

/** Traduce los textos fijos de index.html.
 *  data-i18n            -> textContent
 *  data-i18n-html       -> innerHTML (solo claves propias, nunca datos externos)
 *  data-i18n-placeholder, data-i18n-title, data-i18n-aria-label -> atributo */
function traducirPagina() {
  document.documentElement.lang = IDIOMA;
  document.title = t("doc.titulo");
  const meta = document.querySelector('meta[name="description"]');
  if (meta) meta.setAttribute("content", t("doc.descripcion"));

  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-html]").forEach((el) => {
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  const atributos = { i18nPlaceholder: "placeholder", i18nTitle: "title",
                      i18nAriaLabel: "aria-label" };
  Object.entries(atributos).forEach(([dato, atributo]) => {
    const selector = `[data-${dato.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}]`;
    document.querySelectorAll(selector).forEach((el) => {
      el.setAttribute(atributo, t(el.dataset[dato]));
    });
  });

  // Selector de idioma: marca el activo y conecta el cambio.
  document.querySelectorAll("[data-idioma]").forEach((boton) => {
    const activo = boton.dataset.idioma === IDIOMA;
    boton.setAttribute("aria-pressed", String(activo));
    boton.title = `${t("idioma.cambiar")} ${boton.dataset.idioma.toUpperCase()}`;
    boton.addEventListener("click", () => {
      if (activo) return;
      recordarIdioma(boton.dataset.idioma);
      // Se quita ?lang= para que no pise la elección recién hecha al recargar.
      const url = new URL(location.href);
      url.searchParams.delete("lang");
      location.replace(url.toString());
    });
  });
}

traducirPagina();
