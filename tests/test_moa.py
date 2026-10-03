import asyncio

from app import search


async def main():
    print("Testing React Research Loop...")
    try:
        res = await search.react_research_loop("Test query")
        print(res[:200])
    except Exception as e:
        print("ReAct Error:", e)

if __name__ == "__main__":
    asyncio.run(main())
