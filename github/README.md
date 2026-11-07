# Sonnenburg national reference

A self-hosted site about Sonnenburg, with a national home page, geographic gazetteer, encyclopedia, discussions, historical charts, and a table/map population editor. Flask runs behind Waitress; `server.py` binds to `127.0.0.1:8004` by default for your reverse proxy and systemd service.

## Run

Requires Python 3.10+.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./start.sh
```

Open **http://localhost:8004**. Visitors can browse immediately. Existing atlas data is preserved. Overpass is only queried when an administrator presses a refresh button. The map requests OGF tiles as you browse.

When updating an existing installation, **run `pip install -r requirements.txt` again** to install the new website dependencies before restarting the service. `start.sh` installs dependencies automatically only when the virtual environment does not exist.

## OGF sign-in and roles

Sign-in redirects users to **OpenGeofiction**. The site exchanges the authorization code on the server and verifies the account via OGF's authenticated `/api/0.6/user/details.json` endpoint. It uses OAuth 2 with PKCE and state validation, through Authlib. OGF passwords are never entered into this site and provider tokens are not retained after login.

- **MiniMapper** and **jr000** are administrators, matched exactly against the verified OGF display name.
- All other signed-in accounts are readers.
- Signed-out visitors and readers can view pages, discussions, charts, and the atlas; they can sort, filter, export data, and use the random picker.
- Only administrators can change population/density or central towns, refresh OGF data, edit wiki pages, add/remove comments, or create/edit/delete charts. Every write is checked on the server, as well as in the interface.

### Configure your subdomain

1. Sign into OGF and open **https://opengeofiction.net/oauth2/applications**.
2. Register a **confidential** application, such as “Sonnenburg national reference”.
3. Set its exact redirect URI to **`https://YOUR-SUBDOMAIN/auth/callback`**.
4. Enable only **`read_prefs`** (read user preferences), which allows retrieving the verified account identity. No map editing permissions are needed.
5. Put these environment variables in your systemd environment file (see `.env.example`):

```ini
SITE_URL=https://YOUR-SUBDOMAIN
OGF_CLIENT_ID=YOUR_APPLICATION_CLIENT_ID
OGF_CLIENT_SECRET=YOUR_APPLICATION_CLIENT_SECRET
```

6. Have your reverse proxy forward that subdomain to **127.0.0.1:8004**, preserving the original **Host** header and terminating HTTPS. Restart your service after setting the environment.

`SITE_URL` must match the origin users visit, including a nonstandard port if one is used. It determines the callback, allowed write origin, trusted host, and Secure cookie behavior. Public origins must use HTTPS. This application lives at the subdomain root, not beneath a URL prefix. No forwarded-header trust is needed; the public origin is explicit.

`SESSION_SECRET` is optional: the app generates a key in `data/.session-secret` with mode 0600 if unset. Keep that file stable across restarts. Alternatively supply a random secret of at least 32 characters through the environment. Sessions expire after 12 hours. Cookies are HttpOnly, SameSite=Lax, and Secure for HTTPS. Writes also require a session-bound CSRF token.

`DATA_DIR` optionally moves all persistent data to an absolute writable directory. Existing `data/atlas.json` must be copied there if changing it. Run a **single application process**; Waitress handles concurrent requests with threads. Atlas refresh jobs and its in-memory snapshot are shared within that process.

The script does **not** automatically read `.env`; supply variables using systemd's `EnvironmentFile`, your process manager, or your shell. Keep environment files containing credentials private.

Without OAuth credentials, browsing works and sign-in explains the missing configuration. There is **no development login or username-based bypass**. Real OGF consent and callback completion require your registered application; tests mock the provider and cannot substitute for the deployment check. For local OAuth testing, register `http://127.0.0.1:8004/auth/callback` and use `SITE_URL=http://127.0.0.1:8004`.

## Pages and writing

- **`/`** — national home page with interactive regional map and counts. Administrators can edit its introduction, stored in the `Sonnenburg` article.
- **`/explorer/`** — the original sortable map/table editor. Table-name clicks still zoom the map. Map popup titles now link to place profiles.
- **`/wiki/places/`** — searchable directory, filtered by administrative level.
- **`/wiki/places/RELATION_ID/`** — a place profile for every mapped level 4/6/7/8 relation and the level 2 nation, with a boundary map, automatic statistics, parent/child links, an editable article, custom infobox fields, and discussion.
- **`/wiki/places/RELATION_ID/charts/`** — independent historical indicators for that place. Add yearly population, GDP per capita, or any numeric series; choose line/bar charts and units. All charts include a data table and source/notes field. Historical series **do not** change the current atlas population or roll up to parents.
- **`/wiki/`** — searchable encyclopedia, with administrator article creation and categories such as Person, Law, History, or Culture.
- **`/wiki/ARTICLE-SLUG/`** — general articles, infoboxes, revision history, and comments.

