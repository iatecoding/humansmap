// Sort options exposed by the person/org leaderboards.
//
// Only sorts the backend can serve from an index are listed. Sorting persons
// by nationalities[0] cannot use the nationalities[*] array index and
// full-scans the collection, so it is deliberately not offered in the UI.

export const PERSON_SORT_DEFAULT = 'degree'
export const ORG_SORT_DEFAULT = 'members'

export const PERSON_SORTS = ['degree', 'birth_date']
export const ORG_SORTS = ['members', 'founded']

const LABEL_KEYS = {
  degree: 'sortDegree',
  birth_date: 'sortBirth',
  members: 'sortMembers',
  founded: 'sortFounded',
}

/** i18n key for a sort option, falling back to the raw key. */
export function sortLabelKey(sort) {
  return LABEL_KEYS[sort] || sort
}

/** Default sort for the active tab. */
export function sortDefault(tab) {
  return tab === 'orgs' ? ORG_SORT_DEFAULT : PERSON_SORT_DEFAULT
}

/** Sort options for the active tab. */
export function sortOptions(tab) {
  return tab === 'orgs' ? ORG_SORTS : PERSON_SORTS
}

/** True when a sort value is valid for the given tab. */
export function isValidSort(tab, sort) {
  return sortOptions(tab).includes(sort)
}
