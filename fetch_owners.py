"""
Get the property owner for each Tampa lodging lead.

Queries the SWFWMD regional GIS server, which mirrors the Hillsborough
County parcel layer including OWNERNAME. No download, no API key, no cost.

Matching is by COORDINATE, not address string: for each lead we ask the
server which parcel polygon contains that lat/lng. That sidesteps every
address-formatting problem.

The layer number and the field names are resolved from the service itself
at startup rather than hardcoded, so a layer that has been renumbered or a
column that has been renamed is reported and worked around instead of
returning 43 empty rows.

RUN
---
    pip install requests pandas openpyxl
    python fetch_owners.py

Needs tampa_lodging_leads_clean.csv in the same folder.
Takes about a minute for 43 leads.

OUTPUT
------
    leads_with_owners.csv    all leads + owner name, mailing address, folio
    owner_portfolios.csv     owners holding more than one property
"""

import re
import sys
import time
import requests
import pandas as pd

SERVICE = ('https://www25.swfwmd.state.fl.us/arcgiswmis/rest/services'
           '/r27/LocationInfo/MapServer')
LAYER_ID = 71                    # last known parcel layer; verified at startup
LAYER_NAME_RE = re.compile(r'parcel', re.I)

# Canonical field -> names it has been known by. The first one that actually
# exists on the layer wins, so a rename upstream costs a lookup, not a run.
FIELD_CANDIDATES = {
    'OWNERNAME':  ('OWNERNAME', 'OWNER_NAME', 'OWNERNME', 'OWN_NAME', 'OWNER'),
    'OWNERADD1':  ('OWNERADD1', 'OWNER_ADD1', 'OWNERADDR1', 'OWNADDR1', 'MAILADD1'),
    'OWNERADD2':  ('OWNERADD2', 'OWNER_ADD2', 'OWNERADDR2', 'OWNADDR2', 'MAILADD2'),
    'OWNERCITY':  ('OWNERCITY', 'OWNER_CITY', 'OWNCITY', 'MAILCITY'),
    'OWNERSTATE': ('OWNERSTATE', 'OWNER_STATE', 'OWNSTATE', 'MAILSTATE'),
    'OWNERZIP':   ('OWNERZIP', 'OWNER_ZIP', 'OWNZIP', 'MAILZIP', 'OWNERZIPCODE'),
    'SITUSADD1':  ('SITUSADD1', 'SITUS_ADD1', 'SITUSADDRESS', 'SITEADDR', 'SITEADD1'),
    'FOLIONUM':   ('FOLIONUM', 'FOLIO', 'FOLIO_NUM', 'PARCELID', 'STRAP', 'PIN'),
    'PARUSEDESC': ('PARUSEDESC', 'PAR_USE_DESC', 'USEDESC', 'DORUSEDESC', 'USE_DESC'),
    'ASSD_TOT':   ('ASSD_TOT', 'ASSDTOT', 'ASSESSED_TOTAL', 'TOTAL_ASSESSED', 'JUST_VALUE'),
    'YRBLT_ACT':  ('YRBLT_ACT', 'YRBLT', 'ACTYRBLT', 'YEAR_BUILT'),
    'SALE1_AMT':  ('SALE1_AMT', 'SALE_AMT1', 'SALEAMT1', 'S1_AMT', 'LASTSALEAMT'),
    'SALE1_YEAR': ('SALE1_YEAR', 'SALE_YR1', 'SALEYR1', 'S1_YEAR', 'LASTSALEYR'),
    'ACRES':      ('ACRES', 'ACREAGE', 'GIS_ACRES', 'CALCACRES'),
}
REQUIRED = ('OWNERNAME',)        # without this there is no point continuing

LEADS = 'tampa_lodging_leads_clean.csv'
PAUSE = 0.4          # be polite to a public server
TIMEOUT = 30
RETRIES = 3


def _norm(name):
    return re.sub(r'[^A-Z0-9]', '', str(name).upper())


def get_json(session, url, params=None):
    """GET with retries. Raises on an ArcGIS-level error payload."""
    params = dict(params or {})
    params.setdefault('f', 'json')
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
        except Exception as e:                       # network, HTTP, bad JSON
            last = e
            if attempt < RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise
        if isinstance(data, dict) and 'error' in data:
            err = data['error']
            raise RuntimeError(f"{url} -> {err.get('code','?')}: "
                               f"{err.get('message','?')} "
                               f"{'; '.join(err.get('details') or [])}".strip())
        return data
    raise last


