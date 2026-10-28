# -*- coding: utf-8 -*-
"""Знімок для ЛР5 з файлу OSM XML, коли Overpass недоступний.

1) Скачайте вивантаження Львова з BBBike у форматі «OSM XML gzip'd» (.osm.gz).
2) Запустіть поруч із ноутбуком:

    python osm_xml_to_snapshot.py planet_23.89,49.76_24.14,49.91.osm.gz

Створює теку osm_львів з тими самими шарами, які читає ноутбук:
boundary, heritage, stations, amenities, road_nodes, road_edges.

Межа міста: якщо в теці вже є boundary.parquet — береться він, інакше
запит до Nominatim (це інший сервіс, не Overpass).
"""
import argparse, gzip, json, pathlib, shutil, sys, time, warnings
import xml.etree.ElementTree as ET
warnings.filterwarnings('ignore')

TOURISM = ['museum', 'attraction', 'gallery', 'artwork', 'viewpoint']

# Той самий фільтр, яким osmnx 2.x відбирає network_type='walk'
# (osmnx/_overpass.py, _get_network_filter). В Overpass «!~» — пошук
# підрядка регулярним виразом, тож "motor" відсікає і motorway, і motorway_link.
import re
BAD_HIGHWAY = re.compile('abandoned|bus_guideway|construction|cycleway|motor|no|planned|'
                         'platform|proposed|raceway|razed|rest_area|services')
NOT_MATCH = {'area': re.compile('yes'), 'access': re.compile('private'),
             'foot': re.compile('no'), 'service': re.compile('private'),
             'sidewalk': re.compile('separate'), 'sidewalk:both': re.compile('separate'),
             'sidewalk:left': re.compile('separate'), 'sidewalk:right': re.compile('separate')}


def is_walk(tags):
    hw = tags.get('highway')
    if hw is None or BAD_HIGHWAY.search(hw):
        return False
    return not any(k in tags and rx.search(tags[k]) for k, rx in NOT_MATCH.items())


def open_xml(path):
    return gzip.open(path, 'rb') if str(path).endswith('.gz') else open(path, 'rb')


def unpack(src, dst):
    """features_from_xml читає лише розпакований файл."""
    with open_xml(src) as f, open(dst, 'wb') as g:
        shutil.copyfileobj(f, g, 1 << 20)


