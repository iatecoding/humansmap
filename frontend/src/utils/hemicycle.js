/**
 * Hemicycle (parliamentary chamber) layout utilities.
 *
 * Westminster-style two-bench layout: government on the left, opposition on
 * the right, with a central aisle. Parties are arranged left-to-right on the
 * political spectrum within each bench.
 */

// Maps sub-party QIDs to their canonical parent party QID.
// Empty: all sub-parties (Scottish Labour, Welsh Labour, etc.) display as their own
// squares at the top of the hemicycle so regional groups remain visible.
export const PARTY_PARENT_MAP = {}

// Sub-parties whose member seat dots should use the parent party's color.
// The party square at the top keeps its own distinct color for visual identity;
// only the individual MP dots are unified with the parent color.
export const PARTY_DOT_COLOR_OVERRIDE = {
  'Q3179541': 'Q9630',  // Scottish Labour dots → Labour red
  'Q832685':  'Q9630',  // Welsh Labour dots → Labour red
}

// Parties whose elected members do not take their seats in the chamber.
// Their dots are rendered muted/hollow in the hemicycle view.
export const NON_SITTING_PARTIES = new Set([
  'Q76382',   // Sinn Féin
])

// Per-member party overrides for known Wikidata mismatches.
// Affects only the hemicycle display — DB values are untouched.
// '__independent__' = show as Independent (grey dot).
export const MEMBER_PARTY_OVERRIDE = {
  // P102 edge missing in Wikidata — these are Labour
  'Q24061230': 'Q9630',  // Florence Eshalomi
  'Q75292880': 'Q9630',  // James Murray
  'Q30163380': 'Q9630',  // Jo Platt
  'Q6698469':  'Q9630',  // Lucy Powell
  'Q501926':   'Q9630',  // Jonathan Reynolds
  'Q20648420': 'Q9630',  // Anna Turley
  'Q30164792': 'Q9630',  // Alex Sobel
  // Sitting as Independent despite Labour label in Wikidata
  'Q291169':   '__independent__',  // Jeremy Corbyn
  'Q63929279': '__independent__',  // Zarah Sultana
  'Q30164380': '__independent__',  // Rosie Duffield
}

// Official or widely-recognised colors for UK political parties.
// Keyed by Wikidata QID.
export const PARTY_COLORS = {
  'Q76382':    '#006E37', // Sinn Féin — deep Irish green
  'Q175443':   '#1A8A7E', // SDLP — teal (pulled clear of the green cluster)
  'Q9669':     '#4DB523', // Green Party — vivid lime green
  'Q244927':   '#F6CB2F', // Alliance Party of Northern Ireland — bright yellow
  'Q104721252':'#00BBCC', // Reform UK — bright cyan-teal
  'Q135183212':'#006B7A', // Restore Britain — dark teal (distinct from Reform)
  'Q10691':    '#4A8C00', // Plaid Cymru — strong olive green
  'Q10658':    '#F5A500', // Scottish National Party — golden amber (distinct from Alliance yellow)
  'Q9630':     '#E4003B', // Labour Party — bold red
  'Q3179541':  '#C84B00', // Scottish Labour — burnt orange-red (distinct hue from Labour)
  'Q832685':   '#8A1050', // Welsh Labour — deep magenta-burgundy (distinct hue from both)
  'Q9626':     '#0077CC', // Conservative Party — royal blue
  'Q3243587':  '#64B0E8', // Scottish Conservative — pale steel blue
  'Q9624':     '#F09020', // Liberal Democrats — warm amber-orange
  'Q3250438':  '#FFCA28', // Scottish Liberal Democrats — golden yellow (distinct from amber)
  'Q215519':   '#D4521C', // Democratic Unionist Party — Ulster orange
  'Q841045':   '#7E57C2', // Ulster Unionist Party — medium purple
  'Q985076':   '#1A237E', // Traditional Unionist Voice — deep indigo navy
}

