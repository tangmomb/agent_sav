// Draws each product of the catalog as an SVG garment, tinted with its real color.

const COLORS = {
  "Vert mousse": "#4d5533", "Bleu ciel": "#a9c3da", "Indigo": "#33426a", "Écru": "#ebe3d2", "Sable": "#c6ae87",
  "Gris chiné": "#9c9b98", "Marine": "#27314d", "Bordeaux": "#692532", "Noir": "#262627", "Bleu brut": "#3a4e78",
  "Olive": "#70703f", "Argile": "#b9805f", "Anthracite": "#44484e", "Blanc cassé": "#f3efe5",
  "Carreaux marine": "#2f3f66", "Caramel": "#b37d4b", "Gris ardoise": "#64707b", "Terracotta": "#b65c41",
  "Bleu nuit": "#202a50", "Blanc": "#f8f7f3",
};

// Each shape lists its filled parts ("sides" are mirrored), its seams and its textured bands.
const SLEEVE = "M63 37C45 44 37 60 32 88L15 170Q14 177 20 179L38 183Q44 184 46 177L66 106Z";
const SHORT_SLEEVE = "M63 37C49 43 41 51 35 66L22 94Q20 100 26 102L52 110L66 88Z";
const TORSO = "M75 29C66 32 61 35 57 40L61 110L57 187Q57 194 64 194L136 194Q143 194 143 187L139 110L143 40C139 35 134 32 125 29C120 45 80 45 75 29Z";
const V_TORSO = "M76 27C66 31 61 35 57 40L61 110L57 187Q57 194 64 194L136 194Q143 194 143 187L139 110L143 40C139 35 134 31 124 27L100 92Z";
const SHIRT_TORSO = "M76 28C66 32 61 35 57 40L61 110L58 182Q72 200 100 196Q128 200 142 182L139 110L143 40C139 35 134 32 124 28L100 46Z";
const NECK = "M76 28C86 23 114 23 124 28C118 42 82 42 76 28Z";
const CREW = "M75 29C80 45 120 45 125 29L120 25C115 38 85 38 80 25Z";
const COLLAR = "M78 22Q74 27 72 34L88 54L100 40Q84 34 78 22Z";
const PANTS = "M66 18H134L148 196Q148 202 142 202H114Q109 202 108 196L100 82L92 196Q91 202 86 202H58Q52 202 52 196Z";
const BUTTONS = (from, to, count) => Array.from({length: count}, (_, i) => [100, from + (to - from) * i / (count - 1)]);

