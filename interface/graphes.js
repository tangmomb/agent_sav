// Draws what agent_sav/graph_map.py reads in the code. Nothing about the graphs is written here.

const WIDTH = 148, HEIGHT = 56, END_WIDTH = 62, END_HEIGHT = 30;
const OFFSETS = {conversation: 36, case: 610};
const BAND = {y: 482, height: 84};
const VIEWS = {all: "0 0 1270 1100", conversation: "0 0 1270 570", case: "0 478 1270 625"};
const TITLES = {conversation: "Graphe conversation", case: "Graphe dossier"};
const NOTES = {conversation: "un thread par chat", case: "un thread par demande, sans modèle"};

const panel = document.querySelector("#panel");
const canvas = document.querySelector("#canvas");
const matrix = document.querySelector("#matrix");
const stage = document.querySelector(".stage");
let data, graphs, svg;

// Small helpers: text always goes through textContent.

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (name === "onclick") node.addEventListener("click", value);
    else if (value !== undefined && value !== false) node.setAttribute(name, value === true ? "" : value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

function draw(tag, attributes = {}, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

const nodeOf = (graph, id) => graphs[graph].nodes.find((node) => node.id === id);
const isEnd = (node) => node.id.startsWith("__");
const label = (id) => ({__start__: "début", __end__: "fin"})[id] ?? id;
const allSql = (node) => [...(node.sql ?? []), ...(node.router?.sql ?? [])];
const unique = (values) => [...new Set(values)];
const writesOf = (node) => unique(allSql(node).flatMap((each) => each.writes));
const readsOf = (node) => unique(allSql(node).flatMap((each) => each.reads));
const keysWritten = (node) => unique((node.updates ?? []).flatMap((update) => update.keys.map((key) => key.key)));
const keysRead = (node) => unique([...(node.reads ?? []), ...(node.router?.reads ?? [])]);
const exitsOf = (node) => [...node.exits].sort((a, b) => (a.target === "__end__") - (b.target === "__end__"));
const everyNode = () => Object.values(graphs).flatMap((graph) => graph.nodes.map((node) => ({graph: graph.id, node})));

// Drawing the graphs.

const center = (graph, node) => {
  const [x, y] = node.position ?? [640, 20];
  return [x, y + OFFSETS[graph]];
};

function port(graph, node, side, offset) {
  const [x, y] = center(graph, node);
  const [w, h] = isEnd(node) ? [END_WIDTH, END_HEIGHT] : [WIDTH, HEIGHT];
  return {l: [x - w / 2, y + offset], r: [x + w / 2, y + offset], t: [x + offset, y - h / 2], b: [x + offset, y + h / 2]}[side];
}

// Straight lines with rounded corners.
function pathThrough(points, radius = 10) {
  let path = `M${points[0]}`;
  for (let i = 1; i < points.length - 1; i++) {
    const [before, corner, after] = [points[i - 1], points[i], points[i + 1]];
    const toward = (from, to) => {
      const length = Math.hypot(to[0] - from[0], to[1] - from[1]);
      const step = Math.min(radius, length / 2) / (length || 1);
      return [from[0] + (to[0] - from[0]) * step, from[1] + (to[1] - from[1]) * step];
    };
    path += `L${toward(corner, before)}Q${corner} ${toward(corner, after)}`;
  }
  return `${path}L${points.at(-1)}`;
}

function edgePath(graph, source, leaving) {
  const target = nodeOf(graph, leaving.target);
  if (!leaving.route) return pathThrough([center(graph, source), center(graph, target)]);
  const [side, offset, corners, arrival, arrivalOffset] = leaving.route;
  return pathThrough([
    port(graph, source, side, offset), ...corners.map(([x, y]) => [x, y + OFFSETS[graph]]), port(graph, target, arrival, arrivalOffset),
  ]);
}

const tableCenter = (index) => [86 + index * 137, BAND.y + 50];

function pressable(node, action, name) {
  node.setAttribute("tabindex", "0");
  node.setAttribute("role", "button");
  node.setAttribute("aria-label", name);
  node.addEventListener("click", action);
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      action();
    }
  });
  return node;
}

