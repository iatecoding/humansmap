/*
 * HumansMap static showcase.
 *
 * A backend-free re-implementation of the core browsing experience: search,
 * person/organization/event pages, and a Cytoscape graph. Everything runs in
 * the browser from a single JSON file (a small real Wikidata slice).
 */
(function () {
  'use strict';

  var app = document.getElementById('app');

  var TYPE = { persons: 'person', organizations: 'organization', events: 'event' };
  var TYPE_LABEL = { person: 'Person', organization: 'Organization', event: 'Event' };
  var COLOR = { person: '#bf6b28', organization: '#8a8278', event: '#ce9178' };
  var COUNTRY = {
    Q30: 'United States', Q38: 'Italy', Q145: 'United Kingdom', Q142: 'France',
    Q183: 'Germany', Q29: 'Spain', Q55: 'Netherlands', Q34: 'Sweden',
    Q155: 'Brazil', Q16: 'Canada', Q148: 'India', Q159: 'Russia',
    Q84: 'London', Q142: 'France'
  };

  var DATA = null;
  var IDX = null;
  var cy = null;

  /* ----------------------------- helpers ---------------------------------- */

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function initials(name) {
    if (!name) return '?';
    var parts = String(name).trim().split(/\s+/);
    return ((parts[0] || '')[0] + (parts.length > 1 ? (parts[parts.length - 1] || '')[0] : '')).toUpperCase();
  }

  function titleCase(s) {
    return String(s || '').replace(/_/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
  }

  function qidOf(id) { return String(id).split('/')[1]; }

  function nodeHref(id) {
    var coll = String(id).split('/')[0];
    if (coll === 'persons') return '#/person/' + qidOf(id);
    if (coll === 'organizations') return '#/org/' + qidOf(id);
    if (coll === 'events') return '#/event/' + qidOf(id);
    return '#/';
  }

  function wikidataUrl(qid) { return 'https://www.wikidata.org/wiki/' + qid; }

  function byKey(arr, key) {
    var m = {};
    (arr || []).forEach(function (d) { m[d[key]] = d; });
    return m;
  }

  /* ----------------------------- indexing --------------------------------- */

  function buildIndex(data) {
    var nodes = {};
    var lists = { person: [], organization: [], event: [] };

    ['persons', 'organizations', 'events'].forEach(function (coll) {
      var type = TYPE[coll];
      (data[coll] || []).forEach(function (doc) {
        var id = coll + '/' + doc._key;
        nodes[id] = { id: id, coll: coll, type: type, doc: doc };
        lists[type].push(nodes[id]);
      });
    });

    var edges = [];
    var adj = {};
    ['relationships', 'affiliations', 'involvements', 'org_structure'].forEach(function (coll) {
      (data[coll] || []).forEach(function (e) {
        if (!nodes[e._from] || !nodes[e._to]) return;
        var edge = { from: e._from, to: e._to, type: e.type, coll: coll, property: e.wikidata_property, role: e.role };
        edges.push(edge);
        (adj[e._from] = adj[e._from] || []).push({ edge: edge, other: e._to, dir: 'out' });
        (adj[e._to] = adj[e._to] || []).push({ edge: edge, other: e._from, dir: 'in' });
      });
    });

    var orgMembers = {};
    edges.forEach(function (e) {
      if (e.coll !== 'affiliations') return;
      if (String(e.to).indexOf('organizations/') !== 0) return;
      if (e.type === 'insider_of' || e.type === 'holds_shares_of') return;
      orgMembers[e.to] = (orgMembers[e.to] || 0) + 1;
    });

    return {
      nodes: nodes, lists: lists, edges: edges, adj: adj, orgMembers: orgMembers,
      topPeople: lists.person.slice().sort(function (a, b) {
        return (b.doc.wikidata_statements_count || 0) - (a.doc.wikidata_statements_count || 0);
      }),
      topOrgs: lists.organization.slice().sort(function (a, b) {
        return (orgMembers[b.id] || 0) - (orgMembers[a.id] || 0);
      })
    };
  }

  function search(term) {
    var q = term.trim().toLowerCase();
    if (!q) return [];
    var out = [];
    Object.keys(IDX.nodes).forEach(function (id) {
      var n = IDX.nodes[id];
      var hay = (n.doc.name_en || '') + ' ' + (n.doc.name_it || '') + ' ' + (n.doc.description_en || '');
      if (hay.toLowerCase().indexOf(q) !== -1) out.push(n);
    });
    out.sort(function (a, b) {
      return (b.doc.wikidata_statements_count || 0) - (a.doc.wikidata_statements_count || 0);
    });
    return out.slice(0, 20);
  }

  /* ----------------------------- rendering -------------------------------- */

  function avatar(node, extraClass) {
    return '<span class="avatar ' + node.type + (extraClass ? ' ' + extraClass : '') + '">' +
      esc(initials(node.doc.name_en)) + '</span>';
  }

  function personCard(node, rank) {
    var d = node.doc;
    var occ = (d.occupations || []).slice(0, 2).map(titleCase).join(', ');
    return '<a class="card" href="' + nodeHref(node.id) + '">' +
      (rank ? '<span class="rank">#' + rank + '</span>' : '') +
      avatar(node) +
      '<h3>' + esc(d.name_en) + '</h3>' +
      '<p>' + esc(d.description_en || '') + '</p>' +
      '<div class="meta">' + esc(occ || '—') + ' · ' + (d.wikidata_statements_count || 0) + ' statements</div>' +
      '</a>';
  }

  function orgCard(node, rank) {
    var d = node.doc;
    var members = IDX.orgMembers[node.id] || 0;
    return '<a class="card" href="' + nodeHref(node.id) + '">' +
      (rank ? '<span class="rank">#' + rank + '</span>' : '') +
      avatar(node) +
      '<h3>' + esc(d.name_en) + '</h3>' +
      '<p>' + esc(d.description_en || '') + '</p>' +
      '<div class="meta">' + esc(titleCase(d.type || 'organization')) +
      (members ? ' · ' + members + ' affiliates' : '') + '</div>' +
      '</a>';
  }

  function renderHome() {
    var s = IDX.lists;
    var html = '';
    html += '<section class="hero">' +
      '<h1>Explore connections between public figures.</h1>' +
      '<p>This is a static, backend-free demo of <strong>HumansMap</strong>. ' +
      'It is generated from a small real slice of Wikidata (CC0): ' +
      'people, organizations, events and the relations between them. ' +
      'Search, entity pages and the graph all run in your browser.</p>' +
      '</section>';

    html += '<div class="notice">' +
      '<strong>This demo differs from the real site.</strong> ' +
      'The live HumansMap is a full-stack application (FastAPI + ArangoDB + ' +
      'Redis + a Vue single-page app) covering millions of entities, with live ' +
      'SEC EDGAR filings, full-depth graph traversal, leaderboards, a ' +
      'path-finding game, a trivia game, a scrollable connection feed, and nine ' +
      'languages. This page is a reduced, offline snapshot: ' +
      s.person.length + ' people, ' + s.organization.length + ' organizations and ' +
      s.event.length + ' events, with search, entity pages and a graph ' +
      're-implemented in plain JavaScript. It is a preview, not the product.' +
      '</div>';

    html += '<div class="hero-media">' +
      '<figure><img src="media/explore-graph.png" alt="The live site: explore graph" loading="lazy" ' +
      'onerror="this.closest(\'figure\').style.display=\'none\'"><figcaption>The live site — Explore</figcaption></figure>' +
      '<figure><img src="media/person-card.png" alt="The live site: a person page" loading="lazy" ' +
      'onerror="this.closest(\'figure\').style.display=\'none\'"><figcaption>The live site — a person’s page</figcaption></figure>' +
      '</div>';

    html += '<div class="stats">' +
      '<div class="stat"><b>' + s.person.length + '</b><span>People</span></div>' +
      '<div class="stat"><b>' + s.organization.length + '</b><span>Organizations</span></div>' +
      '<div class="stat"><b>' + s.event.length + '</b><span>Events</span></div>' +
      '<div class="stat"><b>' + IDX.edges.length + '</b><span>Relations</span></div>' +
      '</div>';

    html += '<div class="searchbox"><input id="search" type="search" ' +
      'placeholder="Search people, organizations, events…" autocomplete="off" /></div>' +
      '<ul class="results" id="results"></ul>';

    html += '<h2 class="section">Most documented people</h2>' +
      '<p class="section-note">Ranked by number of Wikidata statements.</p>' +
      '<div class="cards">' + IDX.topPeople.slice(0, 12).map(function (n, i) { return personCard(n, i + 1); }).join('') + '</div>';

    html += '<h2 class="section">Organizations</h2>' +
      '<p class="section-note">Ranked by number of affiliated people in the dataset.</p>' +
      '<div class="cards">' + IDX.topOrgs.slice(0, 8).map(function (n, i) { return orgCard(n, i + 1); }).join('') + '</div>';

    app.innerHTML = html;
    bindSearch();
  }

  function bindSearch() {
    var input = document.getElementById('search');
    var results = document.getElementById('results');
    if (!input) return;
    input.addEventListener('input', function () {
      var hits = search(input.value);
      results.innerHTML = hits.map(function (n) {
        return '<li><a href="' + nodeHref(n.id) + '">' +
          '<span class="kind">' + TYPE_LABEL[n.type] + '</span>' +
          '<span>' + esc(n.doc.name_en) + '</span></a></li>';
      }).join('');
    });
  }

  function edgePhrase(type) {
    var map = {
      member_of: 'Member of', educated_at: 'Educated at', position_held: 'Position held at',
      employee: 'Employee of', founder: 'Founder of', chief_executive: 'Executive of',
      board_member: 'Board member of', spouse: 'Spouse of', parent: 'Parent of',
      child: 'Child of', sibling: 'Sibling of', participant: 'Participated in',
      owns: 'Owns', holds_shares_of: 'Holds shares of', subsidiary_of: 'Subsidiary of',
      professional_collaborator: 'Collaborated with', relative: 'Relative of',
      unmarried_partner: 'Partner of', filer: 'Filed by'
    };
    return map[type] || titleCase(type);
  }

  function connectionsFor(id) {
    var rows = IDX.adj[id] || [];
    var groups = {};
    rows.forEach(function (r) {
      var label = edgePhrase(r.edge.type);
      (groups[label] = groups[label] || []).push(r);
    });
    return groups;
  }

  function renderConnections(id) {
    var groups = connectionsFor(id);
    var keys = Object.keys(groups).sort();
    if (!keys.length) return '<p class="section-note">No relations in this dataset.</p>';
    return '<div class="connections"><h2>Connections</h2>' + keys.map(function (label) {
      var links = groups[label].map(function (r) {
        var n = IDX.nodes[r.other];
        if (!n) return '';
        return '<a href="' + nodeHref(n.id) + '">' + esc(n.doc.name_en) + '</a>';
      }).join('');
      return '<div class="conn-group"><span class="conn-type">' + esc(label) + '</span>' +
        '<div class="conn-list">' + links + '</div></div>';
    }).join('') + '</div>';
  }

  function fact(label, value) {
    if (value == null || value === '') return '';
    return '<dt>' + esc(label) + '</dt><dd>' + value + '</dd>';
  }

  function renderDetail(node) {
    var d = node.doc;
    var html = '<a class="crumb" href="#/">&larr; All entities</a><div class="detail">';
    html += '<header>' + avatar(node) + '<div><h1>' + esc(d.name_en) + '</h1>' +
      (d.name_it && d.name_it !== d.name_en ? '<p class="subtitle">' + esc(d.name_it) + '</p>' : '') +
      '<p class="subtitle">' + esc(d.description_en || '') + '</p></div></header>';

    var chips = '';
    (d.occupations || []).forEach(function (o) { chips += '<span class="chip">' + esc(titleCase(o)) + '</span>'; });
    (d.nationalities || []).forEach(function (n) { chips += '<span class="chip accent">' + esc(n) + '</span>'; });
    if (chips) html += '<div class="chips">' + chips + '</div>';

    html += '<dl class="facts">';
    html += fact('Type', TYPE_LABEL[node.type]);
    if (node.type === 'person') {
      html += fact('Born', esc(d.birth_date));
      html += fact('Died', esc(d.death_date));
      html += fact('Statements', d.wikidata_statements_count);
    } else if (node.type === 'organization') {
      html += fact('Kind', esc(titleCase(d.type || '')));
      html += fact('Founded', esc(d.founded));
      html += fact('Country', esc(COUNTRY[d.country_id] || d.country_id || ''));
      html += fact('Affiliates', IDX.orgMembers[node.id] || 0);
    } else {
      html += fact('Date', esc(d.date));
    }
    html += fact('Wikidata', '<a href="' + wikidataUrl(qidOf(node.id)) + '" rel="noopener">' + esc(qidOf(node.id)) + '</a>');
    html += '</dl>';

    html += '<div class="actions">' +
      '<a class="btn" href="#/graph/' + qidOf(node.id) + '">Show in graph</a>' +
      '<a class="btn ghost" href="' + wikidataUrl(qidOf(node.id)) + '" rel="noopener">View on Wikidata</a>' +
      '</div>';

    html += renderConnections(node.id);
    html += '</div>';
    app.innerHTML = html;
  }

  /* ------------------------------- graph ---------------------------------- */

  function destroyGraph() {
    if (cy) { cy.destroy(); cy = null; }
  }

  function hasCoseBilkent() {
    try { return typeof cytoscape !== 'undefined' && !!cytoscape('layout', 'cose-bilkent'); }
    catch (e) { return false; }
  }

  function renderGraph(focusQid) {
    var focusId = focusQid ? resolveId(focusQid) : null;
    var html = '<a class="crumb" href="#/">&larr; All entities</a>' +
      '<h2 class="section" style="margin-top:0">Connection graph</h2>' +
      '<p class="graph-hint">Click a node to open its page. Colors show the entity type.' +
      ' This is the full demo dataset; the real site renders far larger neighborhoods.</p>' +
      '<div id="graph"></div>' +
      '<div class="legend">' +
      '<span><i style="background:' + COLOR.person + '"></i>Person</span>' +
      '<span><i style="background:' + COLOR.organization + '"></i>Organization</span>' +
      '<span><i style="background:' + COLOR.event + '"></i>Event</span>' +
      '</div>';
    app.innerHTML = html;

    if (typeof cytoscape === 'undefined') {
      document.getElementById('graph').innerHTML =
        '<p class="graph-error" style="padding:1rem">Graph library failed to load.</p>';
      return;
    }

    var nodes = Object.keys(IDX.nodes).map(function (id) {
      var n = IDX.nodes[id];
      return {
        data: {
          id: id,
          label: n.doc.name_en,
          type: n.type,
          color: COLOR[n.type],
          size: n.type === 'person' ? 22 : (n.type === 'organization' ? 26 : 24),
          shape: n.type === 'person' ? 'ellipse' : (n.type === 'organization' ? 'round-rectangle' : 'hexagon')
        }
      };
    });

    var edges = IDX.edges.map(function (e, i) {
      return { data: { id: 'e' + i, source: e.from, target: e.to, label: edgePhrase(e.type) } };
    });

    var useBilkent = hasCoseBilkent();
    var layoutOpts = { animate: false, idealEdgeLength: 110, nodeRepulsion: 8000, padding: 24 };
    var layout = { name: useBilkent ? 'cose-bilkent' : 'cose', animate: false, padding: 24 };
    if (useBilkent) {
      layout.idealEdgeLength = layoutOpts.idealEdgeLength;
      layout.nodeRepulsion = layoutOpts.nodeRepulsion;
    }

    cy = cytoscape({
      container: document.getElementById('graph'),
      elements: { nodes: nodes, edges: edges },
      wheelSensitivity: 0.2,
      layout: layout,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': 'data(color)',
            'shape': 'data(shape)',
            'width': 'data(size)',
            'height': 'data(size)',
            'label': 'data(label)',
            'font-size': 8,
            'color': '#1c1510',
            'text-wrap': 'ellipsis',
            'text-max-width': 95,
            'text-valign': 'bottom',
            'text-margin-y': 3,
            'border-width': 1,
            'border-color': '#ffffff'
          }
        },
        { selector: 'edge', style: { 'width': 1, 'line-color': '#c9c2b3', 'curve-style': 'haystack' } },
        { selector: 'node:selected', style: { 'border-width': 3, 'border-color': '#1b4d8e' } }
      ]
    });

    cy.on('tap', 'node', function (evt) {
      var id = evt.target.id();
      if (focusId && id === focusId) return;
      window.location.hash = nodeHref(id).slice(1);
    });

    function fitOrFocus() {
      if (focusId && cy.getElementById(focusId).length) {
        var n = cy.getElementById(focusId);
        cy.center(n);
        n.select();
      } else {
        cy.fit(undefined, 28);
      }
    }
    // cose-bilkent may finish synchronously or asynchronously; cover both.
    cy.one('layoutstop', fitOrFocus);
    fitOrFocus();
  }

  function resolveId(qidOrId) {
    if (IDX.nodes[qidOrId]) return qidOrId;
    var pref = ['persons/', 'organizations/', 'events/'];
    for (var i = 0; i < pref.length; i++) {
      if (IDX.nodes[pref[i] + qidOrId]) return pref[i] + qidOrId;
    }
    return null;
  }

  /* ------------------------------ routing --------------------------------- */

  function route() {
    destroyGraph();
    var hash = window.location.hash.replace(/^#\/?/, '');
    var parts = hash.split('/');
    var r = parts[0] || 'home';
    var id = parts[1] || null;
    window.scrollTo(0, 0);

    if (r === 'graph') return renderGraph(id);
    if (r === 'person' || r === 'org' || r === 'event') {
      var full = resolveId(id);
      if (full) return renderDetail(IDX.nodes[full]);
    }
    return renderHome();
  }

  function setRepoLink() {
    var link = document.getElementById('repo-link');
    if (!link) return;
    var m = /^([^.]+)\.github\.io$/.exec(window.location.hostname);
    if (m) {
      var seg = window.location.pathname.split('/').filter(Boolean)[0];
      if (seg) { link.href = 'https://github.com/' + m[1] + '/' + seg; return; }
    }
    link.href = 'https://github.com/';
  }

  /* ------------------------------- boot ----------------------------------- */

  fetch('data/demo_graph.json')
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function (data) {
      DATA = data;
      IDX = buildIndex(data);
      setRepoLink();
      window.addEventListener('hashchange', route);
      route();
    })
    .catch(function (err) {
      app.innerHTML = '<p class="graph-error">Could not load the demo graph (' + esc(err.message) + ').</p>';
    });
})();
