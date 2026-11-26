// Draws what agent_sav/graph_map.py reads in the code. Nothing about the graphs is written here.

const WIDTH = 148, HEIGHT = 56, END_WIDTH = 88, END_HEIGHT = 30;
const OFFSETS = {conversation: 36, case: 632};
const BAND = {y: 482, height: 84};
const VIEWS = {all: "0 0 1270 1122", conversation: "0 0 1270 570", case: "0 478 1270 647"};
const TITLES = {conversation: "Graphe conversation", case: "Graphe dossier"};
const NOTES = {conversation: "un thread par chat, relancé à chaque message", case: "un thread par demande, sans modèle"};

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
// The chat graph runs once for each customer message: its end is not that of the chat.
const ENDS = {conversation: "fin du tour", case: "fin"};
const label = (graph, id) => ({__start__: "début", __end__: ENDS[graph]})[id] ?? id;
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

const tableCenter = (index) => [86 + index * 137, BAND.y + 56];

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
  name.textContent = label(graph, node.id);
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
  svg.append(draw("path", {class: "band-head", d: `M8 ${BAND.y + 29}V${BAND.y + 10}Q8 ${BAND.y} 18 ${BAND.y}H1252Q1262 ${BAND.y} 1262 ${BAND.y + 10}V${BAND.y + 29}Z`}));
  const title = draw("text", {class: "graph-note band-title", x: 20, y: BAND.y + 19.5});
  title.textContent = "Base SQL, schémas shop et sav, partagés : les deux graphes ne s’appellent jamais, ils se parlent par ces tables";
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
        tip.textContent = `${label(graph.id, node.id)} → ${label(graph.id, leaving.target)} : ${leaving.when}`;
        hit.append(tip);
        hit.addEventListener("click", () => go(isEnd(node) ? "" : `node/${graph.id}/${node.id}/${leaving.target}`));
        svg.append(edge, hit);
      }
    }
    for (const node of graph.nodes) svg.append(drawNode(graph.id, node));
  }

  data.tables.forEach((table, i) => {
    const [x, y] = tableCenter(i);
    const shape = draw("g", {class: `table ${table.schema}`, "data-table": table.name}, draw("rect", {x: x - 63, y: y - 17, width: 126, height: 34, rx: 6}));
    const name = draw("text", {x, y});
    name.textContent = table.name;
    const tip = draw("title");
    tip.textContent = `${table.schema}.${table.name}`;
    shape.append(name, tip);
    svg.append(pressable(shape, () => go(`table/${table.name}`), `Table ${table.schema}.${table.name}`));
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
const section = (title, ...content) => [el("h3", {class: Object.values(TITLES).includes(title) && "graph-name"}, title), ...content];
const source = (title, code) => el("details", {}, el("summary", {class: "muted"}, title), el("pre", {}, ...colored(code, "python")));

// Code with its syntax coloured.
const STRING = String.raw`"(?:[^"\\\n]|\\.)*"`;
const SYNTAX = {
  json: new RegExp(String.raw`(?<note>^\s*// ….*$)|(?<name>${STRING}(?=\s*:))|(?<text>${STRING})|(?<number>-?\b\d[\d.eE+-]*)|(?<word>\b(?:true|false|null)\b)`, "gm"),
  python: new RegExp(String.raw`(?<note>#.*)|(?<text>(?<!\w)[rbf]{0,2}(?:"""[\s\S]*?"""|'''[\s\S]*?'''|${STRING}|'(?:[^'\\\n]|\\.)*'))`
    + String.raw`|(?<word>\b(?:and|as|assert|break|class|continue|def|del|elif|else|except|False|finally|for|from|if|import|in|is|lambda|None|not|or|pass|raise|return|True|try|while|with|yield)\b)`
    + String.raw`|(?<name>\b[A-Za-z_]\w*(?=\())|(?<number>\b\d[\d_.]*)`, "g"),
  sql: new RegExp(String.raw`(?<note>--.*)|(?<text>'(?:[^']|'')*')|(?<number>%s|%\(\w+\)s|\b\d+\b)|(?<name>\b[a-z_]+(?=\())`
    + String.raw`|(?<word>\b[A-Z]{2,}\b)`, "g"),
};

function colored(code, language) {
  const shown = [];
  let done = 0;
  for (const match of code.matchAll(SYNTAX[language])) {
    const [kind] = Object.entries(match.groups).find(([, value]) => value !== undefined);
    shown.push(code.slice(done, match.index), el("span", {class: `code-${kind}`}, match[0]));
    done = match.index + match[0].length;
  }
  return [...shown, code.slice(done)];
}

function conditions(list, title = "si") {
  return list.length ? el("div", {class: "when"}, el("span", {}, title), ...list.map((each) => el("code", {}, ...colored(each, "python")))) : null;
}

const ticked = (text) => text.split("`").map((part, i) => i % 2 ? el("code", {}, part) : part);

const answered = (text) => text.split(/\n\s*\n/).map((block) => {
  const lines = block.split("\n");
  if (!lines.every((line) => line.includes(" | "))) return el("p", {}, ...ticked(block));
  const [head, ...rows] = lines.map((line) => line.split(" | "));
  return el("table", {}, el("thead", {}, el("tr", {}, ...head.map((each) => el("th", {}, ...ticked(each))))),
    el("tbody", {}, ...rows.map((row) => el("tr", {}, ...row.map((each) => el("td", {}, ...ticked(each)))))));
});

function personalSection(node) {
  const notes = Object.entries(node.personal ?? {});
  return notes.length ? [el("div", {class: "part personal"}, el("p", {class: "part-name"}, "Notes personnelles"),
    ...notes.map(([question, answer]) => el("details", {}, el("summary", {}, question), ...answered(answer))))] : [];
}

function nodePanel(graph, node, focus) {
  const facts = [
    node.model && "appelle le modèle",
    node.pause && "se met en pause (interrupt)",
    !node.model && !node.pause && !writesOf(node).length && !readsOf(node).length && "ne touche que l’état",
    !writesOf(node).length && readsOf(node).length > 0 && "lit la base",
    writesOf(node).length > 0 && "écrit en base",
  ].filter(Boolean);
  const own = [
    ...readsSection(graph, node),
    ...readExamples(graph, node),
    ...producedSection(graph, node),
    ...nodeExamples(graph, node),
    ...exitsSection(graph, node, focus),
    ...sqlSection(node),
    ...section("Code", source("Voir la fonction", node.code)),
  ];
  return [
    el("p", {class: "kicker graph-name"}, TITLES[graph]),
    el("h2", {}, node.id),
    el("p", {class: "place"}, node.place),
    el("p", {class: "role"}, node.role),
    el("div", {class: "chips facts"}, ...facts.map((each) => el("span", {class: "fact"}, each))),
    ...entriesSection(graph, node),
    ...(node.model ? [  // what concerns the model is told apart from what the node does
      el("div", {class: "part model-part"}, el("p", {class: "part-name"}, "LLM"), ...promptSection(node.model)),
      el("div", {class: "part node-part"}, el("p", {class: "part-name"}, "Nœud"), ...own),
    ] : own),
    ...personalSection(node),
  ];
}

// A text of the code, with its variables standing out.
function templated(text) {
  return text.split(/(\{[^{}]+\})/).map((part) => /^\{.+\}$/.test(part) ? el("mark", {}, part) : part);
}

// JSON as it is sent.
const json = (value) => el("pre", {class: "json"}, ...colored(JSON.stringify(value, null, 2), "json"));

function promptSection(sent) {
  const copy = el("button", {type: "button", class: "chip", onclick: async () => {
    try {
      await navigator.clipboard.writeText(sent.instructions);
      copy.textContent = "Copié";
    } catch {
      copy.textContent = "Copie refusée";
    }
  }}, "Copier");
  return section("Ce qu’il envoie au modèle",
    el("div", {class: "status"}, chip(sent.name, "model"), el("span", {class: "muted"}, "réglable par SAV_MODEL")),
    el("h4", {}, "1. Prompt partie <system>"),
    el("div", {class: "status"}, el("span", {class: "place"}, sent.place), copy),
    el("pre", {class: "prompt"}, ...templated(sent.instructions)),
    ...sent.additions.map((each) => el("div", {class: "card"}, el("p", {}, "Ajouté à la fin"), conditions(each.when),
      el("pre", {}, ...templated(each.text.trim())))),
    el("h4", {}, "2. Prompt parties <user>, <assistant> et <tool>"),
    el("div", {class: "card"}, el("code", {}, ...colored(sent.messages, "python")),
      el("p", {class: "muted"}, `Les ${sent.history} derniers au plus, à partir d’un message du client.`)),
    ...(sent.example ? [
      el("p", {class: "muted exchange-title"}, "Un vrai échange, tel que le modèle le reçoit après la partie <system>. Il doit répondre au dernier message."),
      json(sent.example)] : []),
    el("h4", {}, `3. Outils (${sent.tools.length})`),
    ...sent.tools.map((tool) => el("details", {class: "card sql"},
      el("summary", {}, el("span", {class: `op ${tool.reads ? "read" : "write"}`}, tool.reads ? "LECTURE" : "ACTION"), el("code", {}, tool.name)),
      json(tool.sent))),
    el("h4", {}, "4. Réglages de l’appel"),
    el("div", {class: "card"}, ...sent.settings.map((each) => el("div", {class: "pair"}, chip(each.name), el("code", {}, each.value)))));
}

// What the recording left out is said in a comment, not as if it were a value.
const cutShown = (json) => json
  .replace(/^(\s*)"… et (\d+) avant",?$/gm, (_, space, count) => `${space}// … ${count} message${count > 1 ? "s" : ""} plus ancien${count > 1 ? "s" : ""}, non affiché${count > 1 ? "s" : ""}`)
  .replace(/^(\s*)"… et (\d+) de plus",?$/gm, (_, space, count) => `${space}// … ${count} autre${count > 1 ? "s" : ""}, non affiché${count > 1 ? "s" : ""}`);

// One value for each key, kept from a real run. Of several, the one that shows the most.
function examples(list) {
  const best = new Map();
  for (const {name, kind, to, recorded, note, tall, kept} of list) {
    const shown = recorded ? cutShown(JSON.stringify(recorded.value, null, 2)) : "";
    const before = best.get(`${kind}/${name}`);
    if (!before || shown.length > before.shown.length) best.set(`${kind}/${name}`, {name, kind, to, shown, note, tall, kept});
  }
  return best.size ? [el("div", {class: "card examples"}, ...[...best.values()].map(({name, kind, to, shown, note, tall, kept}) =>
    el("div", {class: shown.length < 12 ? "example short" : tall ? "example thread" : "example"}, chip(name, kind, to), note && el("p", {class: "muted"}, note),
      shown ? el("pre", {}, ...colored(shown, "json")) : !kept && el("p", {class: "muted"}, "Aucun enregistré pour cette version du code."))))] : [];
}

// One pass adds one message or two: the thread shows how they follow one another.
const threadExample = (node, key) => node.thread && key === "messages"
  ? {recorded: {value: node.thread}, tall: true, note: `Un vrai fil, tel que l’état le garde. ${node.id} vient d’y ajouter le dernier message.`} : {};

// What the node was given, all at the same moment of a real run.
function readExamples(graph, node) {
  if (!node.reads.length) return [];
  return section("Exemple de valeurs lues par le nœud", ...examples(node.reads.map((key) => ({name: key, kind: "read", to: `key/${graph}/${key}`,
    recorded: key in node.read_example ? {value: node.read_example[key]} : null}))));
}

function nodeExamples(graph, node) {
  const parts = (value, kind) => Object.entries(value ?? {}).map(([name, each]) => ({name, kind, recorded: {value: each}}));
  const named = (key) => ({name: key.key, ...(key.declared ? {kind: "state", to: `key/${graph}/${key.key}`} : {kind: "other"})});
  const produced = node.updates.map((update) => update.keys.map((key) => ({...named(key), recorded: key.example, note: key.note, ...threadExample(node, key.key)})));
  const groups = [...(node.pause ? [parts(node.pause.example, "other")] : []), ...produced];
  for (const {situation, key, note} of node.kept) groups[situation].push({name: key, kind: "state", to: `key/${graph}/${key}`, note, kept: true});
  // An artifact shows in the message that carries it, unless its example has none.
  const carried = produced.flat().some((each) => each.name === "messages" && JSON.stringify(each.recorded ?? null).includes('"artifact"'));
  if (!carried) groups.at(-1)?.push(...parts(node.artifact_example, "sub"));
  const titled = (title, group) => {
    const card = examples(group);
    return title && card.length ? [el("h4", {}, title), ...card] : card;
  };
  const shown = groups.flatMap((group, i) => {
    const update = node.updates[i - (groups.length - node.updates.length)];
    return [...titled(update?.case ?? node.situations[i], group), ...(update?.more ?? []).flatMap((each) => titled(each.case,
      update.keys.map((key) => ({...named(key), recorded: {value: each.values[key.key]}}))))];
  });
  return shown.length ? section("Exemple de valeurs produites par le nœud", ...shown) : [];
}

// The keys the node's own code looks at, and what for. Its exits read their own.
function readsSection(graph, node) {
  return section("Clés de l’état que le nœud consulte", ...(node.reads.length
    ? [el("p", {class: "muted"}, "Lire une clé ne la modifie pas."),
      el("div", {class: "card reads"}, ...node.reads.map((key) => el("div", {class: "pair"},
        chip(key, "read", `key/${graph}/${key}`), el("p", {}, node.read_notes[key] ?? ""))))]
    : [el("p", {class: "muted"}, "Aucune.")]));
}

// Every key a node produces, with its sub-keys. Those of the state stand out; the others never reach the shared schema.
function producedSection(graph, node) {
  const rows = new Map();  // the same key may come from several returns
  for (const key of node.updates.flatMap((update) => update.keys)) {
    const inner = rows.get(key.key)?.inner ?? [];
    rows.set(key.key, {shown: key.declared ? chip(key.key, "state", `key/${graph}/${key.key}`) : chip(key.key, "other"),
      inside: key.declared, inner: unique([...inner, ...key.inner])});
  }
  // The artifact of a ToolMessage is kept with it, in messages.
  if (node.artifacts.length) rows.set("artifact()", {shown: chip("messages.artifact", "state", `key/${graph}/messages`), inside: true, inner: node.artifacts});
  if (node.pause) rows.set("interrupt()", {shown: chip("interrupt", "pause"), inside: false, inner: node.pause.payload.map((each) => each.key)});
  const all = [...rows.values()], alone = all.filter(({inner}) => !inner.length), nested = all.filter(({inner}) => inner.length);
  const legend = [
    all.some(({inside}) => inside) && ["clé", "state", "du state"],
    nested.some(({inside}) => inside) && ["clé", "sub", "sous-clé"],
    all.some(({inside}) => !inside) && ["clé", "other", "hors du state"],
  ].filter(Boolean);
  return section("Clés produites par le nœud", ...(all.length
    ? [el("div", {class: "chips key-legend"}, ...legend.map(([name, kind, text]) => el("span", {}, chip(name, kind), " ", el("span", {class: "muted"}, text)))),
      el("div", {class: "card"}, alone.length > 0 && chips(...alone.map(({shown}) => shown)),
        ...nested.map(({shown, inside, inner}) => el("div", {class: "produced"}, shown, el("span", {class: "muted"}, "›"),
          chips(...inner.map((name) => chip(name, inside ? "sub" : "other"))))))]
    : [el("p", {class: "muted"}, "Aucune.")]));
}

const after = (text) => ` : ${text[0].toLowerCase()}${text.slice(1)}`;

function entriesSection(graph, node) {
  const list = graphs[graph].nodes.flatMap((from) => exitsOf(from).filter((leaving) => leaving.target === node.id).map((leaving) =>
    el("p", {class: "entry"}, "via ", from.id === "__start__" ? chip("début") : chip(from.id, "", `node/${graph}/${from.id}`), after(leaving.when))));
  if (node.pause) list.push(el("p", {class: "entry"}, "via ", chip("⏸ reprise", "pause"), " : la réponse attendue arrive (",
    el("code", {}, node.pause.schema), "). Le nœud est rejoué depuis le début."));
  return section("Comment on y entre", ...list);
}

function exitsSection(graph, node, focus) {
  const list = exitsOf(node).map((leaving, i) => el("p", {class: `entry${leaving.target === focus ? " focus" : ""}`},
    el("b", {}, String(i + 1)), " vers ", leaving.target === "__end__" ? chip(ENDS[graph]) : chip(leaving.target, "", `node/${graph}/${leaving.target}`),
    after(leaving.when)));
  const others = [];
  if (node.pause) others.push(el("p", {class: "entry"}, chip("⏸ pause", "pause"),
    after("L’exécution s’arrête ici, sauvegardée. Rejoué depuis le début à la reprise.")));
  others.push(el("p", {class: "entry muted"}, chip("échec"), after(node.retry
    ? `${node.retry.attempts} tentatives si l’erreur est ${node.retry.on}. Sinon l’exécution s’arrête et reprendra à ce nœud.`
    : "Pas de nouvelle tentative : l’exécution s’arrête et reprendra à ce nœud.")));
  return [
    ...section("Par où on en sort", ...list, ...others),
    node.router && el("div", {class: "card"},
      el("p", {class: "muted"}, "Nœud suivant choisi par cette fonction qui lit :"),
      chips(...node.router.reads.map((key) => chip(key, "read", `key/${graph}/${key}`))),
      el("p", {class: "muted"}, "Exemple code de routage :"), el("p", {class: "place"}, node.router.place), el("pre", {}, ...colored(node.router.code, "python"))),
  ];
}

function statement(each, node) {
  const kind = each.writes.length ? "write" : "read";
  const lock = /FOR (NO KEY )?UPDATE/.test(each.sql);
  return el("details", {class: "card sql"},
    el("summary", {}, el("span", {class: `op ${kind}`}, each.operation),
      ...(each.writes.length ? each.writes : each.reads).map((table) => el("code", {}, table)),
      lock && chip("verrou"),
      each.why && el("p", {class: "why"}, each.why)),
    el("div", {class: "sql-body"},
      el("p", {}, each.when.length ? node.sql_note : "Requête SQL exécutée à chaque passage :"),
      el("pre", {}, ...colored(each.sql, "sql")),
      el("dl", {}, ...[["Colonnes", each.columns], ["Lit aussi", each.writes.length ? each.reads : []], ["Code", [each.function, each.place]]]
        .filter(([, values]) => values.length).flatMap(([name, values]) => [
          el("dt", {}, name), el("dd", {}, ...values.map((value) => el("code", {}, value)))]))));
}

function sqlSection(node) {
  const written = node.sql.filter((each) => each.writes.length), read = node.sql.filter((each) => !each.writes.length);
  const routing = node.router?.sql ?? [];
  if (!written.length && !read.length && !routing.length) {
    return section("Ce qu’il fait dans la base", el("p", {class: "muted"}, "Rien dans la boutique. LangGraph y enregistre seulement son checkpoint."));
  }
  return [el("div", {class: "part db-part"}, el("p", {class: "part-name"}, "Base SQL"), ...section("Ce qu’il fait dans la base",
    ...node.transitions.map((change) => el("div", {class: "card"},
      el("div", {class: "status"}, el("span", {class: "muted"}, "cases.status"), ...change.from.map((each) => chip(each)), "→", chip(change.to, "write")),
      conditions(change.when),
      change.mails.length > 0 && el("p", {}, `Mail : ${change.mails.join(" ou ")} (${change.sender === "shop" ? "de la boutique" : "de l’agent"})`),
      change.effect && el("p", {}, "Dans la même transaction : ", el("code", {}, change.effect)))),
    written.length > 0 && el("h4", {}, "Écritures"), ...written.map((each) => statement(each, node)),
    read.length > 0 && el("h4", {}, "Lectures"), ...read.map((each) => statement(each, node)),
    routing.length > 0 && el("h4", {}, "Pour choisir la sortie"), ...routing.map((each) => statement(each, node))))];
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
    el("p", {class: "kicker"}, "Table"), el("h2", {}, `${table.schema}.${table.name}`),
    ...["writes", "reads"].flatMap((kind) => section(kind === "writes" ? "Écrite par" : "Lue par",
      ...Object.keys(graphs).flatMap((graph) => [el("h4", {}, TITLES[graph]), users(list.filter((each) => each.kind === kind && each.graph === graph))]))),
    ...(bridges.length ? section("Passages d’un graphe à l’autre", ...bridges.map(bridgeCard)) : []),
    ...section("Colonnes", el("table", {}, el("tbody", {}, ...table.columns.map((column) => el("tr", {}, el("td", {}, el("code", {}, column.name)), el("td", {class: "muted"}, column.type)))))),
    ...(table.indexes.length ? section("Garanties", ...table.indexes.map((index) => el("div", {class: "card"}, el("code", {}, index.name), el("pre", {}, ...colored(index.rule, "sql"))))) : []),
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
    el("p", {class: "kicker graph-name"}, `${TITLES[graph]} · clé d’état`), el("h2", {}, key.name),
    el("p", {class: "role"}, key.note),
    ...section("Type", el("pre", {}, ...colored(key.type, "python")), el("p", {class: "muted"}, key.reducer
      ? `Chaque écriture est fusionnée par ${key.reducer}.` : "Chaque écriture remplace la valeur.")),
    ...section("Produite par", users(list.filter((each) => each.kind === "writes")),
      graphs[graph].input.keys.includes(key.name) && el("p", {class: "muted"}, "Donnée aussi à l’entrée du graphe."),
      ...values.map(({node, update, each}) => el("div", {class: "card"}, el("div", {class: "pair"}, chip(node.id, "write", `node/${graph}/${node.id}`), el("code", {}, `= ${each.value}`),
        each.messages.length > 0 && el("p", {class: "inner"}, `Messages : ${each.messages.join(", ")}`),
        each.inner.length > 0 && el("p", {class: "inner"}, `Sous-clés : ${each.inner.join(", ")}`)), conditions(update.when)))),
    ...(values.length ? section("Exemple", ...examples(values.map(({node, each}) => ({name: key.name, kind: "state", recorded: each.example, note: each.note, ...threadExample(node, key.name)})))) : []),
    ...section("Lue par", users(list.filter((each) => each.kind === "reads"))),
  ];
}

const bridgeCard = (bridge) => el("div", {class: "card"},
  el("div", {class: "status"}, chip(bridge.from, "write", `node/${bridge.from_graph}/${bridge.from}`), "→", chip(bridge.table, "", `table/${bridge.table}`),
    "→", chip(bridge.to, "read", `node/${bridge.to_graph}/${bridge.to}`)),
  el("p", {class: "muted"}, bridge.note));

function schemaPanel() {
  return [
    el("h2", {}, "Schémas d’état et base SQL"),
    el("p", {class: "role muted"}, "L’état que les nœuds d’un même graphe se partagent. Choisissez une clé, un nœud, une flèche ou une table."),
    ...Object.values(graphs).flatMap((graph) => section(TITLES[graph.id],
      el("p", {}, "Schéma d’état : ", el("code", {}, graph.state)),
      el("p", {class: "muted launch"}, ...(graph.input.keys.length === graph.schema.length ? ["Peut être lancé avec des valeurs de l’état déjà remplies."]
        : [`Ne peut être lancé qu’avec ${graph.input.keys.length > 1 ? "les clés" : "la clé"} `, ...graph.input.keys.flatMap((key, i) => [i ? ", " : "", el("code", {}, key)]),
          graph.input.keys.length > 1 ? " renseignées." : " renseignée."])),
      ...graph.schema.flatMap((key) => [
        el("table", {class: "grid"}, el("thead", {}, el("tr", {}, ...["Clé", "Type", "Écriture"].map((each) => el("th", {}, each)))),
          el("tbody", {}, el("tr", {}, el("td", {}, chip(key.name, "", `key/${graph.id}/${key.name}`)),
            el("td", {}, el("code", {}, key.type)), el("td", {class: "muted"}, key.reducer ?? "remplace")))),
        el("p", {class: "key-note"}, key.note)]),
      graph.context && el("p", {class: "muted"}, "Contexte d’exécution, fixé par le serveur et jamais dans l’état : ",
        ...graph.context.fields.flatMap((each) => [el("code", {}, `${each.name}: ${each.type}`), " "])))),
    ...section("Ce que les deux graphes partagent",
      el("p", {class: "muted"}, "Aucun état : seulement la base."), ...data.bridges.map(bridgeCard)),
    el("div", {class: "part db-part"}, el("p", {class: "part-name"}, "Base SQL"),
      ...data.schemas.flatMap((schema) => section(`Schéma ${schema.name}`, el("p", {}, schema.note),
        ...schema.groups.flatMap((group) => [el("h4", {}, group.name), el("p", {class: "muted"}, group.note),
          el("table", {class: "grid shop-grid"}, el("thead", {}, el("tr", {}, ...["Table", "À quoi elle sert", "Liée à"].map((each) => el("th", {}, each)))),
            el("tbody", {}, ...group.tables.map((table) => el("tr", {}, el("td", {}, chip(table.name, "", table.used ? `table/${table.name}` : undefined)),
              el("td", {}, table.note), el("td", {class: "muted"}, table.links.join(", ") || "—")))))]))),
      el("p", {class: "muted"}, "L’état des graphes n’est dans aucun des deux : LangGraph l’enregistre dans un troisième schéma, langgraph.")),
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
        el("td", {class: "sep"}, exitsOf(node).map((leaving) => label(graph.id, leaving.target)).join(", ")))))))];
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
  panel.querySelector(".entry.focus")?.scrollIntoView({block: "center"});
}

