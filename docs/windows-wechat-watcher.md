# Windows WeChat Watcher

This watcher is for personal/static WeChat QR-code receipts. It runs on the Windows PC that logs into the receiving WeChat account, reads only payment-related text from local WeChat message sources, extracts the paid amount, and posts it to:

```text
POST https://Zteapi.com/qrpay/api/watch/wechat-receipt
```

The server still performs the final match. If the watcher does not send `out_trade_no`, `qrpay-bridge` only confirms a payment when exactly one pending WeChat order has the same unique `pay_amount`.

## Preconditions

- Windows 10/11.
- Python 3.10+ is installed and available as `python`.
- PC WeChat is logged into the receiving WeChat account.
- `ylytdeng/wechat-decrypt` is installed on the same Windows account and has produced a decrypted `decrypted\message` directory.
- The WeChat payment/receipt helper chat is present in the local WeChat message database.
- Server config keeps WeChat amount jitter enabled:

```text
QRPAY_ENABLE_WECHAT_CODE=true
QRPAY_AMOUNT_JITTER_METHODS=wechat_code
QRPAY_AMOUNT_JITTER_CENTS=50
```

## Configure

Use the watcher directory:

```powershell
cd C:\path\to\sub2api-nvidia-gateway\cloud-deploy\qrpay-bridge\watchers
Copy-Item .\wechat_windows_watcher.env.example .\wechat_windows_watcher.env
notepad .\wechat_windows_watcher.env
```

Fill:

```text
QRPAY_BRIDGE_URL=https://Zteapi.com/qrpay
QRPAY_WATCHER_SECRET=your-real-secret
WECHAT_WATCHER_SOURCE=wechat-decrypt-db
WECHAT_DECRYPT_MESSAGE_DIR=C:\path\to\wechat-decrypt\decrypted\message
WECHAT_DECRYPT_DB_GLOB=message_*.db
WECHAT_WATCHER_STATE_RETENTION_DAYS=30
```

Do not commit `wechat_windows_watcher.env`.

This source is receipt-only: it polls the already-decrypted message SQLite files in place, parses WeChat receipt/transfer records, keeps a small local `seen_receipts` cache, and sends only the payment event to `qrpay-bridge`. It does not export chats, images, voice, video, or full WeChat data to the server. The local cache is pruned by `WECHAT_WATCHER_STATE_RETENTION_DAYS`.

OCR remains a fallback. To use it, set `WECHAT_WATCHER_SOURCE=wechat-window-ocr`. `WECHAT_WINDOW_OCR_NO_FOREGROUND=false` first tries to capture the WeChat window in the background. If that capture is blank, it falls back to bringing WeChat forward and reading the visible window.

## Parser Smoke Test

This does not contact the server:

```powershell
python .\wechat_windows_watcher.py --test-text "微信收款助手 收款到账0.01元"
```

Expected result: JSON with `"amount": "0.01"` and a `wechat-win-...` transaction id.

For a transfer XML smoke test:

```powershell
python .\wechat_windows_watcher.py --test-text '<msg><appmsg><title>微信转账</title><type>2000</type><wcpayinfo><paysubtype>3</paysubtype><feedesc>￥0.01</feedesc><transcationid>test123</transcationid></wcpayinfo></appmsg></msg>'
```

## Dry Run

Start with dry run:

```powershell
.\run_wechat_windows_watcher.ps1 -DryRun
```

Important behavior:

- On first startup it scans current source rows and marks old matching receipts as seen.
- It does not confirm orders in `-DryRun`.
- Keep this window open, create a tiny WeChat test order, pay it, and watch for `DRY-RUN match amount=...`.

If the dry run sees nothing with `wechat-decrypt-db`, confirm that `WECHAT_DECRYPT_MESSAGE_DIR` points at the active decrypted message directory and that the latest WeChat receipt message appears in those `message_*.db` files.

## Real Run

After dry run can see a new real receipt:

```powershell
.\run_wechat_windows_watcher.ps1
```

The watcher sends:

```json
{
  "amount": "0.01",
  "transaction_id": "wechat-decrypt-...",
  "payer": "",
  "source": "wechat-decrypt-db",
  "source_id": "message_0.db:Msg_xxx:123:456",
  "observed_at": "2026-05-17T20:00:00+08:00",
  "raw_hash": "sha256..."
}
```

By default it does not send the raw notification text. Add `-SendRawText` only while debugging.

## Optional Startup Task

After it works manually, create a logon task:

```powershell
.\run_wechat_windows_watcher.ps1 -InstallStartupTask
```

This starts the watcher hidden after the receiving Windows user logs in. To start it hidden immediately:

```powershell
.\run_wechat_windows_watcher.ps1 -StartHidden
```

To remove the logon task:

```powershell
.\run_wechat_windows_watcher.ps1 -UninstallStartupTask
```

Sub2API user login in the browser cannot and should not start this watcher. The watcher belongs to the receiving WeChat account's Windows machine. Once the scheduled task is installed, it starts with that Windows account, not with each website user.

This is the intended "automatic realtime" mode for production: the local Windows account logs in, WeChat stays logged in, `wechat-decrypt` keeps its decrypted message output available, and the scheduled watcher continuously posts heartbeats. The user payment center shows those heartbeats as 微信监听正常/异常.

## Limitations

This is less stable than Android VMQ or official WeChat Pay:

- It depends on PC WeChat and the `wechat-decrypt` output remaining current.
- If PC sleeps, logs out, WeChat exits, or the decrypted DB stops updating, receipts may not be detected.
- OCR is still available as a fallback, but it is not the primary source.
- It should be treated as a practical bridge, not a bank-grade payment callback.
