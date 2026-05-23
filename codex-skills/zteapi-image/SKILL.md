---
name: zteapi-image
description: Generate images through ZteAPI/Sub2API in Codex when the user asks for ZteAPI image generation, 中转站生图, gpt-image-2, image2, 图片生成, 画一张图, or 用 ZteAPI 生图. Uses the user's GPT/OpenAI OAuth Sub2API key for both text and image workflows.
metadata:
  short-description: ZteAPI gpt-image-2 image generation
---

# ZteAPI Image Generation

Use this skill when the user asks Codex to generate an image through ZteAPI, Sub2API, `gpt-image-2`, image2, 中转站生图, or says "用 ZteAPI 生图：...".

## Workflow

1. Extract the user's image prompt exactly enough to preserve subject, style, and constraints.
2. Resolve `scripts/generate_zteapi_image.py` relative to this `SKILL.md`, then run the bundled script:

```bash
python scripts/generate_zteapi_image.py --prompt "PROMPT" --output "zteapi-image.png"
```

Use an absolute output path when possible. If the user requests size, pass `--size`; otherwise use the default `1024x1024`.

3. Read the script's JSON output. It includes `output_path`, `model`, `size`, and `png_bytes`.
4. Show the saved image to the user with Markdown using the absolute path:

```markdown
![ZteAPI image](ABSOLUTE_OUTPUT_PATH)
```

## Key Handling

- Prefer one key for text and images: `ZTEAPI_GPT_KEY`.
- `ZTEAPI_IMAGE_KEY` is accepted only as an optional fallback for users who intentionally split keys.
- If no environment key is set, the script tries the user's Codex config at `%USERPROFILE%\.codex\config.toml` or `~/.codex/config.toml` and reads the ZteAPI GPT provider's `env_key` or `experimental_bearer_token`.
- Never print API keys or base64 image payloads.

## API Contract

- Base URL: `https://Zteapi.com/v1`
- Endpoint: `POST /v1/images/generations`
- Model: `gpt-image-2`
- Response field: `data[0].b64_json`

Do not send `gpt-image-2` to `/v1/responses` or `/v1/chat/completions`.
