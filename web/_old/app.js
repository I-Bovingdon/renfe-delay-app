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
    // Si la hora elegida ya pasó hace rato, se entiende que es de mañana.
    // Sin esto, pedir "las 07:30" a las 21:00 no devolvería ningún tren.
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

/** Pinta la interfaz con el color de la línea del trayecto. */
function aplicarColorDeLinea(color) {
  document.documentElement.style.setProperty("--linea", color || "var(--tinta)");
}

function mostrar(id, visible) {
  $(id).hidden = !visible;
}

// ---------------------------------------------------------------------------
// Autocompletado de estaciones
// ---------------------------------------------------------------------------

/**
 * Conecta un campo de texto con su lista de sugerencias.
 * Implementa el patrón combobox: flechas para moverse, Enter para elegir,
 * Escape para cerrar. Sin esto no se puede usar con teclado.
 */
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
    // Si ambos extremos comparten una sola línea, ya se puede vestir la interfaz.
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
      return cerrar(); // Un fallo del autocompletado no debe romper el formulario.
    }

    if (!opciones.length) return cerrar();

    lista.innerHTML = opciones
      .map(
        (e, i) => `
        <li class="sugerencia" role="option" id="${idLista}-${i}" aria-selected="false">
          <span class="sugerencia__nombre">${e.nombre}</span>
          <span class="insignias">${e.lineas.map((l) => `<span class="insignia">${l}</span>`).join("")}</span>
        </li>`
      )
      .join("");

    lista.querySelectorAll(".sugerencia").forEach((li, i) => {
      li.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); // evita que el blur cierre la lista antes del clic
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

/** Color de una línea, con un gris de reserva si el catálogo no lo trae. */
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

/**
 * Clasifica el retraso en tres tramos con sentido para el viajero.
 * Los umbrales están aquí y en ningún otro sitio: si se discuten, se cambian
 * una vez. Cada tramo lleva SIEMPRE icono y texto, nunca solo color: hay gente
 * que no distingue el verde del rojo y el resultado se proyecta en una defensa.
 */
function clasificarRetraso(segundos) {
  const min = Math.round(segundos / 60);
  if (min <= 2) return { clase: "puntual", icono: "●", texto: "En hora", min };
  if (min <= 10) return { clase: "leve", icono: "▲", texto: `+${min} min`, min };
  return { clase: "alto", icono: "■", texto: `+${min} min`, min };
}

/**
 * Banda de llegada: dibuja dónde se espera al tren entre el percentil 10 y el 90.
 * La escala arranca en la hora teórica y llega hasta el p90 con un margen, de
 * modo que bandas anchas se ven anchas: la incertidumbre tiene que notarse.
 */
function pintarMargen(tramo) {
  if (!tramo.retraso_s.con_intervalo) return "";

  const { p10, p50, p90 } = tramo.retraso_s;
  const tope = Math.max(p90 * 1.15, 300); // al menos 5 min de escala
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

/** Pinta la lista de trenes con su hora estimada de llegada. */
function pintarResultado(datos) {
  if (!datos.opciones.length) {
    avisar("Sin trenes para ese trayecto", datos.aviso || "Prueba con otra hora.");
    return;
  }

  // Cada tarjeta se pinta con SU línea. El formulario solo se tiñe si todos los
  // trenes van por la misma: Atocha–Alcalá lo cubren el C2 y el C7, y teñir la
  // interfaz del color del primero sería sencillamente falso.
  const lineas = new Set(datos.opciones.map((op) => op.tramos[0].line_id));
  aplicarColorDeLinea(lineas.size === 1 ? colorDeLinea([...lineas][0]) : null);

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

  // Aviso honesto cuando falta alguna fuente: el sistema dice con qué información
  // trabaja en lugar de disimularlo. Es un argumento de defensa, no un defecto.
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

  mostrar("panel-resultado", true);
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
  // Enter desde cualquier campo lanza la consulta, si no hay lista abierta.
  if (ev.key === "Enter" && !ev.target.closest(".sugerencias") &&
      $("sugerencias-origen").hidden && $("sugerencias-destino").hidden) {
    consultar();
  }
});

cargarColores();
