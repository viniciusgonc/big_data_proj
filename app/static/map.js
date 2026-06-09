const CORES = {
  "Fluído":             "#2ECC71",
  "Lentidão/Trânsito":  "#F1C40F",
  "Parado/Garagem":     "#E74C3C",
};

const ANIMATION_DURATION = 28_000; // ms — anima suavemente em ~28s (antes do próximo refresh de 30s)

let mapa, heatLayer;
let marcadores    = L.layerGroup();  // ônibus em tempo real (stream)
let rotaHistLayer = L.layerGroup();  // nuvem de pontos históricos
let modoAtual     = null;            // { tipo: "todos"|"linha"|"proximos"|"rota"|"calor", ... }
let userMarker    = null;
let raioCircle    = null;
let userLat       = null;
let userLng       = null;

// ── Streaming SSE — fonte única da verdade ───────────────────────────────────
// Mapa de marcadores ativos: ordem → { marker, lat, lon }
let streamMarkers = {};
let sseSource     = null;

// ── Relógio em tempo real ────────────────────────────────────────────────────
// Atualiza #stat-hora a cada segundo, independentemente dos dados da API.
setInterval(() => {
  const el = document.getElementById("stat-hora");
  if (el) el.textContent = new Date().toLocaleTimeString("pt-BR");
}, 1000);

// ── Inicialização do mapa ────────────────────────────────────────────────────

function initMap() {
  mapa = L.map("map", { zoomControl: false, preferCanvas: true }).setView([-22.9068, -43.1729], 11);

  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
    attribution: "© OpenStreetMap © CARTO",
    maxZoom: 19,
  }).addTo(mapa);

  L.control.zoom({ position: "bottomright" }).addTo(mapa);
  marcadores.addTo(mapa);
  rotaHistLayer.addTo(mapa);

  // Clique no mapa para simular localização
  mapa.on("click", function (e) {
    userLat = e.latlng.lat;
    userLng = e.latlng.lng;

    if (userMarker) userMarker.remove();
    userMarker = L.circleMarker([userLat, userLng], {
      radius: 10, color: "#1A6FBF", fillColor: "#1A6FBF",
      fillOpacity: 0.9, weight: 3,
    }).addTo(mapa).bindPopup("📍 Localização simulada").openPopup();

    mostrarInfo(`📍 Localização definida: ${userLat.toFixed(5)}, ${userLng.toFixed(5)}`);
  });

  carregarSnapshot();
  iniciarStream(); // SSE inicia e permanece ativo para sempre
}

// ── Snapshot de métricas ─────────────────────────────────────────────────────

async function carregarSnapshot() {
  try {
    const res = await fetch("/api/snapshot");
    const d   = await res.json();

    document.getElementById("stat-onibus").textContent  = d.onibus_ativos?.toLocaleString("pt-BR") ?? "—";
    document.getElementById("stat-linhas").textContent  = d.linhas_ativas?.toLocaleString("pt-BR") ?? "—";
    document.getElementById("stat-vel").textContent     = d.vel_media_kmh ? `${d.vel_media_kmh} km/h` : "—";
    document.getElementById("stat-parados").textContent = d.pct_parados   ? `${d.pct_parados}%` : "—";
    // Nota: #stat-hora é gerenciado pelo relógio de 1s acima, não pelos dados da API
  } catch (e) {
    console.warn("Erro ao carregar snapshot:", e);
  }

  await popularSelectLinhas();
}

// ── Dropdown de linhas ───────────────────────────────────────────────────────

async function popularSelectLinhas() {
  const select = document.getElementById("select-linha");
  if (!select || select.options.length > 2) return;

  try {
    const res = await fetch("/api/linhas");
    if (!res.ok) { console.warn("/api/linhas retornou:", res.status); return; }
    const d = await res.json();
    d.linhas.forEach(l => {
      const opt = document.createElement("option");
      opt.value = l;
      opt.textContent = l;
      select.appendChild(opt);
    });
    console.log(`✅ ${d.linhas.length} linhas carregadas no dropdown`);
  } catch (e) {
    console.warn("Erro ao carregar lista de linhas:", e);
  }
}

function selecionarLinhaDropdown(linha) {
  if (!linha) return;
  document.getElementById("input-linha").value = linha;
  buscarLinha();
}

// ── Modo 1: Onde está minha linha (filtro local no SSE) ──────────────────────

