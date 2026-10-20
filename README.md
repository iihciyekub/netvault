# NetVault

**`nv`** is a lightweight CLI for uploading, searching, and downloading PDFs from a shared NetVault server. A simple web dashboard handles PDF and journal management.

## Install

**macOS / Linux**

```bash
curl -fsSLo /tmp/netvault-install.sh https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.sh
bash /tmp/netvault-install.sh
```

**Windows (PowerShell)**

```powershell
irm https://raw.githubusercontent.com/iihciyekub/netvault/main/scripts/install.ps1 -OutFile ./netvault-install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File ./netvault-install.ps1
Remove-Item ./netvault-install.ps1
```

## Use `nv`

```bash
nv login https://iiaide.com/nv
nv upload ./papers
nv search "journal"
nv download --file ./dois.txt --to ./downloads
nv status
nv update
```

Run `nv --help` for all commands.

## Web management

Open **[NetVault Web](https://iiaide.com/nv/web)** to search and manage PDFs, journal lists, and account/admin settings.

### Offline folder cleanup

Preview recursively with `nv tidy .` and apply with `nv tidy . --apply`. Only confirmed duplicates in the same directory are deleted; PDFs without a DOI in Document Info or pages 1-2 move into each parent's `unidentifys/`. The command never accesses the server.

### Single operation journal

All local CLI operation history lives in `.netvault/history.jsonl` at the chosen root. Read-only previews write nothing; applied file changes and unresolved upload outcomes use the hidden JSONL. A completed upload removes resolved per-PDF entries from the active log after verifying the server result. `nv upload --retry-failed` reads the journal; historical SQLite and JSON records are retained, not overwritten or deleted. Add `--report custom.json` (upload) only when an external consumer needs a legacy export.


### Download PDFs by journal title

Use the exact journal title (case/spacing normalized) to download every PDF
**already stored in NetVault** for that journal:

```bash
nv download --journal "Management Science" --to ./management-science --workers 8
```

This fetches the journal's server-side DOI inventory before downloading and
uses the usual checksum verification, skip-existing, and resume behavior.
It does not obtain papers that are not stored in NetVault. An unknown title
returns an error rather than performing a partial/fuzzy journal match.
Existing DOI and `--file` inputs remain supported and can be combined.
