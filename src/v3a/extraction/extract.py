"""Turn a parsed gamestate into plain rows (dicts) for analysis tables.

Save fields are named by their game names (K.<name>; ids are looked up in library/tokens/ per game version).
A column that copies one save field has that field's game name; computed columns have descriptive names
(population, literacy_rate ...); pop_type / building_type keep a prefix because the game field is just `type` / `building`.
Units and confidence come from the token dictionary and the calibration notes (UI screenshots 2026-11-12,
debug-mode text save 2026-11-15). Meanings marked low/med are hypotheses.
"""
import os
from collections import defaultdict

from v3a.core.v3parse import Obj, Tok, K, label, tname, num, raw
from v3a.core.v3data import db, game_date, date_iso, GAME, POP_SCALE


def scalar(v):
    """Leaf value suitable for a table cell (None for blocks)."""
    if isinstance(v, (Obj, list)):
        return None
    if isinstance(v, Tok):
        return '@' + tname(v)
    return v


# ------------------------------------------------------------------------------------------ game data (optional)
_GOODS = None


def goods_names():
    """Goods in index order (common/goods, file order) from the game install, or [] without game files."""
    global _GOODS
    if _GOODS is None:
        _GOODS = []
        if os.path.isdir(os.path.join(GAME, 'common', 'goods')):
            from v3a.core.gamefiles import definitions
            fo = definitions().get('goods')
            _GOODS = [k for k, _, _ in fo.defs] if fo else []
    return _GOODS


def good_name(i):
    """Goods name for a goods index (None without game files)."""
    g = goods_names()
    return g[i] if isinstance(i, int) and 0 <= i < len(g) else None


# ------------------------------------------------------------------------------------------ index
BUILDING_FIELDS = [   # (column = game field name, unit, confidence, description)
    ('levels', 'levels', 'high', 'Building level (= sum of ownership levels)'),
    ('last_updated_level', 'levels', 'med', 'Equals levels except while (re)constructing / downsizing (was read as target_level)'),
    ('staffing', 'levels', 'high', 'Employment-filled levels; goods in/out = PM amount x staffing x throughput'),
    ('throughput', 'multiplier', 'high', 'Throughput multiplier, UI +25% = 1.25'),
    ('salary_rate', 'GBP/week per 10k', 'high', 'Weekly GBP per 10,000 wage-weighted employees (was wage_rate)'),
    ('cash_reserves', 'GBP', 'high', 'Current cash reserves (UI cash bar); cap 25,000 per level'),
    ('goods_sales', 'GBP/week', 'high', 'Weekly sales value of outputs (was revenue); absent on government buildings'),
    ('goods_cost', 'GBP/week', 'high', 'Weekly cost of input goods, last tick (was input_cost)'),
    ('profit_after_reserves', 'GBP/week', 'high', 'Realised weekly balance of last tick = goods_sales - goods_cost - wages (was weekly_balance)'),
    ('government_dividends', 'GBP/week', 'med', 'Equals profit_after_reserves on government buildings (was government_expense)'),
]


