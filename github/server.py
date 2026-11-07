#!/usr/bin/env python3
"""Local Sonnenburg atlas. No requests to Overpass happen at startup."""
import argparse
import copy
import collections
import heapq
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.parse
import urllib.request
from shapely.geometry import Polygon, LineString, shape, mapping
from shapely.ops import polygonize, polygonize_full, unary_union, transform
from shapely import make_valid
from pyproj import Transformer

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(ROOT / 'data'))).resolve() / 'atlas.json'
API = 'https://overpass.opengeofiction.net/api/interpreter'
LOCK = threading.RLock()
PROJECT = Transformer.from_crs('EPSG:4326', '+proj=laea +lat_0=56.5 +lon_0=48 +datum=WGS84 +units=m', always_xy=True).transform
STATE = {'features': [], 'edits': {}, 'counts': {}, 'updated': None, 'warnings': [], 'country': None}
JOB = {'running': False, 'message': '', 'error': None, 'done': 0, 'total': 0}
if DATA.exists():
    STATE.update(json.loads(DATA.read_text()))


def persist():
    DATA.parent.mkdir(exist_ok=True)
    temp = DATA.with_suffix('.tmp')
    temp.write_text(json.dumps(STATE, ensure_ascii=False))
    os.replace(temp, DATA)


def overpass(query):
    request = urllib.request.Request(API, data=urllib.parse.urlencode({'data': query}).encode(), headers={'User-Agent': 'SonnenburgLocalAtlas/1.0'})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=260) as response:
                result = json.load(response)
            if result.get('remark'):
                raise ValueError(result['remark'])
            return result['elements']
        except Exception as error:
            last = error
            if attempt < 2:
                time.sleep(2 + attempt * 3)
    raise RuntimeError(f'Overpass request failed: {last}')


def polygon_geometry(element):
    """Polygonize unordered/reversed OSM ways; never close incomplete boundaries."""
    if element['type'] == 'way':
        points = [(p['lon'], p['lat']) for p in element.get('geometry', []) if p]
        return make_valid(Polygon(points)) if len(points) >= 4 and points[0] == points[-1] else None
    rings = {}
    for role in ('outer', 'inner'):
        lines = []
        for member in element.get('members', []):
            if member['type'] != 'way' or (member.get('role') or 'outer') != role:
                continue
            points = [(p['lon'], p['lat']) for p in member.get('geometry', []) if p]
            if len(points) > 1:
                lines.append(LineString(points))
        if lines:
            polygons_geom, cuts, dangles, invalid = polygonize_full(unary_union(lines))
            if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty:
                return None
            polygons = list(polygons_geom.geoms)
        else:
            polygons = []
        rings[role] = unary_union(polygons) if polygons else None
    outer, inner = rings['outer'], rings['inner']
    if outer is None:
        return None
    return make_valid(outer.difference(inner) if inner is not None else outer)


def repair_geometry(element, elements):
    """Bridge gaps using shortest paths on existing administrative linework only.

    Returned boundaries are explicitly marked provisional. No straight-line
    closures or edits to upstream data are made.
    """
    own = {m['ref'] for m in element.get('members', []) if m['type'] == 'way'}
    lines = [[(p['lon'], p['lat']) for p in m['geometry']] for m in element.get('members', []) if m['type'] == 'way' and m.get('geometry') and (m.get('role') or 'outer') == 'outer']
    endpoints = collections.Counter(p for line in lines for p in (line[0], line[-1]))
    loose = [p for p, n in endpoints.items() if n % 2]
    if not loose or len(loose) > 10:
        return None
    ways = {m['ref']: m for e in elements for m in e.get('members', []) if m['type'] == 'way' and m.get('geometry') and m['ref'] not in own}
    graph = collections.defaultdict(dict)
    for member in ways.values():
        coords = [(p['lon'], p['lat']) for p in member['geometry']]
        for a, b in zip(coords, coords[1:]):
            distance = math.hypot((a[0] - b[0]) * .55, a[1] - b[1])
            graph[a][b] = graph[b][a] = distance
    while loose:
        start = loose.pop(0)
        queue, distances, previous, end = [(0, start)], {start: 0}, {}, None
        while queue:
            distance, a = heapq.heappop(queue)
            if distance > distances[a]:
                continue
            if a in loose:
                end = a
                break
            for b, weight in graph[a].items():
                candidate = distance + weight
                if candidate < distances.get(b, math.inf):
                    distances[b], previous[b] = candidate, a
                    heapq.heappush(queue, (candidate, b))
        if end is None:
            return None
        direct = math.hypot((start[0] - end[0]) * .55, start[1] - end[1])
        if distances[end] > max(direct * 3, .002):
            return None
        loose.remove(end)
        path = [end]
        while path[-1] != start:
            path.append(previous[path[-1]])
        lines.append(path)
    polygons = list(polygonize(unary_union([LineString(line) for line in lines])))
    return make_valid(unary_union(polygons)) if polygons else None


