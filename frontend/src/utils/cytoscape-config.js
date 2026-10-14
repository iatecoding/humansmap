// 16 editorial, ink-like colors for edge types — muted for paper canvas
const PALETTE = [
  '#2d6b9e', '#7b3f28', '#3a7a64', '#8b2525',
  '#4a6840', '#9b5218', '#3d4f82', '#7a3555',
  '#28706b', '#6e5825', '#5a3878', '#3a5e36',
  '#8c6328', '#2e4068', '#6b3028', '#455a45',
]

// Groups of edge types that share a color and should collapse into one filter entry
export const EDGE_COLLAPSE_GROUPS = [
  { types: ['founder', 'owner'],                              labelKey: 'ringFounders' },
  { types: ['chief_executive', 'chairperson', 'board_member'], labelKey: 'ringDirectors' },
]

// Quick lookup: type → group index
export const EDGE_TYPE_GROUP = new Map(
  EDGE_COLLAPSE_GROUPS.flatMap((g, i) => g.types.map((t) => [t, i]))
)

// Edge types that map to org-circle role colors — pre-seeded so legend matches ring borders
const ROLE_EDGE_COLORS = {
  founder:         '#c08020',  // amber — matches founders ring
  owner:           '#c08020',
  chief_executive: '#2e8a7a',  // teal — matches directors ring
  chairperson:     '#2e8a7a',
  board_member:    '#2e8a7a',
  // Phase 4: person↔person non-family (§2a / 4.1)
  unmarried_partner:        '#b85c7a',  // rose — warm, intimate but distinct from spouse ring colors
  doctoral_advisor:         '#4a6e9e',  // muted steel blue — academic, authority
  doctoral_student:         '#6a8cb8',  // lighter steel blue — paired with advisor, subordinate hue
  professional_collaborator:'#5e7a5a',  // muted sage green — collegial, cooperative
  // Phase 4: org↔org structure (§2b / 4.3)
  parent_of:    '#7a5c38',  // warm brown — hierarchy upward (child→parent)
  subsidiary_of:'#9e7848',  // lighter warm tan — hierarchy downward (parent→sub)
  owned_by:     '#6b5080',  // muted violet — ownership inbound
  owns:         '#8a6aa0',  // lighter violet — ownership outbound; paired with owned_by
}

const edgeColorMap = new Map(Object.entries(ROLE_EDGE_COLORS))
let colorIndex = 0

export function getEdgeColor(type) {
  if (!edgeColorMap.has(type)) {
    edgeColorMap.set(type, PALETTE[colorIndex % PALETTE.length])
    colorIndex++
  }
  return edgeColorMap.get(type)
}

export function resetEdgeColors() {
  // Restore pre-seeded role colors, then clear the rest
  edgeColorMap.clear()
  Object.entries(ROLE_EDGE_COLORS).forEach(([k, v]) => edgeColorMap.set(k, v))
  colorIndex = 0
}

export function setEdgeColors(types) {
  resetEdgeColors()
  types.forEach((t) => getEdgeColor(t))
}

export function getEdgeColorMap() {
  return new Map(edgeColorMap)
}

// edgeTypes: the translations map (t.value.edgeTypes or t.edgeTypes from the caller)
export function translateEdgeType(type, edgeTypes = {}) {
  return edgeTypes[type] || type.replace(/_/g, ' ')
}

// Occupation-based node colors — desaturated for paper theme
const OCCUPATION_COLORS = {
  politician:       '#2e5fa3',
  businessperson:   '#2e7a3e',
  journalist:       '#c07820',
  lawyer:           '#5a3278',
  judge:            '#7a2020',
  military_officer: '#2a6858',
  criminal:         '#b82020',
  actor:            '#8b3568',
  writer:           '#5e7020',
  musician:         '#207a8b',
  banker:           '#9b5218',
  investor:         '#3d4f82',
  activist:         '#7a3555',
}