class Index:
    """Lookups shared by all extractors for one parsed save."""

    def __init__(self, g):
        self.g = g
        self.date = game_date(g)
        self.date_iso = date_iso(self.date)
        self.countries = db(g, 'country_manager')
        self.states = db(g, 'states')
        self.pops = db(g, 'pops')
        self.buildings = db(g, 'building_manager')
        self.ownership = db(g, K.building_ownership_manager)
        self.tag = {i: c.get(K.definition) for i, c in self.countries.items()}
        self.state_owner = {i: s.get(K.country) for i, s in self.states.items()}
        self.state_region = {i: s.get(K.region) for i, s in self.states.items()}
        self._pop_agg = self._emp = None
        self.rankings = self._rankings()

    def _rankings(self):
        out = {}
        rk = self.g.get(K.country_rankings)
        if isinstance(rk, Obj):
            for e in rk.getall(K.country_rankings):
                items = e if isinstance(e, list) and not isinstance(e, Obj) else [e]
                for r in items:
                    if isinstance(r, Obj) and r.get(K.country) is not None:
                        out[r.get(K.country)] = r
        return out

    def great_powers(self):
        """{country_id: ranking Obj} of countries ranked 'great_power' in this save."""
        return {c: r for c, r in self.rankings.items() if r.get(K.rank) == 'great_power'}

    @property
    def pop_agg(self):
        """Pop sums per state and per country (people, i.e. already x POP_SCALE)."""
        if self._pop_agg is None:
            S = defaultdict(lambda: defaultdict(float)); C = defaultdict(lambda: defaultdict(float))
            for p in self.pops.values():
                sid = p.get(K.location)
                wa, dn = num(p.get(K.workforce)) or 0.0, num(p.get(K.dependents)) or 0.0
                lit = num(p.get(K.num_literate)) or 0.0
                for a in (S[sid], C[self.state_owner.get(sid)]):
                    a['workforce'] += wa * POP_SCALE; a['dependents'] += dn * POP_SCALE
                    a['literate'] += lit * POP_SCALE
                    if wa + dn > 0:
                        a['pops'] += 1
                        a['wealth_x_size'] += (num(p.get(K.wealth)) or 0) * (wa + dn) * POP_SCALE
            self._pop_agg = (S, C)
        return self._pop_agg

    @property
    def employment(self):
        """{building_id: {pop_type: workers}}."""
        if self._emp is None:
            E = defaultdict(lambda: defaultdict(float))
            for p in self.pops.values():
                w = p.get(K.workplace)
                if w is not None:
                    E[w][p.get(K.type)] += (num(p.get(K.workforce)) or 0.0) * POP_SCALE
            self._emp = E
        return self._emp


# ------------------------------------------------------------------------------------------ rows
def ts_last(block):
    """Latest value of a time series block {sample_rate, count, channels = { i = {date index values} }}."""
    ch = block.get(K.channels) if isinstance(block, Obj) else None
    c = next((x for _, x in ch if isinstance(x, Obj)), None) if isinstance(ch, Obj) else None
    vals = c.get(K.values) if c is not None else None
    if not isinstance(vals, list) or not vals:
        return None
    i = c.get(K.index)
    return num(vals[i - 1]) if isinstance(i, int) and 0 < i <= len(vals) else num(vals[-1])


def country_rows(ix, only_existing=True):
    """One row per (existing) country: curated economy, population, politics columns (COUNTRY_COLUMNS)."""
    S, C = ix.pop_agg
    tech = {t.get(K.country): t for t in db(ix.g, K.technology).values()}
    gps = ix.great_powers()
    bcount = defaultdict(lambda: [0, 0.0])
    for b in ix.buildings.values():
        c = ix.state_owner.get(b.get(K.state))
        bcount[c][0] += 1; bcount[c][1] += num(b.get(K.levels)) or 0
    rows = []
    for cid, c in sorted(ix.countries.items()):
        main = bool(c.get(K.is_main_tag))
        if only_existing and not main:
            continue
        a = C.get(cid, {}); bud = c.get(K.budget) or Obj(); ps = c.get(K.pop_statistics) or Obj()
        b5 = bud.get(K.country_building_budget) or Obj(); r = ix.rankings.get(cid) or Obj(); t = tech.get(cid) or Obj()
        pop = a.get('workforce', 0) + a.get('dependents', 0)
        tax, exp = num(b5.get(K.income)), num(b5.get(K.expense))
        gdp = ts_last(c.get(K.gdp))
        rows.append({
            'country_id': cid, 'tag': c.get(K.definition), 'is_main_tag': main,
            'government': scalar(c.get(K.government)), 'religion': scalar(c.get(K.religion)),
            'rank': r.get(K.rank), 'score': num(r.get(K.score)), 'is_great_power': cid in gps,
            'market': num(c.get(K.market)), 'prestige': num(r.get(K.prestige)),
            'states_owned': len(c.get(K.states) or []),
            'population': round(pop), 'workforce': round(a.get('workforce', 0)),
            'dependents': round(a.get('dependents', 0)), 'num_literate': round(a.get('literate', 0)),
            'literacy_rate': a['literate'] / a['workforce'] if a.get('workforce') else None,
            'population_lower_strata': round((num(ps.get(K.population_lower_strata)) or 0) * POP_SCALE),
            'population_middle_strata': round((num(ps.get(K.population_middle_strata)) or 0) * POP_SCALE),
            'population_upper_strata': round((num(ps.get(K.population_upper_strata)) or 0) * POP_SCALE),
            'avg_wealth': a['wealth_x_size'] / pop if pop else None,
            'gdp': gdp, 'gdp_per_capita': gdp / pop if gdp and pop else None,
            'credit': num(bud.get(K.credit)),
            'money': num(bud.get(K.money)), 'investment_pool': num(bud.get(K.investment_pool)),
            'income': tax, 'expense': exp,
            'population_radicals': round((num(ps.get(K.population_radicals)) or 0) * POP_SCALE),
            'population_loyalists': round((num(ps.get(K.population_loyalists)) or 0) * POP_SCALE),
            'literacy': ts_last(c.get(K.literacy)), 'avgsoltrend': ts_last(c.get(K.avgsoltrend)),
            'technologies_known': len(t.get(K.acquired_technologies) or []), 'research_technology': scalar(t.get(K.research_technology)),
            'buildings': bcount[cid][0], 'building_levels': bcount[cid][1],
        })
    return rows