def write_walk_xml(src, dst):
    """Лишає тільки пішохідні шляхи і вузли, на які вони посилаються."""
    keep_ways, need = [], set()
    for _, el in ET.iterparse(src, events=('end',)):
        if el.tag == 'way':
            tags = {t.get('k'): t.get('v') for t in el.findall('tag')}
            if is_walk(tags):
                refs = [nd.get('ref') for nd in el.findall('nd')]
                need.update(refs)
                keep_ways.append((el.get('id'), refs, tags))
            el.clear()
        elif el.tag in ('relation',):
            el.clear()
    esc = lambda s: (s.replace('&', '&amp;').replace('"', '&quot;')
                      .replace('<', '&lt;').replace('>', '&gt;'))
    with open(dst, 'w', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n')
        for _, el in ET.iterparse(src, events=('end',)):
            if el.tag == 'node':
                if el.get('id') in need:
                    out.write('<node id="%s" lat="%s" lon="%s"/>\n'
                              % (el.get('id'), el.get('lat'), el.get('lon')))
                el.clear()
            elif el.tag in ('way', 'relation'):
                el.clear()
        for wid, refs, tags in keep_ways:
            out.write('<way id="%s">' % wid)
            out.write(''.join('<nd ref="%s"/>' % r for r in refs))
            out.write(''.join('<tag k="%s" v="%s"/>' % (esc(k), esc(v)) for k, v in tags.items()))
            out.write('</way>\n')
        out.write('</osm>\n')
    return len(keep_ways), len(need)


def clean(gdf):
    """Як у ноутбуці: неоднорідні колонки зводимо до тексту цілком."""
    import pandas as pd
    g = gdf.reset_index()
    for c in g.columns:
        if c == 'geometry':
            continue
        boxed = g[c].map(lambda v: isinstance(v, (list, dict, set, tuple))).any()
        if boxed or g[c].dtype == object:
            g[c] = g[c].map(
                lambda v: None if v is None or (isinstance(v, float) and pd.isna(v))
                else (json.dumps(v, ensure_ascii=False)
                      if isinstance(v, (list, dict, set, tuple)) else str(v))
            ).astype('string')
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('xml', help='файл .osm або .osm.gz')
    ap.add_argument('--place', default='Львів, Україна')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()

    import osmnx as ox
    import geopandas as gpd

    out = pathlib.Path(a.out or ('osm_' + a.place.split(',')[0].strip().lower().replace(' ', '_')))
    out.mkdir(exist_ok=True)
    tmp = out / '_tmp'; tmp.mkdir(exist_ok=True)
    man = {'_джерело': 'вивантаження OSM XML (BBBike), не Overpass',
           '_файл': pathlib.Path(a.xml).name,
           '_файл_МБ': round(pathlib.Path(a.xml).stat().st_size / 1e6, 2),
           '_дата': time.strftime('%Y-%m-%d %H:%M'), '_osmnx': ox.__version__}
    t_all = time.time()

    def save(g, name, sec):
        p = out / (name + '.parquet')
        clean(g).to_parquet(p, index=False)
        man[name] = {'записів': int(len(g)), 'файл_МБ': round(p.stat().st_size / 1e6, 3),
                     'секунд': round(sec, 1)}
        print('  %-11s %7d записів | %6.2f МБ | %5.1f с'
              % (name, len(g), man[name]['файл_МБ'], sec))

    print('0/5 розпаковую XML')
    t0 = time.time()
    raw = tmp / 'all.osm'
    unpack(a.xml, raw)
    print('     %.0f МБ за %.0f с' % (raw.stat().st_size / 1e6, time.time() - t0))

    print('1/5 межа міста')
    bp = out / 'boundary.parquet'
    if bp.exists():
        boundary = gpd.read_parquet(bp); print('  boundary   беру наявний файл')
    else:
        t0 = time.time()
        boundary = ox.geocode_to_gdf(a.place)
        save(boundary, 'boundary', time.time() - t0)
    poly = boundary.geometry.iloc[0]
    b = poly.bounds
    print('     межа: %.3f,%.3f — %.3f,%.3f (вивантаження має її покривати)' % b)

    layers = [
        ('heritage', {'historic': True, 'tourism': TOURISM}, '2/5 пам\u2019ятки'),
        ('stations', {'railway': ['station', 'halt'], 'building': ['train_station'],
                      'amenity': ['bus_station']}, '3/5 вокзали'),
        ('amenities', {'amenity': ['cafe', 'restaurant', 'drinking_water',
                                   'toilets', 'pharmacy']}, '4/5 заклади'),
    ]
    for name, tags, title in layers:
        print(title)
        t0 = time.time()
        save(ox.features_from_xml(raw, polygon=poly, tags=tags), name, time.time() - t0)

    print('5/5 пішохідна мережа')
    t0 = time.time()
    walk = tmp / 'walk.osm'
    nw, nn = write_walk_xml(raw, walk)
    print('     після фільтра walk: %d шляхів, %d вузлів' % (nw, nn))
    # bidirectional=True: osmnx так робить для walk сам — пішохід іде
    # одностороннньою вулицею в обидва боки
    G = ox.graph_from_xml(walk, bidirectional=True, simplify=True, retain_all=True)
    G = ox.truncate.truncate_graph_polygon(G, poly)
    G = ox.truncate.largest_component(G)
    nodes, edges = ox.convert.graph_to_gdfs(G)
    el = time.time() - t0
    save(nodes, 'road_nodes', el / 2)
    save(edges, 'road_edges', el / 2)
    man['_граф'] = {'вузлів': int(len(nodes)), 'ребер': int(len(edges)), 'тип': 'walk'}

    shutil.rmtree(tmp, ignore_errors=True)
    man['_секунд_усього'] = round(time.time() - t_all, 1)
    (out / 'manifest.json').write_text(json.dumps(man, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
    print('\nГотово за %.0f с. Тека: %s' % (man['_секунд_усього'], out.resolve()))
    print('Перевірка: python check_snapshot.py %s' % out)


if __name__ == '__main__':
    main()
