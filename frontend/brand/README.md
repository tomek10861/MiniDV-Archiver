# MiniDV Archiver — znak "Dioda REC" (3b)

Kolory: grafit `#23282F`, cyjan `#00C2CE`.

| plik | zastosowanie |
|---|---|
| `favicon.svg` | favicon (nowoczesne przeglądarki, skaluje się bez utraty) |
| `favicon-32.png` / `favicon-16.png` | fallback dla starszych przeglądarek |
| `apple-touch-icon.png` (180x180) | ikona na ekranie głównym iOS |
| `favicon-512.png` | manifest PWA / sklepy, źródło do dalszych rozmiarów |
| `mark-rec-mono.svg` | wersja jednokolorowa (nadruk, dokumentacja) |

## Wpięcie w `frontend/index.html`

```html
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
```

Pliki połóż w `frontend/` (nginx serwuje ten katalog jako root).