COUNTRY_COLUMNS = [
    ('country_id', 'INTEGER', 'id', 'high', 'Internal country id (referenced by the game field country)'),
    ('tag', 'TEXT', '', 'high', 'Country tag, e.g. GBR (game field definition)'),
    ('is_main_tag', 'INTEGER', 'bool', 'med', 'Game field is_main_tag: set on the countries that exist in this save (others are filtered out)'),
    ('government', 'TEXT', '', 'med', 'Government type key'),
    ('religion', 'TEXT', '', 'high', 'State religion'),
    ('rank', 'TEXT', '', 'high', "Country rank class, e.g. 'great_power'"),
    ('score', 'INTEGER', '?', 'med', 'Game field score in the rankings table (was rank_position; GBR = 1 at start, unit not yet checked)'),
    ('is_great_power', 'INTEGER', 'bool', 'high', "rank = 'great_power' in this save"),
    ('market', 'INTEGER', 'id', 'low', 'Game field market on the country (was read as great-power position, wrong); probably the market id'),
    ('prestige', 'REAL', 'points', 'high', 'Game field prestige in the rankings table'),
    ('states_owned', 'INTEGER', 'count', 'high', 'Number of states owned'),
    ('population', 'INTEGER', 'people', 'high', 'Working adults + dependents in owned states'),
    ('workforce', 'INTEGER', 'people', 'high', 'Sum of pop workforce, working-age adults (was working_adults)'),
    ('dependents', 'INTEGER', 'people', 'high', 'Sum of pop dependents'),
    ('num_literate', 'INTEGER', 'people', 'med', 'Sum of pop num_literate, literate working adults (was literate_adults)'),
    ('literacy_rate', 'REAL', 'fraction', 'med', 'num_literate / workforce'),
    ('population_lower_strata', 'INTEGER', 'people', 'high', 'Population of lower strata, cached pop_statistics (was pop_lower_strata)'),
    ('population_middle_strata', 'INTEGER', 'people', 'high', 'Population of middle strata, cached pop_statistics (was pop_middle_strata)'),
    ('population_upper_strata', 'INTEGER', 'people', 'high', 'Population of upper strata, cached pop_statistics (was pop_upper_strata)'),
    ('avg_wealth', 'REAL', 'level', 'med', 'Size-weighted mean pop wealth level'),
    ('gdp', 'REAL', 'GBP', 'high', 'GDP: latest sample of the game series gdp; the UI shows the current value, within ~2% (USA 1848: 21.9M vs UI 22.3M)'),
    ('credit', 'REAL', 'GBP', 'high', 'Credit limit, UI Credit (was credit_limit; wrongly used as GDP before 2026-11-12)'),
    ('gdp_per_capita', 'REAL', 'GBP/person', 'med', 'gdp / population'),
    ('money', 'REAL', 'GBP', 'med', 'Treasury'),
    ('investment_pool', 'REAL', 'GBP', 'high', 'Investment pool (UI budget)'),
    ('income', 'REAL', 'GBP/week', 'high', 'budget/country_building_budget/income: taxation income = income + poll/consumption taxes + dividends, last week (was tax_income)'),
    ('expense', 'REAL', 'GBP/week', 'med', 'budget/country_building_budget/expense: total expenses, last week (was expenses)'),
    ('population_radicals', 'INTEGER', 'people', 'high', 'Radical pops, UI Radicals (was radicals)'),
    ('population_loyalists', 'INTEGER', 'people', 'high', 'Loyalist pops, UI Loyalists (was loyalists)'),
    ('literacy', 'REAL', 'fraction', 'high', 'Literacy as shown in the UI (incorporated states), latest of the game series literacy (was literacy_ui)'),
    ('avgsoltrend', 'REAL', 'level', 'high', 'Average standard of living (UI), latest of the game series avgsoltrend (was standard_of_living)'),
    ('technologies_known', 'INTEGER', 'count', 'high', 'Number of acquired_technologies'),
    ('research_technology', 'TEXT', '', 'high', 'Technology currently researched (was researching)'),
    ('buildings', 'INTEGER', 'count', 'high', 'Building objects in owned states'),
    ('building_levels', 'REAL', 'levels', 'med', 'Sum of building levels in owned states'),
]


