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