function drawNode(graph, node) {
  const [x, y] = center(graph, node);
  const [w, h] = isEnd(node) ? [END_WIDTH, END_HEIGHT] : [WIDTH, HEIGHT];
  const kinds = ["node", isEnd(node) && "end", node.pause && "pause", node.model && "model"].filter(Boolean);
  const shape = draw("g", {class: kinds.join(" "), "data-node": `${graph}/${node.id}`},
    draw("rect", {x: x - w / 2, y: y - h / 2, width: w, height: h, rx: isEnd(node) ? h / 2 : 9}));
  const name = draw("text", {x, y: isEnd(node) || !subtitle(node) ? y : y - 8});
  name.textContent = label(node.id);
  shape.append(name);
  if (!isEnd(node) && subtitle(node)) {
    const sub = draw("text", {x, y: y + 13, class: "sub"});
    sub.textContent = subtitle(node);
    shape.append(sub);
  }
  return isEnd(node) ? shape : pressable(shape, () => go(`node/${graph}/${node.id}`), `Nœud ${node.id}`);
}

function subtitle(node) {
  if (node.pause) return "attend une reprise";
  if (node.model) return "modèle";
  const count = (verb, tables) => `${verb} ${tables} table${tables > 1 ? "s" : ""}`;
  if (writesOf(node).length) return count("écrit", writesOf(node).length);
  return readsOf(node).length ? count("lit", readsOf(node).length) : "état seulement";
}

function drawAll() {
  svg = draw("svg", {viewBox: VIEWS.all, role: "group", "aria-label": "Les deux graphes et la base"});
  const defs = draw("defs");
  for (const [id, color] of [["arrow", "#8a8a96"], ["arrow-on", "#b3c8ff"], ["arrow-bridge", "#f0a35e"]]) {
    defs.append(draw("marker", {id, viewBox: "0 0 8 8", refX: 7, refY: 4, markerWidth: 8, markerHeight: 8, orient: "auto-start-reverse", markerUnits: "userSpaceOnUse"},
      draw("path", {d: "M0 0L8 4L0 8z", fill: color})));
  }
  svg.append(defs);

  svg.append(draw("rect", {class: "band", x: 8, y: BAND.y, width: 1254, height: BAND.height, rx: 10}));
  const title = draw("text", {class: "graph-note", x: 20, y: BAND.y + 19});
  title.textContent = "Base SQL, partagée : les deux graphes ne s’appellent jamais, ils se parlent par ces tables";
  svg.append(title);

  const index = (name) => data.tables.findIndex((table) => table.name === name);
  for (const bridge of data.bridges) {
    const from = center(bridge.from_graph, nodeOf(bridge.from_graph, bridge.from));
    const to = center(bridge.to_graph, nodeOf(bridge.to_graph, bridge.to));
    const [x, y] = tableCenter(index(bridge.table));
    for (const d of [curve([from[0] + 20, from[1] + HEIGHT / 2], [x, y - 17]), curve([x, y + 17], [to[0] - 20, to[1] - HEIGHT / 2])]) {
      svg.append(draw("path", {class: "bridge", d, "marker-end": "url(#arrow-bridge)"}));
    }
  }

  for (const graph of Object.values(graphs)) {
    const heading = draw("text", {class: "graph-title", x: 20, y: OFFSETS[graph.id] - 10});
    heading.textContent = TITLES[graph.id];
    const note = draw("text", {class: "graph-note", x: 20, y: OFFSETS[graph.id] + 10});
    note.textContent = NOTES[graph.id];
    svg.append(heading, note);
    for (const node of graph.nodes) {
      for (const leaving of node.exits) {
        const d = edgePath(graph.id, node, leaving);
        const edge = draw("path", {
          class: `edge${leaving.conditional ? " conditional" : ""}`, d, "marker-end": "url(#arrow)",
          "data-edge": `${graph.id}/${node.id}/${leaving.target}`,
        });
        const hit = draw("path", {class: "edge-hit", d});
        const tip = draw("title");
        tip.textContent = `${label(node.id)} → ${label(leaving.target)} : ${leaving.when}`;
        hit.append(tip);
        hit.addEventListener("click", () => go(isEnd(node) ? "" : `node/${graph.id}/${node.id}/${leaving.target}`));
        svg.append(edge, hit);
      }
    }
    for (const node of graph.nodes) svg.append(drawNode(graph.id, node));
  }

  data.tables.forEach((table, i) => {
    const [x, y] = tableCenter(i);
    const shape = draw("g", {class: "table", "data-table": table.name}, draw("rect", {x: x - 63, y: y - 17, width: 126, height: 34, rx: 6}));
    const name = draw("text", {x, y});
    name.textContent = table.name;
    shape.append(name);
    svg.append(pressable(shape, () => go(`table/${table.name}`), `Table ${table.name}`));
  });
  svg.append(draw("g", {id: "overlay"}));
  canvas.replaceChildren(svg);
}