def numeric_population(value):
    try:
        number = float(str(value).replace(',', '').replace(' ', ''))
        return round(number) if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError):
        return None


def process_boundaries(elements, water_elements=()):
    warnings, features, geometries = [], [], {}
    nodes = {e['id']: e.get('tags', {}) for e in elements if e['type'] == 'node'}
    water = [polygon_geometry(e) for e in water_elements]
    water = unary_union([g for g in water if g is not None and not g.is_empty])
    projected_water = transform(PROJECT, water)
    country = None
    for element in elements:
        tags = element.get('tags', {})
        if element['type'] != 'relation' or tags.get('admin_level') not in ('2', '4', '6', '7', '8'):
            continue
        geom = polygon_geometry(element)
        derived = False
        name = tags.get('name', f'Relation {element["id"]}')
        if geom is None or geom.is_empty:
            geom = repair_geometry(element, elements)
            derived = geom is not None and not geom.is_empty
            if derived:
                warnings.append(f'{name}: PROVISIONAL boundary. Gaps were closed by shortest paths along existing neighboring administrative borders. Verify this geometry before relying on its area or parent assignments.')
        if geom is None or geom.is_empty or geom.geom_type not in ('Polygon', 'MultiPolygon'):
            raise ValueError(f'{name} has an incomplete or invalid polygon. Previous saved boundaries were kept.')
        projected = transform(PROJECT, geom)
        land = projected.difference(projected_water).area / 1e6
        centre = tags.get('admin_centre') or tags.get('capital') or ''
        for member in element.get('members', []):
            if member.get('role') == 'admin_centre':
                centre = nodes.get(member['ref'], {}).get('name', centre)
        feature = {'type': 'Feature', 'geometry': mapping(geom), 'properties': {
            'id': element['id'], 'name': name, 'level': int(tags['admin_level']),
            'area': round(land, 6), 'boundaryArea': round(projected.area / 1e6, 6),
            'sourcePopulation': numeric_population(tags.get('population')), 'centralTown': centre,
            'parents': {}, 'tags': tags, 'geometryDerived': derived}}
        if tags['admin_level'] == '2':
            country = feature
        else:
            features.append(feature)
            geometries[element['id']] = projected
    for feature in features:
        p = feature['properties']
        targets = {8: [7, 6, 4], 7: [6, 4], 6: [4], 4: []}[p['level']]
        for level in targets:
            candidates = []
            child = geometries[p['id']]
            for parent in features:
                pp = parent['properties']
                if pp['level'] == level:
                    pg = geometries[pp['id']]
                    if child.intersects(pg):
                        candidates.append((child.intersection(pg).area / max(child.area, 1e-9), pp['id']))
            ratio, parent_id = max(candidates, default=(0, None))
            if ratio >= .5:
                p['parents'][str(level)] = parent_id
                if ratio < .98:
                    warnings.append(f'{p["name"]}: level {level} parent covers {ratio:.1%} of its boundary; assigned by largest overlap.')
            else:
                warnings.append(f'{p["name"]}: no containing level {level} parent found.')
    return features, country, warnings