def resolve_layer(session):
    """Find the parcel layer id, whether or not it is still LAYER_ID."""
    root = get_json(session, SERVICE)
    layers = root.get('layers') or []
    if not layers:
        raise SystemExit(f'{SERVICE} listed no layers - service moved?')
    by_id = {lyr['id']: lyr.get('name', '') for lyr in layers}

    if LAYER_ID in by_id and LAYER_NAME_RE.search(by_id[LAYER_ID]):
        print(f'layer {LAYER_ID}: {by_id[LAYER_ID]}')
        return LAYER_ID

    hits = [i for i, n in by_id.items() if LAYER_NAME_RE.search(n)]
    if not hits:
        print('no parcel layer found. Layers on this service:', file=sys.stderr)
        for i, n in sorted(by_id.items()):
            print(f'  {i:>3}  {n}', file=sys.stderr)
        raise SystemExit('update LAYER_ID / LAYER_NAME_RE and re-run')

    # Prefer a Hillsborough-specific parcel layer if the service carries
    # parcel layers for several counties.
    chosen = next((i for i in hits if re.search(r'hillsborough', by_id[i], re.I)),
                  hits[0])
    was = by_id.get(LAYER_ID, '(absent)')
    print(f'!! layer {LAYER_ID} is now "{was}" - using layer {chosen}: '
          f'"{by_id[chosen]}" instead')
    if len(hits) > 1:
        print('   other parcel layers: '
              + ', '.join(f'{i} ({by_id[i]})' for i in hits if i != chosen))
    return chosen


def resolve_fields(session, layer_id):
    """Map each canonical field to whatever the layer actually calls it."""
    meta = get_json(session, f'{SERVICE}/{layer_id}')
    actual = {}
    for f in meta.get('fields') or []:
        actual.setdefault(_norm(f.get('name')), f['name'])
        if f.get('alias'):
            actual.setdefault(_norm(f['alias']), f['name'])
    if not actual:
        raise SystemExit(f'layer {layer_id} reported no fields')

    mapping, renamed, missing = {}, [], []
    for canon, candidates in FIELD_CANDIDATES.items():
        for cand in candidates:
            if _norm(cand) in actual:
                mapping[canon] = actual[_norm(cand)]
                if mapping[canon] != canon:      # exact, so OWNER_NAME counts
                    renamed.append(f'{canon} -> {mapping[canon]}')
                break
        else:
            missing.append(canon)

    if renamed:
        print('!! renamed fields: ' + ', '.join(renamed))
    if missing:
        print('!! fields not on this layer (left blank): ' + ', '.join(missing))
    gone = [c for c in REQUIRED if c not in mapping]
    if gone:
        print(f'layer {layer_id} has no owner-name field. Available fields:',
              file=sys.stderr)
        print('  ' + ', '.join(sorted(set(actual.values()))), file=sys.stderr)
        raise SystemExit(f'required field(s) missing: {", ".join(gone)}')
    return mapping


def owner_at(lat, lng, session, query_url, out_fields):
    """Return the parcel record whose polygon contains this point."""
    params = {
        'geometry':       f'{lng},{lat}',
        'geometryType':   'esriGeometryPoint',
        'inSR':           '4326',                    # our coords are WGS84
        'spatialRel':     'esriSpatialRelIntersects',
        'outFields':      out_fields,
        'returnGeometry': 'false',
        'f':              'json',
    }
    data = get_json(session, query_url, params)
    feats = data.get('features', [])
    if not feats:
        return None, 'no parcel at this point'
    if len(feats) > 1:
        return feats[0]['attributes'], f'{len(feats)} parcels - VERIFY'
    return feats[0]['attributes'], 'ok'


