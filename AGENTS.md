# NetVault Development Guidelines

## Release consistency

- A versioned release includes CLI, server, rendered web UI, static asset URLs and usage guides.
- Keep the public CLI command set small; update existing help and examples when behavior changes.
- Use the package version for UI and asset versions; do not add separately maintained frontend versions.
- GitHub Actions handles package publication. Deploy the matching server/web version directly on the VPS when releasing; verify live CLI/API/web versions before reporting completion.
- Keep web guide copy buttons aligned with the displayed commands and CLI declarations; version consistency checks include `scripts/check-cli-guide.py`.

## Web Interface Language

- All user-visible text in the NetVault web interface must be written in English.
- This rule applies to templates, JavaScript-generated messages, form labels, buttons, headings, help text, placeholders, validation messages, accessibility labels, and other UI copy.
- Do not add Chinese or mixed-language UI text unless the user explicitly changes this project-wide requirement.
- Before completing a web UI change, scan the templates and first-party static files for Chinese characters and run the test suite. The automated UI-language test must remain enabled.
- User-provided content and document metadata are data, not interface copy, and are not subject to this restriction.