def snapshot():
    with LOCK:
        result = copy.deepcopy(STATE)
        result.pop('edits', None)
        result.pop('counts', None)
        rows = {f['properties']['id']: f['properties'] for f in result['features']}
        for p in rows.values():
            edit = STATE['edits'].get(str(p['id']), {})
            p['population'] = edit.get('population', p['sourcePopulation'])
            p['centralTown'] = edit.get('centralTown', p['centralTown'])
            p['objects'] = STATE['counts'].get(str(p['id']))
            p['children'] = []
            p['editable'] = p['level'] in (7, 8)
            p['missing'] = int(p['population'] is None)
        for p in rows.values():
            if p['level'] == 7:
                p['children'] = [r['id'] for r in rows.values() if r['level'] == 8 and r['parents'].get('7') == p['id']]
            elif p['level'] in (4, 6):
                p['children'] = [r['id'] for r in rows.values() if r['level'] == 7 and r['parents'].get(str(p['level'])) == p['id']]
        for level in (7, 6, 4):
            for p in rows.values():
                if p['level'] != level:
                    continue
                if p['children'] or level in (4, 6):
                    children = [rows[i] for i in p['children']]
                    p['population'] = sum(r['population'] or 0 for r in children)
                    p['missing'] = sum(r['missing'] for r in children) if children else 1
                    p['editable'] = False
        for p in rows.values():
            p['density'] = p['population'] / p['area'] if p['population'] is not None and p['area'] else None
        result['job'] = copy.deepcopy(JOB)
        return result


def refresh():
    with LOCK:
        JOB.update(message='Fetching administrative boundaries…', done=0, total=2)
    query = '''[out:json][timeout:180];rel["admin_level"="2"]["name"="Sonnenburg"]->.country;
    .country map_to_area->.nation;rel(area.nation)["boundary"="administrative"]["admin_level"~"^(4|6|7|8)$"]->.regions;
    (.country;.regions;);out body geom;node(r.regions:"admin_centre");out tags;'''
    elements = overpass(query)
    countries = [e for e in elements if e.get('tags', {}).get('admin_level') == '2']
    if len(countries) != 1:
        raise ValueError('Expected exactly one nation named Sonnenburg.')
    country_id = countries[0]['id']
    with LOCK:
        JOB.update(message='Fetching mapped water and calculating land areas…', done=1)
    water = overpass(f'''[out:json][timeout:180];rel({country_id});map_to_area->.nation;
    (way(area.nation)["natural"="water"];rel(area.nation)["natural"="water"];
    way(area.nation)["waterway"="riverbank"];rel(area.nation)["waterway"="riverbank"];);out body geom;''')
    features, country, warnings = process_boundaries(elements, water)
    if not features:
        raise ValueError('No administrative regions found; previous data was kept.')
    with LOCK:
        STATE.update(features=features, country=country, warnings=warnings, updated=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        persist()
        JOB.update(message=f'Loaded {len(features)} boundaries. Your population edits were preserved.', done=2)


def count_objects():
    with LOCK:
        ids = [f['properties']['id'] for f in STATE['features']]
        JOB.update(done=0, total=len(ids), message='Counting mapped objects…')
    for start in range(0, len(ids), 6):
        batch = ids[start:start + 6]
        query = '[out:json][timeout:180];' + ''.join(f'rel({rid});out ids;map_to_area->.a;nwr(area.a);out count;' for rid in batch)
        elements = overpass(query)
        updates, current = {}, None
        for element in elements:
            if element['type'] == 'relation':
                current = element['id']
            elif element['type'] == 'count' and current is not None:
                tags = element['tags']
                updates[str(current)] = {k: int(tags[k]) for k in ('nodes', 'ways', 'relations', 'total')}
                updates[str(current)]['updated'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                current = None
        if len(updates) != len(batch):
            raise ValueError('Overpass returned incomplete object counts. Completed batches were saved.')
        with LOCK:
            STATE['counts'].update(updates)
            persist()
            JOB.update(done=start + len(batch), message=f'Counted {start + len(batch)} / {len(ids)} regions…')
    with LOCK:
        JOB['message'] = 'Mapped object counts are up to date.'


def run_job(function):
    try:
        function()
    except Exception as error:
        with LOCK:
            JOB.update(error=str(error), message='Refresh stopped. Previously saved data is available.')
    finally:
        with LOCK:
            JOB['running'] = False


if __name__ == '__main__':
    import sys
    sys.modules['server'] = sys.modules[__name__]
    from webapp import create_app
    from waitress import serve
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8004)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    print(f'Sonnenburg → http://{args.host}:{args.port}', flush=True)
    serve(create_app(), host=args.host, port=args.port, threads=8)