function curve([x1, y1], [x2, y2]) {
  const middle = (y1 + y2) / 2;
  return `M${x1} ${y1}C${x1} ${middle} ${x2} ${middle} ${x2} ${y2}`;
}

// Highlighting.

function clearMarks() {
  svg.classList.remove("dim");
  for (const node of svg.querySelectorAll(".selected, .near, .writes, .reads, .leaving, .arriving, .focus")) {
    node.classList.remove("selected", "near", "writes", "reads", "leaving", "arriving", "focus");
  }
  for (const edge of svg.querySelectorAll(".edge")) edge.setAttribute("marker-end", "url(#arrow)");
  svg.querySelector("#overlay").replaceChildren();
}

const shapeOf = (graph, id) => svg.querySelector(`[data-node="${graph}/${id}"]`);
const tableShape = (name) => svg.querySelector(`[data-table="${name}"]`);

function link(graph, node, table, kind) {
  const [nx, ny] = center(graph, node);
  const [tx, ty] = tableCenter(data.tables.findIndex((each) => each.name === table));
  const above = graph === "conversation";
  const d = curve([nx, ny + (above ? HEIGHT / 2 : -HEIGHT / 2)], [tx, ty + (above ? -17 : 17)]);
  svg.querySelector("#overlay").append(draw("path", {class: `link ${kind}`, d}));
}

function markNode(graph, node, focus) {
  svg.classList.add("dim");
  shapeOf(graph, node.id).classList.add("selected");
  exitsOf(node).forEach((leaving, i) => {
    shapeOf(graph, leaving.target).classList.add("near");
    const edge = svg.querySelector(`[data-edge="${graph}/${node.id}/${leaving.target}"]`);
    edge.classList.add(leaving.target === focus ? "focus" : "leaving");
    edge.setAttribute("marker-end", "url(#arrow-on)");
    const at = edge.getPointAtLength(Math.min(26, edge.getTotalLength() / 2));
    const number = draw("g", {class: "number"}, draw("circle", {cx: at.x, cy: at.y, r: 9}));
    const digit = draw("text", {x: at.x, y: at.y + .5});
    digit.textContent = i + 1;
    number.append(digit);
    svg.querySelector("#overlay").append(number);
  });
  for (const other of graphs[graph].nodes) {
    if (other.exits.some((leaving) => leaving.target === node.id) && other.id !== node.id) {
      shapeOf(graph, other.id).classList.add("near");
      svg.querySelector(`[data-edge="${graph}/${other.id}/${node.id}"]`).classList.add("arriving");
    }
  }
  for (const [tables, kind] of [[readsOf(node), "reads"], [writesOf(node), "writes"]]) {
    for (const table of tables) {
      tableShape(table).classList.add(kind);
      if (kind === "writes" || !writesOf(node).includes(table)) link(graph, node, table, kind);
    }
  }
}

function markUsers(users) {
  svg.classList.add("dim");
  for (const {graph, node, kind} of users) shapeOf(graph, node.id)?.classList.add(kind);
}

// The panel.

const chip = (text, kind = "", to) => to
  ? el("button", {type: "button", class: `chip ${kind}`, onclick: () => go(to)}, text)
  : el("span", {class: `chip ${kind}`}, text);
const chips = (...list) => el("div", {class: "chips"}, ...list);
const section = (title, ...content) => [el("h3", {}, title), ...content];
const source = (title, code) => el("details", {}, el("summary", {class: "muted"}, title), el("pre", {}, code));

function conditions(list, title = "si") {
  return list.length ? el("div", {class: "when"}, el("span", {}, title), ...list.map((each) => el("code", {}, each))) : null;
}