Article syntax supports Markdown headings, tables, lists, links, and images, plus a MediaWiki-like subset:

```text
== Geography ==
'''Bold''' and ''italic'' text.
Read [[Constitution|the constitution]].
Visit [[places/441973|Stad Sonnenburg]].
```

Simple `{| ... |}` MediaWiki tables are also supported; full MediaWiki templates, extensions, and transclusion are not. Preview uses the same server renderer as published articles. HTML is sanitized. Infobox fields use one `Label: Value` per line. Automatically computed geographic statistics appear separately from custom fields.

Article edits retain revision history. An administrator can select **Use as draft** on a historical revision, review it, and save it as a new revision. Concurrent article saves and chart updates detect stale edits rather than overwriting newer changes. Comments are plain text, attributed to the signed-in OGF username; removing a comment hides it while retaining it in storage.

## Atlas editing and calculations

- Choose level 4, 6, 7, or 8; sort any column, search, or filter by canton.
- Administrators edit population or density and press Enter or leave the cell. Density is converted to a whole-person population, so small rounding differences are expected.
- Level 7 communes with level 8 children sum those children; communes without level 8 children are editable. Levels 4 and 6 sum level 7 communes directly, avoiding double counting.
- Blank means **unknown**; zero is explicitly zero. An asterisk marks incomplete population totals.
- Central towns are imported from explicit centre/capital tags or admin-centre members when available and can be edited on this site.
- Heatmaps support population, density, land area, mapped objects, objects per km², canton, and level 4 region. Settings include linear/log/quantile scales, opacity, labels, and basemap visibility.
- The wheel includes all places of the chosen level, independent of table filters, with equal selection probability. CSV exports the current filtered/sorted table.

**Pull from Overpass** fetches boundaries and mapped water. **Refresh objects** separately counts all nodes, ways, and relations per boundary; it can take several minutes. Both run in the background. Failed boundary refreshes retain prior data, and completed count batches are saved immediately.

Land areas use a local Lambert azimuthal equal-area projection, subtracting mapped `natural=water` / `waterway=riverbank` polygons. Unmapped water may remain included. Areas are fixed between refreshes; these are map calculations, not official survey measurements.

Incomplete boundaries may be closed using shortest paths along existing neighboring administrative borders. These are explicitly **provisional (†)** and appear in geometry notes, place profiles, map popups, tables, and CSV. Nothing modifies OGF. If a boundary cannot be reconstructed, the refresh fails and preserves saved data.

Parent assignment uses largest polygon overlap, requiring at least 50% coverage. Missing parents or overlap below 98% are reported in **About the data**. Level 8 places without a level 7 parent do not contribute to parent totals. National population sums communes; national land area uses the country polygon.

Object counts follow **Overpass area membership**, not strict polygon containment: nodes inside, ways with qualifying nodes, and relations with qualifying members. Shared objects can belong to several regions. Parent counts are queried independently and never summed from children. Counts retain their own timestamps, shown on object-cell hover and in CSV.

## Persistence and backups

Keep the entire **`data/`** directory (or your configured `DATA_DIR`):

- `atlas.json` — boundaries, counts, and site population/central-town edits, keyed by relation ID.
- `site.sqlite3` — articles, revision history, comments, and historical charts.
- `.session-secret` — generated signing key, if no environment secret is supplied.

For a simple consistent backup, stop the service and copy the directory. For online SQLite backups use SQLite's backup API; copying only `site.sqlite3` while WAL is active can miss recent writes. Include the atlas JSON too. No content edits write back to OGF.

Refreshes preserve edits, including those for temporarily missing relations. A commune's earlier direct population remains saved but is ignored while it has level 8 children. Historical chart series stay independent.

## Verify

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover geometry/water areas, population rollups, permissions for both admins and normal users, CSRF, OAuth state/PKCE, verified identity, sanitized wiki text, revisions/conflicts, comments, and chart validation/storage. Provider responses are mocked; live login needs the deployment credentials.

The Overpass interpreter is `https://overpass.opengeofiction.net/api/interpreter`. Map imagery uses `https://tile.opengeofiction.net/ogf-carto/{z}/{x}/{y}.png`. Leaflet 1.9.4 and its license are vendored in `static/vendor`. No JavaScript build step is needed.