const SHAPES = {
  knit: {sides: [SLEEVE], inside: NECK, parts: [TORSO, CREW], bands: ["M57 180H143V187Q143 194 136 194H64Q57 194 57 187Z"], sideBands: ["M18 160L50 166L46 177Q44 184 38 183L20 179Q14 177 15 170Z"]},
  shirt: {sides: [SLEEVE], inside: NECK, parts: [SHIRT_TORSO], sideParts: [COLLAR], seams: ["M94 46V196", "M106 44V196", "M112 80H130V100L121 105L112 100Z"], sideSeams: ["M18 163L48 169"], buttons: BUTTONS(62, 182, 6)},
  overshirt: {sides: [SLEEVE], inside: NECK, parts: [SHIRT_TORSO], sideParts: [COLLAR], seams: ["M93 46V196", "M107 44V196"], sideSeams: ["M18 163L48 169", "M68 78H88V100L78 106L68 100Z", "M68 84H88"], buttons: BUTTONS(62, 182, 6)},
  tee: {sides: [SHORT_SLEEVE], inside: NECK, parts: [TORSO, CREW], seams: ["M58 188H142"], sideSeams: ["M24 91L55 102"]},
  polo: {sides: [SHORT_SLEEVE], inside: NECK, parts: [V_TORSO.replace("L100 92Z", "L100 50Z")], sideParts: [COLLAR], seams: ["M93 46V92H107V46", "M58 188H142"], sideBands: ["M24 91L55 101L52 110L26 102Q20 100 22 94Z"], buttons: BUTTONS(62, 82, 2)},
  cardigan: {sides: [SLEEVE], inside: "M76 27C86 22 114 22 124 27L100 92Z", parts: [V_TORSO], bands: ["M57 180H143V187Q143 194 136 194H64Q57 194 57 187Z"], sideBands: ["M18 160L50 166L46 177Q44 184 38 183L20 179Q14 177 15 170Z"], seams: ["M70 29L95 96V194", "M130 29L106 96V194"], sideSeams: ["M66 140H88V166H66"], buttons: BUTTONS(102, 184, 5)},
  jacket: {sides: [SLEEVE], inside: NECK, parts: [TORSO.replace("C120 45 80 45 75 29", "L100 48L75 29")], sideParts: [COLLAR], seams: ["M100 48V194", "M107 46V194", "M112 72H132V94H112Z"], sideSeams: ["M18 163L48 169", "M65 132H91V172Q91 176 87 176H69Q65 176 65 172Z", "M65 139H91"], buttons: BUTTONS(62, 176, 5)},
  puffer: {sides: ["M64 36C42 40 32 60 27 88L10 168Q9 178 18 180L38 184Q47 184 49 176L68 106Z"], inside: NECK, parts: ["M76 30C64 32 59 35 55 40L58 110L52 186Q52 195 62 195H138Q148 195 148 186L142 110L145 40C141 35 136 32 124 30Z", "M74 34C72 24 75 15 82 13H118C125 15 128 24 126 34Q100 44 74 34Z"], seams: ["M100 14V195", "M73 24Q100 32 127 24"], sideSeams: ["M69 152L85 172"]},
  tank: {inside: "M78 22C82 60 118 60 122 22L124 34C122 60 78 60 76 34Z", parts: ["M72 20L82 18C82 60 118 60 118 18L128 20C126 56 134 72 146 80L141 120L144 188Q144 194 138 194H62Q56 194 56 188L59 120L54 80C66 72 74 56 72 20Z"], seams: ["M57 188H143", "M78 19C78 66 122 66 122 19"]},
  dress: {sides: [SHORT_SLEEVE], inside: NECK, parts: ["M76 28C66 32 61 35 57 40L64 112L44 202Q44 208 50 208H150Q156 208 156 202L136 112L143 40C139 35 134 32 124 28L100 46Z", "M63 108H137V118H63Z"], sideParts: [COLLAR], seams: ["M95 46V108M95 118V208", "M106 44V108M106 118V208"], sideSeams: ["M74 122L62 206", "M24 91L55 102"], buttons: [...BUTTONS(60, 98, 3), ...BUTTONS(134, 190, 4)]},
  skirt: {parts: ["M66 38H134L160 176Q160 182 154 183Q100 192 46 183Q40 182 40 176Z", "M66 34H134V50H66Z"], seams: ["M100 50V187", "M42 172Q100 182 158 172"], sideSeams: ["M66 60Q84 68 88 50"], buttons: [[100, 42]]},
  jeans: {parts: [PANTS, "M66 16H134V28H66Z"], seams: ["M100 28V82", "M106 28V54Q105 60 100 62", "M53 194H92M108 194H147"], sideSeams: ["M66 46Q84 48 88 28"], buttons: [[100, 22]]},
  chino: {parts: [PANTS, "M66 16H134V28H66Z"], seams: ["M100 28V82", "M106 28V54Q105 60 100 62"], sideSeams: ["M76 28L66 56", "M76 40L73 192"], buttons: [[100, 22]]},
  shorts: {parts: ["M60 52H140L158 150Q158 156 152 157L112 162L100 100L88 162L48 157Q42 156 42 150Z", "M60 48H140V64H60Z"], seams: ["M100 64V100", "M95 64Q92 78 94 88M105 64Q108 76 106 84"], sideSeams: ["M44 144L90 150", "M70 64L60 92"]},
  scarf: {parts: ["M60 30Q100 4 140 30L132 62Q100 80 68 62Z", "M112 58L140 50L150 188H114Z", "M62 44L92 62L94 168H58Z"], seams: ["M66 48Q100 66 134 46"], fringe: [[58, 94, 168], [114, 150, 188]]},
  beanie: {parts: ["M44 136C40 70 66 40 100 40C134 40 160 70 156 136Z", "M38 128Q100 112 162 128L160 166Q100 154 40 166Z"]},
  tote: {straps: ["M72 84V50Q72 22 100 22Q128 22 128 50V84"], parts: ["M42 74H158L164 192Q164 198 158 198H42Q36 198 36 192Z"], seams: ["M42 84H158", "M66 74V96H78V74M122 74V96H134V74"]},
  pouch: {parts: ["M30 78H170Q176 78 176 84L170 156Q169 164 162 164H38Q31 164 30 156L24 84Q24 78 30 78Z"], seams: ["M26 92H174", "M34 88H166"], zip: "M158 90L164 112"},
  foulard: {parts: ["M22 64L178 64L100 186Z", "M34 46L166 46L178 64H22Z"], seams: ["M36 72H164L100 172Z"]},
};

