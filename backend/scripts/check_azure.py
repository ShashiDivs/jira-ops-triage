"""Quick diagnostic: verifies whichever LLM provider is active (OpenAI, if OPENAI_API_KEY is
set, else Azure AI Foundry) actually works before running the full seed script. Checks, in
order: which provider is active, resource reachable, chat model, embedding model.
Run with: python -m scripts.check_azure
"""
import agents.llm as llm
from config import get_settings


def main() -> None:
    s = get_settings()

    if not llm.llm_configured():
        raise SystemExit(
            "No LLM provider configured — set OPENAI_API_KEY, or "
            "AZURE_AI_FOUNDRY_ENDPOINT / AZURE_AI_FOUNDRY_API_KEY, in .env"
        )

    provider = "OpenAI" if s.openai_configured else "Azure AI Foundry"
    print(f"provider:             {provider}")
    print(f"chat model:           {llm._chat_model}")
    print(f"embedding model:      {llm._embedding_model}")
    print()

    client = llm._client

    print("1. Listing available models...")
    try:
        models = {m.id for m in client.models.list()}
        print(f"   OK — {len(models)} models visible.")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}")
        print("   -> auth is wrong, or the account/subscription isn't active.")
        return

    print(f"\n2. Chat completion with '{llm._chat_model}'...")
    try:
        resp = client.chat.completions.create(
            model=llm._chat_model,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        print(f"   OK — got: {resp.choices[0].message.content!r}")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}")
        print(f"   -> '{llm._chat_model}' isn't a valid/available model for this provider.")

    print(f"\n3. Embeddings with '{llm._embedding_model}'...")
    try:
        resp = client.embeddings.create(model=llm._embedding_model, input=["test"])
        print(f"   OK — got a {len(resp.data[0].embedding)}-dim embedding.")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {e}")
        print(f"   -> '{llm._embedding_model}' isn't a valid/available/deployed embedding model.")


if __name__ == "__main__":
    main()