// Abbreviated display names to avoid label overlap.
// Falls back to the raw party name if not listed here.
export const PARTY_DISPLAY_NAMES = {
  'Q76382':    'Sinn Féin',
  'Q175443':   'SDLP',
  'Q9669':     'Green',
  'Q244927':   'Alliance',
  'Q104721252':'Reform',
  'Q135183212':'Restore',
  'Q10691':    'Plaid',
  'Q10658':    'SNP',
  'Q9630':     'Labour',
  'Q3179541':  'Scot Labour',
  'Q832685':   'Welsh Labour',
  'Q9626':     'Cons',
  'Q3243587':  'Scot Cons',
  'Q9624':     'Lib Dems',
  'Q3250438':  'Scot Lib Dems',
  'Q215519':   'DUP',
  'Q841045':   'UUP',
  'Q985076':   'TUV',
}

// Left-to-right political spectrum order (lower = further left).
// Sub-parties placed immediately after their parent.
export const PARTY_ORDER = {
  'Q76382':    1,    // Sinn Féin
  'Q175443':   3,    // SDLP
  'Q9669':     4,    // Green Party
  'Q9630':     6,    // Labour
  'Q3179541':  6.1,  // Scottish Labour
  'Q832685':   6.2,  // Welsh Labour
  'Q10691':    8,    // Plaid Cymru
  'Q10658':    9,    // SNP
  'Q244927':   11,   // Alliance
  'Q9624':     12,   // Lib Dems
  'Q3250438':  12.1, // Scottish Liberal Democrats
  'Q215519':   14,   // DUP
  'Q841045':   15,   // UUP
  'Q985076':   16,   // TUV
  'Q9626':     18,   // Conservative
  'Q3243587':  18.1, // Scottish Conservative
  'Q104721252':20,   // Reform UK
  'Q135183212':20.1, // Restore Britain
}

// Auto-assign fallback colors for unlisted parties.
const FALLBACK_PALETTE = [
  '#7B68EE', '#CD853F', '#20B2AA', '#FF6347', '#4169E1',
  '#DAA520', '#6495ED', '#DC143C', '#32CD32', '#FF69B4',
]
let _fallbackIdx = 0
const _assignedColors = {}

export function partyColor(qid) {
  if (PARTY_COLORS[qid]) return PARTY_COLORS[qid]
  if (_assignedColors[qid]) return _assignedColors[qid]
  const c = FALLBACK_PALETTE[_fallbackIdx % FALLBACK_PALETTE.length]
  _fallbackIdx++
  _assignedColors[qid] = c
  return c
}

export function partyDisplayName(qid, rawName) {
  return PARTY_DISPLAY_NAMES[qid] ?? rawName ?? 'Independent'
}

export function partyOrderVal(qid) {
  return PARTY_ORDER[qid] ?? 50
}

/**
 * Group raw members (from API) into ordered party buckets.
 *
 * @param {Array}  members         - [{qid, name, image, party_qid, party_name}]
 * @param {number} otherThreshold  - parties with fewer seats are merged into "Other"
 *                                   (default 1 = keep every party with ≥1 member)
 * @returns {Array} sorted party buckets, each: {qid, name, color, order, isSynthetic, members[]}
 */
export function groupByParty(members, otherThreshold = 1) {
  const buckets = {}

  for (const m of members) {
    const rawPqid = m.party_qid ?? null
    // Apply per-member override first, then sub-party merge, then raw value.
    const pqid = m.qid in MEMBER_PARTY_OVERRIDE
      ? MEMBER_PARTY_OVERRIDE[m.qid]
      : rawPqid ? (PARTY_PARENT_MAP[rawPqid] ?? rawPqid) : '__independent__'
    const pname = pqid !== '__independent__'
      ? partyDisplayName(pqid, m.party_name)
      : 'Independent'

    if (!buckets[pqid]) {
      const ownColor = pqid === '__independent__' ? '#888888' : partyColor(pqid)
      const dotParent = PARTY_DOT_COLOR_OVERRIDE[pqid]
      buckets[pqid] = {
        qid: pqid,
        name: pname,
        color: ownColor,
        dotColor: dotParent ? partyColor(dotParent) : ownColor,
        order: pqid === '__independent__' ? 49 : partyOrderVal(pqid),
        isSynthetic: !pqid.startsWith('Q'), // __independent__, __other__, etc.
        nonSitting: NON_SITTING_PARTIES.has(pqid),
        members: [],
      }
    }
    buckets[pqid].members.push(m)
  }

  const sorted = Object.values(buckets).sort((a, b) => a.order - b.order)
  const main = []
  const otherMembers = []

  for (const p of sorted) {
    if (!p.isSynthetic && p.members.length < otherThreshold) {
      otherMembers.push(...p.members)
    } else {
      main.push(p)
    }
  }

  if (otherMembers.length > 0) {
    main.push({
      qid: '__other__',
      name: 'Other',
      color: '#AAAAAA',
      order: 98,
      isSynthetic: true,
      members: otherMembers,
    })
  }

  const result = main.sort((a, b) => a.order - b.order)
  result.forEach(p => p.members.sort((a, b) => (a.name || '').localeCompare(b.name || '')))
  return result
}

