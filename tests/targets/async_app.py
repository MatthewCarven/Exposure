"""Test target: an asyncio program (asyncio.run on the main thread)."""
import asyncio
import sys
import threading

state = {"ticks": 0, "fetched": []}


def where():
    return threading.current_thread().name


async def fetch(x):
    await asyncio.sleep(0.05)
    state["fetched"].append(x)
    return x * 2


async def failing():
    await asyncio.sleep(0.01)
    raise KeyError("missing")


async def main():
    seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 60
    print("ready", flush=True)
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        state["ticks"] += 1
        await asyncio.sleep(0.01)


if __name__ == "__main__":
    asyncio.run(main())