function buscarLinha() {
  const input = document.getElementById("input-linha").value.trim().toUpperCase();

  // Se o usuário selecionou "Todas as linhas" ou enviou vazio, reseta para mostrar a frota toda
  if (!input || input === "TODAS") {
    limparMarcadoresStream();
    limparRotaHist();
    if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }
    
    modoAtual = { tipo: "todos" };
    mostrarInfo("🌍 Exibindo todas as linhas em tempo real...");
    document.getElementById("input-linha").value = ""; // Limpa o campo para ficar clean
    return;
  }

  // Comportamento normal para buscar uma linha específica
  limparMarcadoresStream();
  limparRotaHist();
  if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }

  modoAtual = { tipo: "linha", valor: input };
  mostrarInfo(`🚌 Aguardando dados da linha ${input}...`);
}

// ── Modo 2: Rota histórica como nuvem de pontos ──────────────────────────────

async function identificarRota() {
  const select = document.getElementById("select-rota");
  const linha  = select ? select.value : document.getElementById("input-linha").value.trim().toUpperCase();
  if (!linha) return mostrarErro("Selecione ou digite uma linha para identificar a rota.");

  // O SSE continua ativo — apenas limpa camadas visuais anteriores
  limparMarcadoresStream();
  limparRotaHist();
  if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }

  modoAtual = { tipo: "rota", valor: linha };
  mostrarInfo(`🗺️ Carregando nuvem de pontos da linha ${linha}...`);

  try {
    const res = await fetch(`/api/rota/${encodeURIComponent(linha)}`);

    if (res.status === 503) {
      return mostrarErro("Arquivo histórico não encontrado. Execute a pipeline silver primeiro.");
    }
    if (res.status === 404) {
      return mostrarErro(`Nenhum histórico encontrado para a linha '${linha}'.`);
    }

    const d = await res.json();

    if (!d.pontos || d.pontos.length === 0) {
      return mostrarErro(`Nenhum ponto válido para a linha ${linha}.`);
    }

    // ── Renderiza nuvem de pontos ──────────────────────────────────────────
    const BATCH = 500; // insere em lotes para não travar o navegador
    let i = 0;

    function inserirLote() {
      const fim = Math.min(i + BATCH, d.pontos.length);
      for (; i < fim; i++) {
        const [lat, lon] = d.pontos[i];
        L.circleMarker([lat, lon], {
          radius: 2,
          color: "#F28C28",
          fillColor: "#F28C28",
          fillOpacity: 0.35,
          weight: 0,
          interactive: false, // pontos estáticos não precisam capturar eventos
        }).addTo(rotaHistLayer);
      }

      if (i < d.pontos.length) {
        // Cede o controle ao navegador entre lotes (não trava a UI)
        requestAnimationFrame(inserirLote);
      } else {
        // Todos os pontos inseridos — ajusta zoom e exibe info
        const bounds = rotaHistLayer.getBounds();
        if (bounds.isValid()) mapa.fitBounds(bounds, { padding: [40, 40] });
        mostrarInfo(`🗺️ Linha ${d.linha} — ${d.total_pontos.toLocaleString("pt-BR")} pontos históricos`);
      }
    }

    requestAnimationFrame(inserirLote);

  } catch (e) {
    mostrarErro("Erro ao carregar rota histórica.");
    console.error(e);
  }
}

// ── Modo 3: Ônibus próximos (filtro local no SSE) ────────────────────────────

async function buscarProximos() {
  const raio = parseInt(document.getElementById("input-raio").value) || 500;

  if (userLat && userLng) {
    _ativarModoProximos(userLat, userLng, raio);
    return;
  }

  mostrarInfo("📍 Obtendo sua localização...");

  if (!navigator.geolocation) {
    return mostrarErro("Clique no mapa para definir sua localização.");
  }

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      userLat = pos.coords.latitude;
      userLng = pos.coords.longitude;
      _ativarModoProximos(userLat, userLng, raio);
    },
    () => mostrarErro("Permissão negada. Clique no mapa para simular sua localização.")
  );
}

function _ativarModoProximos(lat, lon, raio) {
  limparMarcadoresStream();
  limparRotaHist();
  if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }
  if (raioCircle) { raioCircle.remove(); raioCircle = null; }

  modoAtual = { tipo: "proximos", lat, lon, raio };

  if (userMarker) userMarker.remove();
  userMarker = L.circleMarker([lat, lon], {
    radius: 10, color: "#1A6FBF", fillColor: "#1A6FBF",
    fillOpacity: 0.9, weight: 3,
  }).addTo(mapa).bindPopup("📍 Você está aqui").openPopup();

  raioCircle = L.circle([lat, lon], {
    radius: raio, color: "#1A6FBF", fillOpacity: 0.05, weight: 1,
  }).addTo(mapa);

  mapa.setView([lat, lon], 15);
  mostrarInfo(`🔍 Aguardando ônibus em ${raio}m...`);
}

