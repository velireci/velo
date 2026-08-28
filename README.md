# velo

## Tampa lodging owner lookup

Two scripts turn the 43 Tampa lodging leads into a call list with the
property owner attached.

    pip install requests pandas openpyxl

    python fetch_owners.py     # leads_with_owners.csv + owner_portfolios.csv
    python merge_owners.py     # owner_name -> col A, owner_mailing -> col D

`fetch_owners.py` asks the SWFWMD regional GIS server which parcel polygon
contains each lead's lat/lng, so matching never depends on address strings.
The parcel layer number and the field names are resolved from the service at
startup instead of being hardcoded: a renumbered layer or a renamed column
prints a `!!` line and is worked around, and a missing owner field aborts
rather than writing 43 empty rows.

`merge_owners.py` writes into the `Call List` sheet, matching on
business_name (tolerant of case, padding and curly apostrophes). It checks
the A/D/H headers before writing and refuses if the columns have moved.
`--dry-run` reports without saving; `--out FILE` writes a copy.

    python test_fetch_owners.py

runs both against a fake ArcGIS service on localhost - no network needed.

### Network note

`fetch_owners.py` needs outbound access to `www25.swfwmd.state.fl.us`.
Sandboxes with an egress allowlist will fail every row with
`ProxyError ... 403 Forbidden`; that is the network, not the script.