const GARMENTS = {
  "Pull en laine côtelée": ["knit", "rib", "Mailles"], "Chemise Oxford": ["shirt", "weave", "Hauts"],
  "Jean droit": ["jeans", "denim", "Bas"], "T-shirt en coton épais": ["tee", "plain", "Hauts"],
  "Veste de travail": ["jacket", "twill", "Vestes"], "Cardigan en mérinos": ["cardigan", "fine", "Mailles"],
  "Pantalon chino": ["chino", "twill", "Bas"], "Sweat à col rond": ["knit", "plain", "Mailles"],
  "Robe chemise": ["dress", "plain", "Robes et jupes"], "Jupe en denim": ["skirt", "denim", "Robes et jupes"],
  "Polo en maille": ["polo", "fine", "Mailles"], "Short en lin": ["shorts", "weave", "Bas"],
  "Blouson matelassé": ["puffer", "quilt", "Vestes"], "Débardeur côtelé": ["tank", "rib", "Hauts"],
  "Surchemise en flanelle": ["overshirt", "plaid", "Vestes"], "Écharpe en laine": ["scarf", "rib", "Accessoires"],
  "Bonnet en mérinos": ["beanie", "rib", "Accessoires"], "Sac cabas en toile": ["tote", "weave", "Accessoires"],
  "Pochette zippée": ["pouch", "weave", "Accessoires"], "Foulard imprimé": ["foulard", "print", "Accessoires"],
};

// [tile width, tile height, marks], drawn with the seam color.
const TEXTURES = {
  rib: [5, 10, '<path d="M1.2 0V10" stroke-width="1.5" opacity=".75"/><path d="M3.8 0V10" stroke-width=".5" opacity=".35"/>'],
  fine: [2.6, 4, '<path d="M.6 0V4" stroke-width=".6" opacity=".5"/>'],
  weave: [3, 3, '<path d="M0 .5H1.5M1.5 2H3" stroke-width=".7" opacity=".5"/>'],
  twill: [4, 4, '<path d="M-1 5L5 -1M3 5L5 3M-1 1L1 -1" stroke-width=".7" opacity=".4"/>'],
  denim: [3, 3, '<path d="M-1 4L4 -1M2 4L4 2M-1 1L1 -1" stroke-width=".9" opacity=".55"/>'],
  quilt: [40, 19, '<path d="M0 1Q20 5 40 1" stroke-width="2" opacity=".9"/>'],
  plaid: [24, 24, '<path d="M0 6H24M6 0V24" stroke-width="8" opacity=".55"/><path d="M0 18H24M18 0V24" stroke="#dfd6c2" stroke-width="1.2" opacity=".6"/>'],
  print: [18, 18, '<circle cx="5" cy="5" r="2.6" fill="#e6c9a0" stroke="none"/><circle cx="14" cy="14" r="1.6" fill="#e9e3d6" stroke="none"/>'],
};

let count = 0;

