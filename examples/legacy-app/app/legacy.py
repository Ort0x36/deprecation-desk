"""A tiny app that is quietly broken, for trying depdesk on something real."""

OLD = "claude-opus-4-1-20250805"
CYBER = "gpt-5.4-cyber"
NEW = "claude-opus-5"


def summarise(client, text):
    return client.messages.create(
        model=NEW,
        temperature=0,
        max_tokens=512,
        messages=[{"role": "user", "content": text}],
    )
