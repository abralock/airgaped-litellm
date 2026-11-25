# Using the LLM gateway

The gateway gives you one OpenAI-compatible address for Claude models, with your own API
key. Your admin sends you an invite link to get started.

## Get a key

1. Open the invite link and set a password. Afterwards, log in at `https://<gateway>/ui`.
2. **Virtual Keys → + Create New Key**: give it a name, pick the models, create it.
3. Copy the key (`sk-...`) right away. It's shown only once.

Limits:

- Each key expires after at most **90 days**. Create a new one when that happens.
- Each key can spend at most **$50 in total**. This doesn't reset; create a new key when
  it runs out.
- All your keys together count against your own limit of **$50 every 30 days**.

Delete a key in the UI as soon as you stop using it, or if it may have leaked.

## Models

| Model | Goes to |
| --- | --- |
| `bedrock/claude-sonnet` | Claude Sonnet on AWS Bedrock. Falls back to `corp/claude-sonnet` if Bedrock fails |
| `bedrock/claude-haiku` | Claude Haiku on AWS Bedrock (faster, cheaper). Falls back to `corp/claude-haiku` |
| `corp/claude-sonnet` | Claude Sonnet through the corporate LiteLLM |
| `corp/claude-haiku` | Claude Haiku through the corporate LiteLLM |

`GET https://<gateway>/v1/models` with your key lists what you can use.

## Call a model

curl:

```sh
curl https://<gateway>/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "bedrock/claude-sonnet", "messages": [{"role": "user", "content": "Hello"}]}'
```

OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(base_url="https://<gateway>/v1", api_key="sk-...")
reply = client.chat.completions.create(
    model="bedrock/claude-sonnet",
    messages=[{"role": "user", "content": "Hello"}],
)
print(reply.choices[0].message.content)
```

Any tool that accepts an OpenAI-compatible base URL and API key works the same way. Use
`https://<gateway>/v1` as the base URL.

If your tool rejects the gateway's certificate, ask your admin for the company CA bundle
and point the tool at it (`SSL_CERT_FILE` for Python).

## See your usage

The UI's **Usage** page shows your spend per key and model.
