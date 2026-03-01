import asyncio
from extensions import client

async def reset_vector_stores():
    stores = await client.vector_stores.list()
    print(f"Found {len(stores.data)} vector stores. Deleting...")
    
    for vs in stores.data:
        await client.vector_stores.delete(vs.id)
        print(f"Deleted: {vs.id} | Name: {vs.name}")
    
    new_store = await client.vector_stores.create(name="ubti_main_store")
    print(f"\nCreated new store: {new_store.id} | Name: {new_store.name}")
    
    return new_store.id

asyncio.run(reset_vector_stores())