# CDN cutover runbook

This stack can run behind a Hong Kong anti-DDoS CDN or Cloudflare. Keep Caddy as the origin HTTPS reverse proxy and put the CDN in front of `Zteapi.com`.

## Preferred edge

Use Cloudflare as the current primary edge. Keep the Hong Kong anti-DDoS CDN prepared as a separate future cutover path, but do not stack it in front of or behind Cloudflare for the same production hostname unless you are deliberately debugging a short maintenance window.

Current CNMCDN preparation for `Zteapi.com`:

- Site ID: `90352`
- CNAME: `drknxj52.svipcdn.cn`
- Package: Asia-Pacific basic anti-DDoS, expires at `2026-06-15 17:07:34`
- Current authoritative DNS: `ns3.diymysite.com` / `ns4.diymysite.com`
- Current production DNS still points directly to the origin until DNS is changed.

Do not switch production DNS until HTTPS is bound on the CDN. CNMCDN's managed ZeroSSL flow requires either:

- changing DNS to the CNMCDN CNAME first, then requesting the certificate, or
- configuring DNS API credentials in CNMCDN so it can complete DNS validation.

Avoid uploading the origin server's TLS private key to the CDN unless you explicitly accept the extra key-exposure risk.

## Required CDN behavior

Origin:

- Origin address: the server public IP.
- Origin protocol: HTTPS.
- Origin port: 443.
- Origin Host header: `Zteapi.com`.
- Enable SNI for `Zteapi.com` when the CDN has this option.
- Pass through `X-Forwarded-For`, `X-Real-IP`, and `X-Forwarded-Proto`.
- Enable WebSocket pass-through if the CDN offers it.

Never cache these dynamic routes:

- `/`
- `/login*`
- `/register*`
- `/forgot-password*`
- `/reset-password*`
- `/dashboard*`
- `/purchase`
- `/payment`
- `/subscriptions`
- `/orders`
- `/api/*`
- `/v1/*`
- `/health`
- `/qrpay/*`

These static routes can be cached briefly:

- `/assets/*`
- `/docs/*`
- `/qrpay-assets/*`
- `/logo.png`
- `/favicon.ico`
- `/manifest*`
- `/robots.txt`
- `/sw.js`

Do not use a broad CDN rule that caches `html` or `htm` for this application. Login, dashboard, payment and order pages are dynamic and must remain private. The origin Caddy config sends `no-store` on the dynamic shell as a second safety layer.

For this repository, Caddy also sends conservative cache headers so a CDN mistake is less likely to cache payment or API state.

## Hong Kong CDN setup checklist

1. Add `Zteapi.com` as an accelerated domain.
2. Set origin to the server public IP, HTTPS 443, Host/SNI `Zteapi.com`.
3. Add the no-cache rules listed above before any broad cache rule.
4. Add short static cache rules for `/assets/*`, `/docs/*` and `/qrpay-assets/*`.
5. Enable WAF/CC protection.
6. Add rate limits for:
   - `/login*`
   - `/register*`
   - `/api/*`
   - `/v1/*`
   - `/qrpay/api/*`
7. Keep `/qrpay/api/watch/*` reachable for the Windows WeChat watcher.
8. Bind HTTPS on the CDN. Prefer managed ZeroSSL/Let's Encrypt with DNS validation over uploading the origin TLS private key.
9. Point DNS `@` and `www` to the CDN CNAME/IP provided by the CDN. If the DNS provider does not support CNAME flattening at the apex, use the provider's ALIAS/ANAME feature, the CDN's provided A records, or move DNS to a provider that supports proxied apex records.
10. Run:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
BASE_URL=https://Zteapi.com ORIGIN_IP=SERVER_PUBLIC_IP EXPECTED_CDN=hongkong ./scripts/cdn-preflight.sh
```

Ongoing status check:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
CNMCDN_SITE_ID=90352 \
CNMCDN_CNAME=drknxj52.svipcdn.cn \
CNMCDN_EXPIRES_AT='2026-06-15 17:07:34' \
ORIGIN_IP=38.97.254.150 \
./scripts/cdn-status.sh
```

## Cloudflare fallback

Cloudflare is the current primary setup for `Zteapi.com`:

1. Create a Cloudflare account.
2. Add `Zteapi.com`.
3. Change registrar nameservers to the two Cloudflare nameservers.
4. Add DNS records:
   - `A @ -> SERVER_PUBLIC_IP`, proxied.
   - `CNAME www -> Zteapi.com`, proxied.
5. SSL/TLS mode: `Full (strict)`. Do not use `Flexible`.
6. Cache rules: bypass cache for all dynamic routes listed above.
7. WAF/rate limits: protect login, registration, API and QRPay endpoints.
8. Keep `/qrpay/api/watch/*` reachable for the Windows WeChat watcher; do not put managed challenges on watcher callback paths.
9. Run:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
BASE_URL=https://Zteapi.com ORIGIN_IP=SERVER_PUBLIC_IP EXPECTED_CDN=cloudflare ./scripts/cdn-preflight.sh
```

After the Cloudflare account, zone, nameservers and API token are ready, the DNS-record part can be applied with:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
CF_API_TOKEN='cloudflare-api-token-with-dns-edit' \
CF_ZONE_NAME=Zteapi.com \
ORIGIN_IP=38.97.254.150 \
./scripts/cloudflare-fallback.sh --apply
```

The script intentionally does not create a Cloudflare account, complete email verification, or change registrar nameservers. Those steps require interactive account ownership confirmation.

## Origin lockdown

Only lock the origin after CDN cutover is tested from a normal browser and the WeChat watcher is healthy.

Recommended firewall policy:

- Allow SSH only from the operator's trusted IP.
- Allow ports 80/443 only from the active CDN origin IP ranges.
- Keep Docker-internal services unexposed.

Do not commit CDN passwords, Cloudflare passwords, API tokens, DNS credentials or registrar credentials to Git.
