# TLM Auto Download

This service periodically mirrors all packs listed by
`https://tlmdl.cfpa.team/info.json`, verifies each pack with the upstream CRC32
checksum, and builds one merged Touhou Little Maid custom pack zip.

The output zip is meant to be served by your own HTTP server, then referenced in
`ClientPackDownloadUrls`.

## What it does

- Fetches `info.json` on a fixed interval.
- Downloads only missing or changed packs.
- Verifies each downloaded pack with the `checksum` field from `info.json`.
- Builds one merged zip that keeps standard TLM paths like `assets/<namespace>/...`.
- Adds directory entries such as `assets/<namespace>/` so TLM can discover each namespace.
- Avoids nested zip packs. If a source pack contains `modelA.zip`, it will not be unpacked.

## Docker Compose

```yaml
services:
  tlm-auto-download:
    image: ghcr.io/zack-zzq/tlm-downloader:latest
    restart: unless-stopped
    environment:
      TLM_INTERVAL_SECONDS: "21600"
      TLM_DOWNLOAD_DELAY_SECONDS: "3"
    volumes:
      - ./data:/data
```

The generated pack will be written to:

```text
./data/output/tlm_all_packs.zip
```

Publish that file through your own web server and configure the mod server:

```toml
ClientPackDownloadUrls = [
  "https://example.com/tlm/tlm_all_packs.zip"
]
```

## Environment variables

| Name | Default | Description |
| --- | --- | --- |
| `TLM_INFO_URL` | `https://tlmdl.cfpa.team/info.json` | Upstream index URL. |
| `TLM_INTERVAL_SECONDS` | `21600` | Loop interval. Six hours by default. |
| `TLM_DOWNLOAD_DELAY_SECONDS` | `3` | Delay after each actual package download. |
| `TLM_HTTP_TIMEOUT_SECONDS` | `60` | HTTP request timeout. |
| `TLM_MAX_PACK_BYTES` | `26214400` | Max size per source pack. Matches the mod default 25 MiB. |
| `TLM_CACHE_DIR` | `/data/cache` | Cache for source zip files. |
| `TLM_OUTPUT_ZIP` | `/data/output/tlm_all_packs.zip` | Merged output zip. |
| `TLM_STATE_DIR` | `/data/state` | State and manifest directory. |
| `TLM_RUN_ONCE` | `false` | Run one cycle and exit. Useful for cron or CI. |
| `TLM_DELETE_STALE` | `false` | Delete cached zips no longer listed in `info.json`. |

## Local run

```bash
TLM_RUN_ONCE=true python -m tlm_auto_download
```

If running outside Docker, set `PYTHONPATH=src` from this directory.
