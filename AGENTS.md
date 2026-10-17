# NetVault Development Guidelines

## Release consistency

- A versioned release includes CLI, server, rendered web UI, static asset URLs and usage guides.
- Keep the public CLI command set small; update existing help and examples when behavior changes.
- Use the package version for UI and asset versions; do not add separately maintained frontend versions.
- GitHub Actions handles package publication. Deploy the matching server/web version directly on the VPS when releasing; verify live CLI/API/web versions before reporting completion.
- Keep web guide copy buttons aligned with the displayed commands and CLI declarations; version consistency checks include `scripts/check-cli-guide.py`.

## Web Interface Language

- NetVault now supports **English (en), Simplified Chinese (zh-CN), and Traditional Chinese (zh-TW)**, as explicitly requested on 2026-10-17.
- Write new canonical UI text in English in Jinja templates and application JavaScript, and supply both Chinese variants in `static/i18n-catalog.tsv`. Regenerate `static/i18n.js` with `python scripts/build_i18n.py` and keep the generated bundle in sync with its source.
- The only Chinese text directly embedded in HTML is the language menu's own native-language labels (简体中文 and 繁體中文). Other user-facing interface translations belong in the catalog; do not spread hard-coded localized strings across templates and feature scripts.
- The menu icon lives immediately below the theme button in the bottom-left sidebar; guest login pages have an equivalent floating chooser.
- Keep DOI identity, PDF titles and authors, journal names, filenames, technical commands, user-entered values, and research data in their original language. Language selection must never modify or submit this data.
- Run `tests/test_i18n_web.py`, the in-repo JavaScript locale behavior tests and the existing web UI language-source guard before deploying. Keep the guard enabled, updated to allow only the explicit localized-menu labels and translation catalog.