function showView(view) {
  for (const button of document.querySelectorAll("[data-view][aria-pressed]")) button.setAttribute("aria-pressed", String(button.dataset.view === view));
  stage.dataset.view = view;
  canvas.hidden = view === "matrix";
  matrix.hidden = view !== "matrix";
  if (VIEWS[view]) svg.setAttribute("viewBox", VIEWS[view]);
}

// Reading the graphs, and reading them again when the code changes.

const live = document.querySelector("#live");
const problem = document.querySelector("#problem");
const currentView = () => stage.dataset.view ?? "all";

function remember() {
  return {view: currentView(), panel: panel.scrollTop, x: stage.scrollLeft, y: stage.scrollTop,
    open: [...panel.querySelectorAll("details[open] > summary")].map((each) => each.textContent)};
}

function recall(place) {
  showView(place.view);
  show();
  for (const summary of panel.querySelectorAll("details > summary")) summary.parentElement.open = place.open.includes(summary.textContent);
  panel.scrollTop = place.panel;
  stage.scrollTo(place.x, place.y);
}

async function read() {
  const response = await fetch("/api/graphs");
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(result.error), {explained: true});
  const place = remember();
  data = result;
  graphs = Object.fromEntries(data.graphs.map((graph) => [graph.id, graph]));
  drawAll();
  drawMatrix();
  recall(place);
}

