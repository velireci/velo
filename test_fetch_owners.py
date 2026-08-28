"""
Offline checks for fetch_owners.py.

Stands up a fake ArcGIS MapServer on localhost and runs the real script
against it, so the layer-resolution and field-resolution paths are exercised
without touching the live SWFWMD service.

    python test_fetch_owners.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pandas as pd

import fetch_owners

CANON_FIELDS = ['OWNERNAME', 'OWNERADD1', 'OWNERADD2', 'OWNERCITY',
                'OWNERSTATE', 'OWNERZIP', 'SITUSADD1', 'FOLIONUM',
                'PARUSEDESC', 'ASSD_TOT', 'YRBLT_ACT', 'SALE1_AMT',
                'SALE1_YEAR', 'ACRES']

# Two leads share an owner so the portfolio rollup has something to find.
SHARED_OWNER = 'NEBRASKA LODGING LLC'


class Config:
    """What the fake service should pretend to be for one test."""
    def __init__(self, layers, fields, parcel_layer):
        self.layers = layers              # {id: name}
        self.fields = fields              # {canonical: served name} or None to omit
        self.parcel_layer = parcel_layer  # id that answers /query


class Handler(BaseHTTPRequestHandler):
    cfg = None

    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        parts = [p for p in url.path.split('/') if p]

        if parts[-1] == 'MapServer':
            return self._send({'layers': [{'id': i, 'name': n}
                                          for i, n in sorted(self.cfg.layers.items())]})

        if parts[-1] == 'query':
            layer = int(parts[-2])
            if layer != self.cfg.parcel_layer:
                return self._send({'error': {'code': 400, 'message': 'Invalid layer'}})
            lng, lat = (float(x) for x in q['geometry'][0].split(','))
            return self._send(self._features(lat, lng, q['outFields'][0]))

        layer = int(parts[-1])
        if layer not in self.cfg.layers:
            return self._send({'error': {'code': 400, 'message': 'Invalid layer'}})
        served = self.cfg.fields
        return self._send({'fields': [{'name': served[c], 'alias': served[c],
                                       'type': 'esriFieldTypeString'}
                                      for c in CANON_FIELDS if c in served]})

    def _features(self, lat, lng, out_fields):
        requested = out_fields.split(',')
        served = self.cfg.fields
        # A couple of fixed coordinates stand in for the awkward cases.
        if round(lat, 4) == 27.8534:            # MacDill AFB - federal land
            return {'features': []}
        owner = (SHARED_OWNER if str(lng).startswith('-82.451')
                 else f'OWNER {abs(lng):.4f} LLC')
        values = {
            'OWNERNAME': owner,
            'OWNERADD1': '123 MAIN ST',
            'OWNERADD2': 'STE 4',
            'OWNERCITY': 'BRANDON',
            'OWNERSTATE': 'FL',
            'OWNERZIP': '33511',
            'SITUSADD1': '1 SITE RD',
            'FOLIONUM': '0123456789',
            'PARUSEDESC': 'HOTEL/MOTEL',
            'ASSD_TOT': '1250000',
            'YRBLT_ACT': '1962',
            'SALE1_AMT': '900000',
            'SALE1_YEAR': '2014',
            'ACRES': '0.75',
        }
        attrs = {served[c]: values[c] for c in CANON_FIELDS
                 if c in served and served[c] in requested}
        feats = [{'attributes': attrs}]
        if round(lat, 4) == 27.9529:            # Barrymore - on a boundary
            feats.append({'attributes': dict(attrs)})
        return {'features': feats}


@contextlib.contextmanager
def service(cfg):
    Handler.cfg = cfg
    srv = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address
    try:
        yield f'http://{host}:{port}/arcgis/rest/services/r27/LocationInfo/MapServer'
    finally:
        srv.shutdown()


@contextlib.contextmanager
def workdir():
    src = os.path.abspath(fetch_owners.LEADS)
    d = tempfile.mkdtemp()
    shutil.copy(src, os.path.join(d, fetch_owners.LEADS))
    cwd = os.getcwd()
    os.chdir(d)
    try:
        yield d
    finally:
        os.chdir(cwd)
        shutil.rmtree(d)


def run(cfg):
    """Run main() against the fake service. Returns (exit_reason, stdout)."""
    with service(cfg) as url, workdir() as d:
        fetch_owners.SERVICE = url
        fetch_owners.PAUSE = 0
        buf = io.StringIO()
        reason = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                fetch_owners.main()
            except SystemExit as e:
                reason = str(e)
        out = buf.getvalue()
        csv = (pd.read_csv(os.path.join(d, 'leads_with_owners.csv'))
               if os.path.exists(os.path.join(d, 'leads_with_owners.csv')) else None)
        port = (pd.read_csv(os.path.join(d, 'owner_portfolios.csv'))
                if os.path.exists(os.path.join(d, 'owner_portfolios.csv')) else None)
    return reason, out, csv, port


def check(name, cond, detail=''):
    print(f'{"PASS" if cond else "FAIL"}  {name}' + (f'  [{detail}]' if not cond else ''))
    return bool(cond)


def main():
    identity = {c: c for c in CANON_FIELDS}
    normal_layers = {0: 'Aerials', 71: 'Hillsborough County Parcels', 88: 'Soils'}
    ok = True

    # 1. Nothing has moved.
    reason, out, csv, port = run(Config(normal_layers, identity, 71))
    ok &= check('happy path exits cleanly', reason is None, reason)
    ok &= check('uses layer 71', 'layer 71: Hillsborough County Parcels' in out)
    ok &= check('43 rows written', csv is not None and len(csv) == 43,
                None if csv is None else len(csv))
    ok &= check('42 of 43 owners matched (1 federal parcel)',
                csv is not None and csv.owner_name.notna().sum() == 42,
                None if csv is None else csv.owner_name.notna().sum())
    ok &= check('owner_mailing assembled',
                csv is not None and
                csv.owner_mailing.dropna().iloc[0] == '123 MAIN ST STE 4, BRANDON, FL 33511',
                None if csv is None else csv.owner_mailing.dropna().iloc[0])
    ok &= check('federal parcel reported, not crashed',
                csv is not None and
                (csv.match == 'no parcel at this point').sum() == 1)
    ok &= check('boundary parcel flagged VERIFY',
                csv is not None and csv.match.str.contains('VERIFY').sum() == 1)
    ok &= check('owner_name is first column',
                csv is not None and list(csv.columns)[:3] ==
                ['owner_name', 'owner_mailing', 'match'])
    ok &= check('portfolio rollup found the shared owner',
                port is not None and SHARED_OWNER in set(port.owner_name),
                None if port is None else list(port.owner_name))

    # 2. The parcel layer has been renumbered.
    moved = {0: 'Aerials', 71: 'Storm Surge', 104: 'Hillsborough County Parcels'}
    reason, out, csv, port = run(Config(moved, identity, 104))
    ok &= check('renumbered layer is found', reason is None, reason)
    ok &= check('renumbering is reported', 'using layer 104' in out)
    ok &= check('renumbered run still matches owners',
                csv is not None and csv.owner_name.notna().sum() == 42)

    # 3. Fields renamed upstream, one dropped.
    renamed = dict(identity, OWNERNAME='OWNER_NAME', OWNERZIP='MAILZIP')
    del renamed['ACRES']
    reason, out, csv, port = run(Config(normal_layers, renamed, 71))
    ok &= check('renamed fields resolve', reason is None, reason)
    ok &= check('rename is reported', 'OWNERNAME -> OWNER_NAME' in out)
    ok &= check('dropped field is reported', 'ACRES' in out and 'left blank' in out)
    ok &= check('renamed run still matches owners',
                csv is not None and csv.owner_name.notna().sum() == 42)
    ok &= check('dropped field is blank, not fatal',
                csv is not None and csv.acres.isna().all())

    # 4. No owner field at all - must fail loudly, not write empty rows.
    gutted = {c: c for c in CANON_FIELDS if c != 'OWNERNAME'}
    reason, out, csv, port = run(Config(normal_layers, gutted, 71))
    ok &= check('missing owner field aborts', reason is not None and
                'required field' in reason.lower(), reason)
    ok &= check('available fields listed on abort', 'OWNERADD1' in out)

    # 5. Parcel layer gone entirely.
    reason, out, csv, port = run(Config({0: 'Aerials', 71: 'Soils'}, identity, 71))
    ok &= check('missing parcel layer aborts with the layer list',
                reason is not None and 'LAYER_ID' in reason, reason)

    print('\n' + ('all checks passed' if ok else 'FAILURES ABOVE'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