// Family relationship border colors — used in useGraph.js and mirrored in GraphLegend.vue
export const FAMILY_BORDER_COLORS = {
  parent:   '#c05818',   // deep burnt orange — parents
  sibling:  '#2e78b8',   // medium blue — siblings (lateral)
  relative: '#8a7020',   // dark golden/mustard — extended relatives
}
// Spouse unit colors — 4 hues for up to 4 spouses on the warm cream canvas
// (#f2ede3). Chosen to stay visually distinct from:
//   - root node amber border (#d4920a);
//   - fixed family colors: parent orange, sibling blue, relative mustard,
//     child-unknown purple;
//   - occupation node fills, especially businessperson (#2e7a3e) and
//     military_officer (#2a6858) — a greenish border on a greenish fill
//     visually disappears.
// Order: 1st rose · 2nd teal · 3rd slate-violet · 4th magenta.
export const FAMILY_UNIT_COLORS = ['#c22a66', '#1a8fa4', '#6856a5', '#9a2ca0']
// Children whose parent's spouse is unknown — deep indigo, distinct from the
// magenta unit[3] so a 4-spouse family never collides with the fallback.
export const FAMILY_CHILD_UNKNOWN_COLOR = '#3a2a78'

export function getNodeColor(occupations) {
  if (!occupations || !occupations.length) return '#8a7b6e'
  for (const occ of occupations) {
    if (OCCUPATION_COLORS[occ]) return OCCUPATION_COLORS[occ]
  }
  return '#6b5e52'
}

