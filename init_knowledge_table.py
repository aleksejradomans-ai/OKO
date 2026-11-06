import sys
sys.path.insert(0, 'src')
import knowledge_indexer

knowledge_indexer.setup_database()
print("OK: таблица knowledge создана/проверена")
