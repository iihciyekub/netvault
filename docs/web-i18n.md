# NetVault Website Localization

NetVault offers English (en), 简体中文 (zh-CN) and 繁體中文 (zh-TW).
The compact language icon lives **immediately beneath the theme toggle** at
the bottom of the left sidebar. Login pages (without a sidebar) show the same
menu beneath the floating theme toggle.

## Behavior

- Default: the browser's preferred language when it is English, Simplified
  Chinese or Traditional Chinese; otherwise English.
- Override: click the language icon and choose one of the three native-language
  options. The selection is persisted with the browser's localStorage key
  `netvault:language:v1`, with the `netvault_locale` cookie as fallback.
- On choosing a new locale, the current page reloads in place (same path and
  query), clearing the English/Chinese PJAX page cache. This does not log out,
  change user permissions, or send document updates. Unsaved text in an open
  form may be lost on a language switch.
- All interface text starts from English canonical templates. The client
  renderer safely localizes recognized labels, form placeholders, tooltips,
  accessible names, tab titles and dynamically inserted status messages.
  MutationObserver catches dynamic uploads, journals and PDF result UIs.
  All important labels are present in the local bundled catalog.
- Commands in `<code>`, PDF metadata, DOI, paper titles, filenames, user-entered
  text, journal names, and original research data are deliberately **not
  translated**. Never rewrite document identities during language switching.
- The only JavaScript is first-party, bundled locally and permitted by the
  existing CSP. The translation engine makes no API, translation-service or
  external network requests. Without JS, English forms and help remain
  usable; the picker itself needs JavaScript.

## Editing UI translations

1. Add or edit canonical English text in Jinja templates or `static/app.js`.
2. Add a line to
   `packages/netvault-server/src/netvault_server/server/static/i18n-catalog.tsv`:
   `Original English<TAB>简体中文<TAB>繁體中文`. Do **not** translate command
   syntax, DOI identifiers, user data, or field values.
3. Build:
   `python scripts/build_i18n.py`.
   The script combines the catalog with `static/i18n-runtime.js` into
   `static/i18n.js` and rejects conflicting keys and malformed rows.
4. Run the tests:
   `pytest -q tests/test_i18n_web.py` and
   `node tests/check_i18n_ui.cjs`. The canonical English source test remains
   enabled in `tests/test_web_and_stats.py`.
5. Deploy the generated asset, source catalog, templates, CSS, main script,
   and hash-aware server renderer together. Rebuild the web container.
   Confirm the `/nv/static/i18n.js` request returns 200 and the admin and
   logged-out HTML both contain the language picker.

## Production precautions

This is a **UI-only** feature. Do not start maintenance jobs, PDFCraft
compression, Crossref writeback, backups, or migrations unrelated to UI language.
Preserve unrelated uncommitted changes to the NetVault repository on the
developer workstation and VPS.