function nodePanel(graph, node, focus) {
  const schema = Object.fromEntries(graphs[graph].schema.map((key) => [key.name, key]));
  const badges = [
    node.model && chip("appelle le modèle", "model"),
    node.pause && chip("⏸ interrupt", "pause"),
    !node.model && !node.pause && !writesOf(node).length && !readsOf(node).length && chip("ne touche que l’état"),
    !writesOf(node).length && readsOf(node).length > 0 && chip("lit la base", "read"),
    writesOf(node).length > 0 && chip("écrit en base", "write"),
  ].filter(Boolean);
  return [
    el("p", {class: "kicker"}, TITLES[graph]),
    el("h2", {}, node.id),
    el("p", {class: "place"}, node.place),
    el("p", {class: "role"}, node.role),
    el("div", {class: "badges"}, chips(...badges)),
    ...section("Ce qu’il lit dans l’état", node.reads.length
      ? chips(...node.reads.map((key) => chip(key, "read", `key/${graph}/${key}`)))
      : el("p", {class: "muted"}, "Rien.")),
    ...producedSection(graph, node, schema),
    ...exitsSection(graph, node, focus),
    ...sqlSection(node),
    ...section("Code", source("Voir la fonction", node.code)),
  ];
}

function producedSection(graph, node, schema) {
  const some = node.updates.filter((update) => update.keys.length);
  const cards = some.map((update) => el("div", {class: "card"},
    conditions(update.when),
    ...update.keys.map((key) => el("div", {class: "pair"},
      key.declared ? chip(key.key, "write", `key/${graph}/${key.key}`) : chip(key.key, "danger"),
      el("code", {}, key.value),
      !key.declared && el("p", {class: "inner"}, "Hors du schéma : LangGraph ignore cette clé."),
      schema[key.key]?.reducer && el("p", {class: "inner"}, `Fusionnée par ${schema[key.key].reducer}.`),
      key.messages.length > 0 && el("p", {class: "inner"}, `Messages : ${key.messages.join(", ")}`),
      key.inner.length > 0 && el("p", {class: "inner"}, `Contient, sans que le schéma le déclare : ${key.inner.join(", ")}`),
    ))));
  const outside = [];
  if (node.pause) {
    outside.push(el("div", {class: "card"},
      el("p", {}, "La pause montre :"),
      ...node.pause.payload.map((each) => el("div", {class: "pair"}, chip(each.key, "pause"), el("code", {}, each.value))),
      el("p", {}, "et attend, pour reprendre, un ", el("code", {}, node.pause.schema), " :"),
      ...node.pause.fields.map((each) => el("div", {class: "pair"}, chip(each.name, "pause"), el("code", {}, each.type)))));
  }
  if (node.artifacts.length) {
    outside.push(el("div", {class: "card"}, el("p", {}, "Dans l’artifact des ToolMessage, que le modèle ne voit pas :"),
      chips(...node.artifacts.map((each) => chip(each)))));
  }
  return [
    ...section("Ce qu’il produit dans l’état", ...(cards.length ? cards : [
      el("p", {class: "muted"}, node.updates.length ? "Aucune clé : il n’agit que hors de l’état." : "Rien.")])),
    ...(outside.length ? [el("h4", {}, "Hors du schéma d’état"), ...outside] : []),
  ];
}

function exitsSection(graph, node, focus) {
  const list = exitsOf(node).map((leaving, i) => el("div", {class: `card exit${leaving.target === focus ? " focus" : ""}`},
    el("b", {}, String(i + 1)),
    el("div", {}, leaving.target === "__end__" ? chip("fin") : chip(leaving.target, "", `node/${graph}/${leaving.target}`)),
    el("p", {}, leaving.when)));
  const others = [];
  if (node.pause) others.push(el("div", {class: "card exit other"}, el("b", {}, "⏸"), el("div", {}, "pause"),
    el("p", {class: "muted"}, "L’exécution s’arrête ici, sauvegardée. Rejoué depuis le début à la reprise.")));
  others.push(el("div", {class: "card exit other"}, el("b", {}, "!"), el("div", {}, "échec"), el("p", {class: "muted"}, node.retry
    ? `${node.retry.attempts} tentatives si l’erreur est ${node.retry.on}. Sinon l’exécution s’arrête et reprendra à ce nœud.`
    : "Pas de nouvelle tentative : l’exécution s’arrête et reprendra à ce nœud.")));
  return [
    ...section("Par où on en sort", ...list, ...others),
    node.router && el("div", {class: "card"},
      el("p", {class: "muted"}, "Choisi par cette fonction, qui lit "),
      chips(...node.router.reads.map((key) => chip(key, "read", `key/${graph}/${key}`))),
      el("p", {class: "place"}, node.router.place), el("pre", {}, node.router.code)),
  ];
}

