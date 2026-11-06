import sys
sys.path.insert(0, "C:/oko/src")
sys.path.insert(0, "C:/oko")
import bot

result = bot._query_knowledge_db("Что говорится про purple hexagon lighthouse protocol seven?")
print(result)
