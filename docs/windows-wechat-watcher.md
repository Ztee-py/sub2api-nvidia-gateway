# Windows WeChat Watcher

This watcher is for personal/static WeChat QR-code receipts. It runs on the Windows PC that logs into the receiving WeChat account, reads new WeChat receipt notifications, extracts the paid amount, and posts it to:

```text
POST https://Zteapi.com/qrpay/api/watch/wechat-receipt
```

The server still performs the final match. If the watcher does not send `out_trade_no`, `qrpay-bridge` only confirms a payment when exactly one pending WeChat order has the same unique `pay_amount`.

## Preconditions

- Windows 10/11.
- Python 3.10+ is installed and available as `python`.
- PC WeChat is logged into the receiving WeChat account.
- Windows notifications are enabled for WeChat.
- The WeChat payment/receipt helper chat is not muted.
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
```

Do not commit `wechat_windows_watcher.env`.

## Parser Smoke Test

This does not contact the server:

```powershell
python .\wechat_windows_watcher.py --test-text "微信收款助手 收款到账0.01元"
```

Expected result: JSON with `"amount": "0.01"` and a `wechat-win-...` transaction id.

## Dry Run

Start with dry run:

```powershell
.\run_wechat_windows_watcher.ps1 -DryRun
```

Important behavior:

- On first startup it scans current Windows notifications and marks old matching receipts as seen.
- It does not confirm orders in `-DryRun`.
- Keep this window open, create a tiny WeChat test order, pay it, and watch for `DRY-RUN match amount=...`.

If the dry run sees nothing, check Windows notification settings first. If PC WeChat does not create receipt notifications on your machine, this source cannot see payments.

## Real Run

After dry run can see a new real receipt:

```powershell
.\run_wechat_windows_watcher.ps1
```

The watcher sends:

```json
{
  "amount": "0.01",
  "transaction_id": "wechat-win-...",
  "payer": "",
  "source": "windows-notification-db",
  "source_id": "Notification:123",
  "observed_at": "2026-05-17T20:00:00+08:00"
}
```

By default it does not send the raw notification text. Add `-SendRawText` only while debugging.

## Optional Startup Task

After it works manually, create a logon task:

```powershell
$watcher = "C:\path\to\sub2api-nvidia-gateway\cloud-deploy\qrpay-bridge\watchers\run_wechat_windows_watcher.ps1"
schtasks /Create /TN "ZteAPI WeChat Watcher" /SC ONLOGON /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"$watcher`"" /F
```

## Limitations

This is less stable than Android VMQ or official WeChat Pay:

- It depends on PC WeChat and Windows actually producing receipt notifications.
- If Windows Focus Assist hides notifications, the watcher may miss them.
- If PC sleeps, logs out, or WeChat changes notification format, receipts may not be detected.
- It should be treated as a practical bridge, not a bank-grade payment callback.