function statement(each, note) {
  const kind = each.writes.length ? "write" : "read";
  const lock = /FOR (NO KEY )?UPDATE/.test(each.sql);
  return el("details", {class: "card sql"},
    el("summary", {}, el("span", {class: `op ${kind}`}, each.operation),
      ...(each.writes.length ? each.writes : each.reads).map((table) => el("code", {}, table)),
      lock && chip("verrou"), note && el("span", {class: "muted"}, note)),
    each.columns.length > 0 && el("p", {class: "muted"}, "Colonnes : ", el("code", {}, each.columns.join(", "))),
    each.writes.length > 0 && each.reads.length > 0 && el("p", {class: "muted"}, "Lit aussi : ", el("code", {}, each.reads.join(", "))),
    ...each.when.map((way, i) => conditions(way, i ? "ou si" : "si")),
    !each.when.length && el("p", {class: "muted"}, "Sans condition."),
    el("p", {class: "place"}, `${each.function} · ${each.place}`),
    el("pre", {}, each.sql));
}

function sqlSection(node) {
  const written = node.sql.filter((each) => each.writes.length), read = node.sql.filter((each) => !each.writes.length);
  const routing = node.router?.sql ?? [];
  if (!written.length && !read.length && !routing.length) {
    return section("Ce qu’il fait dans la base", el("p", {class: "muted"}, "Rien dans la boutique. LangGraph y enregistre seulement son checkpoint."));
  }
  return section("Ce qu’il fait dans la base",
    ...node.transitions.map((change) => el("div", {class: "card"},
      el("div", {class: "status"}, el("span", {class: "muted"}, "cases.status"), ...change.from.map((each) => chip(each)), "→", chip(change.to, "write")),
      conditions(change.when),
      change.mails.length > 0 && el("p", {}, `Mail : ${change.mails.join(" ou ")} (${change.sender === "shop" ? "de la boutique" : "de l’agent"})`),
      change.effect && el("p", {}, "Dans la même transaction : ", el("code", {}, change.effect)))),
    written.length > 0 && el("h4", {}, "Écritures"), ...written.map((each) => statement(each)),
    read.length > 0 && el("h4", {}, "Lectures"), ...read.map((each) => statement(each)),
    routing.length > 0 && el("h4", {}, "Pour choisir la sortie"), ...routing.map((each) => statement(each)));
}

function users(list) {
  return list.length ? chips(...list.map(({graph, node, kind}) => chip(node.id, kind === "writes" ? "write" : "read", `node/${graph}/${node.id}`)))
    : el("p", {class: "muted"}, "Aucun nœud.");
}

function tableUsers(name) {
  return everyNode().filter(({node}) => !isEnd(node)).flatMap(({graph, node}) => [
    ...(writesOf(node).includes(name) ? [{graph, node, kind: "writes"}] : []),
    ...(readsOf(node).includes(name) ? [{graph, node, kind: "reads"}] : []),
  ]);
}

function tablePanel(table) {
  const list = tableUsers(table.name);
  const bridges = data.bridges.filter((bridge) => bridge.table === table.name);
  return [
    el("p", {class: "kicker"}, "Table"), el("h2", {}, `shop.${table.name}`),
    ...["writes", "reads"].flatMap((kind) => section(kind === "writes" ? "Écrite par" : "Lue par",
      ...Object.keys(graphs).flatMap((graph) => [el("h4", {}, TITLES[graph]), users(list.filter((each) => each.kind === kind && each.graph === graph))]))),
    ...(bridges.length ? section("Passages d’un graphe à l’autre", ...bridges.map(bridgeCard)) : []),
    ...section("Colonnes", el("table", {}, el("tbody", {}, ...table.columns.map((column) => el("tr", {}, el("td", {}, el("code", {}, column.name)), el("td", {class: "muted"}, column.type)))))),
    ...(table.indexes.length ? section("Garanties", ...table.indexes.map((index) => el("div", {class: "card"}, el("code", {}, index.name), el("pre", {}, index.rule)))) : []),
  ];
}

