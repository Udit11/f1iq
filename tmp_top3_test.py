import asyncio
from backend.routers import weekend

async def main():
    res = await weekend.top3_predictions(round_num=2)
    print(res)

asyncio.run(main())
