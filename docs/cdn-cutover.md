# CDN cutover runbook

This stack can run behind a Hong Kong anti-DDoS CDN or Cloudflare. Keep Caddy as the origin HTTPS reverse proxy and put the CDN in front of `Zteapi.com`.

## Preferred edge

Use the Hong Kong anti-DDoS CDN first when it is active, because the site has China/Asia users and dynamic routes such as login, API calls and QR-code payment polling.

Use Cloudflare as the fallback when the Hong Kong CDN expires or becomes unavailable.

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
- `/zteapi-floating-doc.css`
- `/zteapi-floating-doc.js`

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
8. Point DNS `A @` and `A www` to the CDN CNAME/IP provided by the CDN.
9. Run:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
BASE_URL=https://Zteapi.com ORIGIN_IP=SERVER_PUBLIC_IP EXPECTED_CDN=hongkong ./scripts/cdn-preflight.sh
```

## Cloudflare fallback

If the Hong Kong CDN expires, move DNS to Cloudflare:

1. Create a Cloudflare account.
2. Add `Zteapi.com`.
3. Change registrar nameservers to the two Cloudflare nameservers.
4. Add DNS records:
   - `A @ -> SERVER_PUBLIC_IP`, proxied.
   - `CNAME www -> Zteapi.com`, proxied.
5. SSL/TLS mode: `Full (strict)`. Do not use `Flexible`.
6. Cache rules: bypass cache for all dynamic routes listed above.
7. WAF/rate limits: protect login, registration, API and QRPay endpoints.
8. Run:

```bash
cd /opt/sub2api-nvidia/cloud-deploy
BASE_URL=https://Zteapi.com ORIGIN_IP=SERVER_PUBLIC_IP EXPECTED_CDN=cloudflare ./scripts/cdn-preflight.sh
```

## Origin lockdown

Only lock the origin after CDN cutover is tested from a normal browser and the WeChat watcher is healthy.

Recommended firewall policy:

- Allow SSH only from the operator's trusted IP.
- Allow ports 80/443 only from the active CDN origin IP ranges.
- Keep Docker-internal services unexposed.

Do not commit CDN passwords, Cloudflare passwords, API tokens, DNS credentials or registrar credentials to Git.
