import asyncio
import sys
sys.path.insert(0, "C:/oko/src")
sys.path.insert(0, "C:/oko")
from src.governor import AgentGovernor

async def main():
    governor = AgentGovernor()
    await governor.start()

    query = "Что говорится про purple hexagon lighthouse protocol seven?"

    # Только релевантный документ, без шума от остальных 4
    clean_knowledge = (
        "Global Sanity Check Document\n"
        "This document is used for a full end-to-end sanity test of the Oko RAG pipeline after merging two\n"
        "independent development sessions: the registry race-condition fix and the PDF ingestion feature set.\n"
        "The unique marker phrase for retrieval testing is: purple hexagon lighthouse protocol seven.\n"
        "This document also serves as input for the discuss command test. A specific fact to verify explicit\n"
        "extraction: the configured discuss context window is eight thousand one hundred ninety two tokens.\n"
        "If both the standard RAG search and the discuss mode can correctly surface information from this\n"
        "document, the merged system is confirmed healthy."
    )

    full_context_payload = (
        f"История диалога:\n(начало диалога)\n\n"
        f"Дополнительные справочные данные:\n{clean_knowledge}"
    )

    response = await governor.execute_request(
        user_input=query, user_id=999, context=full_context_payload
    )
    print("=== MODEL RESPONSE (clean context, implicit question) ===")
    print(response)

    await governor.shutdown()

asyncio.run(main())