def state_rows(ix):
    """One row per state: region, owner, population, buildings (STATE_COLUMNS)."""
    S, _ = ix.pop_agg
    bl = defaultdict(lambda: [0, 0.0])
    for b in ix.buildings.values():
        bl[b.get(K.state)][0] += 1; bl[b.get(K.state)][1] += num(b.get(K.levels)) or 0
    rows = []
    for sid, s in sorted(ix.states.items()):
        a = S.get(sid, {}); pop = a.get('workforce', 0) + a.get('dependents', 0)
        rows.append({'state_id': sid, 'region': s.get(K.region), 'owner_id': s.get(K.country),
                     'owner_tag': ix.tag.get(s.get(K.country)), 'population': round(pop),
                     'workforce': round(a.get('workforce', 0)), 'pops': int(a.get('pops', 0)),
                     'literacy_rate': a['literate'] / a['workforce'] if a.get('workforce') else None,
                     'buildings': bl[sid][0], 'building_levels': bl[sid][1]})
    return rows


STATE_COLUMNS = [
    ('state_id', 'INTEGER', 'id', 'high', 'Internal state id'),
    ('region', 'TEXT', '', 'high', 'State region key, e.g. STATE_NEW_YORK'),
    ('owner_id', 'INTEGER', 'id', 'high', 'Owning country id'),
    ('owner_tag', 'TEXT', '', 'high', 'Owning country tag'),
    ('population', 'INTEGER', 'people', 'high', 'Working adults + dependents'),
    ('workforce', 'INTEGER', 'people', 'high', 'Sum of pop workforce, working-age adults (was working_adults)'),
    ('pops', 'INTEGER', 'count', 'high', 'Pop objects with size > 0'),
    ('literacy_rate', 'REAL', 'fraction', 'med', 'num_literate / workforce'),
    ('buildings', 'INTEGER', 'count', 'high', 'Building objects in the state'),
    ('building_levels', 'REAL', 'levels', 'med', 'Sum of building levels'),
]


def pop_rows(ix):
    """One row per pop with size > 0, sizes converted to people (POP_COLUMNS)."""
    rows = []
    for pid, p in ix.pops.items():
        wa, dn = num(p.get(K.workforce)) or 0.0, num(p.get(K.dependents)) or 0.0
        if wa + dn <= 0:
            continue
        sid = p.get(K.location)
        rows.append({'pop_id': pid, 'state_id': sid, 'country_tag': ix.tag.get(ix.state_owner.get(sid)),
                     'pop_type': scalar(p.get(K.type)), 'culture_id': num(p.get(K.culture)),
                     'religion': scalar(p.get(K.religion)), 'workplace_id': num(p.get(K.workplace)),
                     'workforce': round(wa * POP_SCALE), 'dependents': round(dn * POP_SCALE),
                     'num_literate': round((num(p.get(K.num_literate)) or 0) * POP_SCALE),
                     'wealth': num(p.get(K.wealth)), 'job_satisfaction': num(p.get(K.job_satisfaction)),
                     'political_strength': num(p.get(K.political_strength))})
    return rows