export function getCytoscapeStyle(imageUrlFn, themeOpts = {}) {
  const {
    textOutlineColor = '#f2ede3',
    labelColor = '#1c1510',
    edgeLabelColor = '#6b5e52',
  } = themeOpts
  return [
    {
      selector: 'node',
      style: {
        shape: 'ellipse',  // explicit default — catches nodes with missing entityType
        label: 'data(label)',
        'text-valign': 'bottom',
        'text-halign': 'center',
        'font-size': '14px',
        'font-weight': 600,
        'font-family': 'DM Sans, sans-serif',
        'text-margin-y': 8,
        'text-max-width': '120px',
        'text-wrap': 'wrap',
        'min-zoomed-font-size': 8,
        color: labelColor,
        'text-outline-color': textOutlineColor,
        'text-outline-width': 3,
        width: 'data(size)',
        height: 'data(size)',
        'background-color': (ele) => ele.data('color') || '#8a7b6e',
        'border-width': 2,
        'border-color': 'rgba(0,0,0,0.08)',
        'background-fit': 'cover',
        'background-clip': 'node',
      },
    },
    {
      // Orgs as rounded rectangles — label always below the shape
      selector: 'node[entityType="organization"]',
      style: {
        shape: 'round-rectangle',
        'corner-radius': 3,
        'text-valign': 'bottom',
        'text-halign': 'center',
        'text-margin-y': 8,
        'font-size': '11px',
      },
    },
    {
      // Wikidata photography is predominantly portrait-oriented. Keep roughly
      // the same node area as the old circles so graph density stays comparable.
      selector: 'node[entityType="person"]',
      style: {
        shape: 'round-rectangle',
        width: (ele) => Math.max(38, ele.data('size') * 0.82),
        height: (ele) => ele.data('size') * 1.22,
        'corner-radius': 6,
      },
    },
    {
      // Events as hexagons — distinct from both circle and diamond
      // Minor size compensation ×1.1: hexagon fills ~86% of bounding box vs circle
      selector: 'node[entityType="event"]',
      style: {
        shape: 'hexagon',
        width: (ele) => ele.data('size') * 1.1,
        height: (ele) => ele.data('size') * 1.1,
      },
    },
    {
      selector: 'node[hasImage]',
      style: {
        'background-image': 'data(imageUrl)',
        'background-fit': 'cover',
        'background-clip': 'node',  // clips photo to node shape (ellipse/squircle)
      },
    },
    {
      selector: 'node.root-node',
      style: {
        'border-width': 4,
        'border-color': '#d4920a',
        'border-opacity': 1,
      },
    },
    {
      selector: 'node:selected',
      style: {
        'border-width': 6,
        'border-color': '#b03030',
        'overlay-opacity': 0.08,
      },
    },
    {
      selector: 'edge',
      style: {
        width: 2.2,
        opacity: 0.3,
        'line-color': 'data(color)',
        'target-arrow-color': 'data(color)',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 1.2,
        'curve-style': 'bezier',
        'control-point-step-size': 60,
        label: '',
        'font-size': '10.5px',
        'font-family': 'DM Sans, sans-serif',
        'text-rotation': 'autorotate',
        'text-margin-y': -8,
        color: edgeLabelColor,
        'text-outline-color': textOutlineColor,
        'text-outline-width': 1.5,
      },
    },
    {
      selector: 'edge:selected',
      style: {
        width: 4,
        opacity: 0.9,
        'line-color': '#b03030',
      },
    },
    {
      selector: 'node.ring-circle',
      style: {
        'background-opacity': 0,
        'border-width': 1.5,
        'border-style': 'dashed',
        'border-color': 'rgba(138, 130, 120, 0.3)',  // warm grey from --color-org
        'border-opacity': 1,
        shape: 'ellipse',
        width: 'data(ringSize)',
        height: 'data(ringSize)',
        label: 'data(ringLabel)',
        'text-valign': 'top',
        'text-halign': 'right',
        'text-margin-x': -12,
        'text-margin-y': 6,
        'font-size': '11px',
        'font-weight': 500,
        'font-family': 'DM Sans, sans-serif',
        color: 'rgba(138, 130, 120, 0.5)',  // warm grey, readable
        'text-outline-color': textOutlineColor,
        'text-outline-width': 2,
        'min-zoomed-font-size': 0,
        events: 'no',
        'z-index': 0,
        'z-compound-depth': 'bottom',
      },
    },
    {
      selector: 'node.dimmed',
      style: { opacity: 0.25 },
    },
    {
      selector: 'edge.dimmed',
      style: { opacity: 0.08 },
    },
    {
      selector: '.highlighted',
      style: { opacity: 1 },
    },
    {
      selector: '.layout-hidden',
      style: { display: 'none' },
    },
    {
      // Ghost party-name and count nodes — invisible bodies used as positioned labels
      // above/below parliament party squares. Inline styles supply the actual styling;
      // this rule suppresses the default node background before they apply.
      selector: 'node[entityType="_pname"]',
      style: {
        'background-opacity': 0,
        'border-width': 0,
        width: 1,
        height: 1,
        events: 'no',
        'z-index': 1,
      },
    },
    {
      // Parliament aisle divider — a narrow tan rectangle in graph space that
      // tracks zoom/pan alongside the seat nodes. Non-interactive.
      selector: 'node.parliament-aisle',
      style: {
        shape: 'rectangle',
        width: 6,
        height: 'data(aisleHeight)',
        'background-color': '#e0d8cc',
        'border-width': 0,
        label: '',
        'text-opacity': 0,
        events: 'no',
        'z-index': 0,
        'z-compound-depth': 'bottom',
      },
    },
  ]
}

export function getLayoutOptions(nodeCount) {
  // cose-bilkent produces better layouts for star topologies and handles
  // node overlap natively via its quality:'proof' mode.
  return {
    name: 'cose-bilkent',
    animate: 'end',
    animationDuration: 400,
    randomize: true,
    nodeRepulsion: Math.max(6000, nodeCount * 350),
    idealEdgeLength: Math.max(80, Math.min(200, nodeCount * 3)),
    edgeElasticity: 0.45,
    nestingFactor: 0.1,
    gravity: 0.25,
    gravityRange: 3.8,
    numIter: 2500,
    tilingPaddingVertical: 10,
    tilingPaddingHorizontal: 10,
    nodeDimensionsIncludeLabels: true,
    quality: 'default',
  }
}
