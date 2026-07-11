# NetVault Development Guidelines

## Web Interface Language

- All user-visible text in the NetVault web interface must be written in English.
- This rule applies to templates, JavaScript-generated messages, form labels, buttons, headings, help text, placeholders, validation messages, accessibility labels, and other UI copy.
- Do not add Chinese or mixed-language UI text unless the user explicitly changes this project-wide requirement.
- Before completing a web UI change, scan the templates and first-party static files for Chinese characters and run the test suite. The automated UI-language test must remain enabled.
- User-provided content and document metadata are data, not interface copy, and are not subject to this restriction.