POP_COLUMNS = [
    ('pop_id', 'INTEGER', 'id', 'high', 'Internal pop id'),
    ('state_id', 'INTEGER', 'id', 'high', 'State the pop lives in'),
    ('country_tag', 'TEXT', '', 'high', 'Owner of that state'),
    ('pop_type', 'TEXT', '', 'high', 'Game field type, e.g. laborers, farmers, capitalists'),
    ('culture_id', 'INTEGER', 'id', 'high', 'Game field culture: id in the cultures database'),
    ('religion', 'TEXT', '', 'high', 'Religion key'),
    ('workplace_id', 'INTEGER', 'id', 'high', 'Game field workplace: building the pop works in (was workplace_building_id)'),
    ('workforce', 'INTEGER', 'people', 'high', 'Working adults: game field workforce x 100,000 (was working_adults)'),
    ('dependents', 'INTEGER', 'people', 'high', 'Dependents (x 100,000)'),
    ('num_literate', 'INTEGER', 'people', 'med', 'Literate working adults: num_literate x 100,000 (was literate_adults)'),
    ('wealth', 'INTEGER', 'level', 'med', 'Wealth level 1-70 (standard-of-living-like)'),
    ('job_satisfaction', 'REAL', '?', 'low', 'Game field job_satisfaction (was read as savings, wrong); unit and range not yet checked'),
    ('political_strength', 'REAL', '?', 'med', 'Game field political_strength; unit not yet checked'),
]


def building_rows(ix, countries=None):
    """One row per building (optionally only in states owned by `countries` = set of country ids)."""
    rows = []
    for bid, b in ix.buildings.items():
        sid = b.get(K.state); owner = ix.state_owner.get(sid)
        if countries is not None and owner not in countries:
            continue
        pms = b.get(K.production_methods)
        emp = ix.employment.get(bid, {})
        r = {'building_id': bid, 'building_type': scalar(b.get(K.building)), 'state_id': sid,
             'region': ix.state_region.get(sid), 'country_id': owner, 'country_tag': ix.tag.get(owner),
             'production_methods': list(pms) if isinstance(pms, list) else None,
             'employed': round(sum(emp.values())),
             'employment_by_type': '; '.join('%s:%d' % (k, round(v)) for k, v in sorted(emp.items(), key=lambda a: -a[1]))}
        for col, *_ in BUILDING_FIELDS:
            r[col] = num(b.get(raw(col)))
        rows.append(r)
    return rows


BUILDING_COLUMNS = [
    ('building_id', 'INTEGER', 'id', 'high', 'Internal building id'),
    ('building_type', 'TEXT', '', 'high', 'e.g. building_textile_mills'),
    ('state_id', 'INTEGER', 'id', 'high', 'State id (4294967295 = removed building)'),
    ('region', 'TEXT', '', 'high', 'State region key'),
    ('country_id', 'INTEGER', 'id', 'high', 'Owner of the state the building is in (not the building owner)'),
    ('country_tag', 'TEXT', '', 'high', 'Tag of that country'),
    ('production_methods', 'TEXT', '', 'high', "Active production methods, ' | ' separated"),
    ('employed', 'INTEGER', 'people', 'high', 'Working adults employed (pops whose workplace is this building)'),
    ('employment_by_type', 'TEXT', '', 'high', 'Workers by pop type'),
] + [(c, 'REAL', u, conf, d) for c, u, conf, d in BUILDING_FIELDS]