def main():
    leads = pd.read_csv(LEADS)
    if 'lat' not in leads.columns:
        raise SystemExit('CSV has no lat/lng columns')
    print(f'{len(leads)} leads to look up\n')

    session = requests.Session()
    session.headers['User-Agent'] = 'parcel-lookup/1.0'

    layer_id = resolve_layer(session)
    fields = resolve_fields(session, layer_id)
    query_url = f'{SERVICE}/{layer_id}/query'
    out_fields = ','.join(fields.values())
    print()

    def attr(attrs, canon):
        """Read a canonical field out of a feature's attributes."""
        return attrs.get(fields[canon]) if canon in fields else None

    rows = []
    for i, r in enumerate(leads.itertuples(index=False), 1):
        if pd.isna(r.lat) or pd.isna(r.lng):
            rows.append({'match': 'no coordinates'})
            print(f'{i:>3}. {r.business_name[:38]:<38} - no coordinates')
            continue
        try:
            attrs, status = owner_at(r.lat, r.lng, session, query_url, out_fields)
        except Exception as e:
            attrs, status = None, f'request failed: {e}'
        if attrs:
            mail = ' '.join(str(attr(attrs, k) or '').strip()
                            for k in ('OWNERADD1', 'OWNERADD2')).strip()
            city = ', '.join(x for x in (attr(attrs, 'OWNERCITY'),
                                         attr(attrs, 'OWNERSTATE')) if x)
            rows.append({
                'owner_name':    (attr(attrs, 'OWNERNAME') or '').strip(),
                'owner_mailing': f"{mail}, {city} {attr(attrs, 'OWNERZIP') or ''}".strip(', '),
                'owner_city':    attr(attrs, 'OWNERCITY'),
                'owner_state':   attr(attrs, 'OWNERSTATE'),
                'folio':         attr(attrs, 'FOLIONUM'),
                'parcel_use':    attr(attrs, 'PARUSEDESC'),
                'parcel_addr':   attr(attrs, 'SITUSADD1'),
                'assessed_value': attr(attrs, 'ASSD_TOT'),
                'year_built':    attr(attrs, 'YRBLT_ACT'),
                'last_sale_amt': attr(attrs, 'SALE1_AMT'),
                'last_sale_yr':  attr(attrs, 'SALE1_YEAR'),
                'acres':         attr(attrs, 'ACRES'),
                'match':         status,
            })
            print(f'{i:>3}. {r.business_name[:38]:<38} -> {rows[-1]["owner_name"][:44]}')
        else:
            rows.append({'match': status})
            print(f'{i:>3}. {r.business_name[:38]:<38} - {status}')
        time.sleep(PAUSE)

    out = pd.concat([leads.reset_index(drop=True),
                     pd.DataFrame(rows)], axis=1)
    front = ['owner_name', 'owner_mailing', 'match', 'business_name', 'phone',
             'website_bucket', 'website', 'priority', 'rating', 'review_count',
             'street', 'zip', 'folio', 'assessed_value', 'last_sale_amt',
             'last_sale_yr', 'year_built', 'parcel_use']
    front = [c for c in front if c in out.columns]
    out = out[front + [c for c in out.columns if c not in front]]
    out.to_csv('leads_with_owners.csv', index=False)

    hit = out.owner_name.notna().sum() if 'owner_name' in out else 0
    print(f'\nmatched {hit} of {len(out)}')
    if hit:
        port = (out[out.owner_name.notna() & (out.owner_name != '')]
                .groupby('owner_name')
                .agg(properties=('business_name', 'count'),
                     names=('business_name', lambda s: ' | '.join(s)),
                     mailing=('owner_mailing', 'first'))
                .query('properties > 1')
                .sort_values('properties', ascending=False))
        port.to_csv('owner_portfolios.csv')
        if len(port):
            print(f'\n*** {len(port)} owners hold more than one property ***')
            print(port[['properties', 'names']].to_string())
        else:
            print('\nno multi-property owners')

    print("""
wrote leads_with_owners.csv

NOTES
- Owners come back as legal entities ('NEBRASKA LODGING LLC'). To get the
  person behind the LLC, run those names through the Apify Sunbiz actor's
  officer search.
- owner_mailing is a strong qualifier on its own. A tax bill going to a
  house in Brandon means a small family operator. One going to a management
  company in another state means you are calling the wrong building.
- Anything marked VERIFY sat on a boundary between two parcels. Check it at
  https://gis.hcpafl.org/PropertySearch/
- The parcel layer number and the field names are resolved at startup, so a
  renumbered layer or a renamed column prints a '!!' line rather than
  failing silently. Anything it cannot resolve is listed against the
  service's own layer/field list at
  https://www25.swfwmd.state.fl.us/arcgiswmis/rest/services/r27/LocationInfo/MapServer
""")


if __name__ == '__main__':
    main()
