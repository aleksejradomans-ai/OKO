import asyncio
import sys
sys.path.insert(0, "C:/oko/src")
sys.path.insert(0, "C:/oko")
import bot
from src.governor import AgentGovernor

async def main():
    governor = AgentGovernor()
    await governor.start()

    query = "Что говорится про purple hexagon lighthouse protocol seven?"
    knowledge = bot._query_knowledge_db(query)

    full_context_payload = (
        f"История диалога:\n(начало диалога)\n\n"
        f"Дополнительные справочные данные:\n{knowledge}"
    )

    print("=== FULL PROMPT LENGTH (chars) ===")
    print(len(full_context_payload))
    print("=== CALLING execute_request ===")

    response = await governor.execute_request(
        user_input=query, user_id=999, context=full_context_payload
    )
    print("=== MODEL RESPONSE ===")
    print(response)

    await governor.shutdown()

asyncio.run(main())