def ownership_rows(ix):
    """One row per building ownership share (OWNERSHIP_COLUMNS)."""
    rows = []
    for oid, o in ix.ownership.items():
        od = o.get(K.identity) or Obj()
        owner_country = od.get(K.country) if isinstance(od, Obj) else None
        owner_building = od.get(K.building) if isinstance(od, Obj) else None
        ob = ix.buildings.get(owner_building) if owner_building is not None else None
        rows.append({'ownership_id': oid, 'building_id': num(o.get(K.building)), 'levels': num(o.get(K.levels)),
                     'owner_kind': 'country' if owner_country is not None else 'building',
                     'owner_country_tag': ix.tag.get(owner_country) if owner_country is not None else
                     ix.tag.get(ix.state_owner.get(ob.get(K.state))) if isinstance(ob, Obj) else None,
                     'owner_building_id': owner_building,
                     'owner_building_type': scalar(ob.get(K.building)) if isinstance(ob, Obj) else None})
    return rows


OWNERSHIP_COLUMNS = [
    ('ownership_id', 'INTEGER', 'id', 'high', 'Ownership record id'),
    ('building_id', 'INTEGER', 'id', 'high', 'Owned building'),
    ('levels', 'REAL', 'levels', 'high', 'Levels owned (sum over records = building level)'),
    ('owner_kind', 'TEXT', '', 'high', "'country' (government) or 'building' (manor house, financial district, company...)"),
    ('owner_country_tag', 'TEXT', '', 'high', 'Owning country, or country where the owning building is'),
    ('owner_building_id', 'INTEGER', 'id', 'high', 'Owning building (if owner_kind = building)'),
    ('owner_building_type', 'TEXT', '', 'high', 'Type of the owning building'),
]


def goods_io_rows(ix):
    """One row per building x good: input/output amounts (GOODS_IO_COLUMNS)."""
    rows = []
    for bid, b in ix.buildings.items():
        for direction, rawk in (('input', K.input_goods), ('output', K.output_goods)):
            blk = b.get(rawk)
            m = blk.get(K.goods) if isinstance(blk, Obj) else None
            if isinstance(m, Obj):
                for gi, e in m:
                    if isinstance(e, Obj):
                        rows.append({'building_id': bid, 'direction': direction, 'good_index': gi,
                                     'good': good_name(gi), 'value': num(e.get(K.value))})
    return rows


GOODS_IO_COLUMNS = [
    ('building_id', 'INTEGER', 'id', 'high', 'Building'),
    ('direction', 'TEXT', '', 'high', 'input or output'),
    ('good_index', 'INTEGER', 'index', 'high', 'Index into common/goods/00_goods.txt'),
    ('good', 'TEXT', '', 'high', 'Good name (NULL when the game files were not available)'),
    ('value', 'REAL', 'units/week', 'high', 'Game field value: amount = PM amount x staffing x throughput (was amount)'),
]


# ------------------------------------------------------------------------------------------ generic facts
def flatten(v, prefix, out, depth=0, max_depth=5, max_list=40):
    """Leaf values of a block as (path, value). Long lists are cut to max_list entries + a '#len' row."""
    if isinstance(v, Obj):
        seen = {}
        for k, x in v:
            key = str(k)
            n = seen.get(key, 0); seen[key] = n + 1
            if n:
                key = '%s#%d' % (key, n)
            p = prefix + '/' + key if prefix else key
            if depth >= max_depth and isinstance(x, (Obj, list)):
                out.append((p + '#len', len(x))); continue
            flatten(x, p, out, depth + 1, max_depth, max_list)
    elif isinstance(v, list):
        if len(v) > max_list:
            out.append((prefix + '#len', len(v)))
        for i, x in enumerate(v[:max_list]):
            p = '%s[%d]' % (prefix, i)
            if depth >= max_depth and isinstance(x, (Obj, list)):
                out.append((p + '#len', len(x))); continue
            flatten(x, p, out, depth + 1, max_depth, max_list)
    else:
        out.append((prefix, scalar(v)))
    return out


def readable_path(path):
    """'k_3171/k_3f17' -> 'budget/gdp' using the token dictionary (unknown ids stay raw)."""
    import re
    return re.sub(r'k_[0-9a-f]{4}', lambda m: label(m.group()).rstrip('?'), path)
