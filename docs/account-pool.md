# NVIDIA Account Pool

The adapter can load a private NVIDIA account credential pool at startup. This keeps email/password credentials out of Git while still making the pool visible to the admin dashboard in masked form.

## Local Setup

1. Copy the example file:

   ```powershell
   Copy-Item .\nvidia-accounts.example.json .\nvidia-accounts.json
   ```

2. Put the real accounts in `nvidia-accounts.json`.

3. Point `.env` at the private file:

   ```text
   NVIDIA_ACCOUNT_POOL_FILE=nvidia-accounts.json
   ```

`nvidia-accounts.json` is ignored by Git and Docker build context rules.

## Format

JSON object:

```json
{
  "accounts": [
    {
      "email": "user@example.com",
      "password": "account-password",
      "enabled": true,
      "note": "NVIDIA Build account"
    }
  ]
}
```

Inline `.env` format is also supported for small pools:

```text
NVIDIA_ACCOUNT_POOL=user@example.com|password|true|note,other@example.com|password|false|standby
```

## Admin Endpoints

```text
GET  /api/admin/accounts
POST /api/admin/pool/reload
```

`/api/admin/accounts` returns masked emails only. `/api/admin/pool/reload` reloads the account file and adds any new `NVIDIA_API_KEYS` from `.env` without restarting the adapter.

## Important

NVIDIA chat requests still require `nvapi-...` API keys. The account pool is a secure credential registry for automation that logs in to NVIDIA Build and obtains or refreshes those keys. Keep the generated `nvapi-...` keys in `NVIDIA_API_KEYS`, then call `/api/admin/pool/reload` or restart the adapter.