function keyUsers(graph, name) {
  return graphs[graph].nodes.filter((node) => !isEnd(node)).flatMap((node) => [
    ...(keysWritten(node).includes(name) ? [{graph, node, kind: "writes"}] : []),
    ...(keysRead(node).includes(name) ? [{graph, node, kind: "reads"}] : []),
  ]);
}

function keyPanel(graph, key) {
  const list = keyUsers(graph, key.name);
  const values = list.filter((each) => each.kind === "writes").flatMap(({node}) => node.updates.flatMap((update) =>
    update.keys.filter((each) => each.key === key.name).map((each) => ({node, update, each}))));
  return [
    el("p", {class: "kicker"}, `${TITLES[graph]} · clé d’état`), el("h2", {}, key.name),
    el("p", {class: "role"}, key.note),
    ...section("Type", el("pre", {}, key.type), el("p", {class: "muted"}, key.reducer
      ? `Chaque écriture est fusionnée par ${key.reducer}.` : "Chaque écriture remplace la valeur.")),
    ...section("Produite par", users(list.filter((each) => each.kind === "writes")),
      graphs[graph].input.keys.includes(key.name) && el("p", {class: "muted"}, "Donnée aussi à l’entrée du graphe."),
      ...values.map(({node, update, each}) => el("div", {class: "card"}, el("div", {class: "pair"}, chip(node.id, "write", `node/${graph}/${node.id}`), el("code", {}, each.value),
        each.messages.length > 0 && el("p", {class: "inner"}, `Messages : ${each.messages.join(", ")}`),
        each.inner.length > 0 && el("p", {class: "inner"}, `Contient : ${each.inner.join(", ")}`)), conditions(update.when)))),
    ...section("Lue par", users(list.filter((each) => each.kind === "reads"))),
  ];
}

const bridgeCard = (bridge) => el("div", {class: "card"},
  el("div", {class: "status"}, chip(bridge.from, "write", `node/${bridge.from_graph}/${bridge.from}`), "→", chip(bridge.table, "", `table/${bridge.table}`),
    "→", chip(bridge.to, "read", `node/${bridge.to_graph}/${bridge.to}`)),
  el("p", {class: "muted"}, bridge.note));

function schemaPanel() {
  return [
    el("h2", {}, "Schémas d’état"),
    el("p", {class: "role muted"}, "L’état que les nœuds d’un même graphe se partagent. Choisissez une clé, un nœud, une flèche ou une table."),
    ...Object.values(graphs).flatMap((graph) => section(TITLES[graph.id],
      el("p", {class: "muted"}, "Entrée : ", el("code", {}, graph.input.name), " · État : ", el("code", {}, graph.state)),
      el("table", {}, el("thead", {}, el("tr", {}, ...["Clé", "Type", "Écriture"].map((each) => el("th", {}, each)))),
        el("tbody", {}, ...graph.schema.map((key) => el("tr", {}, el("td", {}, chip(key.name, "", `key/${graph.id}/${key.name}`)),
          el("td", {}, el("code", {}, key.type)), el("td", {class: "muted"}, key.reducer ?? "remplace"))))),
      graph.context && el("p", {class: "muted"}, "Contexte d’exécution, fixé par le serveur et jamais dans l’état : ",
        ...graph.context.fields.flatMap((each) => [el("code", {}, `${each.name}: ${each.type}`), " "])))),
    ...section("Ce que les deux graphes partagent",
      el("p", {class: "muted"}, "Aucun état : seulement la base."), ...data.bridges.map(bridgeCard)),
    ...section("D’où viennent ces informations", el("p", {class: "muted"},
      "Du code, à chaque démarrage : les flèches viennent des graphes compilés, le reste d’une analyse de leurs fonctions. "
      + "Elle peut lister une requête qu’un nœud n’atteint jamais ; des tests vérifient qu’elle n’en oublie pas dans les parcours joués. "
      + "Seules les phrases sont écrites à la main.")),
  ];
}

// The matrix: every node against every key and table.