// ── Modo 4: Mapa de calor ─────────────────────────────────────────────────────

async function mostrarMapaCalor() {
  limparMarcadoresStream();
  limparRotaHist();
  if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }

  modoAtual = { tipo: "calor" };
  mostrarInfo("🔥 Carregando mapa de calor...");

  try {
    const res = await fetch("/api/mapa-calor?max_pontos=10000");
    const d   = await res.json();

    heatLayer = L.heatLayer(d.pontos, {
      radius: 12, blur: 18, maxZoom: 14,
      gradient: { 0.4: "#1A6FBF", 0.65: "#F1C40F", 1: "#E74C3C" },
    }).addTo(mapa);

    mapa.setView([-22.9068, -43.1729], 11);
    mostrarInfo(`🔥 ${d.total_pontos.toLocaleString("pt-BR")} pontos de lentidão mapeados`);
  } catch (e) {
    mostrarErro("Erro ao carregar mapa de calor.");
    console.error(e);
  }
}

// ── SSE Streaming — conexão permanente, filtros locais ───────────────────────

function iniciarStream() {
  if (sseSource) sseSource.close();

  sseSource = new EventSource("/api/stream");

  sseSource.onmessage = (event) => {
    try {
      const geojson = JSON.parse(event.data);
      _processarMensagemSSE(geojson);
    } catch (e) {
      console.warn("Erro ao processar SSE:", e);
    }
  };

  sseSource.onerror = () => {
    console.warn("SSE desconectado, reconectando em 10s...");
    sseSource.close();
    setTimeout(iniciarStream, 10_000);
  };
}

function _processarMensagemSSE(geojson) {
  const modo = modoAtual;

  // Se for mapa de calor, não exibe os ônibus individuais
  if (modo && modo.tipo === "calor") return;

  let features = geojson.features;

  // Se for modo "linha" OU "rota", queremos filtrar e VER OS ÔNIBUS se movendo
  if (modo && (modo.tipo === "linha" || modo.tipo === "rota")) {
    features = features.filter(f =>
      f.properties.linha?.toUpperCase() === modo.valor
    );

    // Só atualiza o texto do painel se estiver buscando especificamente a linha
    if (modo.tipo === "linha" && features.length > 0) {
      mostrarInfo(`🚌 Linha ${modo.valor} — ${features.length} veículo(s) ao vivo`);
    }
    
  } else if (modo && modo.tipo === "proximos") {
    // Lógica de raio baseada na sua localização
    const { lat, lon, raio } = modo;
    const R = 6_371_000;
    const latR = lat * Math.PI / 180;

    features = features.filter(f => {
      const [flon, flat] = f.geometry.coordinates;
      const dlat = (flat - lat) * Math.PI / 180;
      const dlon = (flon - lon) * Math.PI / 180;
      const a = Math.sin(dlat / 2) ** 2 +
                Math.cos(latR) * Math.cos(flat * Math.PI / 180) * Math.sin(dlon / 2) ** 2;
      return R * 2 * Math.asin(Math.sqrt(a)) <= raio;
    });

    mostrarInfo(`🔍 ${features.length} ônibus em ${modo.raio}m de você`);
  }

  // Devolve os marcadores para o mapa serem animados
  atualizarStreamMarkers({ ...geojson, features });
}

// ── Animação suave de marcadores (SSE) ───────────────────────────────────────

function atualizarStreamMarkers(geojson) {
  const novosOrdens = new Set();

  geojson.features.forEach(f => {
    const { coordinates } = f.geometry;
    const { linha, ordem, velocidade, status } = f.properties;
    const novaLat = coordinates[1];
    const novaLon = coordinates[0];
    const cor = CORES[status] || "#AAAAAA";
    novosOrdens.add(ordem);

    if (streamMarkers[ordem]) {
      // Ônibus já existe — anima suavemente até a nova posição
      const { marker, lat: latAnt, lon: lonAnt } = streamMarkers[ordem];
      animarMarcador(marker, latAnt, lonAnt, novaLat, novaLon, cor);
      streamMarkers[ordem].lat = novaLat;
      streamMarkers[ordem].lon = novaLon;
      marker.setPopupContent(_popupHTML(linha, ordem, velocidade, status, cor));
    } else {
      // Ônibus novo — cria marcador
      const marker = L.circleMarker([novaLat, novaLon], {
        radius: 6, color: cor, fillColor: cor, fillOpacity: 0.85, weight: 2,
      }).bindPopup(_popupHTML(linha, ordem, velocidade, status, cor));
      marker.addTo(mapa);
      streamMarkers[ordem] = { marker, lat: novaLat, lon: novaLon };
    }
  });

  // Remove ônibus que saíram do snapshot (ou do filtro atual)
  Object.keys(streamMarkers).forEach(ordem => {
    if (!novosOrdens.has(ordem)) {
      streamMarkers[ordem].marker.remove();
      delete streamMarkers[ordem];
    }
  });
}