/**
 * Compute x,y seat positions for the Westminster horizontal bench layout.
 *
 * Government bench (LEFT): the party with the most seats — proxy for the
 * ruling party. Opposition bench (RIGHT): all other parties, ordered from
 * most-left-leaning (closest to aisle) to most-right-leaning (wall).
 * Seats fill column-by-column (front/aisle → back/wall).
 *
 * @param {Array}  parties  - output of groupByParty()
 * @param {Object} opts
 * @returns {{ seats, dotR, govCols, oppCols, govAisleX, oppAisleX, height }}
 */
export function computeWestminster(parties, opts = {}) {
  const {
    aisleX    = 450,  // x-centre of the central table/aisle
    aisleHalf = 30,   // half-width of the aisle gap
    rowH      = 28,   // vertical spacing between bench rows
    seatW     = 11,   // horizontal spacing between seats on the same bench
    rows      = 13,   // number of bench rows on each side
    topY      = 60,   // y of the first (front) bench row
    dotR      = 5,    // dot radius (graph units)
  } = opts

  // Largest party by seat count → government bench (LEFT).
  // Regional sub-parties share a floor(order) with their parent (e.g. Scottish Labour
  // is 6.1, Welsh Labour 6.2 — same floor as Labour 6), so they travel with the
  // government party to the same bench. All other parties sit on the RIGHT.
  const largest = [...parties].sort((a, b) => b.members.length - a.members.length)[0]
  const govFloor = largest ? Math.floor(largest.order) : -1
  const govParties = parties.filter(p => Math.floor(p.order) === govFloor)
  const oppParties = parties.filter(p => Math.floor(p.order) !== govFloor)
  // oppParties retains the ideology order from groupByParty (left → right from aisle).

  const govTotal = govParties.reduce((s, p) => s + p.members.length, 0)
  const oppTotal = oppParties.reduce((s, p) => s + p.members.length, 0)

  const govCols = Math.max(1, Math.ceil(govTotal / rows))
  const oppCols = Math.max(1, Math.ceil(oppTotal / rows))

  const govAisleX = aisleX - aisleHalf  // right edge of government bank
  const oppAisleX = aisleX + aisleHalf  // left edge of opposition bank

  // Generate seat positions: column-first (front → back).
  // dir = -1: seats spread leftward (government)
  // dir = +1: seats spread rightward (opposition)
  // reverseColumns: iterate from the far column inward so seat assignment
  // (alphabetical) reads left-to-right on the government bench.
  function gridPositions(aisleEdge, cols, dir, reverseColumns = false) {
    const pos = []
    for (let i = 0; i < cols; i++) {
      const col = reverseColumns ? (cols - 1 - i) : i
      for (let row = 0; row < rows; row++) {
        pos.push({
          x: aisleEdge + dir * (col + 0.5) * seatW,
          y: topY + row * rowH,
        })
      }
    }
    return pos
  }

  const govGrid = gridPositions(govAisleX, govCols, -1, true)
  const oppGrid = gridPositions(oppAisleX, oppCols, +1)

  const seats = []

  let idx = 0
  for (const party of govParties) {
    for (const member of party.members) {
      if (idx >= govGrid.length) break
      seats.push({ ...govGrid[idx++], member, party, side: 'gov' })
    }
  }

  idx = 0
  for (const party of oppParties) {
    for (const member of party.members) {
      if (idx >= oppGrid.length) break
      seats.push({ ...oppGrid[idx++], member, party, side: 'opp' })
    }
  }

  return {
    seats,
    dotR,
    govCols,
    oppCols,
    govAisleX,
    oppAisleX,
    height: topY + rows * rowH + 10,
  }
}