function mix(hex, target, amount) {
  const channels = (color) => [1, 3, 5].map((i) => parseInt(color.slice(i, i + 2), 16));
  const to = channels(target);
  return `rgb(${channels(hex).map((value, i) => Math.round(value + (to[i] - value) * amount)).join(" ")})`;
}

export const colorOf = (color) => COLORS[color] ?? "#8a8a84";
export const categoryOf = (name) => GARMENTS[name]?.[2] ?? "Accessoires";
export const CATEGORIES = [...new Set(Object.values(GARMENTS).map(([, , category]) => category))];

/** SVG markup of a product, built only from this file's own data. */
export function garment(name, color) {
  const [shapeName, texture] = GARMENTS[name] ?? ["tote", "plain"];
  const shape = SHAPES[shapeName];
  const base = colorOf(color);
  const light = [1, 3, 5].reduce((sum, i) => sum + parseInt(base.slice(i, i + 2), 16), 0) / 765;
  const seam = light < .22 ? mix(base, "#ffffff", .26) : mix(base, "#1c140c", light > .8 ? .24 : .38);
  const id = "g" + count++;
  const [width, height, marks] = TEXTURES[texture] ?? [];
  const fabric = (d) => `<path d="${d}" fill="${base}"/>`
    + (marks ? `<path d="${d}" fill="url(#${id}-t)"/>` : "")
    + `<path d="${d}" fill="url(#${id}-l)" stroke="${seam}" stroke-width="1"/>`;
  const ribbed = (d) => `<path d="${d}" fill="${base}"/><path d="${d}" fill="url(#${id}-r)"/><path d="${d}" fill="url(#${id}-l)" stroke="${seam}" stroke-width=".8"/>`;
  const line = (d) => `<path d="${d}" fill="none" stroke="${seam}" stroke-width="1.1"/>`;
  const draw = (list, paint) => (list ?? []).map(paint).join("");
  const mirror = (markup) => markup && markup + `<g transform="matrix(-1 0 0 1 200 0)">${markup}</g>`;
  const fringe = (shape.fringe ?? []).map(([from, to, y]) => {
    let d = "";
    for (let x = from + 2; x < to; x += 4) d += `M${x} ${y}v13`;
    return `<path d="${d}" stroke="${base}" stroke-width="2.2"/>`;
  }).join("");
  const buttons = (shape.buttons ?? []).map(([x, y]) =>
    `<circle cx="${x}" cy="${y}" r="2.3" fill="${mix(base, "#fff8ea", .55)}" stroke="${seam}" stroke-width=".7"/>`).join("");
  const pattern = (suffix, [w, h, content]) =>
    `<pattern id="${id}-${suffix}" width="${w}" height="${h}" patternUnits="userSpaceOnUse"><g stroke="${seam}" fill="none">${content}</g></pattern>`;

  return `<svg viewBox="0 0 200 220" aria-hidden="true" focusable="false" stroke-linejoin="round" stroke-linecap="round"><defs>
    ${marks ? pattern("t", [width, height, marks]) : ""}${pattern("r", TEXTURES.rib)}
    <linearGradient id="${id}-l" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#fff" stop-opacity=".16"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".2"/></linearGradient></defs>
    ${draw(shape.straps, (d) => `<path d="${d}" fill="none" stroke="${seam}" stroke-width="8"/><path d="${d}" fill="none" stroke="${base}" stroke-width="6"/>`)}
    ${mirror(draw(shape.sides, fabric) + draw(shape.sideBands, ribbed))}
    ${shape.inside ? `<path d="${shape.inside}" fill="${mix(base, "#000000", .42)}"/>` : ""}
    ${fringe}${draw(shape.parts, fabric)}${draw(shape.bands, ribbed)}
    ${draw(shape.seams, line)}${mirror(draw(shape.sideSeams, line))}${mirror(draw(shape.sideParts, fabric))}${buttons}
    ${shape.zip ? `<path d="${shape.zip}" stroke="#c9b27e" stroke-width="5"/>` : ""}</svg>`;
}