function explain(error) {
  problem.textContent = error ? `Le code ne peut pas être lu. La page montre la dernière version lisible.\n${error.message}` : "";
  problem.hidden = !error;
}

function tell(text, stale = false) {
  live.hidden = false;
  live.textContent = text;
  live.classList.toggle("stale", stale);
}

async function follow() {
  let known;
  const wait = () => new Promise((resolve) => setTimeout(resolve, 1000));
  for (;; await wait()) {
    if (document.hidden) continue;
    let version;
    try {
      const response = await fetch("/api/graphs/version");
      if ([401, 404].includes(response.status)) return; // the public demo does not change
      version = await response.json();
    } catch {
      tell("Serveur arrêté", true);
      continue;
    }
    known ??= version;
    if (version.page !== known.page) {
      try {
        sessionStorage.setItem("graphs-place", JSON.stringify(remember()));
      } catch { /* the place is only a convenience */ }
      location.reload();
      return;
    }
    if (version.style !== known.style) {
      for (const sheet of document.querySelectorAll('link[rel="stylesheet"]')) sheet.href = `${sheet.href.split("?")[0]}?${version.style}`;
    }
    const changed = version.code !== known.code;
    known = version;
    if (changed || !data) {
      tell("Lecture du code…");
      try {
        await read();
        explain(null);
        tell(`À jour · ${new Date().toLocaleTimeString("fr")}`);
      } catch (error) {
        if (!error.explained) known = {...version, code: ""}; // the server may be restarting: try again
        explain(error);
        tell("Code illisible", true);
      }
    } else if (problem.hidden && live.classList.contains("stale") || live.hidden) {
      tell("En direct");
    }
  }
}

for (const button of document.querySelectorAll("button[data-view]")) button.addEventListener("click", () => showView(button.dataset.view));
document.querySelector("#home").addEventListener("click", () => go(""));
// A click on nothing in the graphs goes back to the schemas.
stage.addEventListener("click", (event) => {
  if (data && location.hash && !event.target.closest(".node:not(.end), .table, .edge-hit, #matrix, .legend")) go("");
});
addEventListener("hashchange", () => data && show());
addEventListener("keydown", (event) => event.key === "Escape" && data && go(""));

try {
  await read();
  const place = sessionStorage.getItem("graphs-place");
  sessionStorage.removeItem("graphs-place");
  if (place) recall(JSON.parse(place));
} catch (error) {
  if (!data) panel.replaceChildren(el("p", {}, "Les graphes n’ont pas pu être lus."));
  if (error.explained) explain(error);
}
follow();