function animarMarcador(marker, latAnt, lonAnt, latNov, lonNov, cor) {
  const passos    = 60;
  const intervalo = ANIMATION_DURATION / passos;
  let   passo     = 0;

  if (marker._animTimer) clearInterval(marker._animTimer);

  marker._animTimer = setInterval(() => {
    passo++;
    const t   = passo / passos;
    const lat = latAnt + (latNov - latAnt) * t;
    const lon = lonAnt + (lonNov - lonAnt) * t;
    marker.setLatLng([lat, lon]);
    marker.setStyle({ color: cor, fillColor: cor });
    if (passo >= passos) clearInterval(marker._animTimer);
  }, intervalo);
}

// ── Renderização GeoJSON (marcadores estáticos — mantido para compatibilidade) ──

function renderizarGeoJSON(geojson, mostrarDistancia = false) {
  marcadores.clearLayers();

  geojson.features.forEach((f) => {
    const { coordinates } = f.geometry;
    const { linha, ordem, velocidade, status, distancia_m } = f.properties;
    const cor = CORES[status] || "#AAAAAA";

    const marcador = L.circleMarker([coordinates[1], coordinates[0]], {
      radius: 7, color: cor, fillColor: cor, fillOpacity: 0.85, weight: 2,
    });

    let html = _popupHTML(linha, ordem, velocidade, status, cor);
    if (mostrarDistancia && distancia_m !== null) {
      html = html.replace("</div>", `<div style="margin-top:4px;color:#888">📍 ${distancia_m}m de você</div></div>`);
    }

    marcador.bindPopup(html);
    marcadores.addLayer(marcador);
  });
}

function _popupHTML(linha, ordem, velocidade, status, cor) {
  return `
    <div style="font-family:'DM Sans',sans-serif;min-width:160px;">
      <div style="font-size:16px;font-weight:700;color:${cor}">Linha ${linha}</div>
      <div style="font-size:12px;color:#666;margin-top:2px;">Veículo: ${ordem}</div>
      <hr style="margin:6px 0;border-color:#eee">
      <div>⏲ <b>${velocidade} km/h</b></div>
      <div style="margin-top:4px;color:${cor}">● ${status}</div>
    </div>`;
}

function ajustarZoom(geojson) {
  if (!geojson.features.length) return;
  const bounds = L.geoJSON(geojson).getBounds();
  if (bounds.isValid()) mapa.fitBounds(bounds, { padding: [40, 40] });
}

// ── Utilitários ───────────────────────────────────────────────────────────────

function limparMarcadoresStream() {
  Object.values(streamMarkers).forEach(({ marker }) => {
    if (marker._animTimer) clearInterval(marker._animTimer);
    marker.remove();
  });
  streamMarkers = {};
  marcadores.clearLayers();
}

function limparRotaHist() {
  rotaHistLayer.clearLayers();
}

function limparTudo() {
  limparMarcadoresStream();
  limparRotaHist();
  if (heatLayer) { mapa.removeLayer(heatLayer); heatLayer = null; }
  if (userMarker) { userMarker.remove(); userMarker = null; }
  if (raioCircle) { raioCircle.remove(); raioCircle = null; }
  document.getElementById("info-bar").textContent = "";
}

function mostrarInfo(msg) {
  const el = document.getElementById("info-bar");
  el.textContent = msg;
  el.style.color = "#2ECC71";
}

function mostrarErro(msg) {
  const el = document.getElementById("info-bar");
  el.textContent = msg;
  el.style.color = "#E74C3C";
}

function setLoading(_ativo) { /* no-op: loadings removidos */ }

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  initMap();

  document.getElementById("input-linha").addEventListener("keydown", (e) => {
    if (e.key === "Enter") buscarLinha();
  });
});