function drawMatrix() {
  const cell = (written, read, separate) => el("td", {class: separate ? "sep" : undefined},
    written && el("b", {class: "w"}, "E"), read && el("b", {class: "r"}, "L"));
  matrix.replaceChildren(el("p", {class: "muted"}, "E : écrit. L : lit, y compris pour choisir la sortie."), ...Object.values(graphs).flatMap((graph) => {
    const nodes = graph.nodes.filter((node) => !isEnd(node));
    const tables = data.tables.filter((table) => nodes.some((node) => [...writesOf(node), ...readsOf(node)].includes(table.name)));
    return [el("h2", {}, TITLES[graph.id]), el("div", {class: "scroll"}, el("table", {},
      el("thead", {},
        el("tr", {}, el("td"), el("th", {class: "group", colspan: graph.schema.length}, "État"), el("th", {class: "group sep", colspan: tables.length}, "Base SQL"), el("th", {class: "group sep"}, "Sorties")),
        el("tr", {}, el("td"),
          ...graph.schema.map((key) => el("th", {}, el("button", {type: "button", onclick: () => go(`key/${graph.id}/${key.name}`)}, key.name))),
          ...tables.map((table, i) => el("th", {class: i ? undefined : "sep"}, el("button", {type: "button", onclick: () => go(`table/${table.name}`)}, table.name))),
          el("th", {class: "sep"}))),
      el("tbody", {}, ...nodes.map((node) => el("tr", {},
        el("th", {}, el("button", {type: "button", onclick: () => go(`node/${graph.id}/${node.id}`)}, node.id)),
        ...graph.schema.map((key) => cell(keysWritten(node).includes(key.name), keysRead(node).includes(key.name))),
        ...tables.map((table, i) => cell(writesOf(node).includes(table.name), readsOf(node).includes(table.name), !i)),
        el("td", {class: "sep"}, exitsOf(node).map((leaving) => label(leaving.target)).join(", ")))))))];
  }));
}

// Where we are lives in the address: it can be shared, and Back works.

function go(place) {
  if (location.hash.slice(1) === place) show();
  else location.hash = place;
}

function show() {
  const [kind, first, second, third] = decodeURIComponent(location.hash.slice(1)).split("/");
  clearMarks();
  let content = null;
  if (kind === "node" && graphs[first] && nodeOf(first, second) && !isEnd(nodeOf(first, second))) {
    markNode(first, nodeOf(first, second), third);
    content = nodePanel(first, nodeOf(first, second), third);
  } else if (kind === "table" && data.tables.some((table) => table.name === first)) {
    markUsers(tableUsers(first));
    tableShape(first).classList.add("selected");
    for (const {graph, node, kind: use} of tableUsers(first)) {
      if (use === "writes" || !writesOf(node).includes(first)) link(graph, node, first, use);
    }
    content = tablePanel(data.tables.find((table) => table.name === first));
  } else if (kind === "key" && graphs[first]?.schema.some((key) => key.name === second)) {
    markUsers(keyUsers(first, second));
    content = keyPanel(first, graphs[first].schema.find((key) => key.name === second));
  }
  panel.replaceChildren(...(content ?? schemaPanel()).filter(Boolean));
  panel.scrollTop = 0;
  panel.querySelector(".exit.focus")?.scrollIntoView({block: "center"});
}

function showView(view) {
  for (const button of document.querySelectorAll("[data-view][aria-pressed]")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  stage.dataset.view = view;
  canvas.hidden = view === "matrix";
  matrix.hidden = view !== "matrix";
  if (VIEWS[view]) svg.setAttribute("viewBox", VIEWS[view]);
}

try {
  const response = await fetch("/api/graphs");
  if (!response.ok) throw new Error(response.statusText);
  data = await response.json();
  graphs = Object.fromEntries(data.graphs.map((graph) => [graph.id, graph]));
  drawAll();
  drawMatrix();
  for (const button of document.querySelectorAll("button[data-view]")) button.addEventListener("click", () => showView(button.dataset.view));
  document.querySelector("#home").addEventListener("click", () => go(""));
  addEventListener("hashchange", show);
  addEventListener("keydown", (event) => event.key === "Escape" && go(""));
  show();
} catch (error) {
  panel.replaceChildren(el("p", {}, "Les graphes n’ont pas pu être lus. Rechargez la page une fois le serveur démarré."));
  throw error;
}
